"""The omp adapter (PLAN §3.1, phase 1).

omp is oh-my-pi (https://github.com/can1357/oh-my-pi, https://omp.sh,
``@oh-my-pi/pi-coding-agent``), a fork of pi with a much larger built-in
toolset. HARNESS.md "omp" has the install method and the reasoning; in short:

- **Credentials.** omp keeps credentials in ``~/.omp/agent/agent.db``
  (``auth_credentials``) and reads settings, rules, skills and MCP servers
  from the home directory (``~/.omp``, and other agents' ``~/.claude``,
  ``~/.codex``, ...). So every run gets a fresh, empty ``HOME`` (uv's cache and
  Python installs are pointed back at the real ones, so the agent's ``uv run``
  doesn't re-download them), and the key goes in only as
  ``OPENROUTER_API_KEY``.
- **Context.** omp reads ``CLAUDE.md`` and ``.claude/skills/`` natively, so
  the workspace is used as it is. Everything else it would discover on its
  own is switched off with flags and a ``--config`` overlay (see ``OVERLAY``).
- **Isolation** and the **charmlint** pin are shared with pi (``_common``).
- **Caps.** omp has ``--max-time`` and ``--max-cost`` of its own, so the
  adapter passes the plan's caps to them; it has no turn cap, so turns are
  enforced on the ``--mode json`` stream, as for pi. The stream also backs up
  the other two (a grace period past the time cap, and a request that starts
  after the cost cap is reached).
- **Cost, tokens, provider.** As for pi: per-message usage from the stream,
  OpenRouter generation ids looked up afterwards for the provider.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import pathlib
import re
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
    provider_label,
    served_by,
)
from ._common import stop_process as _stop
from .base import AdapterResult, ToolEvent, ToolKind

EVENTS_FILE = "omp-events.jsonl"
OVERLAY_FILE = "omp-overlay.yml"

# Context files omp would load from a parent of the workspace (refused).
CONTEXT_FILES = ("AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD", "GEMINI.md")

TOOL_KINDS: dict[str, ToolKind] = {
    "read": "read",
    "bash": "shell",
    "sh": "shell",
    "edit": "edit",
    "write": "edit",
    "ast_edit": "edit",
    "grep": "search",
    "find": "search",
    "glob": "search",
    "ls": "search",
    "ast_grep": "search",
    "web_search": "web",
    "fetch": "web",
    "skill": "skill",
}

# What omp gets from the harness's own shell that would change what it sees.
_DROP_ENV_PREFIXES = ("PI_", "OMP_", "HINDSIGHT_", "XDG_")

# Seconds past the time cap before the adapter kills omp itself (omp's own
# --max-time should have ended the run by then).
TIME_GRACE = 60


class OmpAdapterError(AdapterError):
    pass


# --- parsing -------------------------------------------------------------------

# omp's usage keys -> the tokens_* names pi's adapter records (pi's own keys).
TOKEN_KEYS = {
    "input": "input",
    "output": "output",
    "cacheRead": "cacheRead",
    "cacheWrite": "cacheWrite",
    "totalTokens": "totalTokens",
    "reasoningTokens": "reasoning",
}


@dataclasses.dataclass
class ParsedRun:
    """What an omp ``--mode json`` event stream says about a run."""

    events: list[ToolEvent] = dataclasses.field(default_factory=list)
    turns: int = 0
    requests_started: int = 0
    cost_usd: float = 0.0
    tokens: dict[str, int] = dataclasses.field(default_factory=dict)
    response_ids: list[str] = dataclasses.field(default_factory=list)
    models: list[str] = dataclasses.field(default_factory=list)
    stop_reasons: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    notices: list[str] = dataclasses.field(default_factory=list)
    tool_errors: int = 0
    retries: int = 0
    compactions: int = 0
    ended: bool = False
    stopped: bool = False
    """The adapter stopped omp; the ``turn_end`` omp emits for the aborted turn isn't a turn."""
    session_id: str | None = None
    bad_lines: int = 0
    other_events: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "session":
            self.session_id = record.get("id")
        elif kind == "turn_end":
            self.turns += not self.stopped
        elif kind == "message_start":
            if (record.get("message") or {}).get("role") == "assistant":
                self.requests_started += 1
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
        elif kind == "auto_compaction_end" or kind == "compaction_end":
            self.compactions += 1
        elif kind == "agent_end":
            self.ended = True
        elif kind == "notice":
            self.notices.append(str(record.get("source") or record.get("message") or "notice"))
        elif kind not in ("message_update", "tool_execution_update", "turn_start", "agent_start"):
            self.other_events[str(kind)] += 1

    def _assistant(self, msg: dict[str, Any]) -> None:
        usage = msg.get("usage") or {}
        cost = usage.get("cost") or {}
        self.cost_usd += float(cost.get("total") or 0.0)
        for key, name in TOKEN_KEYS.items():
            if isinstance(usage.get(key), int):
                self.tokens[name] = self.tokens.get(name, 0) + usage[key]
        if msg.get("responseId"):
            self.response_ids.append(msg["responseId"])
        model = msg.get("model")
        if model and model not in self.models:
            self.models.append(model)
        stop = msg.get("stopReason")
        if stop:
            self.stop_reasons.append(stop)
        if stop in ("error", "aborted") or msg.get("errorMessage"):
            self.errors.append(str(msg.get("errorMessage") or f"assistant message {stop}"))


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
    return parse_lines(path.read_bytes().decode().split("\n"))


# --- the adapter -------------------------------------------------------------------


def context_files_above(workspace: pathlib.Path) -> list[pathlib.Path]:
    """Context files omp would load from the workspace's parents."""
    return _common.context_files_above(workspace, CONTEXT_FILES)


def skill_names(workspace: pathlib.Path) -> list[str]:
    root = workspace / ".claude" / "skills"
    if not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.glob("*/SKILL.md"))


def parse_version(out: str) -> str:
    """``omp/18.19.0`` -> ``18.19.0``."""
    m = re.search(r"(\d+\.\d+\.\d+\S*)", out)
    return m.group(1) if m else out.strip()


# Tools: omp's defaults minus ``task`` (subagents' messages and tool calls
# never reach the print-mode stream, so their turns, tool events and
# providers couldn't be recorded) and ``web_search`` (paid OpenRouter calls
# outside the stream's usage; pi has no web tool either).
TOOLS = (
    "read", "bash", "edit", "write", "ast_edit", "eval",
    "glob", "grep", "find", "lsp", "debug", "todo",
)  # fmt: skip

# Capability providers omp would otherwise use to discover context, skills,
# rules, MCP servers, settings and commands from other agents' layouts.
# ``claude`` (``.claude/skills``, ``.claude/CLAUDE.md``) and ``claude-md``
# (``CLAUDE.md``) stay: they are how the task's context reaches omp.
DISABLED_PROVIDERS = (
    "native", "agents", "agents-md", "codex", "cursor", "gemini", "opencode",
    "github", "cline", "windsurf", "vscode", "mcp-json", "claude-plugins",
    "omp-plugins", "agent-plugins", "skillshare", "custom-context", "ssh-json",
)  # fmt: skip

# Model providers whose catalogues omp fetches at start-up without a key, and
# the local servers it probes. The same ``disabledProviders`` setting switches
# them off. Without this, omp 18.19.0 reached seven catalogue hosts besides
# OpenRouter on every run, which the agent job's egress allowlist blocks.
DISABLED_MODEL_PROVIDERS = (
    "zenmux", "kilo", "venice", "alibaba-coding-plan", "charm-hyper", "commandcode",
    "ollama", "lm-studio", "llama.cpp",
)  # fmt: skip

# The --config overlay (it outranks project settings). Quoted "off": YAML 1.1
# reads a bare off as false.
OVERLAY = (
    "disabledProviders:\n"
    + "".join(f"  - {p}\n" for p in (*DISABLED_PROVIDERS, *DISABLED_MODEL_PROVIDERS))
    + """providers:
  cacheWarming: "off"
web_search:
  enabled: false
tools:
  xdev: false
async:
  enabled: false
dev:
  autoqa: false
ttsr:
  enabled: false
mcp:
  enableProjectConfig: false
memory:
  backend: "off"
"""
)


# What omp is given when no reasoning effort is pinned: no --thinking flag, so
# omp's default level. omp sends a reasoning effort whatever it's given (checked
# through the relay, 2026-11-16; HARNESS.md "omp"): no flag sends GLM-5.3
# `max` and DeepSeek V4 Pro `high`; `off` sends GLM-5.3 `low` and turns
# DeepSeek's reasoning off; `auto` first asks another model
# (`typesafe/jev-1.14`, a paid call) how hard the prompt is. No flag is the only
# one that leaves reasoning on for every model without an extra call.
UNSET_THINKING = "default (no --thinking flag)"


def thinking_level(model: Model) -> str | None:
    """omp's ``--thinking`` value for a pinned effort; None (no flag) when unset.

    omp maps the level through its own per-model table, so a level the model's
    table doesn't support is clamped (GLM-5.3: ``medium`` goes out as ``low``).
    """
    if model.reasoning_effort is None:
        return None
    return "off" if model.reasoning_effort == "none" else model.reasoning_effort


class OmpAdapter:
    name = "omp"

    def __init__(
        self,
        pinned_version: str,
        *,
        charmlint: str,
        binary: str = "omp",
        fetch: Fetcher = fetch_generation,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self.pinned_version = pinned_version
        self.charmlint = charmlint
        self.binary = binary
        self._fetch = fetch
        self._popen = popen

    def version(self) -> str:
        exe = shutil.which(self.binary)
        if exe is None:
            raise OmpAdapterError(f"{self.binary!r} is not on PATH; install omp (HARNESS.md)")
        with tempfile.TemporaryDirectory(prefix="wof-omp-ver-") as home:
            out = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
                env={**os.environ, "HOME": home},
                stdin=subprocess.DEVNULL,
            )
        return parse_version(out.stdout)

    def context_file(self) -> str:
        return "CLAUDE.md"

    def command(
        self, workspace: pathlib.Path, prompt: str, model: Model, caps: Caps, overlay: pathlib.Path
    ) -> list[str]:
        cmd = [
            self.binary,
            "--print",
            "--mode", "json",
            "--model", f"openrouter/{model.openrouter_id}",
            "--no-session",
            "--no-title",
            "--no-rules",
            "--no-extensions",
            "--tools", ",".join(TOOLS),
            "--config", str(overlay),
            "--max-time", f"{caps.timeout_minutes * 60}",
        ]  # fmt: skip
        if caps.max_cost_usd is not None:
            cmd += ["--max-cost", f"{caps.max_cost_usd}"]
        level = thinking_level(model)
        if level is not None:
            cmd += ["--thinking", level]
        skills = skill_names(workspace)
        if skills:
            cmd += ["--skills", ",".join(skills)]
        else:
            cmd += ["--no-skills"]
        # omp takes positional messages; "--" stops a prompt starting with "-"
        # being read as a flag.
        return [*cmd, "--", prompt]

    def environment(
        self,
        home: pathlib.Path,
        key: str,
        base: Mapping[str, str] | None,
        constraints: pathlib.Path,
    ) -> dict[str, str]:
        src = os.environ if base is None else base
        env = {k: v for k, v in src.items() if not k.startswith(_DROP_ENV_PREFIXES)}
        for k in ("CLAUDE_CONFIG_DIR", "OTEL_EXPORTER_OTLP_ENDPOINT"):
            env.pop(k, None)
        real_home = pathlib.Path(src.get("HOME") or os.path.expanduser("~"))
        # uv's cache, Python installs and tools stay where they were, so the
        # agent's `uv run` / `uvx` don't download everything again per run.
        env.setdefault("UV_CACHE_DIR", str(real_home / ".cache" / "uv"))
        env.setdefault(
            "UV_PYTHON_INSTALL_DIR", str(real_home / ".local" / "share" / "uv" / "python")
        )
        env.setdefault("UV_TOOL_DIR", str(real_home / ".local" / "share" / "uv" / "tools"))
        _common.write_constraints(constraints, self.charmlint)
        env.update(
            HOME=str(home),
            OPENROUTER_API_KEY=key,
            UV_CONSTRAINT=str(constraints),
            PI_NO_TITLE="1",
            OTEL_SDK_DISABLED="true",
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
            raise OmpAdapterError(f"${openrouter_key_env} is not set")
        if model.providers:
            raise OmpAdapterError("the omp adapter can't pin an OpenRouter provider order yet")
        leaks = context_files_above(workspace)
        if leaks:
            raise OmpAdapterError(
                f"context files above the workspace would reach omp: {', '.join(map(str, leaks))}"
            )
        shadows = shadowing_context_files(workspace)
        if shadows:
            raise OmpAdapterError(
                f"omp would load {', '.join(shadows)} instead of the workspace's CLAUDE.md"
            )
        hide = _common.resolve_hidden(workspace, hide)
        wrapper = _common.hide_wrapper(hide)
        version = self.version()
        if version != self.pinned_version:
            raise OmpAdapterError(f"omp {version} is installed; the pin is {self.pinned_version}")

        events_path = workspace / ".wof" / EVENTS_FILE
        events_path.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wof-omp-") as tmp:
            home = pathlib.Path(tmp) / "home"
            home.mkdir()
            overlay = pathlib.Path(tmp) / OVERLAY_FILE
            overlay.write_text(OVERLAY)
            constraints = pathlib.Path(tmp) / _common.CONSTRAINTS_FILE
            run_env = self.environment(home, key, env, constraints)
            cmd = [*wrapper, *self.command(workspace, prompt, model, caps, overlay)]
            parsed, exit_reason, rc, stderr, wall = self._stream(
                cmd, workspace, run_env, caps, events_path
            )
            stored_credentials = _count_credentials(home)
        if rc == HIDE_FAILED and not parsed.turns and wrapper:
            raise OmpAdapterError(f"could not hide {hide} from the agent: {stderr[-500:]}")
        lookup = served_by(parsed.response_ids, key, self._fetch)
        exit_reason, error = outcome(parsed, exit_reason, rc, stderr, caps, wall)
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
                "omp_exit_code": rc,
                "requests": len(parsed.response_ids),
                "stop_reasons": dict(collections.Counter(parsed.stop_reasons)),
                "tool_errors": parsed.tool_errors,
                "retries": parsed.retries,
                "compactions": parsed.compactions,
                "notices": parsed.notices,
                "other_events": dict(parsed.other_events),
                "omp_models": parsed.models,
                "reasoning_effort": model.reasoning_effort,
                "thinking": thinking_level(model) or UNSET_THINKING,
                "tools": list(TOOLS),
                "skills": skill_names(workspace),
                "agent_charmlint": self.charmlint,
                "hidden_paths": len([h for h in hide if h.exists()]),
                "stored_credentials": stored_credentials,
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
        cost_at_cap: list[int] = []  # requests started when the cap was first reached

        def check(elapsed: float) -> str | None:
            reason = cap_reached(elapsed)
            parsed.stopped = parsed.stopped or reason is not None
            return reason

        def cap_reached(elapsed: float) -> str | None:
            if parsed.turns >= caps.max_turns:
                return "turn_cap"
            # Backstops: omp's own --max-time and --max-cost should end the run first.
            if elapsed >= caps.timeout_minutes * 60 + TIME_GRACE:
                return "time_cap"
            if caps.max_cost_usd is not None and parsed.cost_usd >= caps.max_cost_usd:
                if not cost_at_cap:
                    cost_at_cap.append(parsed.requests_started)
                elif parsed.requests_started > cost_at_cap[0]:
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


COST_CAP_EXIT = 3


def outcome(
    parsed: ParsedRun, exit_reason: str, rc: int, stderr: str, caps: Caps, wall: float
) -> tuple[str, str | None]:
    """The run's exit reason and error, from the adapter's view and omp's exit.

    omp's own caps: exit 3 (or a ``cost-gate`` notice) is the cost cap; the
    time cap aborts the request in flight (``Deadline exceeded``, exit 1) or,
    between turns, just ends the run (exit 0), so it's told apart by the wall
    time having reached the cap.
    """
    if exit_reason != "completed":
        return exit_reason, None
    if rc == COST_CAP_EXIT or "cost-gate" in parsed.notices:
        return "cost_cap", None
    deadline_text = "deadline exceeded"
    if any(deadline_text in e.lower() for e in parsed.errors) or deadline_text in stderr.lower():
        return "time_cap", None
    if wall >= caps.timeout_minutes * 60:
        return "time_cap", None
    if rc != 0 or parsed.errors or not parsed.ended:
        return "error", "; ".join(parsed.errors) or f"omp exited {rc}: {stderr[-500:]}"
    return "completed", None


# Files omp prefers to the workspace-root CLAUDE.md (one context file per
# directory level, highest-priority provider wins); the workspace must not
# have any of them, or the treatment's CLAUDE.md silently wouldn't reach omp.
SHADOWING = (
    "AGENTS.md",
    ".claude/CLAUDE.md",
    ".agents/AGENTS.md",
    ".agent/AGENTS.md",
    ".omp/AGENTS.md",
    ".gemini/GEMINI.md",
    ".github/copilot-instructions.md",
)


def shadowing_context_files(workspace: pathlib.Path) -> list[str]:
    return [n for n in SHADOWING if (workspace / n).is_file()]


def _count_credentials(home: pathlib.Path) -> int | None:
    """Rows in the fresh home's credential store after the run (expected: 0)."""
    import sqlite3

    db = home / ".omp" / "agent" / "agent.db"
    if not db.exists():
        return None
    try:
        with sqlite3.connect(db) as conn:
            return int(conn.execute("select count(*) from auth_credentials").fetchone()[0])
    except sqlite3.Error:
        return None
