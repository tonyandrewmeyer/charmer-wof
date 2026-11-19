"""The pi adapter, against a recorded pi 1.5.2 session and a fake pi process."""

from __future__ import annotations

import io
import json
import pathlib
import subprocess

import pytest

from charmer_wof.adapters import pi
from charmer_wof.adapters.base import get_adapter
from charmer_wof.config import Caps
from charmer_wof.scoring.process import process_outcome

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ONELINE = FIXTURES / "pi-1.5.2-oneline.jsonl"


def test_parse_recorded_session():
    parsed = pi.parse_file(ONELINE)
    assert parsed.turns == 2
    assert parsed.settled and not parsed.aborted
    assert [(e.kind, e.name, e.input["command"]) for e in parsed.events] == [
        ("shell", "bash", "echo hello")
    ]
    assert parsed.cost_from_provider
    assert parsed.cost_usd == pytest.approx(0.0001546224 + 0.0001351237)
    assert parsed.tokens["input"] == 349 + 12
    assert parsed.tokens["output"] == 41 + 25
    assert parsed.tokens["cacheRead"] == 1057 + 1441
    assert parsed.response_ids == [
        "gen-1794369789-IX1ynbK5WlhRVYHWYae5",
        "gen-1794369790-IdfPneVOR579QasPFYVU",
    ]
    assert parsed.models == ["z-ai/glm-5.3"]
    assert parsed.stop_reasons == ["toolUse", "stop"]
    assert parsed.errors == [] and parsed.bad_lines == 0


def test_parse_maps_tool_kinds_and_errors():
    lines = [
        {"type": "tool_execution_start", "toolName": n, "args": {"path": "x"}}
        for n in ("read", "write", "edit", "grep", "find", "ls", "codemode")
    ]
    lines += [
        {"type": "tool_execution_end", "isError": True},
        {"type": "auto_retry_start"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "402 insufficient credits",
                "usage": {"cost": {"total": 0}},
            },
        },
        {"type": "agent_settled", "aborted": True},
    ]
    parsed = pi.parse_lines([json.dumps(r) for r in lines] + ["not json", ""])
    assert [e.kind for e in parsed.events] == [
        "read", "edit", "edit", "search", "search", "search", "other",
    ]  # fmt: skip
    assert parsed.tool_errors == 1 and parsed.retries == 1
    assert parsed.errors == ["402 insufficient credits"]
    assert parsed.aborted and parsed.bad_lines == 1
    assert not parsed.cost_from_provider


def test_served_by_counts_providers_and_retries_missing():
    calls = []

    def fetch(gen_id, key):
        calls.append(gen_id)
        if gen_id == "late" and calls.count("late") == 1:
            return None
        return {"provider_name": "Wafer", "model": "z-ai/glm-5.3-20260816", "total_cost": 0.5}

    out = pi.served_by(["a", "late"], "k", fetch, retry_wait=0)
    assert out["providers"] == {"Wafer": 2}
    assert out["served_models"] == {"z-ai/glm-5.3-20260816": 2}
    assert out["billed_cost_usd"] == 1.0
    assert out["generations_missing"] == 0
    assert calls == ["a", "late", "late"]


def test_provider_label():
    assert pi.provider_label({}) is None
    assert pi.provider_label({"Wafer": 3}) == "Wafer"
    assert pi.provider_label({"B": 1, "A": 1, "Wafer": 5}) == "Wafer+A+B"


def _ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude/skills/add-ingress").mkdir(parents=True)
    (ws / ".claude/skills/add-ingress/SKILL.md").write_text("---\nname: add-ingress\n---\n")
    (ws / ".claude/skills/notes.md").write_text("not a skill dir")
    (ws / "CLAUDE.md").write_text("Run `uvx charmlint .`\n")
    return ws


def test_command_maps_claude_skills_and_isolates(tmp_path, config):
    ws = _ws(tmp_path)
    adapter = pi.PiAdapter("1.5.2", charmlint="0.3.0")
    cmd = adapter.command(ws, "-starts with a dash", config.models["glm-5-3"])
    assert cmd[cmd.index("--model") + 1] == "z-ai/glm-5.3-20260816"
    assert cmd[cmd.index("--provider") + 1] == "openrouter"
    assert cmd[cmd.index("--skill") + 1] == str(ws / ".claude/skills/add-ingress")
    assert cmd.count("--skill") == 1
    for flag in ("--no-skills", "--no-extensions", "--no-mcp", "--no-approve", "--no-session"):
        assert flag in cmd
    assert cmd[-2:] == ["--", "-starts with a dash"]


def test_environment_fresh_agent_dir_key_and_charmlint_pin(tmp_path):
    ws = _ws(tmp_path)
    adapter = pi.PiAdapter("1.5.2", charmlint="0.3.0")
    base = {"PATH": "/bin", "PI_CODING_AGENT_DIR": "/root/.pi/agent", "UV_PYTHON": "3.12"}
    env = adapter.environment(ws, tmp_path / "agent", "sk-test", base, tmp_path / "c.txt")
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path / "agent")
    assert env["OPENROUTER_API_KEY"] == "sk-test"
    assert env["UV_PYTHON"] == "3.12"
    assert pathlib.Path(env["UV_CONSTRAINT"]).read_text() == "charmlint==0.3.0\n"
    assert not env["UV_CONSTRAINT"].startswith(str(ws))


def test_context_files_above(tmp_path):
    ws = tmp_path / "a" / "ws"
    ws.mkdir(parents=True)
    assert tmp_path / "a" / "AGENTS.md" not in pi.context_files_above(ws)
    (tmp_path / "a" / "AGENTS.md").write_text("leak")
    assert tmp_path / "a" / "AGENTS.md" in pi.context_files_above(ws)


class FakePi:
    """A Popen stand-in that replays recorded pi output."""

    def __init__(self, lines: list[bytes], rc: int = 0):
        self.stdout = io.BytesIO(b"".join(lines))
        self.stderr = io.BytesIO(b"")
        self.pid = -1
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc


MODELS = [
    {
        "id": "z-ai/glm-5.3",
        "canonical_slug": "z-ai/glm-5.3-20260816",
        "context_length": 1048576,
        "top_provider": {"max_completion_tokens": 943718},
    }
]


def _adapter(monkeypatch, lines, rc=0, seen=None):
    monkeypatch.setenv("WOF_TEST_KEY", "sk-test")

    def popen(cmd, **kw):
        if seen is not None:
            agent_dir = pathlib.Path(kw["env"]["PI_CODING_AGENT_DIR"])
            seen["models"] = json.loads((agent_dir / "models.json").read_text())
            seen["cmd"] = cmd
        return FakePi(lines, rc)

    adapter = pi.PiAdapter(
        "1.5.2",
        charmlint="0.3.0",
        fetch=lambda gen_id, key: {"provider_name": "Wafer", "model": "m", "total_cost": 0.001},
        models=lambda: MODELS,
        popen=popen,
    )
    monkeypatch.setattr(adapter, "version", lambda: "1.5.2")
    monkeypatch.setattr(pi, "_stop", lambda proc: None)
    monkeypatch.setattr(pi, "context_files_above", lambda ws: [])
    return adapter


def test_run_replays_session(tmp_path, monkeypatch, config):
    ws = _ws(tmp_path)
    adapter = _adapter(monkeypatch, ONELINE.read_bytes().splitlines(keepends=True))
    result = adapter.run(ws, "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "completed" and result.error is None
    assert result.turns == 2 and result.provider == "Wafer"
    assert result.cost_usd == pytest.approx(0.000289746, abs=1e-6)
    assert result.details["billed_cost_usd"] == pytest.approx(0.002)
    assert result.details["skills"] == [".claude/skills/add-ingress"]
    assert (ws / ".wof" / pi.EVENTS_FILE).read_bytes() == ONELINE.read_bytes()
    out = process_outcome(result, config.treatments["none"], Caps())
    assert out.tool_calls == {"bash": 1} and out.exit_reason == "completed"
    assert out.metrics["tokens_input"] == 361
    assert out.details["adapter"]["providers"] == {"Wafer": 2}


def test_run_turn_cap(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, ONELINE.read_bytes().splitlines(keepends=True))
    result = adapter.run(
        _ws(tmp_path), "p", config.models["glm-5-3"], Caps(max_turns=1), "WOF_TEST_KEY"
    )
    assert result.exit_reason == "turn_cap"


def test_run_cost_cap(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, ONELINE.read_bytes().splitlines(keepends=True))
    caps = Caps(max_cost_usd=0.0001)
    result = adapter.run(_ws(tmp_path), "p", config.models["glm-5-3"], caps, "WOF_TEST_KEY")
    assert result.exit_reason == "cost_cap"


def test_run_nonzero_exit_is_an_error(tmp_path, monkeypatch, config):
    lines = ONELINE.read_bytes().splitlines(keepends=True)[:-1]  # never settles
    adapter = _adapter(monkeypatch, lines, rc=1)
    result = adapter.run(_ws(tmp_path), "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    assert result.exit_reason == "error" and "pi exited 1" in result.error


def test_run_refuses_wrong_version_and_missing_key(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, [])
    monkeypatch.setattr(adapter, "version", lambda: "1.6.0")
    with pytest.raises(pi.PiAdapterError, match=r"the pin is 1\.5\.2"):
        adapter.run(_ws(tmp_path), "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")
    monkeypatch.delenv("WOF_TEST_KEY")
    with pytest.raises(pi.PiAdapterError, match="not set"):
        adapter.run(tmp_path / "ws", "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY")


def test_get_adapter(config):
    adapter = get_adapter(config, "pi")
    assert isinstance(adapter, pi.PiAdapter)
    assert adapter.pinned_version == config.harnesses["pi"].version != "unpinned"
    assert adapter.charmlint == config.scoring.static.charmlint
    assert adapter.context_file() == config.tasks["hedgedoc-ingress"].context_file


def test_fixture_is_real_pi_output():
    header = json.loads(ONELINE.read_text().split("\n", 1)[0])
    assert header["type"] == "session" and header["version"] == 3


def test_run_cell_refuses_unpinned_harness(tmp_path, config):
    from charmer_wof import ladder, planner

    harness = config.harnesses["opencode"]
    config.harnesses["opencode"] = harness.model_copy(update={"version": "unpinned"})
    planned = planner.resolve(config, "hedgedoc-ingress/opencode/glm-5-3/none/0")
    with pytest.raises(ladder.RunError, match="unpinned"):
        ladder.run_cell(config, planned, tmp_path / "r.jsonl", gold=tmp_path, dest=tmp_path / "ws")
    assert not (tmp_path / "ws").exists()


def test_cli_real_run_needs_gold(tmp_path, capsys):
    from charmer_wof import cli

    assert cli.main(["run", "hedgedoc-ingress/pi/glm-5-3/none/0"]) == 2
    assert "--gold" in capsys.readouterr().err


def test_hide_wrapper_masks_paths(tmp_path):
    secret = tmp_path / "gold"
    secret.mkdir()
    (secret / "charm.py").write_text("the answer")
    assert pi.hide_wrapper([tmp_path / "absent"]) == []
    wrapper = pi.hide_wrapper([secret])
    assert wrapper[0] == "unshare" and wrapper[-1] == "--"
    out = subprocess.run([*wrapper, "ls", "-A", str(secret)], capture_output=True, text=True)
    if out.returncode == pi.HIDE_FAILED or "unshare" in out.stderr:
        pytest.skip(f"no mount namespaces here: {out.stderr.strip()}")
    assert out.returncode == 0 and out.stdout == ""
    assert (secret / "charm.py").read_text() == "the answer"  # only hidden inside


def test_run_refuses_workspace_inside_hidden_path(tmp_path, monkeypatch, config):
    adapter = _adapter(monkeypatch, [])
    with pytest.raises(pi.PiAdapterError, match="inside a hidden path"):
        adapter.run(
            _ws(tmp_path), "p", config.models["glm-5-3"], Caps(), "WOF_TEST_KEY", hide=[tmp_path]
        )


def test_hidden_paths_cover_gold_and_task_repo(tmp_path, config):
    from charmer_wof import ladder

    task = config.tasks["hedgedoc-ingress"]
    paths = ladder.hidden_paths(tmp_path, task, config.treatments["none"])
    assert paths[0] == tmp_path.resolve()
    assert any(task.root.resolve() == p or p in task.root.resolve().parents for p in paths[1:])


# --- reasoning effort (PLAN §8, 2026-11-14) --------------------------------------


def test_unset_effort_marks_the_model_not_reasoning(tmp_path, monkeypatch, config):
    """Unset: no --thinking, and reasoning: false so pi sends no reasoning field."""
    model = config.models["glm-5-3"]
    assert model.reasoning_effort is None
    seen: dict = {}
    result = _adapter(monkeypatch, ONELINE.read_bytes().splitlines(keepends=True), seen=seen).run(
        _ws(tmp_path), "p", model, Caps(), "WOF_TEST_KEY"
    )
    assert "--thinking" not in seen["cmd"]
    (entry,) = seen["models"]["providers"]["openrouter"]["models"]
    assert entry == {
        "id": "z-ai/glm-5.3-20260816",
        "contextWindow": 1048576,
        "maxTokens": 943718,
        "reasoning": False,
    }
    assert result.details["reasoning_effort"] is None
    assert result.details["thinking_level"] is None


@pytest.mark.parametrize(("effort", "level"), [("high", "high"), ("none", "off")])
def test_set_effort_is_sent(tmp_path, monkeypatch, config, effort, level):
    model = config.models["glm-5-3"].model_copy(update={"reasoning_effort": effort})
    seen: dict = {}
    result = _adapter(monkeypatch, ONELINE.read_bytes().splitlines(keepends=True), seen=seen).run(
        _ws(tmp_path), "p", model, Caps(), "WOF_TEST_KEY"
    )
    assert seen["cmd"][seen["cmd"].index("--thinking") + 1] == level
    (entry,) = seen["models"]["providers"]["openrouter"]["models"]
    assert entry["reasoning"] is True
    assert entry["thinkingLevelMap"] == {level: effort}
    assert entry["compat"] == {"thinkingFormat": "openrouter"}
    assert result.details["reasoning_effort"] == effort
