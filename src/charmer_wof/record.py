"""Build a run record for a cell from the config and the tier outcomes."""

from __future__ import annotations

import datetime as dt
import uuid

from .config import Config
from .planner import PlannedCell
from .schema import (
    HarnessRef,
    JudgeOutcome,
    ModelRef,
    OracleOutcome,
    ProcessOutcome,
    RunArtefacts,
    RunRecord,
    ScoringRef,
    TaskRef,
    TierOutcome,
    Tiers,
    TreatmentRef,
    series_key,
)
from .scoring import scoring_code_version


def scoring_ref(config: Config) -> ScoringRef:
    return ScoringRef(
        rubric_version=config.scoring.rubric_version,
        judge_models=list(config.scoring.judge_models),
        scoring_code_version=scoring_code_version(config.scoring.static.charmlint),
    )


def current_series(config: Config) -> str:
    return series_key(scoring_ref(config))


def stub_tiers(reason: str) -> Tiers:
    """Every tier ``not_run`` with the same reason (dry runs, runs that never started)."""
    return Tiers(
        static=TierOutcome.not_run(reason),
        oracle=OracleOutcome(status="not_run", reason=reason),
        live=TierOutcome.not_run(reason),
        judge=JudgeOutcome(status="not_run", reason=reason),
        process=ProcessOutcome(status="not_run", reason=reason, exit_reason="not_started"),
    )


def build_record(
    config: Config,
    planned: PlannedCell,
    tiers: Tiers,
    *,
    harness_version: str | None = None,
    provider: str | None = None,
    artefacts: RunArtefacts | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
    now: dt.datetime | None = None,
) -> RunRecord:
    cell = planned.cell
    task = config.tasks[cell.task]
    treatment = config.treatments[cell.treatment]
    harness = config.harnesses[cell.harness]
    model = config.models[cell.model]
    scoring = scoring_ref(config)
    return RunRecord(
        run_id=run_id or uuid.uuid4().hex,
        series=series_key(scoring),
        created_at=now or dt.datetime.now(dt.UTC),
        dry_run=dry_run,
        held_out=task.held_out,
        anchor=planned.anchor,
        cell=cell,
        task=TaskRef(
            id=task.id,
            version=task.version,
            kind=task.kind,
            gold_repo=task.gold_repo,
            gold_sha=task.gold_sha,
            gold_subdir=task.gold_subdir,
        ),
        treatment=TreatmentRef(
            id=treatment.id,
            version=treatment.version,
            artefacts=treatment.artefact_versions(task),
        ),
        harness=HarnessRef(name=harness.name, version=harness_version or harness.version),
        model=ModelRef(
            key=model.key,
            openrouter_id=model.openrouter_id,
            tier=model.tier,
            dated=model.dated,
            provider=provider,
            reasoning_effort=model.reasoning_effort,
        ),
        scoring=scoring,
        caps=config.plan.caps,
        tiers=tiers,
        artefacts=artefacts or RunArtefacts(),
    )
