"""The OpenCode adapter (PLAN §3.1, phase 1).

OpenCode is the open-source terminal coding agent (https://opencode.ai,
https://github.com/anomalyco/opencode, formerly sst/opencode; npm
``@opencode/cli``, CLI ``opencode``). This adapter targets OpenCode V2 and its
non-interactive ``opencode run --format json``. HARNESS.md "OpenCode" has the
install method and the reasoning; in short:

- **Credentials.** OpenCode keeps config in ``~/.config/opencode``, sessions
  and credentials in an SQLite database under ``~/.local/share/opencode``
  (``credential``, ``account`` tables), state and cache under ``~/.local/state``
  and ``~/.cache``, and by default talks to one shared background server per
  user. So every run gets a fresh, empty ``HOME`` holding only the adapter's
  global config, runs ``--standalone`` (a private server, a child of the run),
  and the key goes in only as ``OPENROUTER_API_KEY``.
- **Context.** OpenCode V2 reads ``AGENTS.md`` only (no ``CLAUDE.md``
  fallback), and discovers ``.claude/skills/`` natively. The workspace's
  ``CLAUDE.md`` is passed as a configured instruction (its absolute path in the
  config's ``instructions``), which OpenCode puts in the same instructions block
  as an ``AGENTS.md``; nothing is written into the workspace. Built-in skills,
  MCP servers, web search and other providers are off (see ``config_for``).
- **Isolation** and the **charmlint** pin are shared (``_common``).
- **Caps.** OpenCode's only cap is an agent's ``steps``; the adapter sets it one
  above the turn cap as a backstop and enforces turns, time and cost on the
  JSON stream, as for pi.
- **Provider.** OpenCode doesn't report OpenRouter's generation ids, so its
  OpenRouter base URL points at a local recording relay (``_common``) that
  forwards every request unchanged and notes each response's generation id;
  the ids are looked up afterwards, as for pi and omp.
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
from ._common import (  # noqa: F401
    HIDE_FAILED,
    MODELS_URL,
    AdapterError,
    Fetcher,
    ModelsFetcher,
    RecordingRelay,
    fetch_generation,
    fetch_models,
    provider_label,
    served_by,
)
from ._common import stop_process as _stop
from .base import AdapterResult, ToolEvent, ToolKind

EVENTS_FILE = "opencode-events.jsonl"
SYSTEM_CONFIG_DIR = pathlib.Path("/etc/opencode")

# Files OpenCode would pick up from the workspace's parents (refused).
CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", "opencode.json", "opencode.jsonc", ".opencode")
# Files in the workspace that would add instructions, config, agents, commands,
# plugins or skills beyond the task's CLAUDE.md and .claude/skills (refused).
WORKSPACE_EXTRAS = ("opencode.json", "opencode.jsonc", ".opencode", ".agents")
_WALK_SKIP = {".git", ".venv", "node_modules", ".wof", ".tox", "__pycache__"}

TOOL_KINDS: dict[str, ToolKind] = {
    "read": "read",
    "shell": "shell",
    "bash": "shell",
    "edit": "edit",
    "write": "edit",
    "patch": "edit",
    "multiedit": "edit",
    "glob": "search",
    "grep": "search",
    "list": "search",
    "ls": "search",
    "skill": "skill",
    "webfetch": "web",
    "websearch": "web",
}

# What OpenCode gets from the harness's own shell that would change what it sees.
_DROP_ENV_PREFIXES = ("OPENCODE_", "XDG_")

# Permission actions denied for every agent. ``subagent``: a child session's
# steps and tool calls don't reach the parent's `run --format json` stream.
# ``webfetch``/``websearch``: web access outside the task (pi has none, omp's
# runs drop web_search). ``execute``: Code Mode runs tools from JavaScript, so
# its nested calls wouldn't reach the stream as tool events (denying it lists
# the tools directly). ``question``: nobody answers in a non-interactive run.
# The ``opencode_*`` utilities are Code Mode's ``opencode`` namespace, offered
# as direct tools once ``execute`` is denied: moving or renaming the session,
# creating and removing worktrees (a session can move out of the workspace),
# listing models, and MCP resources (no MCP servers are allowed anyway).
DENIED_ACTIONS = (
    "subagent", "webfetch", "websearch", "execute", "question",
    "experimental_image_generate", "browser",
    "opencode_session_move", "opencode_session_rename", "opencode_worktree_create",
    "opencode_worktree_list", "opencode_models",
    "opencode_list_mcp_resources", "opencode_read_mcp_resource",
)  # fmt: skip


class OpenCodeAdapterError(AdapterError):
    pass


# --- parsing -------------------------------------------------------------------


@dataclasses.dataclass
class ParsedRun:
    """What an ``opencode run --format json`` stream says about a run.

    One ``step_start``/``step_finish`` pair per model request (a turn: the
    assistant response and the tool calls it made); ``step_finish`` carries
    that request's cost and tokens. Tool calls arrive as ``tool_use`` parts
    once they've finished.
    """

    events: list[ToolEvent] = dataclasses.field(default_factory=list)
    turns: int = 0
    requests_started: int = 0
    cost_usd: float = 0.0
    tokens: dict[str, int] = dataclasses.field(default_factory=dict)
    finish_reasons: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    tool_errors: int = 0
    stopped: bool = False
    """The adapter stopped OpenCode; a ``step_finish`` after that isn't a turn."""
    session_id: str | None = None
    bad_lines: int = 0
    other_events: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if self.session_id is None and record.get("sessionID"):
            self.session_id = record["sessionID"]
        part = record.get("part") or {}
        if kind == "step_start":
            self.requests_started += 1
        elif kind == "step_finish":
            self._finish(part)
        elif kind == "tool_use":
            name = str(part.get("tool", "?"))
            state = part.get("state") or {}
            args = state.get("input")
            self.events.append(
                ToolEvent(
                    kind=TOOL_KINDS.get(name, "other"),
                    name=name,
                    input=args if isinstance(args, dict) else {"args": args},
                )
            )
            self.tool_errors += state.get("status") == "error"
        elif kind == "error":
            err = record.get("error") or {}
            self.errors.append(f"{err.get('type', 'error')}: {err.get('message', '')}".strip())
        elif kind not in ("text", "reasoning"):
            self.other_events[str(kind)] += 1

    def _finish(self, part: dict[str, Any]) -> None:
        self.turns += not self.stopped
        self.cost_usd += float(part.get("cost") or 0.0)
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        counts = {
            "input": tokens.get("input"),
            "output": tokens.get("output"),
            "reasoning": tokens.get("reasoning"),
            "cacheRead": cache.get("read"),
            "cacheWrite": cache.get("write"),
        }
        counts = {k: v for k, v in counts.items() if isinstance(v, int)}
        # OpenCode has no total; pi's totalTokens is the sum of the parts.
        counts["totalTokens"] = sum(counts.values())
        for k, v in counts.items():
            self.tokens[k] = self.tokens.get(k, 0) + v
        if part.get("reason"):
            self.finish_reasons.append(str(part["reason"]))
        if part.get("reason") == "error":
            self.errors.append("step finished with an error")


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


def parse_version(out: str) -> str:
    """``opencode v2.2.6`` -> ``2.2.6``."""
    m = re.search(r"(\d+\.\d+\.\d+\S*)", out)
    return m.group(1) if m else out.strip()


# --- workspace -------------------------------------------------------------------


def context_files_above(workspace: pathlib.Path) -> list[pathlib.Path]:
    found = []
    for parent in workspace.resolve().parents:
        found += [parent / n for n in CONTEXT_FILES if (parent / n).exists()]
    return found


def workspace_extras(workspace: pathlib.Path) -> list[str]:
    """Workspace files OpenCode would read beyond CLAUDE.md and .claude/skills."""
    found = [n for n in WORKSPACE_EXTRAS if (workspace / n).exists()]
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP]
        if "AGENTS.md" in files:
            found.append(str(pathlib.Path(root, "AGENTS.md").relative_to(workspace)))
    return found


def skill_names(workspace: pathlib.Path) -> list[str]:
    root = workspace / ".claude" / "skills"
    if not root.is_dir():
        return []
    return sorted(p.parent.name for p in root.glob("*/SKILL.md"))


# --- the model ---------------------------------------------------------------------


def model_entry(openrouter_id: str, models: list[dict[str, Any]] | None) -> tuple[str, dict]:
    """The OpenCode model id to select and its ``providers.openrouter.models`` entry.

    OpenCode's OpenRouter catalog lists undated ids (``z-ai/glm-5.3``), so a
    dated id (``z-ai/glm-5.3-20260816``) isn't selectable as it is. When
    OpenRouter lists the dated id as the ``canonical_slug`` of an undated one,
    the undated catalog entry is overridden with ``modelID`` set to the dated
    id: OpenCode keeps its catalog data for the model (limits, reasoning) and
    sends the dated id. Otherwise the id is added as it is (a custom entry,
    which gets OpenCode's defaults if it isn't in the catalog).
    """
    for m in models or []:
        listed = str(m.get("id"))
        # ":batch", ":free" and the like are variants, not the model.
        if (
            m.get("canonical_slug") == openrouter_id
            and listed != openrouter_id
            and ":" not in listed
        ):
            return listed, {"modelID": openrouter_id}
    return openrouter_id, {}


def with_effort(entry: dict[str, Any], effort: str | None) -> dict[str, Any]:
    """The model entry with a pinned reasoning effort, if there is one.

    OpenCode's ``default`` variant sends no ``reasoning`` field, so nothing is
    added when ``effort`` is None. When it's set, the entry's ``body`` (merged
    into every request, per OpenCode's model docs) carries OpenRouter's
    ``reasoning: {"effort": ...}``, the same shape pi sends. (The documented
    ``settings.reasoningEffort`` was checked through the relay too: it sends a
    top-level ``reasoning_effort`` plus a stray ``reasoningEffort`` key.)
    """
    if effort is None:
        return entry
    return {**entry, "body": {"reasoning": {"effort": effort}}}


# --- the adapter -------------------------------------------------------------------


class OpenCodeAdapter:
    name = "opencode"

    def __init__(
        self,
        pinned_version: str,
        *,
        charmlint: str,
        binary: str = "opencode",
        fetch: Fetcher = fetch_generation,
        models: ModelsFetcher = fetch_models,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        relay: Callable[[], RecordingRelay] = RecordingRelay,
    ):
        self.pinned_version = pinned_version
        self.charmlint = charmlint
        self.binary = binary
        self._fetch = fetch
        self._models = models
        self._popen = popen
        self._relay = relay

    def version(self) -> str:
        exe = shutil.which(self.binary)
        if exe is None:
            raise OpenCodeAdapterError(f"{self.binary!r} is not on PATH; install OpenCode")
        with tempfile.TemporaryDirectory(prefix="wof-opencode-ver-") as home:
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

    def config_for(
        self,
        workspace: pathlib.Path,
        selector: str,
        entry: dict[str, Any],
        caps: Caps,
        relay_url: str,
    ) -> dict[str, Any]:
        """The run's global config (``$HOME/.config/opencode/opencode.json``)."""
        permissions: list[dict[str, str]] = [
            {"action": a, "resource": "*", "effect": "deny"} for a in DENIED_ACTIONS
        ]
        # Only the workspace's skills: OpenCode's built-in ones (`opencode`,
        # `report`) are hidden and refused.
        permissions.append({"action": "skill", "resource": "*", "effect": "deny"})
        permissions += [
            {"action": "skill", "resource": s, "effect": "allow"} for s in skill_names(workspace)
        ]
        context = workspace / self.context_file()
        return {
            "$schema": "https://opencode.ai/config.json",
            "update": "disable",
            "websearch": False,
            "snapshots": False,
            "formatter": False,
            "instructions": [str(context)] if context.is_file() else [],
            "default_agent": "build",
            "agents": {"build": {"steps": caps.max_turns + 1}},
            "permissions": permissions,
            "policies": [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "openrouter", "effect": "allow"},
                {"action": "mcp.use", "resource": "*", "effect": "deny"},
                {"action": "websearch.use", "resource": "*", "effect": "deny"},
                {"action": "integration.connect", "resource": "*", "effect": "deny"},
            ],
            "providers": {
                "openrouter": {
                    "settings": {"baseURL": f"{relay_url}/api/v1"},
                    "models": {selector: entry},
                }
            },
        }

    def command(self, selector: str, prompt: str, title: str) -> list[str]:
        return [
            self.binary, "run",
            "--standalone",
            "--auto",
            "--format", "json",
            "--model", f"openrouter/{selector}",
            "--agent", "build",
            "--title", title,
            prompt,
        ]  # fmt: skip

    def environment(
        self,
        home: pathlib.Path,
        key: str,
        base: Mapping[str, str] | None,
        constraints: pathlib.Path,
        tmpdir: pathlib.Path | None = None,
    ) -> dict[str, str]:
        src = os.environ if base is None else base
        env = {k: v for k, v in src.items() if not k.startswith(_DROP_ENV_PREFIXES)}
        for k in ("CLAUDE_CONFIG_DIR", "OTEL_EXPORTER_OTLP_ENDPOINT"):
            env.pop(k, None)
        real_home = pathlib.Path(src.get("HOME") or os.path.expanduser("~"))
        env.setdefault("UV_CACHE_DIR", str(real_home / ".cache" / "uv"))
        env.setdefault(
            "UV_PYTHON_INSTALL_DIR", str(real_home / ".local" / "share" / "uv" / "python")
        )
        env.setdefault("UV_TOOL_DIR", str(real_home / ".local" / "share" / "uv" / "tools"))
        _common.write_constraints(constraints, self.charmlint)
        # The relay is on 127.0.0.1; keep it off any outbound proxy.
        no_proxy = ",".join(
            x for x in (src.get("NO_PROXY") or src.get("no_proxy"), "127.0.0.1,localhost") if x
        )
        env.update(
            HOME=str(home),
            OPENROUTER_API_KEY=key,
            UV_CONSTRAINT=str(constraints),
            OPENCODE_DISABLE_FREE_MODELS="1",
            OTEL_SDK_DISABLED="true",
            NO_PROXY=no_proxy,
            no_proxy=no_proxy,
        )
        # OpenCode tells the model to prefer `$TMPDIR/opencode` for scratch
        # ("pre-created and approved for external access"); with the system
        # TMPDIR that's /tmp/opencode, shared by every run on the machine.
        if tmpdir is not None:
            env["TMPDIR"] = str(tmpdir)
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
            raise OpenCodeAdapterError(f"${openrouter_key_env} is not set")
        if model.providers:
            raise OpenCodeAdapterError(
                "the OpenCode adapter can't pin an OpenRouter provider order yet"
            )
        if SYSTEM_CONFIG_DIR.exists():
            raise OpenCodeAdapterError(f"{SYSTEM_CONFIG_DIR} would reach OpenCode (system config)")
        leaks = context_files_above(workspace)
        if leaks:
            raise OpenCodeAdapterError(
                "context or config files above the workspace would reach OpenCode: "
                + ", ".join(map(str, leaks))
            )
        extras = workspace_extras(workspace)
        if extras:
            raise OpenCodeAdapterError(
                f"OpenCode would also read {', '.join(extras)} from the workspace"
            )
        hide = _common.resolve_hidden(workspace, hide)
        wrapper = _common.hide_wrapper(hide)
        version = self.version()
        if version != self.pinned_version:
            raise OpenCodeAdapterError(
                f"OpenCode {version} is installed; the pin is {self.pinned_version}"
            )
        selector, entry = model_entry(model.openrouter_id, self._models())
        entry = with_effort(entry, model.reasoning_effort)

        events_path = workspace / ".wof" / EVENTS_FILE
        events_path.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wof-opencode-") as tmp, self._relay() as relay:
            home = pathlib.Path(tmp) / "home"
            cfg_dir = home / ".config" / "opencode"
            cfg_dir.mkdir(parents=True)
            config = self.config_for(workspace, selector, entry, caps, relay.url)
            (cfg_dir / "opencode.json").write_text(json.dumps(config, indent=2) + "\n")
            constraints = pathlib.Path(tmp) / _common.CONSTRAINTS_FILE
            run_tmp = pathlib.Path(tmp) / "tmp"
            run_tmp.mkdir()
            run_env = self.environment(home, key, env, constraints, run_tmp)
            cmd = [*wrapper, *self.command(selector, prompt, f"wof {workspace.name}")]
            parsed, exit_reason, rc, stderr, wall = self._stream(
                cmd, workspace, run_env, caps, events_path
            )
            stored_credentials = count_credentials(home)
            relay_summary = relay.summary()
            generation_ids = list(relay.generation_ids)
        if rc == HIDE_FAILED and not parsed.turns and wrapper:
            raise OpenCodeAdapterError(f"could not hide {hide} from the agent: {stderr[-500:]}")
        lookup = served_by(generation_ids, key, self._fetch)
        exit_reason, error = outcome(parsed, exit_reason, rc, stderr)
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
                "opencode_exit_code": rc,
                "opencode_model": f"openrouter/{selector}",
                "opencode_model_entry": entry,
                "reasoning_effort": model.reasoning_effort,
                "steps": caps.max_turns + 1,
                "requests": parsed.requests_started,
                "finish_reasons": dict(collections.Counter(parsed.finish_reasons)),
                "tool_errors": parsed.tool_errors,
                "errors": parsed.errors,
                "other_events": dict(parsed.other_events),
                "denied_actions": list(DENIED_ACTIONS),
                "skills": skill_names(workspace),
                "agent_charmlint": self.charmlint,
                "hidden_paths": len([h for h in hide if h.exists()]),
                "stored_credentials": stored_credentials,
                "stderr_tail": stderr[-500:] if rc else "",
                **relay_summary,
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
            reason = cap_reached(parsed, caps, elapsed)
            parsed.stopped = parsed.stopped or reason is not None
            return reason

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


def cap_reached(parsed: ParsedRun, caps: Caps, elapsed: float) -> str | None:
    """OpenCode has no time or cost cap, so all three are enforced on the stream."""
    if parsed.turns >= caps.max_turns:
        return "turn_cap"
    if elapsed >= caps.timeout_minutes * 60:
        return "time_cap"
    # Checked after each step_finish, so it overshoots by at most one request.
    if caps.max_cost_usd is not None and parsed.cost_usd >= caps.max_cost_usd:
        return "cost_cap"
    return None


def outcome(parsed: ParsedRun, exit_reason: str, rc: int, stderr: str) -> tuple[str, str | None]:
    if exit_reason != "completed":
        return exit_reason, None
    if rc != 0 or parsed.errors:
        return "error", "; ".join(parsed.errors) or f"opencode exited {rc}: {stderr[-500:]}"
    if not parsed.turns:
        return "error", "opencode made no model request"
    return "completed", None


CREDENTIAL_TABLES = ("credential", "account", "control_account")


def count_credentials(home: pathlib.Path) -> int | None:
    """Rows in the fresh home's credential and account tables after the run (expected: 0)."""
    import sqlite3

    db = home / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        return None
    try:
        with sqlite3.connect(db) as conn:
            return sum(
                int(conn.execute(f"select count(*) from {t}").fetchone()[0])
                for t in CREDENTIAL_TABLES
            )
    except sqlite3.Error:
        return None
