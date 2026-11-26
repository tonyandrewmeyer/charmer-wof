"""Expand the configs into the cell list (task x harness x model x treatment x rep),
and sample it for the scheduled runs.

``expand`` gives the full matrix, filtered. ``sample`` is the scheduled runs'
pick (PLAN §6, since 2026-11-26): a task first, then that task's cell with the
fewest records in the current series, seeded by the run's date. The weekly run
samples the feature tasks and a monthly slot the new-charm briefs
(``--kind``). Event-triggered selection (a charmlint release, a new model) and
weighting by spend are still to come.
"""

from __future__ import annotations

import collections
import dataclasses
import itertools
import random
from collections.abc import Iterable, Mapping

from .config import Config, TaskKind
from .schema import Cell

TASK_KINDS = TaskKind.__args__


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
    kinds: Iterable[str] | None = None,
) -> list[PlannedCell]:
    """Every cell, in a stable order; treatments are skipped where they don't apply.

    ``kinds`` keeps only tasks of those kinds (``feature``, ``new-charm``): the
    weekly run samples feature tasks, the monthly new-charm slot the briefs.
    """
    reps = config.plan.reps if reps is None else reps
    kinds = list(kinds) if kinds else None
    if kinds:
        unknown = [k for k in kinds if k not in TASK_KINDS]
        if unknown:
            raise ValueError(f"unknown task kind: {', '.join(unknown)}")
    picked_tasks = [
        t
        for t in _pick(config.tasks, tasks, "task")
        if kinds is None or config.tasks[t].kind in kinds
    ]
    out = []
    for task_id, harness, model, treatment_id in itertools.product(
        picked_tasks,
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


class CapExceeded(ValueError):
    pass


CellKey = tuple[str, str, str, str]
"""``(task, harness, model, treatment)``: a cell without its rep."""


def cell_key(cell: Cell) -> CellKey:
    return (cell.task, cell.harness, cell.model, cell.treatment)


def record_counts(records: Iterable, series: str) -> collections.Counter[CellKey]:
    """Stored records per cell (any rep) in ``series``; dry runs don't count.

    ``records`` are ``RunRecord``s, e.g. ``store.iter_records`` of the results
    branch's ``results.jsonl``. Only the current series counts: a new series
    (a scoring change) starts every cell at zero again (PLAN §1.1).
    """
    out: collections.Counter[CellKey] = collections.Counter()
    for r in records:
        if r.series == series and not r.dry_run:
            out[cell_key(r.cell)] += 1
    return out


def sample(
    cells: list[PlannedCell],
    n: int,
    seed: str,
    counts: Mapping[CellKey, int] | None = None,
) -> list[PlannedCell]:
    """Up to ``n`` of ``cells``: a task first, then that task's least-run cell.

    PLAN §6's rotation, in place of the seeded uniform pick over cells (which
    weighted tasks by how many treatments apply to them, so
    ``hedgedoc-ingress`` took a third of every run). Each draw:

    1. a task, uniformly at random, every task with an unpicked cell drawn once
       before any task is drawn again in this run (a shuffled round, then
       another);
    2. that task's cell with the fewest stored records in the current series
       (``counts``, from ``record_counts``), ties broken at random.

    Both draws come from one ``random.Random(seed)`` over sorted inputs, so the
    same seed (eval.yml: the run's date) and the same records give the same
    plan. With no records (a new series, or a first run) every cell ties and
    step 2 is a seeded pick within the task. A cell is picked at most once per
    run; its ``rep`` is its stored record count, so a cell's repeated runs get
    distinct ids (``.../0``, ``.../1``, ...). Anchors aren't favoured: they have
    their own monthly run (``--anchors-only``). Returned in plan order.
    """
    counts = counts or {}
    rng = random.Random(seed)
    by_task: dict[str, list[PlannedCell]] = {}
    for p in cells:
        by_task.setdefault(p.cell.task, []).append(p)
    taken: set[CellKey] = set()
    picked: list[PlannedCell] = []
    round_left: list[str] = []
    while len(picked) < n:
        open_tasks = sorted(
            t for t, ps in by_task.items() if any(cell_key(p.cell) not in taken for p in ps)
        )
        if not open_tasks:
            break
        round_left = [t for t in round_left if t in open_tasks]
        if not round_left:
            round_left = list(open_tasks)
            rng.shuffle(round_left)
        task = round_left.pop(0)
        options: dict[CellKey, PlannedCell] = {}
        for p in by_task[task]:
            key = cell_key(p.cell)
            if key not in taken:
                options.setdefault(key, p)
        fewest = min(counts.get(k, 0) for k in options)
        tied = sorted(k for k in options if counts.get(k, 0) == fewest)
        key = rng.choice(tied)
        taken.add(key)
        chosen = options[key]
        rep = counts.get(key, 0)
        picked.append(
            dataclasses.replace(chosen, cell=chosen.cell.model_copy(update={"rep": rep}))
        )
    order = {cell_key(c.cell): i for i, c in enumerate(cells)}
    return sorted(picked, key=lambda c: order[cell_key(c.cell)])


def matrix(config: Config, cells: list[PlannedCell], max_cells: int | None = None) -> dict:
    """The GHA matrix for ``cells``: ``{"include": [...]}``, refused over the hard cap."""
    cap = (
        config.plan.gha.max_cells
        if max_cells is None
        else min(max_cells, config.plan.gha.max_cells)
    )
    if len(cells) > cap:
        raise CapExceeded(
            f"{len(cells)} cells is over the per-workflow cap of {cap} "
            "(config/plan.toml [gha] max_cells); narrow the plan or sample it"
        )
    include = []
    for p in cells:
        task = config.tasks[p.cell.task]
        include.append(
            {
                "cell": p.id,
                "slug": p.id.replace("/", "__"),
                "task": p.cell.task,
                "harness": p.cell.harness,
                "harness_version": config.harnesses[p.cell.harness].version,
                "model": p.cell.model,
                "treatment": p.cell.treatment,
                "rep": p.cell.rep,
                "anchor": p.anchor,
                "kind": task.kind,
                "gold_private": task.gold_private,
                "fetch_libs": task.fetch_libs,
                "python": task.python or "",
            }
        )
    return {"include": include}
