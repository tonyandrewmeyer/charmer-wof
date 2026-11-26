from __future__ import annotations

import collections

import pytest

from charmer_wof import planner
from charmer_wof.config import PACKAGE_ROOT, load_config
from charmer_wof.schema import Cell


def test_full_expansion(config):
    cells = planner.expand(config)
    # (hedgedoc-ingress x 5 treatments + 4 feature tasks x {none, tests-target}
    # + 2 new-charm briefs x none) x 3 harnesses x 2 models x 3 reps
    assert len(cells) == (5 + 4 * 2 + 2) * 3 * 2 * 3
    assert len({c.id for c in cells}) == len(cells)
    assert cells == planner.expand(config)  # stable order


def test_anchor_cells_flagged(config):
    anchors = planner.expand(config, anchors_only=True)
    assert len(anchors) == 2 * config.plan.reps
    assert all(c.anchor for c in anchors)
    assert {c.cell.treatment for c in anchors} == {"none", "howto"}


def test_filters_and_reps(config):
    cells = planner.expand(
        config, tasks=["hedgedoc-ingress"], harnesses=["pi"], models=["glm-5-3"], reps=1
    )
    assert [c.id for c in cells] == [
        f"hedgedoc-ingress/pi/glm-5-3/{t}/0"
        for t in ("none", "howto", "skill", "skill-scripts", "tests-target")
    ]
    with pytest.raises(ValueError, match="unknown model"):
        planner.expand(config, models=["gpt-9"])


def test_kinds(config):
    feature = planner.expand(config, kinds=["feature"], reps=1)
    briefs = planner.expand(config, kinds=["new-charm"], reps=1)
    assert {config.tasks[c.cell.task].kind for c in feature} == {"feature"}
    assert {c.cell.task for c in briefs} == {"new-gotify-k8s", "new-pushgateway-k8s"}
    assert len(feature) + len(briefs) == len(planner.expand(config, reps=1))


def test_treatments_only_where_they_apply(private_tree):
    config = load_config([PACKAGE_ROOT, private_tree])
    cells = planner.expand(config, tasks=["secret-new-charm"], reps=1)
    assert {c.cell.treatment for c in cells} == {"none"}
    assert all(c.held_out for c in cells)


def test_cell_id_round_trip():
    cell = Cell(task="t", harness="pi", model="m", treatment="none", rep=2)
    assert Cell.parse(cell.id) == cell
    with pytest.raises(ValueError):
        Cell.parse("t/pi/m/none")


def test_resolve(config):
    planned = planner.resolve(config, "hedgedoc-ingress/pi/deepseek-v4-pro/none/0")
    assert planned.anchor and not planned.held_out
    with pytest.raises(ValueError, match="unknown harness"):
        planner.resolve(config, "hedgedoc-ingress/claude/deepseek-v4-pro/none/0")


def test_resolve_rejects_inapplicable_treatment(private_tree):
    config = load_config([PACKAGE_ROOT, private_tree])
    with pytest.raises(ValueError, match="does not apply"):
        planner.resolve(config, "secret-new-charm/pi/glm-5-3/howto/0")


# --- the scheduled sample (2026-11-26): task first, then the least-run cell ----


def _feature(config):
    return planner.expand(config, kinds=["feature"], reps=1)


def test_sample_is_reproducible(config):
    cells = _feature(config)
    counts = {("hedgedoc-s3", "pi", "glm-5-3", "none"): 2}
    a = planner.sample(cells, 8, "2026-11-30", counts)
    assert a == planner.sample(cells, 8, "2026-11-30", dict(counts))
    assert a != planner.sample(cells, 8, "2026-12-07", counts)


def test_every_task_once_before_any_twice(config):
    cells = _feature(config)
    tasks = {c.cell.task for c in cells}
    for seed in ("2026-11-30", "2026-12-07", "a", "b", "c"):
        picked = planner.sample(cells, 8, seed)
        per_task = collections.Counter(c.cell.task for c in picked)
        assert set(per_task) == tasks  # 5 tasks, 8 cells: all five, then three again
        assert max(per_task.values()) == 2
        assert len({c.id for c in picked}) == 8
        picked = planner.sample(cells, 3, seed)
        assert len({c.cell.task for c in picked}) == 3


def test_the_least_run_cell_of_a_task_is_preferred(config):
    cells = [c for c in _feature(config) if c.cell.task == "hedgedoc-s3"]
    keys = [planner.cell_key(c.cell) for c in cells]
    counts = dict.fromkeys(keys, 3)
    counts[keys[5]] = 1
    (picked,) = planner.sample(cells, 1, "any", counts)
    assert planner.cell_key(picked.cell) == keys[5]
    # Its rep is how many records it already has, so repeated runs get distinct ids.
    assert picked.cell.rep == 1 and picked.id.endswith("/1")
    # The next pick in the same run is a tie among the others, never the same cell.
    two = planner.sample(cells, 2, "any", counts)
    assert len({planner.cell_key(c.cell) for c in two}) == 2
    assert {c.cell.rep for c in two} == {1, 3}


def test_sample_with_more_wanted_than_there_are_cells(config):
    cells = planner.expand(config, kinds=["new-charm"], reps=1)
    assert sorted(c.id for c in planner.sample(cells, 100, "x")) == sorted(c.id for c in cells)


def test_record_counts_are_per_cell_in_the_current_series(config, record):
    other = record.model_copy(update={"series": "s-000000000000"})
    dry = record.model_copy(update={"dry_run": True})
    rep2 = record.model_copy(update={"cell": record.cell.model_copy(update={"rep": 2})})
    counts = planner.record_counts([record, rep2, other, dry], record.series)
    assert counts == {planner.cell_key(record.cell): 2}
