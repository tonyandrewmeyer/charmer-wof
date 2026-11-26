"""Load the task, model, harness, treatment, scoring and plan configs.

A config tree is a directory with a ``config/`` subdirectory holding some or
all of ``tasks.toml``, ``models.toml``, ``harnesses.toml``,
``treatments.toml``, ``scoring.toml``, ``failure_tags.toml`` and
``plan.toml``. Paths inside a tree resolve against that tree's root.

The public tree is this package's own. Held-out tasks (PLAN §8, 2026-11-09)
live in a private repo laid out the same way; pass its root as an extra tree
and its tasks (and any treatments or models it adds) are merged in. Ids must
be unique across trees.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[2]
"""The root of the public config tree (the directory holding ``config/``)."""

PRIVATE_ROOT_ENV = "WOF_PRIVATE_CONFIG"
"""Environment variable naming extra config trees (os.pathsep-separated)."""

LEGACY_PRIVATE_ROOT_ENV = "CWE_PRIVATE_CONFIG"
"""The same, under the package's name before 2026-11-19; read when the new one is unset."""

UNPINNED = "unpinned"

Tier = Literal["frontier", "mid", "open-weight"]
TaskKind = Literal["feature", "new-charm"]
ScoringTier = Literal["static", "oracle", "live", "judge", "process"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
"""OpenRouter's ``reasoning.effort`` values (``none`` turns reasoning off)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- tasks -------------------------------------------------------------------


class OracleSpec(_Strict):
    files: tuple[pathlib.Path, ...]
    install_dir: str = "tests/unit"
    command: tuple[str, ...]
    marker: str = "test__oracle_"
    key_tests: tuple[str, ...] = ()
    key_test: str | None = Field(
        default=None,
        description=(
            "The one test that checks the task's feature itself; when it doesn't pass the "
            "oracle tier is tagged key-oracle-test-failed, whatever the rest scored."
        ),
    )

    @model_validator(mode="after")
    def _key_test_is_reported(self) -> OracleSpec:
        if self.key_test is not None and self.key_test not in self.key_tests:
            raise ValueError(f"key_test {self.key_test!r} must also be in key_tests")
        return self


class EndpointCheck(_Strict):
    name: str
    interface: str
    role: Literal["requires", "provides", "peers"] = "requires"


class CharmLibCheck(_Strict):
    lib: str
    version: str


class StaticChecks(_Strict):
    endpoints: tuple[EndpointCheck, ...] = ()
    charm_libs: tuple[CharmLibCheck, ...] = ()
    actions: tuple[str, ...] = Field(
        default=(),
        description="Actions the charm must declare (charmcraft.yaml or actions.yaml).",
    )
    config_options: tuple[str, ...] = Field(
        default=(),
        description="Config options the charm must declare (charmcraft.yaml or config.yaml).",
    )
    files_present: tuple[str, ...] = ()
    ruff_exclude: tuple[str, ...] = Field(
        default=(),
        description=(
            "Paths ruff skips (--extend-exclude) on the base and the result alike, for a gold "
            "whose own lint doesn't cover them (vendored lib/)."
        ),
    )


class Task(_Strict):
    id: str
    version: str
    kind: TaskKind
    held_out: bool = False
    description: str = ""
    gold_repo: str | None = None
    gold_sha: str | None = None
    gold_subdir: str | None = Field(
        default=None,
        description=(
            "For a monorepo: the charm's directory in the gold repo. The workspace is that "
            "directory alone, and removal_diff and remove_paths are relative to it."
        ),
    )
    gold_private: bool = Field(
        default=False,
        description=(
            "The gold repo needs a read token to clone (hedgedoc-operator). On GHA the prep "
            "job uses HEDGEDOC_GOLD_TOKEN for it, and skips the task's cells without one."
        ),
    )
    fetch_libs: bool = Field(
        default=False,
        description=(
            "Run `charmcraft fetch-libs` in the workspace before committing the base, for a "
            "gold that doesn't commit its lib/ (none of task set v1 needs it)."
        ),
    )
    removal_diff: pathlib.Path | None = None
    removal_strip: int = 1
    remove_paths: tuple[str, ...] = ()
    prompt: pathlib.Path
    starter: pathlib.Path | None = Field(
        default=None,
        description=(
            "New-charm tasks: a directory copied into the otherwise empty workspace "
            "(the agent's context file, nothing of the charm)."
        ),
    )
    context_file: str = "AGENTS.md"
    python: str | None = Field(
        default=None,
        description="The Python the charm's own environment is built with (UV_PYTHON).",
    )
    judge_criteria: pathlib.Path | None = None
    oracle: OracleSpec | None = None
    static_checks: StaticChecks = StaticChecks()
    root: pathlib.Path = Field(description="The config tree this task came from.")

    @model_validator(mode="after")
    def _feature_tasks_are_reproducible(self) -> Task:
        if self.kind == "feature":
            missing = [
                name
                for name in ("gold_repo", "gold_sha", "removal_diff", "oracle")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"feature task {self.id!r} needs {', '.join(missing)}")
        elif self.removal_diff is not None or self.remove_paths:
            raise ValueError(f"new-charm task {self.id!r} has nothing to remove")
        if self.gold_sha is not None and len(self.gold_sha) != 40:
            raise ValueError(f"task {self.id!r}: gold_sha must be a full 40-char SHA")
        return self

    def path(self, rel: pathlib.Path | str) -> pathlib.Path:
        return self.root / rel

    def prompt_text(self) -> str:
        return self.path(self.prompt).read_text().rstrip("\n")


# --- models ------------------------------------------------------------------


class Model(_Strict):
    key: str
    openrouter_id: str
    tier: Tier
    dated: bool = False
    providers: tuple[str, ...] = ()
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description=(
            "OpenRouter reasoning.effort to ask for (PLAN §8, 2026-11-14). Unset means the "
            "provider's default: adapters send an effort only when this is set."
        ),
    )


# --- harnesses ---------------------------------------------------------------


class Harness(_Strict):
    name: str
    version: str
    adapter: str

    @property
    def pinned(self) -> bool:
        return self.version != UNPINNED


# --- treatments --------------------------------------------------------------


class FromOracle(_Strict):
    """Visible files generated from a task's hidden oracle (``tests-target``).

    Every file in the task's ``oracle.files`` is copied into ``to_dir`` (the
    oracle's own ``install_dir`` when unset), with ``rename`` applied to the
    file name and ``replace`` (then the task's ``task_replace`` entry) to the
    text. Taking every oracle file, not a chosen one, is PLAN §8 2026-11-17:
    "tests as the target" means all of the feature's tests.
    """

    applies_to: tuple[str, ...] = ("*",)
    to_dir: str | None = None
    rename: tuple[str, str] = ("test__oracle_", "test_acceptance_")
    replace: tuple[tuple[str, str], ...] = ()
    task_replace: dict[str, tuple[tuple[str, str], ...]] = Field(default_factory=dict)

    def files(self, task: Task) -> list[tuple[pathlib.Path, str]]:
        """``(oracle source, workspace path)`` for each oracle file of ``task``."""
        if task.oracle is None or not ("*" in self.applies_to or task.id in self.applies_to):
            return []
        to_dir = self.to_dir if self.to_dir is not None else task.oracle.install_dir
        old, new = self.rename
        out = []
        for f in task.oracle.files:
            name = pathlib.PurePosixPath(f).name.replace(old, new)
            out.append((task.path(f), f"{to_dir.rstrip('/')}/{name}"))
        return out

    def text(self, task: Task, source: pathlib.Path) -> str:
        text = source.read_text()
        for old, new in (*self.replace, *self.task_replace.get(task.id, ())):
            text = text.replace(old, new)
        return text


class Treatment(_Strict):
    id: str
    version: str
    description: str = ""
    applies_to: tuple[str, ...] = ("*",)
    overlay: pathlib.Path | None = None
    context_append: str = ""
    prompt_append: str = ""
    from_oracle: FromOracle | None = None
    help_markers: tuple[str, ...] = ()
    script_markers: tuple[str, ...] = ()
    artefacts: dict[str, str] = Field(default_factory=dict)
    root: pathlib.Path

    def applies(self, task_id: str) -> bool:
        return "*" in self.applies_to or task_id in self.applies_to

    def overlay_dir(self, task_id: str) -> pathlib.Path | None:
        if self.overlay is None:
            return None
        d = self.root / self.overlay / task_id
        return d if d.is_dir() else None

    def generated_files(self, task: Task) -> list[tuple[pathlib.Path, str]]:
        return [] if self.from_oracle is None else self.from_oracle.files(task)

    def prompt_suffix(self, task: Task) -> str:
        """``prompt_append``, with ``{generated}`` replaced by the generated paths."""
        paths = ", ".join(dest for _, dest in self.generated_files(task))
        return self.prompt_append.replace("{generated}", paths)

    def artefact_versions(self, task: Task) -> dict[str, str]:
        """Every artefact this treatment gives the agent on this task, with its version.

        Pinned artefacts come from config; the overlay (if any) is recorded as
        a content digest so that editing a how-to or skill changes the record
        even if nobody bumps ``version``.
        """
        out = dict(self.artefacts)
        d = self.overlay_dir(task.id)
        if d is not None:
            out["overlay"] = tree_digest(d)
        for source, dest in self.generated_files(task):
            out[f"generated:{dest}"] = text_digest(self.from_oracle.text(task, source))
        return dict(sorted(out.items()))


def file_digest(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def tree_digest(root: pathlib.Path) -> str:
    """A content hash over relative paths and bytes; independent of mtimes and order."""
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        if "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return "sha256:" + h.hexdigest()[:16]


# --- scoring, tags, plan -----------------------------------------------------


class StaticToolConfig(_Strict):
    charmlint: str


class ScoringConfig(_Strict):
    rubric_version: str
    judge_models: tuple[str, ...]
    static: StaticToolConfig

    @field_validator("judge_models")
    @classmethod
    def _sorted_unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("judge_models has duplicates")
        return tuple(sorted(v))


class FailureTag(_Strict):
    name: str
    tiers: tuple[ScoringTier, ...]
    description: str
    deprecated: bool = False


class Caps(_Strict):
    max_turns: int = 150
    timeout_minutes: int = 25
    max_cost_usd: float | None = None


class Anchor(_Strict):
    task: str
    harness: str
    model: str
    treatment: str


class GhaConfig(_Strict):
    """The GitHub Actions eval (eval.yml): how many cells one workflow run may hold."""

    max_cells: int = Field(
        default=24,
        ge=1,
        le=256,
        description=(
            "Hard cap: `wof plan --matrix-json` refuses a plan with more cells than this, "
            "whatever the sample or the manual inputs say (GHA's own matrix limit is 256)."
        ),
    )
    scheduled_sample: int = Field(
        default=8,
        ge=1,
        description=(
            "Cells the weekly scheduled run picks, from the feature tasks only (new-charm "
            "briefs have their own monthly slot; anchors run monthly)."
        ),
    )
    new_charm_sample: int = Field(
        default=2,
        ge=1,
        description=(
            "Cells the monthly new-charm run picks, from the new-charm briefs only "
            "(decided 2026-11-25: briefs leave the weekly sample; PLAN §6 budgets 2-4 a month)."
        ),
    )


class PlanConfig(_Strict):
    reps: int = 3
    caps: Caps = Caps()
    anchors: tuple[Anchor, ...] = ()
    gha: GhaConfig = GhaConfig()


# --- the whole thing ---------------------------------------------------------


class ConfigError(ValueError):
    pass


class Config(_Strict):
    tasks: dict[str, Task]
    models: dict[str, Model]
    harnesses: dict[str, Harness]
    treatments: dict[str, Treatment]
    scoring: ScoringConfig
    failure_tags: dict[str, FailureTag]
    plan: PlanConfig

    @model_validator(mode="after")
    def _cross_references(self) -> Config:
        for t in self.treatments.values():
            for task_id in t.applies_to:
                if task_id != "*" and task_id not in self.tasks:
                    raise ValueError(f"treatment {t.id!r} applies to unknown task {task_id!r}")
            if t.from_oracle is not None:
                for task_id in (*t.from_oracle.applies_to, *t.from_oracle.task_replace):
                    if task_id != "*" and task_id not in self.tasks:
                        raise ValueError(
                            f"treatment {t.id!r} generates from unknown task {task_id!r}"
                        )
                if "{generated}" not in t.prompt_append:
                    raise ValueError(
                        f"treatment {t.id!r} generates files but its prompt_append "
                        "doesn't name them ({generated})"
                    )
        for a in self.plan.anchors:
            for kind, key, table in (
                ("task", a.task, self.tasks),
                ("harness", a.harness, self.harnesses),
                ("model", a.model, self.models),
                ("treatment", a.treatment, self.treatments),
            ):
                if key not in table:
                    raise ValueError(f"anchor names unknown {kind} {key!r}")
        return self

    def tags_for(self, tier: str) -> set[str]:
        return {name for name, tag in self.failure_tags.items() if tier in tag.tiers}


def _read(root: pathlib.Path, name: str) -> dict:
    p = root / "config" / name
    if not p.exists():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def _merge(into: dict, new: dict, what: str, root: pathlib.Path) -> None:
    for key, value in new.items():
        if key in into:
            raise ConfigError(f"duplicate {what} {key!r} (again in {root})")
        into[key] = value


def default_roots() -> list[pathlib.Path]:
    roots = [PACKAGE_ROOT]
    extra = os.environ.get(PRIVATE_ROOT_ENV) or os.environ.get(LEGACY_PRIVATE_ROOT_ENV, "")
    roots += [pathlib.Path(p) for p in extra.split(os.pathsep) if p]
    return roots


def load_config(roots: list[pathlib.Path] | None = None) -> Config:
    """Load and merge config trees; the first tree must hold scoring and plan config."""
    roots = [pathlib.Path(r).resolve() for r in (roots or default_roots())]
    tasks: dict = {}
    models: dict = {}
    harnesses: dict = {}
    treatments: dict = {}
    tags: dict = {}
    for root in roots:
        raw_tasks = _read(root, "tasks.toml").get("tasks", {})
        _merge(
            tasks,
            {k: {**v, "id": k, "root": root} for k, v in raw_tasks.items()},
            "task",
            root,
        )
        raw_models = _read(root, "models.toml").get("models", {})
        _merge(models, {k: {**v, "key": k} for k, v in raw_models.items()}, "model", root)
        raw_h = _read(root, "harnesses.toml").get("harnesses", {})
        _merge(harnesses, {k: {**v, "name": k} for k, v in raw_h.items()}, "harness", root)
        tr = _read(root, "treatments.toml")
        common = tr.get("common_artefacts", {})
        _merge(
            treatments,
            {
                k: {
                    **v,
                    "id": k,
                    "root": root,
                    "artefacts": {**common, **v.get("artefacts", {})},
                }
                for k, v in tr.get("treatments", {}).items()
            },
            "treatment",
            root,
        )
        raw_tags = _read(root, "failure_tags.toml").get("tags", {})
        _merge(tags, {k: {**v, "name": k} for k, v in raw_tags.items()}, "failure tag", root)
    scoring = _read(roots[0], "scoring.toml")
    plan = _read(roots[0], "plan.toml")
    if not scoring:
        raise ConfigError(f"no config/scoring.toml in {roots[0]}")
    try:
        return Config(
            tasks=tasks,
            models=models,
            harnesses=harnesses,
            treatments=treatments,
            scoring=scoring,
            failure_tags=tags,
            plan=plan,
        )
    except ValueError as e:
        raise ConfigError(str(e)) from e
