"""The run record: one versioned schema for one run's result.

This replaces §9's ``score.json`` / ``judge.json`` / ``meta.json`` split.
Per-run files may still be written next to a run for debugging, but the
record is the contract: it is what goes into the results store and what the
reports read.

What each part is for (PLAN §8, 2026-11-09, the 3- and 6-month reports):

- ``task``, ``treatment.artefacts``: treatment effects per task and model,
  and before/after for each charmlint, skill or docs release.
- ``model`` (pinned id, tier, served-by provider), ``harness`` (version):
  trends per tier and per harness, and the anchor set's drift.
- ``tiers.*.failure_tags``: the failure taxonomy, from a fixed vocabulary.
- ``scoring`` and ``series``: PLAN §1.1, records from different rubric,
  judge or scoring-code versions are never silently mixed.

Bump ``SCHEMA_VERSION`` for any change a reader of old records would trip
over; adding an optional field usually is not one. Versions:

- 1 (2026-11-09).
- 2 (2026-11-18): ``task.gold_subdir``. Optional, but a bump anyway, because
  its absence is ambiguous in a version 1 record: the haproxy tasks' version 1
  records have no subdirectory recorded although their workspace was one. In a
  version 2 record ``None`` means the repo root; for a version 1 record, look
  the task up by ``(task.id, task.version)`` in the task config. Version 1
  records are still read, and aren't back-filled.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Caps, ScoringTier, TaskKind, Tier

SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = (1, 2)

TierStatus = Literal["pass", "fail", "partial", "error", "not_run"]
Scalar = int | float | bool | str | None


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- identity ----------------------------------------------------------------


class Cell(_Model):
    """One point in the matrix: task x harness x model x treatment x rep."""

    task: str
    harness: str
    model: str
    treatment: str
    rep: int = Field(ge=0)

    @property
    def id(self) -> str:
        return f"{self.task}/{self.harness}/{self.model}/{self.treatment}/{self.rep}"

    @classmethod
    def parse(cls, cell_id: str) -> Cell:
        parts = cell_id.split("/")
        if len(parts) != 5:
            raise ValueError(f"cell id must be task/harness/model/treatment/rep, got {cell_id!r}")
        task, harness, model, treatment, rep = parts
        return cls(task=task, harness=harness, model=model, treatment=treatment, rep=int(rep))


class TaskRef(_Model):
    id: str
    version: str
    kind: TaskKind
    gold_repo: str | None = None
    gold_sha: str | None = None
    gold_subdir: str | None = Field(
        default=None,
        description=(
            "Schema version 2 on: the charm's directory in a monorepo gold (the workspace is "
            "`git archive <gold_sha>:<gold_subdir>`); None means the repo root. Not recorded "
            "in version 1 records: look the task up by (id, version)."
        ),
    )


class TreatmentRef(_Model):
    id: str
    version: str
    artefacts: dict[str, str] = Field(
        description="Every artefact the agent was given, name -> exact version, SHA or digest."
    )


class HarnessRef(_Model):
    name: str
    version: str = Field(description="As reported by the adapter at run time.")


class ModelRef(_Model):
    key: str
    openrouter_id: str
    tier: Tier
    dated: bool
    provider: str | None = Field(
        default=None,
        description="The OpenRouter provider that served the run; None if the run never started.",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description=(
            "The reasoning effort the model was pinned to (models.toml); None means none was "
            "sent and the provider's default applied (but see HARNESS.md: omp always sends one)."
        ),
    )


class ScoringRef(_Model):
    rubric_version: str
    judge_models: list[str]
    scoring_code_version: str


def series_key(scoring: ScoringRef) -> str:
    """The comparability key (PLAN §1.1): changes when rubric, judges or scoring code change."""
    blob = json.dumps(
        {
            "rubric": scoring.rubric_version,
            "judges": sorted(scoring.judge_models),
            "code": scoring.scoring_code_version,
        },
        sort_keys=True,
    )
    return "s-" + hashlib.sha256(blob.encode()).hexdigest()[:12]


# --- tiers -------------------------------------------------------------------


class TierOutcome(_Model):
    """The common shape of every scoring tier's outcome.

    ``status`` is ``not_run`` (with a ``reason``) when the tier didn't run:
    stubbed, not applicable, or skipped by budget. Tiers are diagnostics, not
    gates (PLAN §4), so a failing tier never stops the next from running.
    """

    status: TierStatus
    reason: str | None = None
    failure_tags: list[str] = Field(default_factory=list)
    metrics: dict[str, Scalar] = Field(default_factory=dict)
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form diagnostics; dropped from public held-out records.",
    )

    @model_validator(mode="after")
    def _reason_when_not_run(self) -> TierOutcome:
        if self.status in ("not_run", "error") and not self.reason:
            raise ValueError(f"a tier with status {self.status!r} needs a reason")
        return self

    @classmethod
    def not_run(cls, reason: str) -> TierOutcome:
        return cls(status="not_run", reason=reason)


_ORACLE_FAILURE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "message": {"type": "string"},
        "lines": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["type", "message"],
}


class OracleOutcome(TierOutcome):
    details: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form diagnostics; dropped from public held-out records. Since 2026-11-26 "
            "(scoring code 6): `failures` (failing oracle test -> exception type, message and "
            "a few report lines; type and message only for a private gold), "
            "`collection_errors` (the same for a module that didn't import) and "
            "`failures_omitted` (how many more there were). Older records have none of them."
        ),
        json_schema_extra={
            "properties": {
                "failures": {"type": "object", "additionalProperties": _ORACLE_FAILURE_SCHEMA},
                "collection_errors": {
                    "type": "object",
                    "additionalProperties": _ORACLE_FAILURE_SCHEMA,
                },
                "failures_omitted": {"type": "integer", "minimum": 1},
                "failure_output": {"type": "string"},
            }
        },
    )
    passed: int | None = None
    total: int | None = None
    agent_tests_passed: int | None = None
    agent_tests_total: int | None = None
    key_tests: dict[str, Literal["pass", "fail", "skip", "missing"]] = Field(default_factory=dict)
    tests: dict[str, Literal["pass", "fail", "skip"]] = Field(default_factory=dict)


class JudgeVerdict(_Model):
    model: str
    scores: dict[str, int]
    total: int | None
    max_total: int
    failure_tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    cost_usd: float | None = None


class JudgeOutcome(TierOutcome):
    verdicts: list[JudgeVerdict] = Field(default_factory=list)


ExitReason = Literal["completed", "turn_cap", "time_cap", "cost_cap", "error", "not_started"]


class ProcessOutcome(TierOutcome):
    cost_usd: float | None = None
    turns: int | None = None
    wall_seconds: float | None = None
    tool_calls_total: int | None = None
    tool_calls: dict[str, int] = Field(default_factory=dict)
    read_help: bool | None = Field(
        default=None, description="None when the treatment offered no help to read."
    )
    ran_scripts: list[str] = Field(default_factory=list)
    exit_reason: ExitReason | None = None


class Tiers(_Model):
    static: TierOutcome
    oracle: OracleOutcome
    live: TierOutcome
    judge: JudgeOutcome
    process: ProcessOutcome

    def items(self) -> list[tuple[ScoringTier, TierOutcome]]:
        return [(name, getattr(self, name)) for name in ScoringTier.__args__]


class RunArtefacts(_Model):
    prompt: str | None = None
    agent_diff: str | None = None
    diff_stats: dict[str, Scalar] = Field(default_factory=dict)


# --- the record --------------------------------------------------------------


class RunRecord(_Model):
    schema_version: Literal[1, 2] = SCHEMA_VERSION
    run_id: str
    series: str = Field(description="series_key(scoring); records only compare within a series.")
    created_at: dt.datetime
    dry_run: bool = False
    held_out: bool
    anchor: bool
    redacted: bool = Field(
        default=False, description="True for the public form of a held-out run."
    )
    cell: Cell
    task: TaskRef
    treatment: TreatmentRef
    harness: HarnessRef
    model: ModelRef
    scoring: ScoringRef
    caps: Caps
    tiers: Tiers
    artefacts: RunArtefacts = Field(default_factory=RunArtefacts)

    @model_validator(mode="after")
    def _consistent(self) -> RunRecord:
        if self.series != series_key(self.scoring):
            raise ValueError(f"series {self.series!r} does not match the scoring versions")
        if (self.cell.task, self.cell.treatment) != (self.task.id, self.treatment.id):
            raise ValueError("cell and task/treatment refs disagree")
        if (self.cell.harness, self.cell.model) != (self.harness.name, self.model.key):
            raise ValueError("cell and harness/model refs disagree")
        if self.redacted and not self.held_out:
            raise ValueError("only held-out records are redacted")
        if self.schema_version == 1 and self.task.gold_subdir is not None:
            raise ValueError("schema version 1 records don't carry task.gold_subdir")
        return self

    def all_failure_tags(self) -> list[tuple[str, str]]:
        out = [(tier, tag) for tier, outcome in self.tiers.items() for tag in outcome.failure_tags]
        out += [("judge", tag) for v in self.tiers.judge.verdicts for tag in v.failure_tags]
        return out


class RecordError(ValueError):
    pass


def check_failure_tags(record: RunRecord, allowed: dict[str, set[str]]) -> None:
    """Raise unless every tag is in the vocabulary for the tier that emitted it.

    ``allowed`` maps tier name -> permitted tags (``Config.tags_for``).
    """
    bad = [f"{tier}:{tag}" for tier, tag in record.all_failure_tags() if tag not in allowed[tier]]
    if bad:
        raise RecordError(f"failure tags not in the vocabulary: {', '.join(bad)}")


def public_form(record: RunRecord) -> RunRecord:
    """The form of a record that may be published.

    Non-held-out records are returned unchanged. For a held-out run (PLAN §8,
    2026-11-09) the scores, failure tags and metadata stay, so the
    public/held-out gap can be measured; the agent diff, the task prompt,
    the oracle's test names and every free-text field go, as does where the
    task came from (its gold repo and SHA would identify the feature).
    """
    if not record.held_out:
        return record
    data = record.model_dump(mode="python")
    data["redacted"] = True
    data["task"]["gold_repo"] = None
    data["task"]["gold_sha"] = None
    data["task"]["gold_subdir"] = None
    data["artefacts"] = {
        "prompt": None,
        "agent_diff": None,
        "diff_stats": {
            k: v for k, v in data["artefacts"]["diff_stats"].items() if k in _PUBLIC_DIFF_STATS
        },
    }
    for tier in data["tiers"].values():
        tier["details"] = {}
        if tier["status"] in ("not_run", "error"):
            tier["reason"] = "redacted (held-out task)"
        else:
            tier["reason"] = None
    oracle = data["tiers"]["oracle"]
    oracle["tests"] = {}
    oracle["key_tests"] = {}
    for verdict in data["tiers"]["judge"]["verdicts"]:
        verdict["notes"] = None
    return RunRecord.model_validate(data)


_PUBLIC_DIFF_STATS = {"files_changed", "insertions", "deletions"}


def json_schema() -> dict[str, Any]:
    schema = RunRecord.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"charmer_wof/run-record.v{SCHEMA_VERSION}.json"
    return schema
