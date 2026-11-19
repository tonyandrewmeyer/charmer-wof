"""Walk one cell through the run and the scoring ladder.

``dry_run``: no agent, no model call, every tier stubbed ``not_run``.
``run_cell`` (phase 1): prepare, run the adapter, snapshot, then every tier
in order, each one a diagnostic rather than a gate (PLAN §4).

On GHA (phase 3) the same steps run in separate jobs, so the agent's job has
no gold and no GitHub access, and the scoring job no model key: ``wof
prepare`` (``workspace.prepare``), ``wof agent`` (``run_agent``, which leaves
the adapter's result in ``.wof/agent-result.json``), then ``wof score``
(``score``, which reads it back). ``run_cell`` is those three in one process.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile

from . import store, workspace
from .adapters.base import AdapterResult, HarnessAdapter, get_adapter
from .config import Config, Task, Treatment
from .planner import PlannedCell
from .record import build_record, stub_tiers
from .schema import (
    JudgeOutcome,
    OracleOutcome,
    ProcessOutcome,
    RunArtefacts,
    RunRecord,
    TierOutcome,
    Tiers,
)
from .scoring import _proc, contamination, oracle, process, static
from .workspace import build_prompt


def dry_run(config: Config, planned: PlannedCell, results: pathlib.Path) -> RunRecord:
    task = config.tasks[planned.cell.task]
    treatment = config.treatments[planned.cell.treatment]
    record = build_record(
        config,
        planned,
        stub_tiers("dry run: tier stubbed"),
        artefacts=RunArtefacts(prompt=build_prompt(task, treatment)),
        dry_run=True,
    )
    return store.append(results, record, allowed_tags=allowed_tags(config))


def baseline_profile(
    ws: pathlib.Path, charmlint: str, python: str | None, ruff_exclude: tuple[str, ...] = ()
) -> dict | None:
    """Ruff and charmlint counts on the prepared base, for the static tier's deltas."""
    sha_file = ws / workspace.HARNESS_DIR / "base-sha"
    if not sha_file.exists():
        return None
    sha = sha_file.read_text().strip()
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp) / "base"
        subprocess.run(
            ["git", "worktree", "add", "-q", "--detach", str(base), sha],
            cwd=ws,
            check=True,
            capture_output=True,
        )
        try:
            return static.lint_profile(base, charmlint, python=python, ruff_exclude=ruff_exclude)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(base)], cwd=ws, capture_output=True
            )


def _toplevel(path: pathlib.Path) -> pathlib.Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True
    )
    return pathlib.Path(out.stdout.strip()) if out.returncode == 0 else path


def hidden_paths(
    gold: pathlib.Path | None, task: Task, treatment: Treatment
) -> list[pathlib.Path]:
    """What the agent must not see: the gold checkout (if any), and the whole repo
    holding the task's oracle and removal diff (here that's also §9's reference trees)."""
    paths = [gold.resolve()] if gold is not None else []
    for root in (task.root, treatment.root):
        top = _toplevel(pathlib.Path(root).resolve())
        if top not in paths:
            paths.append(top)
    return paths


LIVE_NOT_RUN = "live tier is phase 3 (needs Juju; lands with GHA)"
JUDGE_NOT_RUN = "judge tier is phase 4"


AGENT_RESULT = "agent-result.json"
SCORABLE_TIERS = ("static", "oracle", "process", "pack")
"""What ``score`` can be asked for. ``pack`` is the static tier's pack sub-check."""
NOT_SCORED = "not scored in this run (wof score --tiers)"


def run_agent(
    config: Config,
    planned: PlannedCell,
    ws: workspace.Workspace,
    *,
    gold: pathlib.Path | None = None,
    adapter: HarnessAdapter | None = None,
    openrouter_key_env: str = "OPENROUTER_API_KEY",
    hide: list[pathlib.Path] | None = None,
) -> AdapterResult:
    """The agent's run on a prepared workspace; its result is also left in ``.wof/``.

    ``hide`` defaults to ``hidden_paths``: the gold checkout and the config
    tree's git toplevel. The harness version goes into the result's details,
    so scoring elsewhere doesn't need the harness installed.
    """
    cell = planned.cell
    task = config.tasks[cell.task]
    treatment = config.treatments[cell.treatment]
    harness = config.harnesses[cell.harness]
    if not harness.pinned:
        raise RunError(f"harness {harness.name!r} is unpinned; real runs need a pinned version")
    adapter = adapter or get_adapter(config, harness.name)
    result = adapter.run(
        ws.path,
        ws.prompt,
        config.models[cell.model],
        config.plan.caps,
        openrouter_key_env,
        env=_proc.charm_env(task.python),
        hide=hidden_paths(gold, task, treatment) if hide is None else hide,
    )
    result.details.setdefault("harness_version", adapter.version())
    result.details.setdefault("cell", cell.id)
    out = ws.path / workspace.HARNESS_DIR / AGENT_RESULT
    out.write_text(json.dumps(result.to_json(), indent=1, default=str) + "\n")
    return result


def load_agent_result(ws: workspace.Workspace, cell_id: str) -> AdapterResult:
    path = ws.path / workspace.HARNESS_DIR / AGENT_RESULT
    if not path.exists():
        raise RunError(f"{path} is missing: did the agent job run for this workspace?")
    result = AdapterResult.from_json(json.loads(path.read_text()))
    ran = result.details.get("cell")
    if ran is not None and ran != cell_id:
        raise RunError(f"{path} is from {ran}, not {cell_id}")
    return result


def score(
    config: Config,
    planned: PlannedCell,
    ws: workspace.Workspace,
    result: AdapterResult,
    *,
    tiers: tuple[str, ...] = SCORABLE_TIERS,
) -> RunRecord:
    """Snapshot what the agent left and score it; returns the record (not stored).

    Tiers not in ``tiers`` are ``not_run``; live (phase 3, still to come) and
    judge (phase 4) always are.
    """
    unknown = set(tiers) - set(SCORABLE_TIERS)
    if unknown:
        raise RunError(f"unknown tiers {sorted(unknown)}; choose from {list(SCORABLE_TIERS)}")
    cell = planned.cell
    task = config.tasks[cell.task]
    treatment = config.treatments[cell.treatment]
    caps = config.plan.caps
    charmlint = config.scoring.static.charmlint
    workspace.snapshot(ws.path)
    diff, stats = workspace.agent_diff(ws.path, ws.base_sha)
    changed = workspace.changed_paths(ws.path, ws.base_sha)
    if "static" in tiers:
        static_outcome = with_gold_copy(
            static.run_static(
                ws.path,
                task.static_checks,
                charmlint=charmlint,
                # A new charm's base is empty: lint deltas against it would only
                # count the charm's size, so its static tier records counts only.
                baseline=None
                if task.kind == "new-charm"
                else baseline_profile(
                    ws.path, charmlint, task.python, task.static_checks.ruff_exclude
                ),
                python=task.python,
                changed=changed,
                pack="pack" in tiers,
            ),
            task,
            diff,
        )
    else:
        static_outcome = TierOutcome.not_run(NOT_SCORED)
    built = Tiers(
        static=static_outcome,
        oracle=oracle.run_oracle(ws.path, task)
        if "oracle" in tiers
        else OracleOutcome(status="not_run", reason=NOT_SCORED),
        live=TierOutcome.not_run(LIVE_NOT_RUN),
        judge=JudgeOutcome(status="not_run", reason=JUDGE_NOT_RUN),
        process=process.process_outcome(result, treatment, caps, changed, task=task)
        if "process" in tiers
        else ProcessOutcome(status="not_run", reason=NOT_SCORED),
    )
    return build_record(
        config,
        planned,
        built,
        harness_version=result.details.get("harness_version"),
        provider=result.provider,
        artefacts=RunArtefacts(prompt=ws.prompt, agent_diff=diff, diff_stats=stats),
    )


def allowed_tags(config: Config) -> dict[str, set[str]]:
    return {
        tier: config.tags_for(tier) for tier in ("static", "oracle", "live", "judge", "process")
    }


def run_cell(
    config: Config,
    planned: PlannedCell,
    results: pathlib.Path,
    *,
    gold: pathlib.Path | None,
    dest: pathlib.Path,
    adapter: HarnessAdapter | None = None,
    openrouter_key_env: str = "OPENROUTER_API_KEY",
) -> RunRecord:
    """The real run: prepare, agent, snapshot, then every tier in order (PLAN §4).

    Live (phase 3) and judge (phase 4) are recorded ``not_run`` with their
    reasons. An agent error doesn't stop the scoring: whatever the agent left
    is scored like any other result.
    """
    cell = planned.cell
    harness = config.harnesses[cell.harness]
    if not harness.pinned:
        raise RunError(f"harness {harness.name!r} is unpinned; real runs need a pinned version")
    ws = workspace.prepare(config.tasks[cell.task], config.treatments[cell.treatment], gold, dest)
    result = run_agent(
        config, planned, ws, gold=gold, adapter=adapter, openrouter_key_env=openrouter_key_env
    )
    record = score(config, planned, ws, result)
    return store.append(results, record, allowed_tags=allowed_tags(config))


def with_gold_copy(outcome: TierOutcome, task: Task, diff: str | None) -> TierOutcome:
    """The static tier plus the gold-copy check (``scoring.contamination``).

    The check adds the ``gold-copied`` tag and two metrics but doesn't change
    the tier's status: the static tier says whether the charm is sound, and
    the tag says whose charm it is.
    """
    check = contamination.gold_copy(task, diff)
    tags = list(outcome.failure_tags)
    if check.copied and "gold-copied" not in tags:
        tags.append("gold-copied")
    return outcome.model_copy(
        update={
            "failure_tags": tags,
            "metrics": {**outcome.metrics, **check.metrics()},
            "details": {**outcome.details, "gold_copy": check.details()},
        }
    )


class RunError(RuntimeError):
    pass
