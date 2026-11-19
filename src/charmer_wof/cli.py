"""Command line: ``wof plan|run|prepare|static|oracle|schema|series|show|gold-check``."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

from . import ladder, planner, store, workspace
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


def cmd_plan(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("scheduling is phase 3; pass --dry-run to print the cell list", file=sys.stderr)
        return 2
    config = _config(args)
    cells = planner.expand(
        config,
        tasks=args.task,
        harnesses=args.harness,
        models=args.model,
        treatments=args.treatment,
        reps=args.reps,
        anchors_only=args.anchors_only,
    )
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
            copy = contamination.gold_copy(task, record.artefacts.agent_diff)
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
    for name in ("task", "harness", "model", "treatment"):
        s.add_argument(f"--{name}", action="append", help=f"only this {name} (repeatable)")
    s.add_argument("--reps", type=int)
    s.add_argument("--anchors-only", action="store_true")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
