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

import io
import json
import pathlib
import subprocess
import tarfile
import tempfile
import uuid

from . import store, workspace
from .adapters.base import AdapterResult, HarnessAdapter, get_adapter
from .config import Config, Task, Treatment
from .planner import PlannedCell
from .record import build_record, scoring_ref, stub_tiers
from .schema import (
    JudgeOutcome,
    OracleOutcome,
    ProcessOutcome,
    RunArtefacts,
    RunRecord,
    TierOutcome,
    Tiers,
    series_key,
)
from .scoring import _proc, acceptance, contamination, oracle, process, static
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


def gold_lint_profile(config: Config, task: Task, gold: pathlib.Path) -> dict:
    """charmlint on the gold's own tree (``static.gold_lint_profile``), from a checkout.

    The tree is what ``workspace.prepare`` starts from: ``git archive`` of the
    gold SHA (and subdirectory), plus ``charmcraft fetch-libs`` for a task that
    needs it.
    """
    if task.kind != "feature" or task.gold_sha is None:
        raise RunError(f"task {task.id!r} has no gold to lint")
    tree = f"{task.gold_sha}:{task.gold_subdir}" if task.gold_subdir else task.gold_sha
    with tempfile.TemporaryDirectory() as tmp:
        dest = pathlib.Path(tmp) / "gold"
        dest.mkdir()
        tar = subprocess.run(
            ["git", "archive", "--format=tar", tree], cwd=gold, capture_output=True, check=True
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            tf.extractall(dest, filter="data")
        if task.fetch_libs:
            workspace.fetch_libs(dest)
        return static.gold_lint_profile(
            dest,
            config.scoring.static.charmlint,
            task_id=task.id,
            gold_sha=task.gold_sha,
        )


def load_gold_lint(path: pathlib.Path, task: Task, charmlint: str) -> dict:
    """A profile ``wof gold-lint`` wrote, checked against the task and the pin."""
    data = json.loads(path.read_text())
    want = {"task": task.id, "gold_sha": task.gold_sha, "charmlint": charmlint}
    got = {k: data.get(k) for k in want}
    if got != want or not isinstance(data.get("findings"), list):
        raise RunError(f"{path} is not the gold charmlint profile for {want}: {got}")
    return data


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
    gold_lint: dict | None = None,
    without_gold_lint: bool = False,
) -> RunRecord:
    """Snapshot what the agent left and score it; returns the record (not stored).

    Tiers not in ``tiers`` are ``not_run``; live (phase 3, still to come) and
    judge (phase 4) always are. A feature task's static tier needs the gold's
    charmlint profile (``gold_lint``); scoring one without it has to be asked
    for (``without_gold_lint``), and the record says so.
    """
    unknown = set(tiers) - set(SCORABLE_TIERS)
    if unknown:
        raise RunError(f"unknown tiers {sorted(unknown)}; choose from {list(SCORABLE_TIERS)}")
    cell = planned.cell
    task = config.tasks[cell.task]
    if (
        "static" in tiers
        and task.kind == "feature"
        and gold_lint is None
        and not without_gold_lint
    ):
        raise RunError(
            f"{task.id} is a feature task: its static tier needs the gold's charmlint "
            "profile (wof gold-lint, then --gold-lint), or --no-gold-lint to score without one"
        )
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
                gold_lint=gold_lint,
            ),
            task,
            diff,
            treatment,
        )
        static_outcome = with_acceptance_check(static_outcome, task, treatment, workspace=ws.path)
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
    task = config.tasks[cell.task]
    ws = workspace.prepare(task, config.treatments[cell.treatment], gold, dest)
    gold_lint = gold_lint_profile(config, task, gold) if task.kind == "feature" and gold else None
    result = run_agent(
        config, planned, ws, gold=gold, adapter=adapter, openrouter_key_env=openrouter_key_env
    )
    record = score(config, planned, ws, result, gold_lint=gold_lint)
    return store.append(results, record, allowed_tags=allowed_tags(config))


def with_gold_copy(
    outcome: TierOutcome,
    task: Task,
    diff: str | None,
    treatment: Treatment | None = None,
    recorded: dict[str, str] | None = None,
) -> TierOutcome:
    """The static tier plus the gold-copy check (``scoring.contamination``).

    With a ``treatment`` that generates visible tests from the oracle
    (``tests-target``), their lines don't count as copying.

    The check adds the ``gold-copied`` tag and two metrics but doesn't change
    the tier's status: the static tier says whether the charm is sound, and
    the tag says whose charm it is.
    """
    check = contamination.gold_copy(task, diff, treatment, recorded)
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


def with_acceptance_check(
    outcome: TierOutcome,
    task: Task,
    treatment: Treatment,
    *,
    workspace: pathlib.Path | None = None,
    diff: str | None = None,
    recorded: dict[str, str] | None = None,
) -> TierOutcome:
    """The static tier plus ``acceptance_tests`` (``scoring.acceptance``), for a
    treatment that generates visible tests; unchanged for every other treatment.

    Live, the files are read from ``workspace``; a replay passes the record's
    ``diff`` and ``recorded`` artefacts instead. An edit to the tests' meaning
    fails the tier; a reformat is tagged and recorded, and doesn't.
    """
    original = acceptance.generated(task, treatment, recorded)
    if original is None:
        return outcome
    now = (
        acceptance.from_workspace(workspace, list(original))
        if workspace is not None
        else acceptance.from_diff(original, diff)
    )
    result, new_tags = acceptance.check(original, now)
    tags = list(outcome.failure_tags) + [t for t in new_tags if t not in outcome.failure_tags]
    status = outcome.status
    if result["status"] == "fail":
        status = "fail"
    elif result["status"] == "pass" and status == "error":
        status = "pass"
    checks = {**outcome.details.get("checks", {}), "acceptance_tests": result}
    return outcome.model_copy(
        update={
            "status": status,
            "reason": None if status != "error" else outcome.reason,
            "failure_tags": tags,
            "metrics": {
                **outcome.metrics,
                "acceptance_tests_ok": None
                if result["status"] == "not_run"
                else result["status"] == "pass",
            },
            "details": {**outcome.details, "checks": checks},
        }
    )


def _rebuilt_trees(
    config: Config, record: RunRecord, gold: pathlib.Path, dest: pathlib.Path
) -> tuple[pathlib.Path, pathlib.Path]:
    """The record's base and the agent's tree, rebuilt from the gold and the stored diff.

    The diff leaves out ``lib/`` and ``uv.lock`` (``workspace.DIFF_EXCLUDES``), so
    the agent's tree is only as good as that: callers compare what they compute
    on it with what the record holds.
    """
    task = config.tasks[record.task.id]
    ws = workspace.prepare(task, config.treatments[record.treatment.id], gold, dest / "ws")
    base = dest / "base"
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(base), ws.base_sha],
        cwd=ws.path,
        check=True,
        capture_output=True,
    )
    patch = dest / "agent.diff"
    patch.write_text(record.artefacts.agent_diff or "")
    if (record.artefacts.agent_diff or "").strip():
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch)],
            cwd=ws.path,
            check=True,
            capture_output=True,
        )
    return base, ws.path


def rescore(
    config: Config,
    record: RunRecord,
    *,
    gold: pathlib.Path | None = None,
    gold_lint: dict | None = None,
    run_id: str | None = None,
) -> RunRecord:
    """A stored record scored again with today's scoring code, without its workspace.

    What can be recomputed from the record (and, for charmlint, the gold) is:
    the gold-copy check, the acceptance-tests check, and the charmlint check
    against the gold (base and agent trees rebuilt from ``gold`` and the stored
    diff, refused if the rebuilt agent tree doesn't give the recorded total).
    Everything else (ruff, pack, the declared checks, the oracle, process) is
    kept as recorded: none of it changed in scoring code 6, except that the
    oracle's failure output can't be recovered (there was none for a passing
    oracle). The new record is in the current series, keeps ``created_at``,
    gets a new ``run_id`` (so ``wof store`` doesn't skip it as stored), and
    says what it came from in ``tiers.static.details.rescored_from``.
    """
    task = config.tasks[record.task.id]
    treatment = config.treatments[record.treatment.id]
    old = record.tiers.static
    if old.status == "not_run":
        raise RunError(f"{record.run_id}: the static tier didn't run; nothing to re-score")
    checks = {k: dict(v) for k, v in old.details.get("checks", {}).items()}
    metrics = dict(old.metrics)
    tags = [t for t in old.failure_tags if t not in ("gold-copied", "lint-regression")]
    cl = checks.get("charmlint")
    counted = metrics.get("charmlint_delta")
    if cl is not None and cl.get("status") != "error" and task.kind == "feature":
        if gold is None or gold_lint is None:
            cl["gold_baseline"] = {"status": "not_available"}
            cl["reason"] = "re-scored without the gold: findings the gold also has not excused"
            metrics["charmlint_excused"] = None
        else:
            charmlint = config.scoring.static.charmlint
            with tempfile.TemporaryDirectory() as tmp:
                base, agent = _rebuilt_trees(config, record, gold, pathlib.Path(tmp))
                base_p = static.charmlint_counts(base, charmlint)
                agent_p = static.charmlint_counts(agent, charmlint)
            if "error" in base_p or "error" in agent_p:
                raise RunError(
                    f"charmlint did not run: {base_p.get('error') or agent_p.get('error')}"
                )
            if agent_p["total"] != cl.get("total"):
                raise RunError(
                    f"{record.run_id}: the rebuilt tree has {agent_p['total']} charmlint "
                    f"findings, the record {cl.get('total')}: not re-scoring charmlint"
                )
            excused, still_new = static.excused_by_gold(agent_p, base_p, gold_lint)
            counted = agent_p["total"] - base_p["total"] - len(excused)
            cl["gold_baseline"] = static._gold_baseline(gold_lint)
            cl["excused"] = [static._key_json(k) for k in excused[: static.FINDINGS_KEPT]]
            cl["new"] = [static._key_json(k) for k in still_new[: static.FINDINGS_KEPT]]
            cl["status"] = "fail" if counted > 0 else "pass"
            metrics["charmlint_excused"] = len(excused)
    if (
        (metrics.get("ruff_delta") or 0) > 0
        or (metrics.get("ruff_format_delta") or 0) > 0
        or (counted or 0) > 0
    ):
        tags.append("lint-regression")
    statuses = [c["status"] for c in checks.values()]
    status = "fail" if "fail" in statuses else ("pass" if "pass" in statuses else "error")
    details = {**old.details, "checks": checks}
    details.pop("gold_copy", None)
    outcome = old.model_copy(
        update={"status": status, "failure_tags": tags, "metrics": metrics, "details": details}
    )
    outcome = with_gold_copy(
        outcome, task, record.artefacts.agent_diff, treatment, record.treatment.artefacts
    )
    outcome = with_acceptance_check(
        outcome,
        task,
        treatment,
        diff=record.artefacts.agent_diff,
        recorded=record.treatment.artefacts,
    )
    outcome.details["rescored_from"] = {
        "run_id": record.run_id,
        "series": record.series,
        "scoring_code_version": record.scoring.scoring_code_version,
    }
    scoring = scoring_ref(config)
    data = record.model_dump(mode="python")
    data.update(
        run_id=run_id
        or uuid.uuid5(uuid.NAMESPACE_URL, f"{record.run_id}/{series_key(scoring)}").hex,
        series=series_key(scoring),
        scoring=scoring.model_dump(mode="python"),
        schema_version=2,
    )
    data["tiers"]["static"] = outcome.model_dump(mode="python")
    return RunRecord.model_validate(data)


class RunError(RuntimeError):
    pass
