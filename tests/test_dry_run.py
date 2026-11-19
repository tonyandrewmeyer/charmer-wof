from __future__ import annotations

import json

from charmer_wof import cli, ladder, planner, store


def test_dry_run_writes_schema_valid_record(tmp_path, config):
    path = tmp_path / "results.jsonl"
    planned = planner.resolve(config, "hedgedoc-ingress/opencode/glm-5-3/tests-target/1")
    record = ladder.dry_run(config, planned, path)
    assert record.dry_run
    assert all(t.status == "not_run" and t.reason for _, t in record.tiers.items())
    assert record.tiers.process.exit_reason == "not_started"
    assert record.artefacts.prompt.endswith("without modifying them.")
    assert store.read(path) == [record]


def test_cli_plan(capsys):
    assert cli.main(["plan", "--dry-run", "--harness", "pi", "--model", "glm-5-3"]) == 0
    out = capsys.readouterr().out.splitlines()
    # (5 for hedgedoc-ingress + 2 for each of 4 feature tasks + 1 per brief) x 3 reps
    assert len(out) == (5 + 4 * 2 + 2) * 3
    assert cli.main(["plan"]) == 2


def test_cli_plan_json(capsys):
    assert cli.main(["plan", "--dry-run", "--anchors-only", "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows and all(r["anchor"] for r in rows)


def test_cli_run_dry_run(tmp_path, capsys):
    path = tmp_path / "r.jsonl"
    cell = "hedgedoc-ingress/pi/deepseek-v4-pro/none/0"
    assert cli.main(["run", "--dry-run", cell, "--results", str(path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["cell"] == cell and summary["anchor"] is True
    assert set(summary["tiers"].values()) == {"not_run"}
    assert cli.main(["run", cell, "--results", str(path)]) == 2  # no adapters yet
    assert len(store.read(path)) == 1
