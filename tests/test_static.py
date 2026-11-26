from __future__ import annotations

import json
import subprocess

from conftest import write

from charmer_wof.config import CharmLibCheck, EndpointCheck, StaticChecks
from charmer_wof.scoring import static

CHECKS = StaticChecks(
    endpoints=(EndpointCheck(name="ingress", interface="ingress"),),
    charm_libs=(CharmLibCheck(lib="traefik_k8s.ingress", version="2"),),
    files_present=("lib/charms/traefik_k8s/v2/ingress.py",),
)

GOOD_YAML = """
name: hedgedoc-k8s
type: charm
requires:
  ingress:
    interface: ingress
    limit: 1
charm-libs:
  - lib: traefik_k8s.ingress
    version: "2"
"""


class FakeRunner:
    """Answers ruff and charmlint with canned counts; records the env it was given."""

    def __init__(self, ruff=0, unformatted=0, charmlint=0, fail=False):
        self.ruff, self.unformatted, self.charmlint, self.fail = ruff, unformatted, charmlint, fail
        self.envs = []

    def __call__(self, cmd, cwd, env=None, **_):
        self.envs.append(env)
        if self.fail:
            raise FileNotFoundError("uv")
        if "check" in cmd:
            out = json.dumps([{"code": "E501"}] * self.ruff)
        elif "format" in cmd:
            out = "Would reformat: x.py\n" * self.unformatted
        else:
            diags = [{"rule_id": "R1"}] * self.charmlint
            out = json.dumps(
                {
                    "total": self.charmlint,
                    "errors": 0,
                    "warnings": self.charmlint,
                    "diagnostics": diags,
                }
            )
        return subprocess.CompletedProcess(cmd, 0, out, "")


def no_charmcraft(_):
    return None


def charm(tmp_path, yaml_text=GOOD_YAML, lib=True):
    write(tmp_path / "charmcraft.yaml", yaml_text)
    if lib:
        write(tmp_path / "lib/charms/traefik_k8s/v2/ingress.py", "")
    return tmp_path


def run(tmp_path, runner=None, baseline=None, **kw):
    return static.run_static(
        tmp_path,
        CHECKS,
        charmlint="0.3.0",
        baseline=baseline,
        runner=runner or FakeRunner(),
        which=no_charmcraft,
        **kw,
    )


def test_good_charm_passes(tmp_path):
    out = run(charm(tmp_path))
    assert out.status == "pass"
    assert out.failure_tags == []
    assert out.metrics["charm_libs_ok"] and out.metrics["endpoints_ok"]
    checks = out.details["checks"]
    assert checks["pack"] == {"status": "not_run", "reason": "charmcraft not installed"}
    assert checks["typecheck"]["status"] == "not_run"


def test_misnamed_lib_is_tagged(tmp_path):
    out = run(charm(tmp_path, GOOD_YAML.replace("traefik_k8s.ingress", "traefik-k8s.ingress")))
    assert out.status == "fail"
    assert out.failure_tags == ["charm-lib-misnamed"]
    assert out.details["checks"]["charm_libs"]["misnamed"] == ["traefik_k8s.ingress"]


def test_missing_feature_is_tagged(tmp_path):
    out = run(charm(tmp_path, "name: hedgedoc-k8s\ntype: charm\n", lib=False))
    assert out.failure_tags == ["charm-lib-missing", "endpoint-missing"]
    assert out.metrics["files_present_ok"] is False


def test_wrong_interface_counts_as_missing(tmp_path):
    out = run(charm(tmp_path, GOOD_YAML.replace("interface: ingress", "interface: http")))
    assert "endpoint-missing" in out.failure_tags


def test_unparseable_yaml(tmp_path):
    out = run(charm(tmp_path, "name: [unclosed\n"))
    assert out.status == "fail"
    assert "pack-failed" in out.failure_tags
    assert out.details["checks"]["endpoints"]["status"] == "not_run"


def test_lint_deltas_against_baseline(tmp_path):
    baseline = {"ruff": {"check": 3, "format": 0}, "charmlint": {"total": 2}}
    out = run(charm(tmp_path), FakeRunner(ruff=5, charmlint=2), baseline)
    assert out.metrics["ruff_delta"] == 2
    assert out.metrics["charmlint_delta"] == 0
    assert out.status == "fail"
    assert out.failure_tags == ["lint-regression"]
    better = run(charm(tmp_path), FakeRunner(ruff=1, charmlint=1), baseline)
    assert better.status == "pass" and better.metrics["ruff_delta"] == -2


def test_no_baseline_means_counts_only(tmp_path):
    out = run(charm(tmp_path), FakeRunner(ruff=4))
    assert out.metrics["ruff_check"] == 4
    assert out.metrics["ruff_delta"] is None
    assert out.status == "pass"


def test_tool_failure_is_an_error_not_a_crash(tmp_path):
    out = run(charm(tmp_path), FakeRunner(fail=True))
    assert out.details["checks"]["ruff"]["status"] == "error"
    assert out.details["checks"]["charmlint"]["status"] == "error"
    assert out.status == "pass"  # the yaml checks still ran and passed


def test_charm_env_pins_python_and_drops_harness_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/harness/.venv")
    runner = FakeRunner()
    run(charm(tmp_path), runner, python="3.12")
    ruff_env = runner.envs[0]
    assert ruff_env["UV_PYTHON"] == "3.12"
    assert "VIRTUAL_ENV" not in ruff_env


# --- charmlint baselined against the gold (2026-11-26) -------------------------

ACTION_MSG = "Action '{}' does not set 'additionalProperties'"


class FindingsRunner(FakeRunner):
    """charmlint answers with these (rule, path, message) findings, in ``charm_dir``."""

    def __init__(self, findings, charm_dir="/ws"):
        super().__init__()
        self.findings, self.charm_dir = findings, charm_dir

    def __call__(self, cmd, cwd, env=None, **kw):
        if "check" in cmd or "format" in cmd:
            return super().__call__(cmd, cwd, env, **kw)
        diags = [
            {"rule_id": r, "message": m, **({"path": f"{self.charm_dir}/{p}"} if p else {})}
            for r, p, m in self.findings
        ]
        out = {"charm_dir": self.charm_dir, "total": len(diags), "diagnostics": diags}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")


def _profile(findings, charm_dir="/elsewhere"):
    return static.charmlint_counts(None, "0.3.0", FindingsRunner(findings, charm_dir))


BASE = [("ACTIONS-002", "", ACTION_MSG.format("get-certificate"))]
GOLD = [*BASE, ("ACTIONS-002", "", ACTION_MSG.format("get-configuration"))]


def _gold_lint(findings=GOLD):
    return {"gold_sha": "a" * 40, "charmlint": "0.3.0", "total": len(findings),
            "findings": [list(k) for k in static.charmlint_counts(
                None, "0.3.0", FindingsRunner(findings, "/gold"))["findings"]]}  # fmt: skip


def _run_lint(tmp_path, agent, gold_lint):
    baseline = {"ruff": {"check": 0, "format": 0}, "charmlint": _profile(BASE)}
    return run(
        charm(tmp_path), FindingsRunner(agent, str(tmp_path)), baseline, gold_lint=gold_lint
    )


def test_a_finding_the_gold_also_has_is_excused(tmp_path):
    out = _run_lint(tmp_path, GOLD, _gold_lint())
    check = out.details["checks"]["charmlint"]
    assert out.status == "pass" and "lint-regression" not in out.failure_tags
    assert out.metrics["charmlint_delta"] == 1 and out.metrics["charmlint_excused"] == 1
    assert check["gold_baseline"]["status"] == "used"
    assert check["excused"] == [
        {"rule": "ACTIONS-002", "path": "", "message": ACTION_MSG.format("get-configuration")}
    ]
    assert check["new"] == []


def test_a_different_finding_of_the_same_rule_is_not_excused(tmp_path):
    agent = [*BASE, ("ACTIONS-002", "", ACTION_MSG.format("get-config"))]
    out = _run_lint(tmp_path, agent, _gold_lint())
    assert out.status == "fail" and out.failure_tags == ["lint-regression"]
    assert out.metrics["charmlint_excused"] == 0
    assert out.details["checks"]["charmlint"]["new"][0]["message"].startswith(
        "Action 'get-config'"
    )


def test_excusing_counts_by_multiset_and_by_file(tmp_path):
    defer = ("CORRECTNESS-001", "src/charm.py", "event.defer() is not followed by return")
    gold = [*BASE, defer]
    # Two new defers where the gold added one: one is excused, one is new.
    out = _run_lint(tmp_path, [*BASE, defer, defer], _gold_lint(gold))
    assert out.metrics["charmlint_excused"] == 1 and out.status == "fail"
    # The same finding in another file isn't the gold's.
    other = ("CORRECTNESS-001", "src/state.py", defer[2])
    out = _run_lint(tmp_path, [*BASE, other], _gold_lint(gold))
    assert out.metrics["charmlint_excused"] == 0 and out.status == "fail"


def test_without_a_gold_profile_the_record_says_so(tmp_path):
    out = _run_lint(tmp_path, GOLD, None)
    check = out.details["checks"]["charmlint"]
    assert check["gold_baseline"] == {"status": "not_available"}
    assert check["reason"].startswith("no gold baseline")
    assert out.status == "fail" and out.metrics["charmlint_excused"] is None
    assert len(check["new"]) == 1


def test_the_charm_dir_is_taken_out_of_paths_and_messages():
    key = static.finding_key(
        {"rule_id": "R", "path": "/a/b/src/x.py", "message": "see /a/b/src/x.py"}, "/a/b"
    )
    assert key == ("R", "src/x.py", "see src/x.py")
