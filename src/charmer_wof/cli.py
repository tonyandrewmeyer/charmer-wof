"""Command line: ``wof plan|run|prepare|static|oracle|schema|series|show|gold-check``,
and the GHA steps ``wof fetch-gold|agent|score|store``."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

from . import gold, ladder, planner, store, workspace
from .config import load_config
from .record import current_series
from .schema import SCHEMA_VERSION, RunRecord, json_schema
from .scoring import contamination, oracle, static

DEFAULT_RESULTS = pathlib.Path("scratch/results-dry-run.jsonl")
DEFAULT_REAL_RESULTS = pathlib.Path("results/results.jsonl")
SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "schema" / f"run-record.v{SCHEMA_VERSION}.json"
)
CONTAMINATION_TAGS = ("gold-copied", "gold-fetched")


def _config(args: argparse.Namespace):
    roots = [pathlib.Path(p) for p in args.config] if args.config else None
    return load_config(roots)


def _split(values: list[str] | None) -> list[str] | None:
    """Repeatable options that also take comma- or space-separated lists (workflow inputs)."""
    if not values:
        return None
    return [v for value in values for v in value.replace(",", " ").split()] or None


def _default_sample(config, kinds: list[str] | None) -> int:
    """``--sample`` alone: the monthly new-charm slot's size for ``--kind new-charm``,
    otherwise the weekly run's."""
    if kinds == ["new-charm"]:
        return config.plan.gha.new_charm_sample
    return config.plan.gha.scheduled_sample


def cmd_plan(args: argparse.Namespace) -> int:
    if not (args.dry_run or args.matrix_json):
        print("pass --dry-run to print the cell list, or --matrix-json for GHA", file=sys.stderr)
        return 2
    config = _config(args)
    try:
        if _split(args.cells):
            cells = [planner.resolve(config, c) for c in _split(args.cells)]
        else:
            cells = planner.expand(
                config,
                tasks=_split(args.task),
                harnesses=_split(args.harness),
                models=_split(args.model),
                treatments=_split(args.treatment),
                reps=args.reps,
                anchors_only=args.anchors_only,
                kinds=_split(args.kind),
            )
        if args.sample is not None:
            n = args.sample if args.sample >= 0 else _default_sample(config, _split(args.kind))
            counts = None
            if args.records is not None:
                if not args.records.is_file():
                    raise ValueError(f"--records {args.records} does not exist")
                counts = planner.record_counts(
                    store.iter_records(args.records), current_series(config)
                )
                print(
                    f"# {sum(counts.values())} records in series {current_series(config)} "
                    f"from {args.records}",
                    file=sys.stderr,
                )
            cells = planner.sample(cells, n, args.seed, counts)
    except (ValueError, OSError) as e:
        print(f"plan refused: {e}", file=sys.stderr)
        return 2
    if args.matrix_json:
        try:
            out = planner.matrix(config, cells, args.max_cells)
        except planner.CapExceeded as e:
            print(f"plan refused: {e}", file=sys.stderr)
            return 2
        print(json.dumps(out, separators=(",", ":")))
        print(f"# {len(cells)} cells, series {current_series(config)}", file=sys.stderr)
        return 0
    if args.json:
        for p in cells:
            print(json.dumps({"cell": p.id, "anchor": p.anchor, "held_out": p.held_out}))
        return 0
    for p in cells:
        flags = " ".join(f for f, on in (("anchor", p.anchor), ("held-out", p.held_out)) if on)
        print(f"{p.id}{'  [' + flags + ']' if flags else ''}")
    unpinned = sorted(h.name for h in config.harnesses.values() if not h.pinned)
    print(f"# {len(cells)} cells, series {current_series(config)}", file=sys.stderr)
    if unpinned:
        print(f"# unpinned harnesses (dry runs only): {', '.join(unpinned)}", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _config(args)
    overrides = {
        k: v
        for k, v in (
            ("max_turns", args.max_turns),
            ("timeout_minutes", args.timeout_minutes),
            ("max_cost_usd", args.max_cost),
        )
        if v is not None
    }
    if overrides:
        # Tighter caps for one run (a smoke run, a budget-limited rerun); the
        # record keeps the caps it actually ran under.
        plan = config.plan.model_copy(
            update={"caps": config.plan.caps.model_copy(update=overrides)}
        )
        config = config.model_copy(update={"plan": plan})
    planned = planner.resolve(config, args.cell)
    if args.dry_run:
        record = ladder.dry_run(config, planned, args.results or DEFAULT_RESULTS)
        results = args.results or DEFAULT_RESULTS
    else:
        if args.gold is None and config.tasks[planned.cell.task].kind == "feature":
            print("a real run needs --gold <checkout of the task's gold repo>", file=sys.stderr)
            return 2
        dest = args.workspace or pathlib.Path(tempfile.mkdtemp(prefix="wof-run-")) / "ws"
        results = args.results or DEFAULT_REAL_RESULTS
        try:
            record = ladder.run_cell(
                config,
                planned,
                results,
                gold=args.gold.resolve() if args.gold else None,
                dest=dest.resolve(),
            )
        except (ladder.RunError, RuntimeError) as e:
            print(f"run refused: {e}", file=sys.stderr)
            return 2
        print(f"# workspace kept at {dest}", file=sys.stderr)
        flagged = [t for _, t in record.all_failure_tags() if t in CONTAMINATION_TAGS]
        if flagged:
            print(
                f"# REVIEW: {', '.join(flagged)}: this run may have used its gold; "
                "if so, move its record to results/invalid/ (HARNESS.md)",
                file=sys.stderr,
            )
    print(
        json.dumps(
            {
                "run_id": record.run_id,
                "cell": record.cell.id,
                "series": record.series,
                "anchor": record.anchor,
                "tiers": {name: t.status for name, t in record.tiers.items()},
                "results": str(results),
            }
        )
    )
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    config = _config(args)
    planned = planner.resolve(config, args.cell)
    ws = workspace.prepare(
        config.tasks[planned.cell.task],
        config.treatments[planned.cell.treatment],
        args.gold.resolve() if args.gold else None,
        args.dest.resolve(),
    )
    print(json.dumps({"workspace": str(ws.path), "base_sha": ws.base_sha}))
    return 0


def cmd_static(args: argparse.Namespace) -> int:
    config = _config(args)
    task = config.tasks[args.task]
    charmlint = config.scoring.static.charmlint
    ws = args.workspace.resolve()
    baseline = (
        None
        if args.no_baseline or task.kind == "new-charm"
        else ladder.baseline_profile(ws, charmlint, task.python, task.static_checks.ruff_exclude)
    )
    outcome = static.run_static(
        ws, task.static_checks, charmlint=charmlint, baseline=baseline, python=task.python
    )
    print(outcome.model_dump_json(indent=2))
    return 0


def cmd_oracle(args: argparse.Namespace) -> int:
    config = _config(args)
    outcome = oracle.run_oracle(args.workspace.resolve(), config.tasks[args.task])
    print(outcome.model_dump_json(indent=2))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    text = json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text() != text:
            print(f"{SCHEMA_PATH} is out of date; run `wof schema --write`", file=sys.stderr)
            return 1
        return 0
    if args.write:
        SCHEMA_PATH.parent.mkdir(exist_ok=True)
        SCHEMA_PATH.write_text(text)
        print(SCHEMA_PATH)
        return 0
    sys.stdout.write(text)
    return 0


def cmd_series(args: argparse.Namespace) -> int:
    print(current_series(_config(args)))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    groups = store.group_by_series(args.results)
    for series, records in sorted(groups.items()):
        dry = sum(1 for r in records if r.dry_run)
        print(f"{series}: {len(records)} records ({dry} dry runs)")
    return 0


def cmd_gold_check(args: argparse.Namespace) -> int:
    """Re-run both gold-copy signals over stored records (read-only)."""
    config = _config(args)
    print("created\tcell\ttask_v\tcontainment\tcoverage\tworst_file\tcopied\tfetched")
    for path in args.results:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            record = RunRecord.model_validate_json(line)
            task = config.tasks.get(record.task.id)
            if task is None:
                print(f"{record.cell.id}: task not in the config", file=sys.stderr)
                continue
            copy = contamination.gold_copy(
                task,
                record.artefacts.agent_diff,
                config.treatments.get(record.treatment.id),
                record.treatment.artefacts,
            )
            if copy.applies:
                worst = max(
                    (
                        f["ratio"]
                        for f in copy.files.values()
                        if f["gold_lines"] >= contamination.GOLD_COPY_FILE_MIN_LINES
                    ),
                    default=0.0,
                )
                cols = [f"{copy.containment:.2f}", f"{copy.coverage:.2f}", f"{worst:.2f}"]
            else:
                cols = ["n/a", "n/a", "n/a"]
            kept = record.tiers.process.details.get("fetches")
            if kept is None:
                fetched = "not kept"
            else:
                fetched = "yes" if contamination.gold_fetch(task, kept).fetched else "no"
            print(
                "\t".join(
                    [
                        record.created_at.date().isoformat(),
                        record.cell.id,
                        record.task.version,
                        *cols,
                        "yes" if copy.copied else ("n/a" if not copy.applies else "no"),
                        fetched,
                    ]
                )
            )
    return 0


def cmd_fetch_gold(args: argparse.Namespace) -> int:
    config = _config(args)
    task = config.tasks[args.task]
    try:
        gold.fetch(task, args.dest)
    except gold.GoldTokenMissing as e:
        print(f"skipped: {e}", file=sys.stderr)
        return 3
    except gold.GoldError as e:
        print(f"fetch-gold failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"task": task.id, "gold": str(args.dest), "sha": task.gold_sha}))
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    config = _config(args)
    planned = planner.resolve(config, args.cell)
    try:
        ws = workspace.load(args.workspace.resolve())
        result = ladder.run_agent(config, planned, ws)
    except (ladder.RunError, workspace.WorkspaceError, RuntimeError) as e:
        print(f"agent refused: {e}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "cell": planned.id,
                "exit_reason": result.exit_reason,
                "turns": result.turns,
                "cost_usd": result.cost_usd,
                "provider": result.provider,
            }
        )
    )
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    config = _config(args)
    planned = planner.resolve(config, args.cell)
    tiers = tuple(_split([args.tiers]) or ())
    try:
        ws = workspace.load(args.workspace.resolve())
        result = ladder.load_agent_result(ws, planned.id)
        record = ladder.score(config, planned, ws, result, tiers=tiers)
    except (ladder.RunError, workspace.WorkspaceError) as e:
        print(f"score refused: {e}", file=sys.stderr)
        return 2
    store.append(args.results, record, allowed_tags=ladder.allowed_tags(config))
    flagged = [t for _, t in record.all_failure_tags() if t in CONTAMINATION_TAGS]
    print(
        json.dumps(
            {
                "run_id": record.run_id,
                "cell": record.cell.id,
                "series": record.series,
                "tiers": {name: t.status for name, t in record.tiers.items()},
                "review": flagged,
                "results": str(args.results),
            }
        )
    )
    return 0


def cmd_store(args: argparse.Namespace) -> int:
    """Append scored records to the store: validated, tag-checked, never twice, never dry."""
    config = _config(args)
    allowed = ladder.allowed_tags(config)
    seen = {r.run_id for rs in store.group_by_series(args.results).values() for r in rs}
    added = skipped = 0
    for path in args.records:
        for record in store.iter_records(path):
            if record.dry_run:
                print(f"{path}: {record.cell.id}: a dry run; not stored", file=sys.stderr)
                return 1
            if record.run_id in seen:
                skipped += 1
                continue
            store.append(args.results, record, allowed_tags=allowed)
            seen.add(record.run_id)
            added += 1
    print(json.dumps({"added": added, "already_stored": skipped, "results": str(args.results)}))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wof", description=__doc__)
    p.add_argument(
        "--config",
        action="append",
        metavar="ROOT",
        help="config tree root (repeatable; default: this package, plus $WOF_PRIVATE_CONFIG)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("plan", help="expand the configs into the cell list")
    s.add_argument("--dry-run", action="store_true", help="print the cells; schedule nothing")
    s.add_argument(
        "--matrix-json",
        action="store_true",
        help="print the GHA matrix ({'include': [...]}); refused over the [gha] max_cells cap",
    )
    for name in ("task", "harness", "model", "treatment"):
        s.add_argument(
            f"--{name}", action="append", help=f"only this {name} (repeatable, or comma-separated)"
        )
    s.add_argument(
        "--kind",
        action="append",
        help="only tasks of this kind: feature, new-charm (repeatable, or comma-separated)",
    )
    s.add_argument("--reps", type=int)
    s.add_argument("--anchors-only", action="store_true")
    s.add_argument(
        "--cells", action="append", help="exactly these cell ids (repeatable, or comma-separated)"
    )
    s.add_argument(
        "--sample",
        type=int,
        nargs="?",
        const=-1,
        help="a seeded sample of this many (alone: [gha] scheduled_sample, the weekly run, "
        "or with --kind new-charm [gha] new_charm_sample, the monthly new-charm slot)",
    )
    s.add_argument("--seed", default="0", help="the sample's seed (eval.yml: the run's date)")
    s.add_argument(
        "--records",
        type=pathlib.Path,
        help="a results.jsonl whose records in the current series steer --sample to the "
        "least-run cells (eval.yml: the results branch's; an empty file means none yet)",
    )
    s.add_argument("--max-cells", type=int, help="a lower cap than [gha] max_cells")
    s.add_argument("--json", action="store_true", help="one JSON object per line")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("run", help="run one cell (task/harness/model/treatment/rep)")
    s.add_argument("cell")
    s.add_argument("--dry-run", action="store_true", help="stub every tier; call nothing")
    s.add_argument(
        "--results",
        type=pathlib.Path,
        help=f"default {DEFAULT_RESULTS} for dry runs, {DEFAULT_REAL_RESULTS} for real ones",
    )
    s.add_argument("--gold", type=pathlib.Path, help="clone of the task's gold repo (real runs)")
    s.add_argument("--max-turns", type=int, help="override the plan's turn cap for this run")
    s.add_argument("--timeout-minutes", type=int, help="override the plan's time cap")
    s.add_argument("--max-cost", type=float, help="override the plan's dollar cap")
    s.add_argument(
        "--workspace",
        type=pathlib.Path,
        help="where to build the run's workspace (default: a new temp dir, kept afterwards)",
    )
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("prepare", help="build a cell's workspace from a gold checkout")
    s.add_argument("cell")
    s.add_argument("--gold", type=pathlib.Path, help="clone of the gold charm (feature tasks)")
    s.add_argument("--dest", type=pathlib.Path, required=True)
    s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("static", help="run the static tier on a workspace")
    s.add_argument("workspace", type=pathlib.Path)
    s.add_argument("--task", required=True)
    s.add_argument("--no-baseline", action="store_true", help="counts only, no deltas")
    s.set_defaults(func=cmd_static)

    s = sub.add_parser("oracle", help="run the hidden-oracle tier on a workspace")
    s.add_argument("workspace", type=pathlib.Path)
    s.add_argument("--task", required=True)
    s.set_defaults(func=cmd_oracle)

    s = sub.add_parser("schema", help="print, write or check the run-record JSON Schema")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    s.set_defaults(func=cmd_schema)

    s = sub.add_parser("series", help="print the current series key")
    s.set_defaults(func=cmd_series)

    s = sub.add_parser("show", help="count records per series in a results file")
    s.add_argument("results", type=pathlib.Path)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser(
        "gold-check", help="re-run the gold-copy checks over stored records (read-only)"
    )
    s.add_argument("results", type=pathlib.Path, nargs="+")
    s.set_defaults(func=cmd_gold_check)

    s = sub.add_parser(
        "fetch-gold",
        help=f"fetch a task's gold at its SHA (a private gold reads ${gold.TOKEN_ENV}; "
        "exit 3 without it)",
    )
    s.add_argument("task")
    s.add_argument("--dest", type=pathlib.Path, required=True)
    s.set_defaults(func=cmd_fetch_gold)

    s = sub.add_parser(
        "agent", help="run a cell's agent on a prepared workspace (no scoring; GHA agent job)"
    )
    s.add_argument("cell")
    s.add_argument("--workspace", type=pathlib.Path, required=True)
    s.set_defaults(func=cmd_agent)

    s = sub.add_parser(
        "score", help="score a workspace the agent job left, and append its record (GHA)"
    )
    s.add_argument("cell")
    s.add_argument("--workspace", type=pathlib.Path, required=True)
    s.add_argument(
        "--tiers",
        default=",".join(ladder.SCORABLE_TIERS),
        help=f"comma-separated, from {','.join(ladder.SCORABLE_TIERS)} (default: all); "
        "live and judge are always not_run",
    )
    s.add_argument("--results", type=pathlib.Path, required=True)
    s.set_defaults(func=cmd_score)

    s = sub.add_parser(
        "store", help="append scored records to a store, skipping ones already in it (GHA)"
    )
    s.add_argument("records", type=pathlib.Path, nargs="+")
    s.add_argument("--results", type=pathlib.Path, default=DEFAULT_REAL_RESULTS)
    s.set_defaults(func=cmd_store)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
