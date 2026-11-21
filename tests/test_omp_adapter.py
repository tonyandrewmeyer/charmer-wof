"""The omp adapter, against a recorded omp 18.19.0 session and a fake omp process."""

from __future__ import annotations

import io
import json
import pathlib
import re

import pytest

from charmer_wof.adapters import omp
from charmer_wof.adapters.base import get_adapter
from charmer_wof.config import Caps
from charmer_wof.scoring.process import process_outcome

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ONELINE = FIXTURES / "omp-18.19.0-oneline.jsonl"


def test_parse_recorded_session():
    parsed = omp.parse_file(ONELINE)
    assert parsed.turns == 2 and parsed.requests_started == 2
    assert parsed.ended and parsed.errors == [] and parsed.bad_lines == 0
    assert [(e.kind, e.name, e.input["command"]) for e in parsed.events] == [
        ("shell", "bash", "ls /tmp/oneline/secret")
    ]
    assert parsed.cost_usd == pytest.approx(0.003784368 + 0.0007277792)
    # omp's reasoningTokens map to pi's "reasoning", so tokens_* names match.
    assert set(parsed.tokens) == {
        "input", "output", "cacheRead", "cacheWrite", "totalTokens", "reasoning",
    }  # fmt: skip
    assert parsed.tokens["input"] == 8898 and parsed.tokens["reasoning"] == 58
    assert parsed.response_ids == [
        "gen-1794493041-iH0Spm6hYb2neXxKYuae",
        "gen-1794493046-ppuIxDcE38OS5K7aRwrg",
    ]
    assert parsed.models == ["z-ai/glm-5.3-20260816"]
    assert parsed.stop_reasons == ["toolUse", "stop"]


def test_fixture_is_real_omp_output():
    header = json.loads(ONELINE.read_text().split("\n", 1)[0])
    assert header["type"] == "session" and header["version"] == 3


def test_parse_maps_tool_kinds_errors_and_notices():
    lines = [
        {"type": "tool_execution_start", "toolName": n, "args": {"path": "x"}}
        for n in ("read", "write", "edit", "ast_edit", "grep", "glob", "find", "eval", "todo")
    ]
    lines += [
        {"type": "tool_execution_end", "isError": True},
        {"type": "auto_retry_start"},
        {"type": "auto_compaction_end"},
        {"type": "notice", "level": "error", "source": "cost-gate", "message": "Cost cap reached"},
        {"type": "cache_warming_end"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "402 insufficient credits",
                "usage": {"cost": {"total": 0}},
            },
        },
    ]
    parsed = omp.parse_lines([json.dumps(r) for r in lines] + ["not json", ""])
    assert [e.kind for e in parsed.events] == [
        "read", "edit", "edit", "edit", "search", "search", "search", "other", "other",
    ]  # fmt: skip
    assert parsed.tool_errors == 1 and parsed.retries == 1 and parsed.compactions == 1
    assert parsed.notices == ["cost-gate"]
    assert parsed.other_events == {"cache_warming_end": 1}
    assert parsed.errors == ["402 insufficient credits"]
    assert not parsed.ended and parsed.bad_lines == 1


def test_parse_version():
    assert omp.parse_version("omp/18.19.0\n") == "18.19.0"


def _ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude/skills/add-ingress").mkdir(parents=True)
    (ws / ".claude/skills/add-ingress/SKILL.md").write_text("---\nname: add-ingress\n---\n")
    (ws / ".claude/skills/notes.md").write_text("not a skill dir")
    (ws / "CLAUDE.md").write_text("Run `uvx charmlint .`\n")
    return ws


def test_command_caps_skills_and_isolation(tmp_path, config):
    ws = _ws(tmp_path)
    adapter = omp.OmpAdapter("18.19.0", charmlint="0.3.0")
    caps = Caps(max_turns=60, timeout_minutes=20, max_cost_usd=1.0)
    cmd = adapter.command(ws, "-dash", config.models["glm-5-3"], caps, tmp_path / "o.yml")
    assert cmd[cmd.index("--model") + 1] == "openrouter/z-ai/glm-5.3-20260816"
    assert cmd[cmd.index("--max-time") + 1] == "1200"
    assert cmd[cmd.index("--max-cost") + 1] == "1.0"
    assert cmd[cmd.index("--skills") + 1] == "add-ingress"
    assert cmd[cmd.index("--config") + 1] == str(tmp_path / "o.yml")
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert "task" not in tools and "web_search" not in tools and "bash" in tools
    for flag in ("--print", "--no-session", "--no-rules", "--no-extensions", "--no-title"):
        assert flag in cmd
    assert cmd[-2:] == ["--", "-dash"]
    (ws / ".claude/skills/add-ingress/SKILL.md").unlink()
    cmd = adapter.command(ws, "p", config.models["glm-5-3"], Caps(), tmp_path / "o.yml")
    assert "--no-skills" in cmd and "--max-cost" not in cmd


def test_overlay_turns_off_ambient_discovery():
    assert "  - agents-md\n" in omp.OVERLAY and "  - mcp-json\n" in omp.OVERLAY
    assert "  - claude\n" not in omp.OVERLAY and "  - claude-md\n" not in omp.OVERLAY
    assert 'cacheWarming: "off"' in omp.OVERLAY
    assert "web_search:\n  enabled: false" in omp.OVERLAY
    assert "enableProjectConfig: false" in omp.OVERLAY


def test_overlay_turns_off_keyless_model_discovery():
    # The catalogue hosts egress-audit.yml saw omp 18.19.0 reach besides OpenRouter.
    for provider in (
        "zenmux",
        "kilo",
        "venice",
        "alibaba-coding-plan",
        "charm-hyper",
        "commandcode",
    ):
        assert f"  - {provider}\n" in omp.OVERLAY
    for local in ("ollama", "lm-studio", "llama.cpp"):
        assert f"  - {local}\n" in omp.OVERLAY
    assert "  - openrouter\n" not in omp.OVERLAY


def test_environment_fresh_home_key_and_charmlint_pin(tmp_path):
    adapter = omp.OmpAdapter("18.19.0", charmlint="0.3.0")
    base = {
        "PATH": "/bin",
        "HOME": "/root",
        "PI_CODING_AGENT_DIR": "/root/.omp/agent",
        "OMP_PROFILE": "work",
        "XDG_DATA_HOME": "/x",
        "UV_PYTHON": "3.12",
    }
    env = adapter.environment(tmp_path / "home", "sk-test", base, tmp_path / "c.txt")
    assert env["HOME"] == str(tmp_path / "home")
    assert env["OPENROUTER_API_KEY"] == "sk-test"
    assert not {"PI_CODING_AGENT_DIR", "OMP_PROFILE", "XDG_DATA_HOME"} & set(env)
    assert env["UV_PYTHON"] == "3.12"
    assert env["UV_CACHE_DIR"] == "/root/.cache/uv"
    assert pathlib.Path(env["UV_CONSTRAINT"]).read_text() == "charmlint==0.3.0\n"


def test_context_files_above_and_shadowing(tmp_path):
    ws = tmp_path / "a" / "ws"
    ws.mkdir(parents=True)
    (tmp_path / "a" / "CLAUDE.md").write_text("leak")
    assert tmp_path / "a" / "CLAUDE.md" in omp.context_files_above(ws)
    assert omp.shadowing_context_files(ws) == []
    (ws / "AGENTS.md").write_text("would win over CLAUDE.md")
    assert omp.shadowing_context_files(ws) == ["AGENTS.md"]


class FakeOmp:
    """A Popen stand-in that replays recorded omp output."""

    def __init__(self, lines: list[bytes], rc: int = 0, stderr: bytes = b""):
        self.stdout = io.BytesIO(b"".join(lines))
        self.stderr = io.BytesIO(stderr)
        self.pid = -1
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc


def _adapter(monkeypatch, lines, rc=0, stderr=b""):
    monkeypatch.setenv("WOF_TEST_KEY", "sk-test")
    adapter = omp.OmpAdapter(
        "18.19.0",
        charmlint="0.3.0",
        fetch=lambda gen_id, key: {"provider_name": "Wafer", "model": "m", "total_cost": 0.001},
        popen=lambda *a, **kw: FakeOmp(lines, rc, stderr),
    )
    monkeypatch.setattr(adapter, "version", lambda: "18.19.0")
    monkeypatch.setattr(omp, "_stop", lambda proc: None)
    monkeypatch.setattr(omp, "context_files_above", lambda ws: [])
    return adapter


def _lines():
    return ONELINE.read_bytes().splitlines(keepends=True)


def test_run_replays_session(tmp_path, monkeypatch, config):
    ws = _ws(tmp_path)
    adapter = _adapter(monkeypatch, _lines())
    result = adapter.run(ws, "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "completed" and result.error is None
    assert result.turns == 2 and result.provider == "Wafer"
    assert result.cost_usd == pytest.approx(0.004512, abs=1e-6)
    assert result.details["skills"] == ["add-ingress"]
    assert (ws / ".wof" / omp.EVENTS_FILE).read_bytes() == ONELINE.read_bytes()
    out = process_outcome(result, config.treatments["none"], Caps())
    assert out.tool_calls == {"bash": 1} and out.exit_reason == "completed"
    assert out.metrics["tokens_input"] == 8898 and out.metrics["tokens_reasoning"] == 58


def test_run_turn_cap(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, _lines())
    result = adapter.run(
        _ws(tmp_path), "p", config.models["glm-5-3"], Caps(max_turns=1), "WOF_TEST_KEY"
    )
    assert result.exit_reason == "turn_cap"
    # The turn_end omp emits after the adapter stops it isn't counted.
    assert result.turns == 1


def test_run_omp_cost_cap_exit(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, _lines(), rc=3)
    result = adapter.run(
        _ws(tmp_path), "p", config.models["glm-5-3"], Caps(max_cost_usd=0.001), "WOF_TEST_KEY"
    )
    assert result.exit_reason == "cost_cap" and result.error is None


def test_run_cost_backstop_when_omp_keeps_going(tmp_path, monkeypatch, config):
    # The cap is reached after the first request; the second request starting
    # anyway means omp's own gate didn't stop it, so the adapter does.
    adapter = _adapter(monkeypatch, _lines())
    result = adapter.run(
        _ws(tmp_path), "p", config.models["glm-5-3"], Caps(max_cost_usd=0.001), "WOF_TEST_KEY"
    )
    assert result.exit_reason == "cost_cap"


def test_run_deadline_is_time_cap(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, _lines()[:-1], rc=1, stderr=b"Error: Deadline exceeded\n")
    result = adapter.run(_ws(tmp_path), "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "time_cap"


def test_outcome_deadline_between_turns_exits_zero():
    parsed = omp.parse_file(ONELINE)
    caps = Caps(timeout_minutes=1)
    assert omp.outcome(parsed, "completed", 0, "", caps, wall=61.0) == ("time_cap", None)
    assert omp.outcome(parsed, "completed", 0, "", caps, wall=30.0) == ("completed", None)


def test_run_nonzero_exit_is_an_error(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, _lines()[:-1], rc=1)
    result = adapter.run(_ws(tmp_path), "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "error" and "omp exited 1" in result.error


def test_run_refuses(tmp_path, monkeypatch, config):
    model = config.models["glm-5-3"]
    adapter = _adapter(monkeypatch, [])
    ws = _ws(tmp_path)
    (ws / ".github").mkdir()
    (ws / ".github/copilot-instructions.md").write_text("x")
    with pytest.raises(
        omp.OmpAdapterError, match=re.escape("instead of the workspace's CLAUDE.md")
    ):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY")
    (ws / ".github/copilot-instructions.md").unlink()
    with pytest.raises(omp.AdapterError, match="inside a hidden path"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY", hide=[tmp_path])
    monkeypatch.setattr(adapter, "version", lambda: "18.20.0")
    with pytest.raises(omp.OmpAdapterError, match=r"the pin is 18\.19\.0"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY")
    monkeypatch.delenv("WOF_TEST_KEY")
    with pytest.raises(omp.OmpAdapterError, match="not set"):
        adapter.run(ws, "p", model, Caps(), "WOF_TEST_KEY")


def test_get_adapter(config):
    adapter = get_adapter(config, "omp")
    assert isinstance(adapter, omp.OmpAdapter)
    assert adapter.pinned_version == config.harnesses["omp"].version == "18.19.0"
    assert adapter.charmlint == config.scoring.static.charmlint
    assert adapter.context_file() == config.tasks["hedgedoc-ingress"].context_file


def test_skill_uri_read_counts_as_reading_the_help(config):
    from charmer_wof.adapters.base import AdapterResult, ToolEvent

    result = AdapterResult(
        exit_reason="completed", cost_usd=0.0, turns=1, wall_seconds=1.0, provider=None,
        events=[ToolEvent(kind="read", name="read", input={"path": "skill://add-ingress"})],
    )  # fmt: skip
    for treatment in ("skill", "skill-scripts"):
        assert process_outcome(result, config.treatments[treatment], Caps()).read_help


# --- reasoning effort (PLAN §8, 2026-11-14) --------------------------------------


def test_unset_effort_sends_no_thinking_flag(tmp_path, config):
    """Unset: no --thinking (omp's default level; HARNESS.md says what that sends)."""
    model = config.models["glm-5-3"]
    cmd = omp.OmpAdapter("18.19.0", charmlint="0.3.0").command(
        tmp_path, "p", model, Caps(), tmp_path / "o.yml"
    )
    assert "--thinking" not in cmd
    assert omp.thinking_level(model) is None


@pytest.mark.parametrize(("effort", "level"), [("high", "high"), ("none", "off")])
def test_set_effort_is_the_thinking_flag(tmp_path, monkeypatch, config, effort, level):
    model = config.models["glm-5-3"].model_copy(update={"reasoning_effort": effort})
    cmd = omp.OmpAdapter("18.19.0", charmlint="0.3.0").command(
        tmp_path, "p", model, Caps(), tmp_path / "o.yml"
    )
    assert cmd[cmd.index("--thinking") + 1] == level
    assert cmd.index("--thinking") < cmd.index("--")


def test_effort_is_in_the_run_details(tmp_path, monkeypatch, config):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("x")
    for effort, thinking in ((None, omp.UNSET_THINKING), ("low", "low")):
        model = config.models["glm-5-3"].model_copy(update={"reasoning_effort": effort})
        result = _adapter(monkeypatch, _lines()).run(ws, "p", model, Caps(), "WOF_TEST_KEY")
        assert result.details["reasoning_effort"] == effort
        assert result.details["thinking"] == thinking
