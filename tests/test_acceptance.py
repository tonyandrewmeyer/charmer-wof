"""tests-target's one rule (2026-11-26): the visible acceptance tests are left alone."""

from __future__ import annotations

import pytest
from conftest import git

from charmer_wof.ladder import with_acceptance_check
from charmer_wof.schema import TierOutcome
from charmer_wof.scoring import acceptance

TASK = "haproxy-get-configuration"
DEST = "tests/unit/test_acceptance_get_configuration.py"

ORIGINAL = '''"""Tests."""

import json
import os

import pytest


def test_x():
    """Check x."""
    assert json.loads("1") == 1
'''


@pytest.mark.parametrize(
    ("now", "kind"),
    [
        (ORIGINAL, "untouched"),
        (ORIGINAL.replace("\n\n\ndef", "\n\ndef"), "reformatted"),  # the s3 run's blank line
        (ORIGINAL + "# a comment\n", "reformatted"),
        (ORIGINAL.replace("import os\n", ""), "reformatted"),  # ruff --fix: unused import
        (ORIGINAL.replace("== 1", "== 1 or True"), "edited"),
        (ORIGINAL.replace("import json\n", ""), "edited"),  # a used import
        (ORIGINAL.replace('"""Check x."""', '"""Check y."""'), "edited"),  # docstrings count
        (ORIGINAL + "def broken(:\n", "edited"),
        (None, "deleted"),
    ],
)
def test_classify(now, kind):
    assert acceptance.classify(ORIGINAL, now) == kind


def _given(config):
    return acceptance.generated(config.tasks[TASK], config.treatments["tests-target"])


def _workspace(tmp_path, config, edit=lambda text: text):
    for rel, text in _given(config).items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(edit(text))
    return tmp_path


def _check(config, ws, outcome=None):
    return with_acceptance_check(
        outcome or TierOutcome(status="pass"),
        config.tasks[TASK],
        config.treatments["tests-target"],
        workspace=ws,
    )


def test_untouched_tests_pass(tmp_path, config):
    out = _check(config, _workspace(tmp_path, config))
    assert out.status == "pass" and out.failure_tags == []
    assert out.details["checks"]["acceptance_tests"]["files"] == {DEST: "untouched"}
    assert out.metrics["acceptance_tests_ok"] is True


def test_an_edit_to_an_assertion_fails_the_tier(tmp_path, config):
    ws = _workspace(tmp_path, config, lambda t: t.replace('"source": "disk"', '"source": "x"'))
    out = _check(config, ws)
    assert out.status == "fail" and out.failure_tags == [acceptance.EDITED]
    assert out.metrics["acceptance_tests_ok"] is False


def test_a_reformat_is_recorded_and_does_not_fail(tmp_path, config):
    ws = _workspace(tmp_path, config, lambda t: t.replace("\n\n\n", "\n\n", 1))
    out = _check(config, ws, TierOutcome(status="pass", failure_tags=["gold-copied"]))
    assert out.status == "pass"
    assert out.failure_tags == ["gold-copied", acceptance.REFORMATTED]


def test_a_deleted_file_is_an_edit(tmp_path, config):
    out = _check(config, tmp_path)
    assert out.details["checks"]["acceptance_tests"]["files"] == {DEST: "deleted"}
    assert out.status == "fail"


def test_other_treatments_are_left_alone(config, tmp_path):
    before = TierOutcome(status="pass")
    for t in ("none", "howto", "skill", "skill-scripts"):
        after = with_acceptance_check(
            before, config.tasks["hedgedoc-ingress"], config.treatments[t], workspace=tmp_path
        )
        assert after is before


def test_a_replay_rebuilds_the_files_from_the_diff(tmp_path, config):
    given = _given(config)
    git(tmp_path, "init", "-q")
    _workspace(tmp_path, config)
    (tmp_path / "src.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / DEST).write_text(given[DEST].replace("\n\n\n", "\n\n", 1))
    (tmp_path / "src.py").write_text("x = 2\n")
    diff = git(tmp_path, "diff") + "\n"
    rebuilt = acceptance.from_diff(given, diff)
    assert rebuilt[DEST] == (tmp_path / DEST).read_text()
    recorded = config.treatments["tests-target"].artefact_versions(config.tasks[TASK])
    out = with_acceptance_check(
        TierOutcome(status="pass"),
        config.tasks[TASK],
        config.treatments["tests-target"],
        diff=diff,
        recorded=recorded,
    )
    assert out.failure_tags == [acceptance.REFORMATTED]
    # A record from another version of the treatment: nothing to compare with.
    out = with_acceptance_check(
        TierOutcome(status="pass"),
        config.tasks[TASK],
        config.treatments["tests-target"],
        diff=diff,
        recorded={"generated:tests/unit/other.py": "sha256:0"},
    )
    assert out.details["checks"]["acceptance_tests"]["status"] == "not_run"
    assert out.status == "pass" and out.metrics["acceptance_tests_ok"] is None


def test_tags_are_in_the_vocabulary(config):
    assert {acceptance.EDITED, acceptance.REFORMATTED} <= config.tags_for("static")
