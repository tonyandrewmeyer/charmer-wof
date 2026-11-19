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
