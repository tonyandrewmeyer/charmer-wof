"""The OpenCode adapter, against a recorded OpenCode 2.2.6 session and a fake opencode process."""

from __future__ import annotations

import http.server
import io
import json
import pathlib
import threading
import urllib.request
from typing import ClassVar

import pytest

from charmer_wof.adapters import _common, opencode
from charmer_wof.adapters.base import get_adapter
from charmer_wof.config import Caps
from charmer_wof.scoring.process import process_outcome

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ONELINE = FIXTURES / "opencode-2.2.6-oneline.jsonl"


def test_parse_recorded_session():
    parsed = opencode.parse_file(ONELINE)
    assert parsed.turns == 2 and parsed.requests_started == 2
    assert parsed.errors == [] and parsed.bad_lines == 0 and parsed.tool_errors == 0
    assert [(e.kind, e.name, e.input) for e in parsed.events] == [
        ("skill", "skill", {"id": "add-ingress"}),
        ("shell", "shell", {"command": "ls /tmp/claude-0/oc-check/run1/secret"}),
    ]
    assert parsed.cost_usd == pytest.approx(0.00153661 + 0.00335651)
    # The same tokens_* names as pi and omp; totalTokens is the sum of the parts.
    assert parsed.tokens == {
        "input": 3556, "output": 143, "reasoning": 368,
        "cacheRead": 3648, "cacheWrite": 0, "totalTokens": 7715,
    }  # fmt: skip
    assert parsed.finish_reasons == ["tool-calls", "stop"]
    assert parsed.session_id and parsed.session_id.startswith("ses_")


def test_fixture_is_real_opencode_output():
    first = json.loads(ONELINE.read_text().split("\n", 1)[0])
    assert first["type"] == "step_start" and first["part"]["type"] == "step-start"


def test_parse_maps_tool_kinds_and_errors():
    lines = [
        {"type": "tool_use", "part": {"tool": n, "state": {"status": "completed", "input": {}}}}
        for n in ("read", "write", "edit", "patch", "glob", "grep", "list", "todowrite")
    ]
    lines += [
        {"type": "tool_use", "part": {"tool": "shell", "state": {"status": "error", "input": {}}}},
        {"type": "error", "error": {"type": "provider.no-route", "message": "Model unavailable"}},
        {"type": "text", "part": {"text": "hi"}},
        {"type": "something_new"},
    ]
    parsed = opencode.parse_lines([json.dumps(r) for r in lines] + ["not json", ""])
    assert [e.kind for e in parsed.events] == [
        "read", "edit", "edit", "edit", "search", "search", "search", "other", "shell",
    ]  # fmt: skip
    assert parsed.tool_errors == 1
    assert parsed.errors == ["provider.no-route: Model unavailable"]
    assert parsed.other_events == {"something_new": 1} and parsed.bad_lines == 1


def test_step_finish_after_stop_is_not_a_turn():
    parsed = opencode.ParsedRun()
    parsed.feed({"type": "step_finish", "part": {"reason": "tool-calls", "cost": 0.1}})
    parsed.stopped = True
    parsed.feed({"type": "step_finish", "part": {"reason": "error", "cost": 0.01}})
    assert parsed.turns == 1 and parsed.cost_usd == pytest.approx(0.11)


def test_parse_version():
    assert opencode.parse_version("opencode v2.2.6\n") == "2.2.6"


MODELS = [
    {"id": "z-ai/glm-5.3", "canonical_slug": "z-ai/glm-5.3-20260816"},
    {"id": "z-ai/glm-5.3:batch", "canonical_slug": "z-ai/glm-5.3-20260816"},
    {"id": "deepseek/deepseek-v4-pro-0813", "canonical_slug": "deepseek/deepseek-v4-pro-20260813"},
]


def test_model_entry_aliases_a_dated_id_onto_the_catalog_one():
    assert opencode.model_entry("z-ai/glm-5.3-20260816", MODELS) == (
        "z-ai/glm-5.3",
        {"modelID": "z-ai/glm-5.3-20260816"},
    )
    assert opencode.model_entry("deepseek/deepseek-v4-pro-0813", MODELS) == (
        "deepseek/deepseek-v4-pro-0813",
        {},
    )
    assert opencode.model_entry("z-ai/glm-5.3-20260816", None) == ("z-ai/glm-5.3-20260816", {})


def _ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude/skills/add-ingress").mkdir(parents=True)
    (ws / ".claude/skills/add-ingress/SKILL.md").write_text("---\nname: add-ingress\n---\n")
    (ws / ".claude/skills/notes.md").write_text("not a skill dir")
    (ws / "CLAUDE.md").write_text("Run `uvx charmlint .`\n")
    return ws


def test_config_maps_context_skills_caps_and_relay(tmp_path):
    ws = _ws(tmp_path)
    adapter = opencode.OpenCodeAdapter("2.2.6", charmlint="0.3.0")
    caps = Caps(max_turns=60, timeout_minutes=20, max_cost_usd=1.0)
    entry = {"modelID": "z-ai/glm-5.3-20260816"}
    cfg = adapter.config_for(ws, "z-ai/glm-5.3", entry, caps, "http://127.0.0.1:1234")
    assert cfg["instructions"] == [str(ws / "CLAUDE.md")]
    assert cfg["agents"] == {"build": {"steps": 61}}
    assert cfg["websearch"] is False and cfg["update"] == "disable"
    rules = [(r["action"], r["resource"], r["effect"]) for r in cfg["permissions"]]
    for action in ("subagent", "webfetch", "websearch", "execute", "question"):
        assert (action, "*", "deny") in rules
    # Built-in skills are refused; the workspace's are allowed, after the deny.
    assert rules[-2:] == [("skill", "*", "deny"), ("skill", "add-ingress", "allow")]
    policies = [(p["action"], p["resource"], p["effect"]) for p in cfg["policies"]]
    assert policies[:2] == [
        ("provider.use", "*", "deny"),
        ("provider.use", "openrouter", "allow"),
    ]
    assert ("mcp.use", "*", "deny") in policies
    openrouter = cfg["providers"]["openrouter"]
    assert openrouter["settings"]["baseURL"] == "http://127.0.0.1:1234/api/v1"
    assert openrouter["models"] == {"z-ai/glm-5.3": entry}
    (ws / "CLAUDE.md").unlink()
    assert adapter.config_for(ws, "m", {}, caps, "u")["instructions"] == []


def test_command():
    adapter = opencode.OpenCodeAdapter("2.2.6", charmlint="0.3.0")
    cmd = adapter.command("z-ai/glm-5.3", "-dash", "wof ws")
    assert cmd[:2] == ["opencode", "run"] and cmd[-1] == "-dash"
    assert cmd[cmd.index("--model") + 1] == "openrouter/z-ai/glm-5.3"
    assert cmd[cmd.index("--format") + 1] == "json"
    for flag in ("--standalone", "--auto"):
        assert flag in cmd


def test_environment_fresh_home_key_and_charmlint_pin(tmp_path):
    adapter = opencode.OpenCodeAdapter("2.2.6", charmlint="0.3.0")
    base = {
        "PATH": "/bin",
        "HOME": "/root",
        "OPENCODE_CONFIG": "/x.json",
        "OPENCODE_DB": "/x.db",
        "XDG_CONFIG_HOME": "/x",
        "NO_PROXY": "example.com",
        "UV_PYTHON": "3.12",
    }
    env = adapter.environment(tmp_path / "home", "sk-test", base, tmp_path / "c.txt")
    assert "TMPDIR" not in env
    env = adapter.environment(
        tmp_path / "home", "sk-test", base, tmp_path / "c.txt", tmp_path / "tmp"
    )
    assert env["TMPDIR"] == str(tmp_path / "tmp")
    assert env["HOME"] == str(tmp_path / "home")
    assert env["OPENROUTER_API_KEY"] == "sk-test"
    assert not {"OPENCODE_CONFIG", "OPENCODE_DB", "XDG_CONFIG_HOME"} & set(env)
    assert env["OPENCODE_DISABLE_FREE_MODELS"] == "1"
    assert env["NO_PROXY"] == env["no_proxy"] == "example.com,127.0.0.1,localhost"
    assert env["UV_PYTHON"] == "3.12" and env["UV_CACHE_DIR"] == "/root/.cache/uv"
    assert pathlib.Path(env["UV_CONSTRAINT"]).read_text() == "charmlint==0.3.0\n"


def test_context_files_above_and_workspace_extras(tmp_path):
    ws = tmp_path / "a" / "ws"
    (ws / "src" / "sub").mkdir(parents=True)
    (tmp_path / "a" / "AGENTS.md").write_text("leak")
    assert tmp_path / "a" / "AGENTS.md" in opencode.context_files_above(ws)
    assert opencode.workspace_extras(ws) == []
    (ws / "src" / "sub" / "AGENTS.md").write_text("nested, loaded as the agent explores")
    (ws / "opencode.jsonc").write_text("{}")
    (ws / ".venv").mkdir()
    (ws / ".venv" / "AGENTS.md").write_text("in a dependency; not walked")
    assert opencode.workspace_extras(ws) == ["opencode.jsonc", "src/sub/AGENTS.md"]


class FakeOpenCode:
    """A Popen stand-in that replays recorded OpenCode output."""

    def __init__(self, lines: list[bytes], rc: int = 0, stderr: bytes = b""):
        self.stdout = io.BytesIO(b"".join(lines))
        self.stderr = io.BytesIO(stderr)
        self.pid = -1
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc


class FakeRelay:
    url = "http://127.0.0.1:9"
    generation_ids: ClassVar[list[str]] = ["gen-1", "gen-2"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def summary(self):
        return {"relay_requests": 2, "generation_ids": list(self.generation_ids)}


def _adapter(monkeypatch, lines, rc=0, stderr=b"", seen=None):
    monkeypatch.setenv("WOF_TEST_KEY", "sk-test")

    def popen(cmd, **kw):
        if seen is not None:
            home = pathlib.Path(kw["env"]["HOME"])
            seen["config"] = json.loads((home / ".config/opencode/opencode.json").read_text())
            seen["cmd"] = cmd
        return FakeOpenCode(lines, rc, stderr)

    adapter = opencode.OpenCodeAdapter(
        "2.2.6",
        charmlint="0.3.0",
        fetch=lambda gen_id, key: {
            "provider_name": "CoreWeave",
            "model": "m",
            "total_cost": 0.002,
        },
        models=lambda: MODELS,
        popen=popen,
        relay=FakeRelay,
    )
    monkeypatch.setattr(adapter, "version", lambda: "2.2.6")
    monkeypatch.setattr(opencode, "_stop", lambda proc: None)
    monkeypatch.setattr(opencode, "context_files_above", lambda ws: [])
    monkeypatch.setattr(opencode, "SYSTEM_CONFIG_DIR", pathlib.Path("/nonexistent/opencode"))
    return adapter


def _lines():
    return ONELINE.read_bytes().splitlines(keepends=True)


def test_run_replays_session(tmp_path, monkeypatch, config):
    ws = _ws(tmp_path)
    seen: dict = {}
    adapter = _adapter(monkeypatch, _lines(), seen=seen)
    result = adapter.run(ws, "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "completed" and result.error is None
    assert result.turns == 2 and result.provider == "CoreWeave"
    assert result.cost_usd == pytest.approx(0.00489312, abs=1e-6)
    assert result.details["billed_cost_usd"] == pytest.approx(0.004)
    assert result.details["opencode_model"] == "openrouter/z-ai/glm-5.3"
    assert result.details["generation_ids"] == ["gen-1", "gen-2"]
    assert seen["config"]["providers"]["openrouter"]["settings"]["baseURL"].startswith(
        FakeRelay.url
    )
    assert seen["config"]["providers"]["openrouter"]["models"] == {
        "z-ai/glm-5.3": {"modelID": "z-ai/glm-5.3-20260816"}
    }
    assert (ws / ".wof" / opencode.EVENTS_FILE).read_bytes() == ONELINE.read_bytes()
    # Nothing written into the workspace besides the adapter's .wof/.
    assert sorted(p.name for p in ws.iterdir()) == [".claude", ".wof", "CLAUDE.md"]
    out = process_outcome(result, config.treatments["skill"], Caps())
    assert out.tool_calls == {"skill": 1, "shell": 1}
    assert out.read_help  # the skill tool's {"id": "add-ingress"} is a help marker
    assert out.metrics["tokens_input"] == 3556 and out.metrics["tokens_reasoning"] == 368


def test_run_turn_cap(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, _lines())
    result = adapter.run(
        _ws(tmp_path), "p", config.models["glm-5-3"], Caps(max_turns=1), "WOF_TEST_KEY"
    )
    assert result.exit_reason == "turn_cap" and result.turns == 1


def test_run_cost_cap(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, _lines())
    result = adapter.run(
        _ws(tmp_path), "p", config.models["glm-5-3"], Caps(max_cost_usd=0.001), "WOF_TEST_KEY"
    )
    assert result.exit_reason == "cost_cap" and result.turns == 1


def test_cap_reached_time():
    parsed = opencode.ParsedRun()
    assert opencode.cap_reached(parsed, Caps(timeout_minutes=1), 59.0) is None
    assert opencode.cap_reached(parsed, Caps(timeout_minutes=1), 60.0) == "time_cap"


def test_run_errors(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, _lines()[:-1], rc=1, stderr=b"boom\n")
    result = adapter.run(_ws(tmp_path), "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "error" and "opencode exited 1" in result.error
    error = json.dumps({"type": "error", "error": {"type": "provider.no-route", "message": "x"}})
    adapter = _adapter(monkeypatch, [error.encode() + b"\n"], rc=1)
    ws = _ws(tmp_path / "b")
    result = adapter.run(ws, "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "error" and result.error == "provider.no-route: x"


def test_run_refuses(tmp_path, monkeypatch, config):
    model = config.models["glm-5-3"]
    adapter = _adapter(monkeypatch, [])
    ws = _ws(tmp_path)
    (ws / "AGENTS.md").write_text("would be loaded alongside CLAUDE.md")
    with pytest.raises(opencode.OpenCodeAdapterError, match=r"would also read AGENTS\.md"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY")
    (ws / "AGENTS.md").unlink()
    with pytest.raises(opencode.AdapterError, match="inside a hidden path"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY", hide=[tmp_path])
    monkeypatch.setattr(opencode, "SYSTEM_CONFIG_DIR", tmp_path)
    with pytest.raises(opencode.OpenCodeAdapterError, match="system config"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY")
    monkeypatch.setattr(opencode, "SYSTEM_CONFIG_DIR", tmp_path / "nope")
    monkeypatch.setattr(adapter, "version", lambda: "2.3.0")
    with pytest.raises(opencode.OpenCodeAdapterError, match=r"the pin is 2\.2\.6"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY")
    monkeypatch.delenv("WOF_TEST_KEY")
    with pytest.raises(opencode.OpenCodeAdapterError, match="not set"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY")


def test_count_credentials(tmp_path):
    import sqlite3

    assert opencode.count_credentials(tmp_path) is None
    db = tmp_path / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        for t in opencode.CREDENTIAL_TABLES:
            conn.execute(f"create table {t} (id text)")
    assert opencode.count_credentials(tmp_path) == 0
    with sqlite3.connect(db) as conn:
        conn.execute("insert into credential values ('openrouter')")
    assert opencode.count_credentials(tmp_path) == 1


def test_get_adapter(config):
    adapter = get_adapter(config, "opencode")
    assert isinstance(adapter, opencode.OpenCodeAdapter)
    assert adapter.pinned_version == config.harnesses["opencode"].version == "2.2.6"
    assert adapter.charmlint == config.scoring.static.charmlint
    assert adapter.context_file() == config.tasks["hedgedoc-ingress"].context_file


# --- the recording relay -----------------------------------------------------------


class _Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: ClassVar[list[dict]] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Upstream.seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        payload = (
            b'data: {"id":"gen-123-abc","model":"z-ai/glm-5.3-20260816","choices":[]}\n\n'
            b"data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_relay_forwards_and_records_generation_ids(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        with _common.RecordingRelay(f"http://127.0.0.1:{upstream.server_address[1]}") as relay:
            body = json.dumps(
                {
                    "model": "z-ai/glm-5.3-20260816",
                    "stream": True,
                    "tools": [{"type": "function", "function": {"name": n}} for n in "ba"],
                }
            ).encode()
            req = urllib.request.Request(
                f"{relay.url}/api/v1/chat/completions",
                data=body,
                headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as resp:
                assert b"gen-123-abc" in resp.read()
            summary = relay.summary()
    finally:
        upstream.shutdown()
    assert _Upstream.seen[-1] == {
        "path": "/api/v1/chat/completions",
        "auth": "Bearer sk-test",
        "body": body,
    }
    assert relay.generation_ids == ["gen-123-abc"]
    assert summary["requested_models"] == {"z-ai/glm-5.3-20260816": 1}
    assert summary["relay_statuses"] == {"200": 1} and summary["relay_errors"] == []
    assert summary["offered_tools"] == ["a", "b"]


# --- reasoning effort (PLAN §8, 2026-11-14) --------------------------------------


def test_unset_effort_adds_nothing_to_the_model_entry(tmp_path, monkeypatch, config):
    seen: dict = {}
    result = _adapter(monkeypatch, _lines(), seen=seen).run(
        _ws(tmp_path), "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY"
    )
    entry = seen["config"]["providers"]["openrouter"]["models"]["z-ai/glm-5.3"]
    assert entry == {"modelID": "z-ai/glm-5.3-20260816"}
    assert "--variant" not in seen["cmd"]
    assert result.details["reasoning_effort"] is None


def test_set_effort_goes_in_the_request_body(tmp_path, monkeypatch, config):
    model = config.models["glm-5-3"].model_copy(update={"reasoning_effort": "high"})
    seen: dict = {}
    result = _adapter(monkeypatch, _lines(), seen=seen).run(
        _ws(tmp_path), "p", model, Caps(), "WOF_TEST_KEY"
    )
    entry = seen["config"]["providers"]["openrouter"]["models"]["z-ai/glm-5.3"]
    assert entry == {
        "modelID": "z-ai/glm-5.3-20260816",
        "body": {"reasoning": {"effort": "high"}},
    }
    assert result.details["reasoning_effort"] == "high"
    assert result.details["opencode_model_entry"]["body"] == {"reasoning": {"effort": "high"}}
