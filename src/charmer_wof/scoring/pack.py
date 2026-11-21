"""Pack: does ``charmcraft pack`` build the agent's charm? (PLAN §4 tier 1.)

A sub-check of the static tier, ``details.checks.pack``. It packs a copy of
the workspace (so build output never reaches the agent's diff, which is taken
before this runs anyway), with whatever build backend charmcraft is set up
for: LXD on the GHA score job (eval.yml installs it with concierge), LXD or
Multipass on a developer machine. A charm whose ``charmcraft.yaml`` only
parses as YAML can still fail here: the 2026-11-17 Gotify run's
``type: kubernetes`` would have.

Outcomes:

- ``pass``: charmcraft exited 0 and left at least one ``.charm``.
- ``fail``: charmcraft exited non-zero (or left no ``.charm``): the
  ``pack-build-failed`` tag. The tail of its output is kept.
- ``error``: charmcraft couldn't build anything because of the machine, not
  the charm (no build backend, the provider failed to start, the pack timed
  out). No tag; the tier doesn't fail on it.
- ``not_run``: charmcraft isn't installed, or pack was turned off.

The build happens in the base the charm's ``charmcraft.yaml`` names, inside
the backend's container, so a charm's Python comes from that base
(``ubuntu@26.04`` gives alertmanager its 3.14), not from the runner.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
import time
from typing import Any

from . import _proc

TAG = "pack-build-failed"
NOT_INSTALLED = "charmcraft not installed"
TIMEOUT_SECONDS = 45 * 60
OUTPUT_TAIL_LINES = 40

# Copied into the pack directory: the workspace minus the harness's files,
# git, virtualenvs and earlier build output.
_IGNORE = shutil.ignore_patterns(
    ".git", ".wof", ".cwe", ".venv", ".tox", "__pycache__", "*.charm", "build", ".ruff_cache"
)

# Output that means the machine couldn't build, not that the charm is wrong.
# Matched case-insensitively against charmcraft's output.
PROVIDER_ERRORS = (
    "lxd is not installed",
    "lxd requires additional permissions",
    "failed to install lxd",
    "multipass is not installed",
    "unable to connect to lxd",
    "failed to launch",
    "craft_providers",
    "no space left on device",
)


def _tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def charmcraft_version(runner: _proc.Runner = _proc.run, cwd: pathlib.Path | None = None) -> str:
    try:
        proc = runner(["charmcraft", "version"], cwd or pathlib.Path.cwd(), timeout=60)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (proc.stdout or "").strip() or "unknown"


def _make_git_repo(project: pathlib.Path) -> None:
    """Make the pack copy a git repository with one commit.

    Some charms' ``override-build`` runs git (alertmanager-k8s writes ``git
    describe --always`` into a version file), and the copy leaves the
    workspace's ``.git`` out (prepared workspaces have none anyway), so the
    build failed with "not a git repository" whatever the agent wrote. A
    throwaway repository makes those scriptlets work; the version they record
    is meaningless, which doesn't matter to whether the charm packs. Skipped
    if git isn't installed.
    """
    git = shutil.which("git")
    if git is None:
        return
    env = {
        "GIT_AUTHOR_NAME": "wof",
        "GIT_AUTHOR_EMAIL": "wof@invalid",
        "GIT_COMMITTER_NAME": "wof",
        "GIT_COMMITTER_EMAIL": "wof@invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin",
    }
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "--no-gpg-sign", "-m", "wof pack"],
    ):
        subprocess.run([git, *args], cwd=project, env=env, capture_output=True, check=False)


def run_pack(
    workspace: pathlib.Path,
    *,
    runner: _proc.Runner = _proc.run,
    which=shutil.which,
    timeout: int = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Pack a copy of the workspace; returns the sub-check dict (``status``, ``reason``, ...)."""
    if which("charmcraft") is None:
        return {"status": "not_run", "reason": NOT_INSTALLED}
    version = charmcraft_version(runner, workspace)
    with tempfile.TemporaryDirectory(prefix="wof-pack-") as tmp:
        project = pathlib.Path(tmp) / "charm"
        shutil.copytree(workspace, project, ignore=_IGNORE, symlinks=True)
        _make_git_repo(project)
        started = time.monotonic()
        try:
            proc = runner(["charmcraft", "pack", "--verbosity", "brief"], project, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "reason": f"charmcraft pack timed out after {timeout} s",
                "charmcraft_version": version,
                "seconds": round(time.monotonic() - started, 1),
            }
        except OSError as e:
            return {"status": "error", "reason": f"charmcraft did not run: {e}"}
        seconds = round(time.monotonic() - started, 1)
        charms = sorted(p.name for p in project.glob("*.charm"))
    output = (proc.stdout or "") + (proc.stderr or "")
    out: dict[str, Any] = {
        "charmcraft_version": version,
        "seconds": seconds,
        "returncode": proc.returncode,
        "charms": charms,
    }
    if proc.returncode == 0 and charms:
        return {"status": "pass", "reason": None, **out}
    lowered = output.lower()
    provider = next((m for m in PROVIDER_ERRORS if m in lowered), None)
    if provider is not None:
        return {
            "status": "error",
            "reason": f"the build backend failed ({provider!r}), not the charm",
            **out,
            "output_tail": _tail(output),
        }
    reason = (
        f"charmcraft pack exited {proc.returncode}"
        if proc.returncode
        else "charmcraft pack exited 0 but left no .charm"
    )
    return {"status": "fail", "reason": reason, **out, "output_tail": _tail(output)}
