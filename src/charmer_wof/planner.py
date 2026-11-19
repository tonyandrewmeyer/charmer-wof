"""Expand the configs into the cell list (task x harness x model x treatment x rep).

Phase 0 expands the full matrix. Sampling against the budget (PLAN §6) and
event-triggered selection belong to phase 3; they will filter this list, so
the cell id and the anchor flag are already the ones they'll use.
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterable

from .config import Config
from .schema import Cell


@dataclasses.dataclass(frozen=True)
class PlannedCell:
    cell: Cell
    anchor: bool
    held_out: bool

    @property
    def id(self) -> str:
        return self.cell.id


def _pick(available: Iterable[str], wanted: Iterable[str] | None, what: str) -> list[str]:
    available = list(available)
    if not wanted:
        return available
    wanted = list(wanted)
    unknown = [w for w in wanted if w not in available]
    if unknown:
        raise ValueError(f"unknown {what}: {', '.join(unknown)}")
    return wanted


def is_anchor(config: Config, cell: Cell) -> bool:
    return any(
        (a.task, a.harness, a.model, a.treatment)
        == (cell.task, cell.harness, cell.model, cell.treatment)
        for a in config.plan.anchors
    )


def expand(
    config: Config,
    *,
    tasks: Iterable[str] | None = None,
    harnesses: Iterable[str] | None = None,
    models: Iterable[str] | None = None,
    treatments: Iterable[str] | None = None,
    reps: int | None = None,
    anchors_only: bool = False,
) -> list[PlannedCell]:
    """Every cell, in a stable order; treatments are skipped where they don't apply."""
    reps = config.plan.reps if reps is None else reps
    out = []
    for task_id, harness, model, treatment_id in itertools.product(
        _pick(config.tasks, tasks, "task"),
        _pick(config.harnesses, harnesses, "harness"),
        _pick(config.models, models, "model"),
        _pick(config.treatments, treatments, "treatment"),
    ):
        if not config.treatments[treatment_id].applies(task_id):
            continue
        for rep in range(reps):
            cell = Cell(
                task=task_id, harness=harness, model=model, treatment=treatment_id, rep=rep
            )
            anchor = is_anchor(config, cell)
            if anchors_only and not anchor:
                continue
            out.append(PlannedCell(cell, anchor, config.tasks[task_id].held_out))
    return out


def resolve(config: Config, cell_id: str) -> PlannedCell:
    """A single cell by id, checked against the config."""
    cell = Cell.parse(cell_id)
    for kind, key, table in (
        ("task", cell.task, config.tasks),
        ("harness", cell.harness, config.harnesses),
        ("model", cell.model, config.models),
        ("treatment", cell.treatment, config.treatments),
    ):
        if key not in table:
            raise ValueError(f"unknown {kind} {key!r} in cell {cell_id!r}")
    if not config.treatments[cell.treatment].applies(cell.task):
        raise ValueError(f"treatment {cell.treatment!r} does not apply to task {cell.task!r}")
    return PlannedCell(cell, is_anchor(config, cell), config.tasks[cell.task].held_out)
