"""Phase 1 follow-ups (PLAN §8, 2026-11-14): the three new failure tags and the
reasoning-effort field in the record."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
from xml.sax.saxutils import quoteattr

import pytest
from test_static import CHECKS, FakeRunner, charm, no_charmcraft

from charmer_wof import store, workspace
from charmer_wof.adapters.base import AdapterResult
from charmer_wof.config import Caps, OracleSpec
from charmer_wof.planner import resolve
from charmer_wof.record import build_record, stub_tiers
from charmer_wof.scoring import static
from charmer_wof.scoring.oracle import outcome_from_junit
from charmer_wof.scoring.process import process_outcome

RESULTS = pathlib.Path(__file__).parents[1] / "results" / "results.jsonl"
KEY = "test_oracle_requirer_asks_traefik_to_strip_the_prefix"
LIB = "lib/charms/traefik_k8s/v2/ingress.py"


def junit(tests: dict[str, str]) -> str:
    """JUnit XML for {classname::name: status}, as pytest writes it."""
    cases = []
    for key, status in tests.items():
        classname, name = key.split("::")
        inner = {"fail": "<failure message='x'/>", "skip": "<skipped/>"}.get(status, "")
        cases.append(
            f"<testcase classname={quoteattr(classname)} name={quoteattr(name)}>{inner}</testcase>"
        )
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


def oracle_tests(key_status: str = "pass", others: int = 13) -> dict[str, str]:
    tests = {f"tests.unit.test__oracle_ingress::test_oracle_{i}": "pass" for i in range(others)}
    tests[f"tests.unit.test__oracle_ingress::{KEY}"] = key_status
    return tests


# --- key-oracle-test-failed --------------------------------------------------------


def test_the_task_names_strip_prefix_as_its_key_test(config):
    spec = config.tasks["hedgedoc-ingress"].oracle
    assert spec.key_test == KEY
    assert KEY in spec.key_tests
    oracle_file = config.tasks["hedgedoc-ingress"].path(spec.files[0]).read_text()
    assert f"def {KEY}(" in oracle_file


def test_key_test_must_be_reported(config):
    spec = config.tasks["hedgedoc-ingress"].oracle
    with pytest.raises(ValueError, match="key_tests"):
        OracleSpec(**{**spec.model_dump(), "key_test": "test_oracle_something_else"})


@pytest.mark.parametrize(
    ("key_status", "others", "tagged"),
    [
        ("pass", 13, False),
        ("fail", 13, True),  # 13/14, the key one failing
        ("fail", 0, True),
        ("skip", 13, True),
        ("pass", 0, False),  # only the key test: passes, so no tag
    ],
)
def test_key_oracle_test_failed(config, key_status, others, tagged):
    spec = config.tasks["hedgedoc-ingress"].oracle
    out = outcome_from_junit(junit(oracle_tests(key_status, others)), spec)
    assert ("key-oracle-test-failed" in out.failure_tags) is tagged


def test_key_test_missing_from_the_oracle_run_is_tagged(config):
    spec = config.tasks["hedgedoc-ingress"].oracle
    tests = oracle_tests()
    del tests[f"tests.unit.test__oracle_ingress::{KEY}"]
    out = outcome_from_junit(junit(tests), spec)
    assert out.key_tests[KEY] == "missing"
    assert out.failure_tags == ["key-oracle-test-failed"]


def test_no_key_tag_without_a_key_test(config):
    spec = config.tasks["hedgedoc-ingress"].oracle.model_copy(update={"key_test": None})
    out = outcome_from_junit(junit(oracle_tests("fail")), spec)
    assert out.failure_tags == []


# --- no-edits and only-vendored-libs-changed ---------------------------------------


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src/charm.py").write_text("x = 1\n")
    (ws / "uv.lock").write_text("v1\n")
    _git(["init", "-q"], ws)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], ws)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], ws)
    base = _git(["rev-parse", "HEAD"], ws).stdout.strip()
    return ws, base


def _snapshot(ws):
    _git(["add", "-A"], ws)
    _git(
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "r"],
        ws,
    )


def test_changed_paths_keeps_vendored_libs_and_drops_churn(repo):
    ws, base = repo
    (ws / LIB).parent.mkdir(parents=True)
    (ws / LIB).write_text("lib\n")
    (ws / "uv.lock").write_text("v2\n")
    (ws / ".wof").mkdir()
    (ws / ".wof/pi-events.jsonl").write_text("{}\n")
    _snapshot(ws)
    assert workspace.changed_paths(ws, base) == [LIB]
    assert workspace.only_vendored([LIB])
    diff, stats = workspace.agent_diff(ws, base)
    assert diff == "" and stats["files_changed"] == 0  # what the record's diff shows


def test_changed_paths_empty_when_nothing_changed(repo):
    ws, base = repo
    (ws / "uv.lock").write_text("v2\n")  # `uv run` rewriting the lockfile isn't an edit
    _snapshot(ws)
    assert workspace.changed_paths(ws, base) == []
    assert not workspace.only_vendored([])


def test_changed_paths_includes_the_charm(repo):
    ws, base = repo
    (ws / "src/charm.py").write_text("x = 2\n")
    (ws / LIB).parent.mkdir(parents=True)
    (ws / LIB).write_text("lib\n")
    _snapshot(ws)
    assert workspace.changed_paths(ws, base) == [LIB, "src/charm.py"]
    assert not workspace.only_vendored([LIB, "src/charm.py"])


def _result(**kw) -> AdapterResult:
    return AdapterResult(
        exit_reason=kw.pop("exit_reason", "completed"),
        cost_usd=0.1,
        turns=3,
        wall_seconds=1.0,
        provider="X",
        events=[],
    )


@pytest.mark.parametrize(
    ("changed", "tags"),
    [([], ["no-edits"]), ([LIB], []), (["src/charm.py"], []), (None, [])],
)
def test_no_edits(config, changed, tags):
    out = process_outcome(_result(), config.treatments["none"], Caps(), changed)
    assert out.failure_tags == tags


def test_no_edits_alongside_a_cap(config):
    result = _result(exit_reason="turn_cap")
    result.exit_reason = "turn_cap"
    out = process_outcome(result, config.treatments["none"], Caps(), [])
    assert out.failure_tags == ["turn-cap", "no-edits"]
    assert out.details["files_changed_incl_vendored"] == 0


@pytest.mark.parametrize(
    ("changed", "tagged"),
    [
        ([LIB], True),
        ([LIB, "lib/charms/other/v0/x.py"], True),
        ([LIB, "src/charm.py"], False),
        ([], False),
        (None, False),
    ],
)
def test_only_vendored_libs_changed(tmp_path, changed, tagged):
    out = static.run_static(
        charm(tmp_path),
        CHECKS,
        charmlint="0.3.0",
        runner=FakeRunner(),
        which=no_charmcraft,
        changed=changed,
    )
    assert ("only-vendored-libs-changed" in out.failure_tags) is tagged


def test_new_tags_are_in_the_vocabulary_for_their_tiers(config):
    assert "key-oracle-test-failed" in config.tags_for("oracle")
    assert "no-edits" in config.tags_for("process")
    assert "only-vendored-libs-changed" in config.tags_for("static")


# --- the phase 1 smoke records, re-scored offline ------------------------------------


def _smoke():
    """The three phase 1 smoke runs: GLM-5.3 baselines, scored before version 2."""
    return {
        r.cell.harness: r
        for r in store.iter_records(RESULTS)
        if r.cell.treatment == "none"
        and r.cell.model == "glm-5-3"
        and r.scoring.scoring_code_version.startswith("1+")
    }


def _changed_from_record(record) -> list[str]:
    """What the agent changed, rebuilt from a record made before changed_paths existed.

    The record's diff excludes ``lib/`` and the lockfile; the static tier's
    ``files_present`` check passing means the ingress lib (removed from the
    base) was put back, so it's a change too.
    """
    paths = re.findall(r"^diff --git a/(\S+) ", record.artefacts.agent_diff or "", re.M)
    if record.tiers.static.details["checks"]["files_present"]["status"] == "pass":
        paths.append(LIB)
    return sorted(set(paths))


def test_smoke_records_rescored(config):
    smoke = _smoke()
    assert set(smoke) == {"pi", "omp", "opencode"}
    spec = config.tasks["hedgedoc-ingress"].oracle
    got = {}
    for harness, record in smoke.items():
        oracle = outcome_from_junit(junit(record.tiers.oracle.tests), spec)
        assert (oracle.passed, oracle.total) == (
            record.tiers.oracle.passed,
            record.tiers.oracle.total,
        )
        changed = _changed_from_record(record)
        result = _result()
        result.exit_reason = record.tiers.process.exit_reason
        process = process_outcome(result, config.treatments["none"], Caps(), changed)
        got[harness] = (
            sorted(oracle.failure_tags),
            workspace.only_vendored(changed),
            [t for t in process.failure_tags if t == "no-edits"],
        )
    # pi and omp: 13/14, the one failure is the key test.
    assert got["pi"] == (["key-oracle-test-failed"], False, [])
    assert got["omp"] == (["key-oracle-test-failed"], False, [])
    # OpenCode: 0/14, and its only change was the ingress lib it curl'ed into
    # lib/, so it's only-vendored-libs-changed rather than no-edits.
    assert got["opencode"] == (["key-oracle-test-failed"], True, [])


# --- reasoning_effort in the record ---------------------------------------------------


def test_models_leave_reasoning_effort_unset(config):
    assert {m.reasoning_effort for m in config.models.values()} == {None}


def test_record_carries_reasoning_effort(config, tmp_path):
    planned = resolve(config, "hedgedoc-ingress/omp/glm-5-3/none/0")
    unset = build_record(config, planned, stub_tiers("x"), dry_run=True)
    assert unset.model.reasoning_effort is None
    models = {
        **config.models,
        "glm-5-3": config.models["glm-5-3"].model_copy(update={"reasoning_effort": "high"}),
    }
    pinned = config.model_copy(update={"models": models})
    record = build_record(pinned, planned, stub_tiers("x"), dry_run=True)
    assert record.model.reasoning_effort == "high"
    path = tmp_path / "r.jsonl"
    store.append(path, record)
    assert json.loads(path.read_text())["model"]["reasoning_effort"] == "high"
    assert store.read(path)[0].model.reasoning_effort == "high"
