"""Hidden-oracle tier: the gold charm's own tests for the feature.

Ported from §9's ``score.py``: the oracle files are copied into the
workspace after the agent finishes, the whole unit suite runs with JUnit
output, and oracle tests are counted separately from the agent's own. The
files are removed again afterwards whatever happens.

This needs the charm's Python environment (``uv`` resolves it from PyPI) but
no Juju, so it can run anywhere the static tier can.

Failure output (since 2026-11-26): ``details.failures`` keeps, for each failing
oracle test, the exception type, the first line of its message, and a few
lines of pytest's report (the ``>`` line that failed, the ``E`` lines and the
``file:line`` locations), and ``details.collection_errors`` the same for a
module that couldn't be imported. Capped per test (``FAILURE_LINES``,
``FAILURE_LINE_CHARS``) and per record (``FAILURES_MAX`` tests,
``FAILURE_CHARS_MAX`` characters of lines), with workspace and machine paths
taken out, because records are public. For a task whose gold is private
(``gold_private``), pytest's report quotes the gold's own source in every frame
that isn't the agent's, and the frames can't be told apart reliably from the
report alone (an agent's edit and the gold's code share files), so those tasks
keep the exception type and message only. The agent's own tests aren't kept.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from typing import Any

from ..config import OracleSpec, Task
from ..schema import OracleOutcome
from . import _proc

JUNIT = ".wof/oracle-junit.xml"

FAILURE_LINES = 12
FAILURE_LINE_CHARS = 200
FAILURE_MESSAGE_CHARS = 300
FAILURES_MAX = 8
FAILURE_CHARS_MAX = 8000

_LOCATION = re.compile(r"^\S+:\d+: ")
_TYPED = re.compile(r"^([A-Za-z_][\w.]*):(?: (.*)|$)")
_MACHINE_PATH = re.compile(
    r"(?<![\w.~-])/(?:home|tmp|root|runner|private|Users|__w|github)"
    r"(?:/[^\s:'\"()\[\],<>]+)+"
)


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


def scrub(text: str, workspace: pathlib.Path | None) -> str:
    """Take the workspace's path out (relative paths stay), and any other machine path."""
    if workspace is not None:
        for root in {str(workspace), str(workspace.resolve())}:
            text = text.replace(root.rstrip("/") + "/", "")
    return _MACHINE_PATH.sub(lambda m: "<path>/" + m.group(0).rsplit("/", 1)[-1], text)


def _type_and_message(message: str, text: str) -> tuple[str | None, str]:
    """The exception type and the first line of its message.

    From JUnit's ``message`` attribute, or for a collection error (whose
    message is just "collection failure") from the last ``E   Type: ...`` line.
    """
    first = (message or "").splitlines()[0].strip() if message else ""
    if first in ("", "collection failure"):
        e_lines = [ln[1:].strip() for ln in text.splitlines() if ln.startswith("E ")]
        typed = [ln for ln in e_lines if _TYPED.match(ln)]
        first = typed[-1] if typed else (e_lines[-1] if e_lines else first)
    m = _TYPED.match(first)
    if m and not m.group(1)[0].isdigit():
        return m.group(1), (m.group(2) or "").strip()
    return None, first


def _report_lines(text: str) -> list[str]:
    """The lines of pytest's report worth keeping: what failed, where, and why."""
    out: list[str] = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        stripped = ln.rstrip()
        if stripped.startswith(">") or (stripped.startswith("E ") and stripped[1:].strip()):
            out.append(stripped)
        elif _LOCATION.match(stripped):
            out.append(stripped)
            nxt = lines[i + 1].rstrip() if i + 1 < len(lines) else ""
            if nxt.startswith("    ") and nxt.strip() and set(nxt.strip()) != {"^"}:
                out.append(nxt)
    return [x[:FAILURE_LINE_CHARS] for x in out[-FAILURE_LINES:]]


def failure_details(
    xml_text: str,
    marker: str,
    *,
    workspace: pathlib.Path | None = None,
    private: bool = False,
) -> dict[str, Any]:
    """``failures`` and ``collection_errors`` for the record (see the module docstring)."""
    failures: dict[str, dict[str, Any]] = {}
    collection: dict[str, dict[str, Any]] = {}
    omitted = 0
    budget = FAILURE_CHARS_MAX
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is None:
            continue
        classname, name = case.get("classname", ""), case.get("name", "")
        is_collection = not name or (bad.tag == "error" and not classname)
        if not is_collection and marker not in classname and marker not in name:
            continue  # the agent's own test
        if len(failures) + len(collection) >= FAILURES_MAX:
            omitted += 1
            continue
        text = scrub(bad.text or "", workspace)
        kind, message = _type_and_message(scrub(bad.get("message", ""), workspace), text)
        entry: dict[str, Any] = {"type": kind, "message": message[:FAILURE_MESSAGE_CHARS]}
        if not private:
            lines = _report_lines(text)
            while lines and sum(len(x) for x in lines) > budget:
                lines.pop(0)
            budget -= sum(len(x) for x in lines)
            entry["lines"] = lines
        target = collection if is_collection else failures
        target[f"{classname}::{name}"] = entry
    out: dict[str, Any] = {}
    if failures:
        out["failures"] = failures
    if collection:
        out["collection_errors"] = collection
    if omitted:
        out["failures_omitted"] = omitted
    if out and private:
        out["failure_output"] = "type and message only (private gold)"
    return out


def outcome_from_junit(
    xml_text: str,
    spec: OracleSpec,
    *,
    workspace: pathlib.Path | None = None,
    private: bool = False,
) -> OracleOutcome:
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
        details=failure_details(xml_text, spec.marker, workspace=workspace, private=private),
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
    return outcome_from_junit(
        junit.read_text(), spec, workspace=workspace, private=task.gold_private
    )
