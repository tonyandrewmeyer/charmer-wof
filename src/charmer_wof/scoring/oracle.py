"""Hidden-oracle tier: the gold charm's own tests for the feature.

Ported from §9's ``score.py``: the oracle files are copied into the
workspace after the agent finishes, the whole unit suite runs with JUnit
output, and oracle tests are counted separately from the agent's own. The
files are removed again afterwards whatever happens.

This needs the charm's Python environment (``uv`` resolves it from PyPI) but
no Juju, so it can run anywhere the static tier can.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import xml.etree.ElementTree as ET

from ..config import OracleSpec, Task
from ..schema import OracleOutcome
from . import _proc

JUNIT = ".wof/oracle-junit.xml"


def parse_junit(xml_text: str, marker: str) -> tuple[dict[str, str], dict[str, str], bool]:
    """(oracle tests, agent tests, collection error seen) from JUnit XML."""
    oracle: dict[str, str] = {}
    agent: dict[str, str] = {}
    collection_error = False
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        failed = case.find("failure") is not None or case.find("error") is not None
        status = "skip" if case.find("skipped") is not None else ("fail" if failed else "pass")
        if failed and (not name or (case.find("error") is not None and not classname)):
            collection_error = True
        bucket = oracle if marker in classname or marker in name else agent
        bucket[f"{classname}::{name}"] = status
    return oracle, agent, collection_error


def outcome_from_junit(xml_text: str, spec: OracleSpec) -> OracleOutcome:
    oracle, agent, collection_error = parse_junit(xml_text, spec.marker)
    key = {}
    for test in spec.key_tests:
        hits = [s for k, s in oracle.items() if k.endswith("::" + test)]
        key[test] = hits[0] if hits else "missing"
    passed = sum(1 for s in oracle.values() if s == "pass")
    tags = ["oracle-collection-error"] if collection_error or not oracle else []
    if oracle and not collection_error and spec.key_test and key.get(spec.key_test) != "pass":
        tags.append("key-oracle-test-failed")
    return OracleOutcome(
        status="pass" if oracle and passed == len(oracle) else "fail",
        failure_tags=tags,
        passed=passed,
        total=len(oracle),
        agent_tests_passed=sum(1 for s in agent.values() if s == "pass"),
        agent_tests_total=len(agent),
        key_tests=key,
        tests=oracle,
        metrics={"full_pass": bool(oracle) and passed == len(oracle)},
    )


def run_oracle(
    workspace: pathlib.Path, task: Task, runner: _proc.Runner = _proc.run
) -> OracleOutcome:
    spec = task.oracle
    if spec is None:
        return OracleOutcome(status="not_run", reason="task has no hidden oracle")
    junit = workspace / JUNIT
    junit.parent.mkdir(exist_ok=True)
    junit.unlink(missing_ok=True)
    installed = []
    try:
        for f in spec.files:
            target = workspace / spec.install_dir / pathlib.Path(f).name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(task.path(f), target)
            installed.append(target)
        try:
            proc = runner(
                [*spec.command, f"--junitxml={junit}", "-o", "junit_family=xunit2"],
                workspace,
                env=_proc.charm_env(task.python),
            )
        except subprocess.TimeoutExpired:
            return OracleOutcome(status="error", reason="oracle timed out")
    finally:
        for target in installed:
            target.unlink(missing_ok=True)
    if not junit.exists():
        return OracleOutcome(
            status="error",
            reason="no JUnit output (uv or pytest failed to start)",
            details={"stderr_tail": (proc.stderr or "")[-1500:]},
        )
    return outcome_from_junit(junit.read_text(), spec)
