"""The pi adapter (PLAN §3.1, phase 1).

pi (https://pi.dev, ``@earendil-works/pi-coding-agent``) is installed with its
standalone installer (``curl -fsSL https://pi.dev/install.sh | sh``), which
pins every dependency. The installer always installs the latest release, so
the pin in ``config/harnesses.toml`` is checked rather than installed: a run
refuses if ``pi --version`` isn't the pinned version.

How a run is set up (HARNESS.md has the reasoning):

- **Credentials.** pi bills whatever is in its agent dir's ``auth.json`` before
  ``OPENROUTER_API_KEY``, so every run gets a fresh, empty
  ``PI_CODING_AGENT_DIR`` and the key only through ``OPENROUTER_API_KEY``.
- **Context.** pi reads ``AGENTS.md`` and ``CLAUDE.md`` from the working
  directory *and every parent directory*, without project trust. The task's
  ``CLAUDE.md`` therefore reaches pi as it is; the adapter refuses a workspace
  with a context file in any parent, which would leak into the agent's
  context. Skills: pi doesn't look in ``.claude/skills/``, so each
  ``.claude/skills/<name>/`` with a ``SKILL.md`` is passed with ``--skill``
  (left where it is, so the agent's diff and the treatments' help markers are
  the same as under Claude Code). Discovered skills, extensions, MCP servers,
  prompt templates and trust-gated project resources are all switched off, so
  the only things that reach pi are the workspace's context file, the
  treatment's skills and pi's default tools (read, bash, edit, write).
- **Isolation.** pi runs in its own mount namespace (``unshare -m``) with an
  empty read-only tmpfs over every path in ``hide``: ``ladder.run_cell`` passes
  the gold checkout and the repo holding the task's oracle, removal diff and
  §9's reference trees. Without it the agent can ``find /`` its way to the
  gold charm and copy it (the first smoke run did, PLAN §7 phase 1).
- **charmlint.** The context file says ``uvx charmlint .``; the agent's
  environment gets ``UV_CONSTRAINT`` pointing at a file pinning charmlint to
  the scoring version, which ``uvx`` honours.
- **Caps.** pi has no turn, time or cost limit of its own. The adapter reads
  ``--mode json`` events as they arrive and stops pi (SIGTERM, then SIGKILL)
  once a cap is reached: turns at ``turn_end``, cost at each assistant
  ``message_end``, wall time throughout.
- **Reasoning effort.** pi asks for a thinking level on every request unless
  the model is marked as not reasoning (its default level is ``medium``, and
  a dated id it doesn't know gets pi's default level map). So every run writes
  a ``models.json`` entry for the model into the fresh agent dir: with
  ``reasoning_effort`` unset, ``reasoning: false``, and pi sends no
  ``reasoning`` field (the provider's default applies); with it set,
  ``reasoning: true``, a ``thinkingLevelMap`` taking one pi level to the value,
  and ``--thinking <level>``. The entry also carries the context window and
  output limit from OpenRouter's model list, so pi's limits don't fall back to
  its defaults.
- **Cost, tokens, provider.** Every assistant message carries usage with a
  cost that pi takes from OpenRouter's own accounting (``totalFromProvider``),
  so the run's cost is their sum. pi doesn't report which OpenRouter
  provider served a request, but it records OpenRouter's generation id
  (``responseId``); the adapter looks each one up afterwards
  (``/api/v1/generation``) for the provider, the dated model id OpenRouter
  routed to, and the billed cost.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..config import Caps, Model
from . import _common
from ._common import (
    HIDE_FAILED,
    AdapterError,
    Fetcher,
    fetch_generation,
    hide_wrapper,
    provider_label,
    served_by,
)
from ._common import stop_process as _stop
from .base import AdapterResult, ToolEvent, ToolKind

__all__ = ["HIDE_FAILED", "PiAdapter", "hide_wrapper", "provider_label", "served_by"]

CONTEXT_FILES = ("AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD")
SKILL_DIR = pathlib.Path(".claude/skills")
EVENTS_FILE = "pi-events.jsonl"

TOOL_KINDS: dict[str, ToolKind] = {
    "read": "read",
    "bash": "shell",
    "edit": "edit",
    "write": "edit",
    "grep": "search",
    "find": "search",
    "ls": "search",
}

# Environment that must not reach pi from the harness's own shell: another pi
# config, session dir or offline switch would change what the run sees.
_DROP_ENV = ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR", "PI_OFFLINE")

PiAdapterError = AdapterError


# --- parsing -------------------------------------------------------------------


@dataclasses.dataclass
class ParsedRun:
    """What a pi ``--mode json`` event stream says about a run."""

    events: list[ToolEvent] = dataclasses.field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0
    cost_from_provider: bool = True
    tokens: dict[str, int] = dataclasses.field(default_factory=dict)
    response_ids: list[str] = dataclasses.field(default_factory=list)
    models: list[str] = dataclasses.field(default_factory=list)
    stop_reasons: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    tool_errors: int = 0
    retries: int = 0
    compactions: int = 0
    settled: bool = False
    aborted: bool = False
    session_id: str | None = None
    bad_lines: int = 0

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "session":
            self.session_id = record.get("id")
        elif kind == "turn_end":
            self.turns += 1
        elif kind == "message_end":
            msg = record.get("message") or {}
            if msg.get("role") == "assistant":
                self._assistant(msg)
        elif kind == "tool_execution_start":
            name = str(record.get("toolName", "?"))
            args = record.get("args")
            self.events.append(
                ToolEvent(
                    kind=TOOL_KINDS.get(name, "other"),
                    name=name,
                    input=args if isinstance(args, dict) else {"args": args},
                )
            )
        elif kind == "tool_execution_end":
            self.tool_errors += bool(record.get("isError"))
        elif kind == "auto_retry_start":
            self.retries += 1
        elif kind == "compaction_end":
            self.compactions += 1
        elif kind == "agent_settled":
            self.settled = True
            self.aborted = bool(record.get("aborted"))

    def _assistant(self, msg: dict[str, Any]) -> None:
        usage = msg.get("usage") or {}
        cost = usage.get("cost") or {}
        self.cost_usd += float(cost.get("total") or 0.0)
        if not cost.get("totalFromProvider", False):
            self.cost_from_provider = False
        for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens"):
            if isinstance(usage.get(key), int):
                self.tokens[key] = self.tokens.get(key, 0) + usage[key]
        if msg.get("responseId"):
            self.response_ids.append(msg["responseId"])
        model = msg.get("responseModel") or msg.get("model")
        if model and model not in self.models:
            self.models.append(model)
        stop = msg.get("stopReason")
        if stop:
            self.stop_reasons.append(stop)
        if stop == "error" or msg.get("errorMessage"):
            self.errors.append(str(msg.get("errorMessage") or "assistant message ended in error"))


def parse_lines(lines: Iterable[str]) -> ParsedRun:
    parsed = ParsedRun()
    for line in lines:
        line = line.rstrip("\r\n")
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            parsed.bad_lines += 1
            continue
        if isinstance(record, dict):
            parsed.feed(record)
    return parsed


def parse_file(path: pathlib.Path) -> ParsedRun:
    # pi's framing is LF-only; U+2028/2029 inside strings aren't record breaks.
    return parse_lines(path.read_bytes().decode().split("\n"))


# --- the adapter -------------------------------------------------------------------


def context_files_above(workspace: pathlib.Path) -> list[pathlib.Path]:
    """Context files pi would load from the workspace's parents."""
    return _common.context_files_above(workspace, CONTEXT_FILES)


def skill_dirs(workspace: pathlib.Path) -> list[pathlib.Path]:
    root = workspace / SKILL_DIR
    if not root.is_dir():
        return []
    return sorted(p.parent for p in root.glob("*/SKILL.md"))


def thinking_level(model: Model) -> str | None:
    """The ``--thinking`` level for a model: None (no flag) when no effort is pinned."""
    if model.reasoning_effort is None:
        return None
    return "off" if model.reasoning_effort == "none" else model.reasoning_effort


def models_json(model: Model, limits: Mapping[str, int]) -> dict[str, Any]:
    """The run's ``models.json``: one OpenRouter entry for the model.

    Unset ``reasoning_effort``: ``reasoning: false``, so pi sends no
    ``reasoning`` field. Set: the one level ``--thinking`` selects maps to the
    value, sent as OpenRouter's ``reasoning.effort``.
    """
    entry: dict[str, Any] = {"id": model.openrouter_id, **limits}
    level = thinking_level(model)
    if level is None:
        entry["reasoning"] = False
    else:
        entry.update(
            reasoning=True,
            thinkingLevelMap={level: model.reasoning_effort},
            compat={"thinkingFormat": "openrouter"},
        )
    return {"providers": {"openrouter": {"models": [entry]}}}


class PiAdapter:
    name = "pi"

    def __init__(
        self,
        pinned_version: str,
        *,
        charmlint: str,
        binary: str = "pi",
        fetch: Fetcher = fetch_generation,
        models: _common.ModelsFetcher = _common.fetch_models,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self.pinned_version = pinned_version
        self.charmlint = charmlint
        self.binary = binary
        self._fetch = fetch
        self._models = models
        self._popen = popen

    def version(self) -> str:
        exe = shutil.which(self.binary)
        if exe is None:
            raise PiAdapterError(f"{self.binary!r} is not on PATH; install pi (HARNESS.md)")
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, check=True, timeout=60
        )
        return out.stdout.strip()

    def context_file(self) -> str:
        return "CLAUDE.md"

    def command(self, workspace: pathlib.Path, prompt: str, model: Model) -> list[str]:
        level = thinking_level(model)
        cmd = [
            self.binary,
            "--mode", "json",
            "--provider", "openrouter",
            "--model", model.openrouter_id,
            "--no-session",
            "--no-extensions",
            "--no-mcp",
            "--no-skills",
            "--no-prompt-templates",
            "--no-approve",
        ]  # fmt: skip
        if level is not None:
            cmd += ["--thinking", level]
        for d in skill_dirs(workspace):
            cmd += ["--skill", str(d)]
        return [*cmd, "--", prompt]

    def environment(
        self,
        workspace: pathlib.Path,
        agent_dir: pathlib.Path,
        key: str,
        base: Mapping[str, str] | None,
        constraints: pathlib.Path,
    ) -> dict[str, str]:
        env = {k: v for k, v in (os.environ if base is None else base).items()}
        for k in _DROP_ENV:
            env.pop(k, None)
        _common.write_constraints(constraints, self.charmlint)
        env.update(
            PI_CODING_AGENT_DIR=str(agent_dir),
            PI_SKIP_VERSION_CHECK="1",
            PI_TELEMETRY="0",
            OPENROUTER_API_KEY=key,
            UV_CONSTRAINT=str(constraints),
        )
        return env

    def run(
        self,
        workspace: pathlib.Path,
        prompt: str,
        model: Model,
        caps: Caps,
        openrouter_key_env: str,
        *,
        env: Mapping[str, str] | None = None,
        hide: Sequence[pathlib.Path] = (),
    ) -> AdapterResult:
        key = os.environ.get(openrouter_key_env)
        if not key:
            raise PiAdapterError(f"${openrouter_key_env} is not set")
        if model.providers:
            raise PiAdapterError("pi can't pin an OpenRouter provider order yet")
        leaks = context_files_above(workspace)
        if leaks:
            raise PiAdapterError(
                f"context files above the workspace would reach pi: {', '.join(map(str, leaks))}"
            )
        hide = _common.resolve_hidden(workspace, hide)
        wrapper = hide_wrapper(hide)
        version = self.version()
        if version != self.pinned_version:
            raise PiAdapterError(f"pi {version} is installed; the pin is {self.pinned_version}")

        events_path = workspace / ".wof" / EVENTS_FILE
        events_path.parent.mkdir(exist_ok=True)
        # The agent dir and the charmlint constraint live outside the workspace,
        # so the agent's tree holds nothing of the harness's but .wof/.
        with tempfile.TemporaryDirectory(prefix="wof-pi-") as tmp:
            agent_dir = pathlib.Path(tmp) / "agent"
            agent_dir.mkdir()
            models_config = models_json(
                model, _common.model_limits(model.openrouter_id, self._models())
            )
            (agent_dir / "models.json").write_text(json.dumps(models_config, indent=2) + "\n")
            constraints = pathlib.Path(tmp) / _common.CONSTRAINTS_FILE
            run_env = self.environment(workspace, agent_dir, key, env, constraints)
            cmd = [*wrapper, *self.command(workspace, prompt, model)]
            parsed, exit_reason, rc, stderr, wall = self._stream(
                cmd, workspace, run_env, caps, events_path
            )
        if rc == HIDE_FAILED and not parsed.turns and wrapper:
            raise PiAdapterError(f"could not hide {hide} from the agent: {stderr[-500:]}")
        lookup = served_by(parsed.response_ids, key, self._fetch)
        error = None
        failed = rc != 0 or parsed.errors or not parsed.settled or parsed.aborted
        if exit_reason == "completed" and failed:
            exit_reason = "error"
            error = "; ".join(parsed.errors) or f"pi exited {rc}: {stderr[-500:]}"
        return AdapterResult(
            exit_reason=exit_reason,
            cost_usd=round(parsed.cost_usd, 6),
            turns=parsed.turns,
            wall_seconds=round(wall, 1),
            provider=provider_label(lookup["providers"]),
            events=parsed.events,
            transcript_path=events_path,
            error=error,
            tokens=dict(parsed.tokens),
            details={
                "harness_version": version,
                "pi_exit_code": rc,
                "cost_from_provider": parsed.cost_from_provider,
                "requests": len(parsed.response_ids),
                "stop_reasons": dict(collections.Counter(parsed.stop_reasons)),
                "tool_errors": parsed.tool_errors,
                "retries": parsed.retries,
                "compactions": parsed.compactions,
                "pi_models": parsed.models,
                "reasoning_effort": model.reasoning_effort,
                "thinking_level": thinking_level(model),
                "pi_model_entry": models_config["providers"]["openrouter"]["models"][0],
                "skills": [str(d.relative_to(workspace)) for d in skill_dirs(workspace)],
                "agent_charmlint": self.charmlint,
                "hidden_paths": len([h for h in hide if h.exists()]),
                **lookup,
            },
        )

    def _stream(
        self,
        cmd: list[str],
        workspace: pathlib.Path,
        env: dict[str, str],
        caps: Caps,
        events_path: pathlib.Path,
    ) -> tuple[ParsedRun, str, int, str, float]:
        parsed = ParsedRun()

        def check(elapsed: float) -> str | None:
            if elapsed >= caps.timeout_minutes * 60:
                return "time_cap"
            if parsed.turns >= caps.max_turns:
                return "turn_cap"
            if caps.max_cost_usd is not None and parsed.cost_usd >= caps.max_cost_usd:
                return "cost_cap"
            return None

        def bad_line() -> None:
            parsed.bad_lines += 1

        exit_reason, rc, stderr, wall = _common.stream(
            self._popen,
            cmd,
            workspace,
            env,
            events_path,
            feed=parsed.feed,
            check=check,
            on_bad_line=bad_line,
            stop=lambda proc: _stop(proc),
        )
        return parsed, exit_reason, rc, stderr, wall
