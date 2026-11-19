from __future__ import annotations

import os
import pathlib
import subprocess
from collections.abc import Callable, Mapping

Runner = Callable[..., subprocess.CompletedProcess]

# Variables that would make `uv run` inside a charm use the harness's own
# environment instead of the charm's.
_LEAKY = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME")


def charm_env(python: str | None) -> dict[str, str]:
    """The environment for commands run inside a charm workspace.

    Drops anything that points uv at the harness's virtualenv, and pins the
    charm's Python (the task's ``python``) so the venv uv builds matches the
    base the charm runs on, not whatever this machine defaults to.
    """
    env = {k: v for k, v in os.environ.items() if k not in _LEAKY}
    if python:
        env["UV_PYTHON"] = python
    return env


def run(
    cmd: list[str],
    cwd: pathlib.Path,
    timeout: int = 600,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
