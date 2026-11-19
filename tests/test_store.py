from __future__ import annotations

import pytest
from conftest import held_out

from charmer_wof import store
from charmer_wof.schema import RecordError, RunRecord, ScoringRef, series_key


def other_series(record: RunRecord, run_id: str) -> RunRecord:
    scoring = ScoringRef(
        rubric_version="2",
        judge_models=record.scoring.judge_models,
        scoring_code_version=record.scoring.scoring_code_version,
    )
    return RunRecord.model_validate(
        {
            **record.model_dump(),
            "run_id": run_id,
            "scoring": scoring.model_dump(),
            "series": series_key(scoring),
        }
    )


def test_append_and_read(tmp_path, record):
    path = tmp_path / "r.jsonl"
    store.append(path, record)
    store.append(path, record.model_copy(update={"run_id": "r2"}))
    got = store.read(path)
    assert [r.run_id for r in got] == ["r1", "r2"]
    assert got[0] == record
    assert len(path.read_text().splitlines()) == 2


def test_append_is_append_only(tmp_path, record):
    path = tmp_path / "r.jsonl"
    store.append(path, record)
    first = path.read_text()
    store.append(path, record.model_copy(update={"run_id": "r2"}))
    assert path.read_text().startswith(first)


def test_missing_store_reads_empty(tmp_path):
    assert store.read(tmp_path / "nope.jsonl") == []


def test_append_rejects_unknown_tags_and_writes_nothing(tmp_path, config, record):
    path = tmp_path / "r.jsonl"
    record.tiers.live.failure_tags.append("vibes-off")
    allowed = {t: config.tags_for(t) for t in ("static", "oracle", "live", "judge", "process")}
    with pytest.raises(RecordError):
        store.append(path, record, allowed_tags=allowed)
    assert not path.exists()


def test_reader_refuses_to_mix_series(tmp_path, record):
    path = tmp_path / "r.jsonl"
    store.append(path, record)
    store.append(path, other_series(record, "r2"))
    with pytest.raises(store.MixedSeriesError) as e:
        store.read(path)
    assert len(e.value.counts) == 2
    assert [r.run_id for r in store.read(path, series=record.series)] == ["r1"]
    groups = store.group_by_series(path)
    assert sorted(len(v) for v in groups.values()) == [1, 1]


def test_invalid_line_is_reported_with_its_number(tmp_path, record):
    path = tmp_path / "r.jsonl"
    store.append(path, record)
    with path.open("a") as f:
        f.write('{"schema_version": 1}\n')
    with pytest.raises(RecordError, match=r"r\.jsonl:2"):
        store.read(path)


def test_public_store_gets_public_form(tmp_path, record):
    path = tmp_path / "public.jsonl"
    store.append(path, held_out(record), public=True)
    (got,) = store.read(path)
    assert got.redacted and got.artefacts.agent_diff is None
    assert "diff --git" not in path.read_text()
