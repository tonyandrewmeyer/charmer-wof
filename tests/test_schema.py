from __future__ import annotations

import json

import jsonschema
import pytest
from conftest import held_out
from pydantic import ValidationError

from charmer_wof import cli
from charmer_wof.record import scoring_ref
from charmer_wof.schema import (
    SCHEMA_VERSION,
    RecordError,
    RunRecord,
    ScoringRef,
    TierOutcome,
    check_failure_tags,
    json_schema,
    public_form,
    series_key,
)


def allowed(config):
    return {t: config.tags_for(t) for t in ("static", "oracle", "live", "judge", "process")}


def test_record_is_valid_and_round_trips(record):
    again = RunRecord.model_validate_json(record.model_dump_json())
    assert again == record
    assert record.schema_version == SCHEMA_VERSION


def test_record_carries_report_needs(record):
    # PLAN §8 2026-11-09: what the 3- and 6-month reports need, from the first run.
    assert record.task.version == "2"
    assert record.treatment.artefacts["charmlint"] == "0.3.0"
    assert record.harness.version == "1.2.3"
    assert record.model.openrouter_id == "deepseek/deepseek-v4-pro-0813"
    assert record.model.tier == "open-weight"
    assert record.model.provider == "DeepInfra"
    assert record.scoring.judge_models == ["anthropic/claude-sonnet-5.5"]
    assert record.anchor is True
    assert record.held_out is False
    assert {name for name, _ in record.tiers.items()} == {
        "static",
        "oracle",
        "live",
        "judge",
        "process",
    }


def test_exported_json_schema_validates_records(record):
    jsonschema.validate(json.loads(record.model_dump_json()), json_schema())


def test_committed_json_schema_is_current():
    assert cli.main(["schema", "--check"]) == 0


def test_not_run_needs_a_reason():
    with pytest.raises(ValidationError, match="needs a reason"):
        TierOutcome(status="not_run")
    assert TierOutcome.not_run("no juju").reason == "no juju"


def test_series_must_match_scoring(record):
    data = record.model_dump()
    data["series"] = "s-000000000000"
    with pytest.raises(ValidationError, match="does not match"):
        RunRecord.model_validate(data)


def test_cell_must_match_refs(record):
    data = record.model_dump()
    data["cell"]["model"] = "glm-5-3"
    with pytest.raises(ValidationError, match="disagree"):
        RunRecord.model_validate(data)


@pytest.mark.parametrize(
    "change",
    [
        {"rubric_version": "2"},
        {"judge_models": ["anthropic/claude-sonnet-5.5", "z-ai/glm-5.3"]},
        {"scoring_code_version": "99+charmlint-0.3.0"},
    ],
)
def test_series_changes_with_each_versioned_part(config, change):
    base = scoring_ref(config)
    assert series_key(base.model_copy(update=change)) != series_key(base)


def test_series_ignores_judge_order():
    a = ScoringRef(rubric_version="1", judge_models=["a", "b"], scoring_code_version="1")
    b = ScoringRef(rubric_version="1", judge_models=["b", "a"], scoring_code_version="1")
    assert series_key(a) == series_key(b)


def test_series_includes_static_tool_pins(config):
    ref = scoring_ref(config)
    assert ref.scoring_code_version.endswith("+charmlint-0.3.0")


def test_known_failure_tags_pass(config, record):
    check_failure_tags(record, allowed(config))
    assert ("judge", "prefix-not-stripped") in record.all_failure_tags()


def test_unknown_failure_tag_rejected(config, record):
    record.tiers.live.failure_tags.append("vibes-off")
    with pytest.raises(RecordError, match="live:vibes-off"):
        check_failure_tags(record, allowed(config))


def test_tag_on_wrong_tier_rejected(config, record):
    record.tiers.live.failure_tags.append("tests-shallow")  # a judge-only tag
    with pytest.raises(RecordError, match="live:tests-shallow"):
        check_failure_tags(record, allowed(config))


# --- the public form of held-out records ---------------------------------------


def test_public_form_leaves_ordinary_records_alone(record):
    assert public_form(record) is record


def test_public_form_drops_held_out_content(record):
    pub = public_form(held_out(record))
    assert pub.redacted
    assert pub.artefacts.agent_diff is None
    assert pub.artefacts.prompt is None
    assert pub.tiers.oracle.tests == {}
    assert pub.tiers.oracle.key_tests == {}
    assert pub.tiers.oracle.details == {}
    assert pub.tiers.judge.verdicts[0].notes is None
    assert pub.task.gold_repo is None and pub.task.gold_sha is None
    assert "secret oracle output" not in pub.model_dump_json()
    assert "strip_prefix" not in pub.model_dump_json()


def test_public_form_keeps_scores_tags_and_metadata(record):
    src = held_out(record)
    pub = public_form(src)
    assert pub.tiers.oracle.passed == 13 and pub.tiers.oracle.total == 14
    assert pub.tiers.judge.verdicts[0].total == 14
    assert pub.all_failure_tags() == src.all_failure_tags()
    assert pub.artefacts.diff_stats == src.artefacts.diff_stats
    for field in (
        "run_id",
        "series",
        "cell",
        "treatment",
        "harness",
        "model",
        "scoring",
        "anchor",
    ):
        assert getattr(pub, field) == getattr(src, field)
    assert pub.task.id == src.task.id and pub.task.version == src.task.version


def test_public_form_is_valid_and_idempotent(record):
    pub = public_form(held_out(record))
    RunRecord.model_validate_json(pub.model_dump_json())
    assert public_form(pub) == pub


def test_redacted_only_for_held_out(record):
    with pytest.raises(ValidationError, match="only held-out"):
        RunRecord.model_validate({**record.model_dump(), "redacted": True})


# --- schema version 2: task.gold_subdir (2026-11-18) -------------------------


def test_record_carries_gold_subdir(config):
    from charmer_wof.planner import resolve
    from charmer_wof.record import build_record, stub_tiers

    planned = resolve(config, "haproxy-client-ip-hash-salt/pi/glm-5-3/none/0")
    rec = build_record(config, planned, stub_tiers("x"))
    assert rec.schema_version == SCHEMA_VERSION == 2
    assert rec.task.gold_subdir == "haproxy-operator"
    assert rec.task.gold_sha == config.tasks["haproxy-client-ip-hash-salt"].gold_sha


def test_gold_subdir_is_none_at_the_repo_root(record):
    assert record.task.gold_subdir is None
    assert "gold_subdir" in json.loads(record.model_dump_json())["task"]


def test_version_1_records_still_read(record):
    data = json.loads(record.model_dump_json())
    data["schema_version"] = 1
    del data["task"]["gold_subdir"]
    old = RunRecord.model_validate(data)
    assert old.schema_version == 1 and old.task.gold_subdir is None
    jsonschema.validate(data, json_schema())


def test_version_1_records_cannot_carry_gold_subdir(record):
    data = record.model_dump()
    data["schema_version"] = 1
    data["task"]["gold_subdir"] = "charm"
    with pytest.raises(ValidationError, match="gold_subdir"):
        RunRecord.model_validate(data)


def test_stored_records_are_version_1_and_still_valid():
    from charmer_wof.config import PACKAGE_ROOT

    lines = (PACKAGE_ROOT / "results" / "results.jsonl").read_text().splitlines()
    records = [RunRecord.model_validate_json(x) for x in lines if x.strip()]
    assert records and all(r.schema_version == 1 for r in records)
    assert all(r.task.gold_subdir is None for r in records)


def test_public_form_drops_gold_subdir(record):
    data = held_out(record).model_dump()
    data["task"]["gold_subdir"] = "charm"
    pub = public_form(RunRecord.model_validate(data))
    assert pub.task.gold_subdir is None and pub.task.gold_repo is None
