"""Fetch a task's gold charm at its pinned SHA (the GHA prep job's checkout).

A shallow fetch of exactly ``gold_sha`` into an empty repo, which is all
``workspace.prepare`` needs (it ``git archive``s that commit). For a task with
``gold_private`` the token comes from an environment variable (on GHA,
``HEDGEDOC_GOLD_TOKEN``, set only on the prep job's fetch step) and reaches git
through ``GIT_CONFIG_*`` variables, never the command line or a file.
"""

from __future__ import annotations

import base64
import os
import pathlib
import subprocess

from .config import Task

TOKEN_ENV = "WOF_GOLD_TOKEN"


class GoldError(RuntimeError):
    pass


class GoldTokenMissing(GoldError):
    pass


def _auth_env(repo_url: str, token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    host = repo_url.split("://", 1)[-1].split("/", 1)[0]
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def fetch(task: Task, dest: pathlib.Path, *, env: dict[str, str] | None = None) -> pathlib.Path:
    """Fetch ``task.gold_sha`` from ``task.gold_repo`` into ``dest`` (created, must be empty)."""
    if task.kind != "feature" or not task.gold_repo or not task.gold_sha:
        raise GoldError(f"task {task.id!r} has no gold to fetch")
    source = dict(os.environ if env is None else env)
    run_env = {**source, "GIT_TERMINAL_PROMPT": "0"}
    if task.gold_private:
        token = source.get(TOKEN_ENV, "")
        if not token:
            raise GoldTokenMissing(
                f"task {task.id!r}: the gold repo is private and ${TOKEN_ENV} is empty"
            )
        run_env.update(_auth_env(task.gold_repo, token))
    if dest.exists() and any(dest.iterdir()):
        raise GoldError(f"{dest} exists and is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", task.gold_repo],
        ["git", "fetch", "-q", "--depth", "1", "--no-tags", "origin", task.gold_sha],
        ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
    ):
        proc = subprocess.run(cmd, cwd=dest, env=run_env, capture_output=True, text=True)
        if proc.returncode != 0:
            # git doesn't echo extraheader values, but be sure nothing token-like leaks.
            err = proc.stderr.strip()
            if task.gold_private and source.get(TOKEN_ENV):
                err = err.replace(source[TOKEN_ENV], "***")
            raise GoldError(f"{' '.join(cmd[:2])} failed for {task.gold_repo}: {err}")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != task.gold_sha:
        raise GoldError(f"fetched {head}, expected {task.gold_sha}")
    return dest
