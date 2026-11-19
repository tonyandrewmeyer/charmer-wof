from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import subprocess
import textwrap

import pytest

from charmer_wof.config import PACKAGE_ROOT, Config, load_config
from charmer_wof.planner import resolve
from charmer_wof.record import build_record, stub_tiers
from charmer_wof.schema import JudgeVerdict, RunArtefacts, RunRecord

CELL = "hedgedoc-ingress/pi/deepseek-v4-pro/howto/0"


@pytest.fixture
def config() -> Config:
    return load_config([PACKAGE_ROOT])


@pytest.fixture
def record(config: Config) -> RunRecord:
    """A record with something in every tier, as a scored run would have."""
    tiers = stub_tiers("x")
    tiers.static = tiers.static.model_copy(
        update={"status": "fail", "reason": None, "failure_tags": ["charm-lib-misnamed"]}
    )
    tiers.oracle = tiers.oracle.model_copy(
        update={
            "status": "fail",
            "reason": None,
            "passed": 13,
            "total": 14,
            "key_tests": {"test_oracle_requirer_asks_traefik_to_strip_the_prefix": "fail"},
            "tests": {"tests.unit.test__oracle_ingress::test_oracle_a": "pass"},
            "details": {"stderr_tail": "secret oracle output"},
        }
    )
    tiers.judge = tiers.judge.model_copy(
        update={
            "status": "pass",
            "reason": None,
            "verdicts": [
                JudgeVerdict(
                    model="anthropic/claude-sonnet-5.5",
                    scores={"correctness": 6},
                    total=14,
                    max_total=20,
                    failure_tags=["prefix-not-stripped"],
                    notes="forgot strip_prefix on the ingress requirer",
                )
            ],
        }
    )
    return build_record(
        config,
        resolve(config, CELL),
        tiers,
        harness_version="1.2.3",
        provider="DeepInfra",
        artefacts=RunArtefacts(
            prompt="Add ingress...",
            agent_diff="diff --git a/x b/x",
            diff_stats={"files_changed": 2, "insertions": 10, "deletions": 1},
        ),
        run_id="r1",
        now=dt.datetime(2026, 11, 9, tzinfo=dt.UTC),
    )


def held_out(record: RunRecord) -> RunRecord:
    return RunRecord.model_validate({**record.model_dump(), "held_out": True})


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


@pytest.fixture
def private_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A held-out config tree, as the private repo would hold it."""
    root = tmp_path / "private"
    write(root / "tasks" / "secret" / "prompt.md", "Do the secret thing.\n")
    write(
        root / "config" / "tasks.toml",
        """
        [tasks.secret-new-charm]
        version = "1"
        kind = "new-charm"
        held_out = true
        prompt = "tasks/secret/prompt.md"
        """,
    )
    return root


def git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def copy_package_tree(dest: pathlib.Path) -> pathlib.Path:
    shutil.copytree(PACKAGE_ROOT / "config", dest / "config")
    return dest
