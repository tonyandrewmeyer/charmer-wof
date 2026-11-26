from __future__ import annotations

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
