"""Prepare a run's workspace from a task and treatment, and diff the agent's work.

The workspace is a fresh git repo:

1. the gold charm's tree at the task's pinned SHA (from a local checkout);
2. the task's removal diff applied, and ``remove_paths`` deleted;
3. the treatment's overlay copied in, its context line appended, and any
   files it generates from the oracle written (every oracle file, renamed);
4. committed as the base; its SHA is what the agent's diff is taken against.

Harness files go under ``.wof/``, which is excluded from git so it never
shows up in the agent's diff. The hidden oracle is *not* in the workspace;
the oracle tier installs it after the agent finishes.
"""

from __future__ import annotations

import dataclasses
import io
import pathlib
import shutil
import subprocess
import tarfile

from .config import Task, Treatment

HARNESS_DIR = ".wof"
BUILD_DROPPINGS = (
    "__pycache__/",
    "*.py[cod]",
    ".coverage",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".tox/",
    ".venv/",
    "*.charm",
)
_GIT_ID = ["-c", "user.name=wof", "-c", "user.email=wof@localhost", "-c", "commit.gpgsign=false"]
DIFF_EXCLUDES = (":!lib", ":!uv.lock", f":!{HARNESS_DIR}")


class WorkspaceError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Workspace:
    path: pathlib.Path
    base_sha: str
    prompt: str


def _git(args: list[str], cwd: pathlib.Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_GIT_ID, *args], cwd=cwd, capture_output=True, check=True, **kw)


def build_prompt(task: Task, treatment: Treatment) -> str:
    return task.prompt_text() + treatment.prompt_suffix(task)


def prepare(
    task: Task, treatment: Treatment, gold_checkout: pathlib.Path | None, dest: pathlib.Path
) -> Workspace:
    if not treatment.applies(task.id):
        raise WorkspaceError(f"treatment {treatment.id!r} does not apply to {task.id!r}")
    if task.kind == "feature" and gold_checkout is None:
        raise WorkspaceError(f"feature task {task.id!r} needs a gold checkout")
    if dest.exists() and any(dest.iterdir()):
        raise WorkspaceError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)

    # 1. The gold tree at the pinned SHA, without its history; or, for a new
    # charm, an empty tree holding only the task's starter files.
    if task.kind == "new-charm":
        if task.starter is not None:
            shutil.copytree(task.path(task.starter), dest, dirs_exist_ok=True)
    else:
        try:
            tree = f"{task.gold_sha}:{task.gold_subdir}" if task.gold_subdir else task.gold_sha
            tar = _git(["archive", "--format=tar", tree], gold_checkout).stdout
        except subprocess.CalledProcessError as e:
            raise WorkspaceError(
                f"{gold_checkout} does not have {task.gold_sha}: {e.stderr.decode().strip()}"
            ) from e
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            tf.extractall(dest, filter="data")
    _git(["init", "-q"], dest)

    # 1b. A gold that doesn't commit lib/ gets its declared libs from Charmhub,
    # before the removal (so remove_paths can take the feature's lib out again).
    if task.fetch_libs:
        fetch_libs(dest)

    # 2. Remove the feature.
    if task.removal_diff is not None:
        diff = task.path(task.removal_diff).resolve()
        try:
            _git(["apply", f"-p{task.removal_strip}", str(diff)], dest)
        except subprocess.CalledProcessError as e:
            raise WorkspaceError(
                f"removal diff does not apply at {task.gold_sha}: {e.stderr.decode().strip()}"
            ) from e
    for rel in task.remove_paths:
        target = dest / rel
        if not target.exists():
            raise WorkspaceError(f"remove_paths entry {rel!r} is not in the gold tree")
        shutil.rmtree(target) if target.is_dir() else target.unlink()

    # 3. The treatment.
    overlay = treatment.overlay_dir(task.id)
    if overlay is not None:
        shutil.copytree(
            overlay, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
        )
    if treatment.context_append:
        (dest / task.context_file).parent.mkdir(parents=True, exist_ok=True)
        with (dest / task.context_file).open("a") as f:
            f.write(treatment.context_append)
    for source, rel in treatment.generated_files(task):
        out = dest / rel
        if out.exists():
            raise WorkspaceError(f"generated file {rel!r} would overwrite a file in the base")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(treatment.from_oracle.text(task, source))

    # 4. Commit the base; keep harness files out of the diff.
    prompt = build_prompt(task, treatment)
    (dest / HARNESS_DIR).mkdir()
    (dest / HARNESS_DIR / "prompt.md").write_text(prompt + "\n")
    with (dest / ".git" / "info" / "exclude").open("a") as f:
        f.write(f"{HARNESS_DIR}/\n")
        # Build and test droppings, whatever the gold's own .gitignore says (a
        # monorepo subdirectory has none of its own; PLAN §7 2026-11-17).
        f.write("".join(f"{pattern}\n" for pattern in BUILD_DROPPINGS))
    _git(["add", "-A"], dest)
    _git(
        [
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"base: {task.id} v{task.version} + {treatment.id}",
        ],
        dest,
    )
    base_sha = _git(["rev-parse", "HEAD"], dest, text=True).stdout.strip()
    (dest / HARNESS_DIR / "base-sha").write_text(base_sha + "\n")
    return Workspace(dest, base_sha, prompt)


def fetch_libs(dest: pathlib.Path, charmcraft: str = "charmcraft") -> None:
    """``charmcraft fetch-libs`` in ``dest`` (a task with ``fetch_libs``); refuses without it."""
    if shutil.which(charmcraft) is None:
        raise WorkspaceError(
            "the task needs `charmcraft fetch-libs` and charmcraft isn't installed"
        )
    proc = subprocess.run(
        [charmcraft, "fetch-libs"], cwd=dest, capture_output=True, text=True, timeout=600
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            f"charmcraft fetch-libs failed: {(proc.stderr or proc.stdout)[-2000:]}"
        )


def load(path: pathlib.Path) -> Workspace:
    """A workspace ``prepare`` built earlier (in another job, on GHA), from its ``.wof/``."""
    harness = path / HARNESS_DIR
    try:
        base_sha = (harness / "base-sha").read_text().strip()
        prompt = (harness / "prompt.md").read_text().removesuffix("\n")
    except FileNotFoundError as e:
        raise WorkspaceError(f"{path} is not a prepared workspace: {e.filename} missing") from e
    return Workspace(path, base_sha, prompt)


def snapshot(workspace: pathlib.Path) -> None:
    """Commit whatever the agent left, so the diff and the oracle see the same tree."""
    _git(["add", "-A"], workspace)
    _git(["commit", "-q", "--allow-empty", "-m", "agent result"], workspace)


# Paths whose changes don't count as the agent editing anything: harness files,
# and the lockfile `uv run` may rewrite as a side effect.
CHURN = ("uv.lock", f"{HARNESS_DIR}/")
VENDORED = "lib/"


def changed_paths(workspace: pathlib.Path, base_sha: str) -> list[str]:
    """Every path the agent changed against the prepared base, vendored libs included.

    Unlike ``agent_diff`` this keeps ``lib/`` (so "fetched the lib and nothing
    else" is visible) and drops only lockfile churn and harness files. Call it
    after ``snapshot``.
    """
    out = _git(["diff", "--name-only", "--no-renames", base_sha, "HEAD"], workspace, text=True)
    return sorted(
        p
        for p in out.stdout.splitlines()
        if p and not any(p == c or p.startswith(c) for c in CHURN)
    )


def only_vendored(changed: list[str]) -> bool:
    """The agent changed something, and every change is under ``lib/``."""
    return bool(changed) and all(p.startswith(VENDORED) for p in changed)


def agent_diff(workspace: pathlib.Path, base_sha: str) -> tuple[str, dict[str, int]]:
    """The agent's diff (vendored libs and lockfiles excluded) and its stats."""
    diff = _git(["diff", base_sha, "HEAD", "--", ".", *DIFF_EXCLUDES], workspace, text=True)
    numstat = _git(
        ["diff", "--numstat", base_sha, "HEAD", "--", ".", *DIFF_EXCLUDES], workspace, text=True
    )
    files = ins = dels = 0
    for line in numstat.stdout.splitlines():
        a, b, _ = line.split("\t", 2)
        files += 1
        ins += int(a) if a.isdigit() else 0
        dels += int(b) if b.isdigit() else 0
    return diff.stdout, {"files_changed": files, "insertions": ins, "deletions": dels}
