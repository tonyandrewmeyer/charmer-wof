"""Static tier: checks that need no Juju and no model.

Ported from §9's ``score.py`` (ruff and charmlint counts as deltas against
the base, and the charmcraft.yaml checks), with the ingress-specific checks
generalised into each task's declared ``static_checks``.

Sub-checks, each ``pass``/``fail``/``error``/``not_run``:

- ``charmcraft_yaml``: parses, and is a mapping with a ``name``.
- ``endpoints``, ``charm_libs``, ``files_present``: the task's declared checks.
- ``ruff``: ``ruff check`` and ``ruff format --check`` under the charm's own
  environment and config (``uv run``), as §9 did. Fails if either count rose
  above the base.
- ``charmlint``: findings at the pinned version; fails if the total rose,
  not counting new findings the gold also has (since 2026-11-26, decided
  2026-11-25). A feature task's gold is linted where it's available (the prep
  job on GHA, ``wof gold-lint``) and its profile passed in as ``gold_lint``.
  Findings are matched by ``finding_key``: rule, file and message, with the
  charm's own directory taken out, but not the line number, which moves with
  every edit. The message carries what a finding is about (the action, the
  endpoint, the library), so ``ACTIONS-002`` on the task's action matches the
  gold's and on any other action doesn't. Counting is by multiset: a new
  finding is excused only while the gold has more of that key than the base.
  The details list what was excused; ``gold_baseline.status`` says whether a
  gold profile was used, and is never silently absent for a feature task.
- ``pack``: ``charmcraft pack`` on a copy of the workspace (``scoring.pack``),
  since scoring-code version 3. Needs charmcraft and a build backend (LXD on
  GHA); ``not_run`` where charmcraft isn't installed or pack is turned off,
  ``error`` when the backend rather than the charm failed. A failed pack is
  the ``pack-build-failed`` tag.
- ``only-vendored-libs-changed`` (a tag, not a sub-check): given the paths
  the agent changed (``workspace.changed_paths``), it changed something and
  all of it is under ``lib/``.
- ``typecheck``: ``not_run``. TODO: choose the checker and how it's pinned
  (the charm's own config vs a fixed pyright), then wire it in here.

The tier fails if any sub-check that ran failed, and errors only if none of
them could run.
"""

from __future__ import annotations

import collections
import contextlib
import json
import pathlib
import re
import shutil
from typing import Any

import yaml

from .. import workspace as workspace_mod
from ..config import StaticChecks
from ..schema import TierOutcome
from . import _proc
from . import pack as pack_mod

TYPECHECK_TODO = "type check not wired in yet (choose checker and pin; see HARNESS.md)"


def _check(status: str, reason: str | None = None, **values: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **values}


def load_charmcraft(workspace: pathlib.Path) -> tuple[dict | None, dict[str, Any]]:
    path = workspace / "charmcraft.yaml"
    if not path.exists():
        return None, _check("fail", "no charmcraft.yaml")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return None, _check("fail", f"charmcraft.yaml does not parse: {e}")
    if not isinstance(data, dict) or not data.get("name"):
        return None, _check("fail", "charmcraft.yaml is not a mapping with a name")
    return data, _check("pass")


def _norm(lib: str) -> str:
    return re.sub(r"[-_.]", "", lib.lower())


def declared_checks(
    data: dict | None, workspace: pathlib.Path, checks: StaticChecks
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The task's own checks against charmcraft.yaml and the tree; returns (checks, tags)."""
    out: dict[str, dict[str, Any]] = {}
    tags: list[str] = []
    if checks.endpoints:
        if data is None:
            out["endpoints"] = _check("not_run", "charmcraft.yaml unusable")
        else:
            missing = []
            for ep in checks.endpoints:
                declared = (data.get(ep.role) or {}).get(ep.name)
                if not isinstance(declared, dict) or declared.get("interface") != ep.interface:
                    missing.append(f"{ep.role}.{ep.name}")
            out["endpoints"] = _check("fail" if missing else "pass", None, missing=missing)
            if missing:
                tags.append("endpoint-missing")
    if checks.charm_libs:
        if data is None:
            out["charm_libs"] = _check("not_run", "charmcraft.yaml unusable")
        else:
            entries = [x for x in data.get("charm-libs") or [] if isinstance(x, dict)]
            missing, misnamed = [], []
            for want in checks.charm_libs:
                exact = [
                    x
                    for x in entries
                    if x.get("lib") == want.lib
                    and str(x.get("version", "")).startswith(want.version)
                ]
                if exact:
                    continue
                near = [x for x in entries if _norm(str(x.get("lib", ""))) == _norm(want.lib)]
                (misnamed if near else missing).append(want.lib)
            ok = not (missing or misnamed)
            out["charm_libs"] = _check(
                "pass" if ok else "fail", None, missing=missing, misnamed=misnamed
            )
            if missing:
                tags.append("charm-lib-missing")
            if misnamed:
                tags.append("charm-lib-misnamed")
    for kind, wanted, key, extra_file, tag in (
        ("actions", checks.actions, "actions", "actions.yaml", "action-missing"),
        (
            "config_options",
            checks.config_options,
            "config",
            "config.yaml",
            "config-option-missing",
        ),
    ):
        if not wanted:
            continue
        section = (data or {}).get(key) or {}
        declared = set(section.get("options") or {}) if key == "config" else set(section)
        path = workspace / extra_file
        if path.exists():
            with contextlib.suppress(yaml.YAMLError, AttributeError, TypeError):
                loaded = yaml.safe_load(path.read_text()) or {}
                declared |= set(loaded.get("options") or {}) if key == "config" else set(loaded)
        missing = [name for name in wanted if name not in declared]
        out[kind] = _check("fail" if missing else "pass", None, missing=missing)
        if missing:
            tags.append(tag)
    if checks.files_present:
        absent = [p for p in checks.files_present if not (workspace / p).exists()]
        out["files_present"] = _check("fail" if absent else "pass", None, absent=absent)
    return out, tags


def ruff_counts(
    workspace: pathlib.Path,
    runner: _proc.Runner = _proc.run,
    python: str | None = None,
    exclude: tuple[str, ...] = (),
) -> dict[str, Any]:
    env = _proc.charm_env(python)
    extra = [arg for path in exclude for arg in ("--extend-exclude", path)]
    try:
        proc = runner(
            [
                "uv",
                "run",
                "--no-progress",
                "ruff",
                "check",
                "--output-format",
                "json",
                *extra,
                ".",
            ],
            workspace,
            env=env,
        )
        findings = json.loads(proc.stdout or "[]")
        fmt = runner(
            ["uv", "run", "--no-progress", "ruff", "format", "--check", *extra, "."],
            workspace,
            env=env,
        )
    except (OSError, ValueError) as e:
        return {"error": f"ruff did not run: {e}"}
    if not isinstance(findings, list):
        return {"error": "ruff output was not a list"}
    unformatted = sum(
        1 for ln in (fmt.stdout or "").splitlines() if ln.startswith("Would reformat")
    )
    codes = sorted({f.get("code") for f in findings if f.get("code")})
    return {"check": len(findings), "format": unformatted, "codes": codes}


FINDINGS_KEPT = 50
MESSAGE_KEPT = 200


def finding_key(diag: dict[str, Any], charm_dir: str | None) -> tuple[str, str, str]:
    """``(rule, path, message)`` for matching one charmlint finding across trees.

    The charm's directory is taken out of the path and the message, so the
    same finding in the workspace, the base's worktree and the gold's copy has
    the same key. The line number isn't part of it: it moves with any edit.
    """
    prefix = (charm_dir or "").rstrip("/") + "/" if charm_dir else None
    path = str(diag.get("path") or "")
    message = str(diag.get("message") or "")
    if prefix:
        path = path.removeprefix(prefix)
        message = message.replace(prefix, "")
    return (str(diag.get("rule_id") or ""), path, message)


def _key_json(key: tuple[str, str, str]) -> dict[str, str]:
    rule, path, message = key
    return {"rule": rule, "path": path, "message": message[:MESSAGE_KEPT]}


def charmlint_counts(
    workspace: pathlib.Path, version: str, runner: _proc.Runner = _proc.run
) -> dict[str, Any]:
    try:
        proc = runner(
            ["uvx", f"charmlint@{version}", "--format", "json", "--no-colour", "."],
            workspace,
            env=_proc.charm_env(None),
        )
        data = json.loads(proc.stdout)
    except (OSError, ValueError) as e:
        return {"error": f"charmlint did not run: {e}"}
    diags = [d for d in data.get("diagnostics", []) if isinstance(d, dict)]
    rules = sorted(d.get("rule_id") for d in diags if d.get("rule_id"))
    return {
        "total": data.get("total"),
        "errors": data.get("errors"),
        "warnings": data.get("warnings"),
        "rules": rules,
        "findings": sorted(finding_key(d, data.get("charm_dir")) for d in diags),
    }


def _keys(profile: dict[str, Any] | None) -> collections.Counter[tuple[str, str, str]] | None:
    if not profile or profile.get("findings") is None:
        return None
    return collections.Counter(tuple(k) for k in profile["findings"])


def excused_by_gold(
    now: dict[str, Any], base: dict[str, Any] | None, gold: dict[str, Any] | None
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]] | None:
    """(excused, still new): the agent's new findings the gold has too, and the rest.

    New means in the result and not in the base (multiset, by ``finding_key``);
    a new finding is excused while the gold has more of its key than the base
    does. None when any of the three has no findings to compare.
    """
    a, b, g = _keys(now), _keys(base), _keys(gold)
    if a is None or b is None or g is None:
        return None
    new = a - b
    excused = new & (g - b)
    return sorted(excused.elements()), sorted((new - excused).elements())


def lint_profile(
    workspace: pathlib.Path,
    charmlint: str,
    runner: _proc.Runner = _proc.run,
    python: str | None = None,
    ruff_exclude: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The lint counts the deltas are taken against; compute once per prepared base."""
    return {
        "ruff": ruff_counts(workspace, runner, python, ruff_exclude),
        "charmlint": charmlint_counts(workspace, charmlint, runner),
    }


def _delta(now: int | None, base: int | None) -> int | None:
    if now is None or base is None:
        return None
    return now - base


def run_static(
    workspace: pathlib.Path,
    checks: StaticChecks,
    *,
    charmlint: str,
    baseline: dict[str, Any] | None = None,
    python: str | None = None,
    runner: _proc.Runner = _proc.run,
    which=shutil.which,
    changed: list[str] | None = None,
    pack: bool = True,
    gold_lint: dict[str, Any] | None = None,
) -> TierOutcome:
    """``gold_lint`` is the gold's charmlint profile (``gold_lint_profile``), for
    a feature task where the gold could be linted; with a ``baseline`` and
    without it, the charmlint check records ``gold_baseline: not_available``."""
    results: dict[str, dict[str, Any]] = {}
    tags: list[str] = []

    data, results["charmcraft_yaml"] = load_charmcraft(workspace)
    if results["charmcraft_yaml"]["status"] == "fail":
        tags.append("pack-failed")
    declared, declared_tags = declared_checks(data, workspace, checks)
    results.update(declared)
    tags += declared_tags

    lint = lint_profile(workspace, charmlint, runner, python, checks.ruff_exclude)
    base = baseline or {}
    ruff = lint["ruff"]
    if "error" in ruff:
        results["ruff"] = _check("error", ruff["error"])
        ruff_delta = fmt_delta = None
    else:
        ruff_delta = _delta(ruff["check"], base.get("ruff", {}).get("check"))
        fmt_delta = _delta(ruff["format"], base.get("ruff", {}).get("format"))
        worse = (ruff_delta or 0) > 0 or (fmt_delta or 0) > 0
        results["ruff"] = _check(
            "fail" if worse else "pass",
            None if baseline else "no baseline: counts only",
            check=ruff["check"],
            format=ruff["format"],
            codes=ruff["codes"],
        )
    cl = lint["charmlint"]
    cl_excused: int | None = None
    if "error" in cl:
        results["charmlint"] = _check("error", cl["error"])
        cl_delta = cl_counted = None
    else:
        cl_delta = _delta(cl["total"], base.get("charmlint", {}).get("total"))
        cl_counted = cl_delta
        extra: dict[str, Any] = {}
        reason = None if baseline else "no baseline: counts only"
        if baseline:
            gold_base = _gold_baseline(gold_lint)
            extra["gold_baseline"] = gold_base
            # Without a gold profile, every new finding is still new.
            split = excused_by_gold(cl, base.get("charmlint"), gold_lint or {"findings": []})
            if split is not None:
                excused, still_new = split
                extra["new"] = [_key_json(k) for k in still_new[:FINDINGS_KEPT]]
            if gold_lint is None:
                reason = "no gold baseline: findings the gold also has are not excused"
            elif split is None or _keys(gold_lint) is None:
                gold_base["status"] = "not_comparable"
                reason = "the gold or base profile has no findings list: nothing excused"
            else:
                cl_excused = len(excused)
                if cl_delta is not None:
                    cl_counted = cl_delta - cl_excused
                extra["excused"] = [_key_json(k) for k in excused[:FINDINGS_KEPT]]
        results["charmlint"] = _check(
            "fail" if (cl_counted or 0) > 0 else "pass",
            reason,
            **{k: cl[k] for k in ("total", "errors", "warnings", "rules")},
            **extra,
        )
    if (ruff_delta or 0) > 0 or (fmt_delta or 0) > 0 or (cl_counted or 0) > 0:
        tags.append("lint-regression")

    if pack:
        results["pack"] = pack_mod.run_pack(workspace, runner=runner, which=which)
    else:
        results["pack"] = _check("not_run", "pack turned off for this scoring run")
    if results["pack"]["status"] == "fail":
        tags.append(pack_mod.TAG)
    results["typecheck"] = _check("not_run", TYPECHECK_TODO)

    if changed is not None and workspace_mod.only_vendored(changed):
        tags.append("only-vendored-libs-changed")

    statuses = [r["status"] for r in results.values()]
    if "fail" in statuses:
        status = "fail"
    elif "pass" in statuses:
        status = "pass"
    else:
        status = "error"
    metrics = {
        "charmcraft_yaml_ok": results["charmcraft_yaml"]["status"] == "pass",
        "endpoints_ok": _ok(results.get("endpoints")),
        "charm_libs_ok": _ok(results.get("charm_libs")),
        "files_present_ok": _ok(results.get("files_present")),
        "actions_ok": _ok(results.get("actions")),
        "config_options_ok": _ok(results.get("config_options")),
        "ruff_check": ruff.get("check"),
        "ruff_format": ruff.get("format"),
        "ruff_delta": ruff_delta,
        "ruff_format_delta": fmt_delta,
        "charmlint_total": cl.get("total"),
        "charmlint_delta": cl_delta,
        "charmlint_excused": cl_excused,
        "packed": _ok(results["pack"]),
    }
    return TierOutcome(
        status=status,
        reason="no sub-check could run" if status == "error" else None,
        failure_tags=sorted(set(tags)),
        metrics=metrics,
        details={"checks": results, **({"changed_paths": changed} if changed is not None else {})},
    )


def _gold_baseline(gold_lint: dict[str, Any] | None) -> dict[str, Any]:
    if gold_lint is None:
        return {"status": "not_available"}
    return {
        "status": "used",
        "gold_sha": gold_lint.get("gold_sha"),
        "charmlint": gold_lint.get("charmlint"),
        "total": gold_lint.get("total"),
    }


def gold_lint_profile(
    gold_tree: pathlib.Path,
    charmlint: str,
    *,
    task_id: str,
    gold_sha: str | None,
    runner: _proc.Runner = _proc.run,
) -> dict[str, Any]:
    """charmlint on the gold's own tree: what ``run_static`` excuses against.

    Raises ``GoldLintError`` if charmlint didn't run: a feature task is never
    scored as if the gold had no findings.
    """
    counts = charmlint_counts(gold_tree, charmlint, runner)
    if "error" in counts:
        raise GoldLintError(counts["error"])
    return {
        "task": task_id,
        "gold_sha": gold_sha,
        "charmlint": charmlint,
        "total": counts["total"],
        "findings": [list(k) for k in counts["findings"]],
    }


class GoldLintError(RuntimeError):
    pass


def _ok(check: dict[str, Any] | None) -> bool | None:
    if check is None or check["status"] not in ("pass", "fail"):
        return None
    return check["status"] == "pass"
