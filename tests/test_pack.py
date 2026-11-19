"""The pack scorer (scoring.pack) and its place in the static tier, with a fake
charmcraft on PATH: success, a build failure, a backend failure, and no binary."""

from __future__ import annotations

import os
import pathlib
import shutil
import stat

import pytest
from conftest import write

from charmer_wof.config import StaticChecks
from charmer_wof.scoring import pack, scoring_code_version, static

FAKE = """#!/bin/sh
# A fake charmcraft: `version`, and `pack` behaving as $FAKE_PACK says.
case "$1" in
  version) echo "charmcraft 9.9.9"; exit 0 ;;
  pack)
    # Leave a marker so the test can see where pack ran.
    pwd > "$FAKE_PACK_LOG"
    ls -a >> "$FAKE_PACK_LOG"
    case "$FAKE_PACK" in
      ok) echo "Packed test-charm_amd64.charm"; : > test-charm_amd64.charm; exit 0 ;;
      nocharm) echo "Packed nothing"; exit 0 ;;
      build) echo "Failed: 'type: kubernetes' is not a valid charm type" >&2; exit 1 ;;
      lxd) echo "LXD is not installed." >&2; exit 1 ;;
    esac ;;
esac
exit 2
"""


@pytest.fixture
def fake_charmcraft(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "charmcraft"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "pack.log"
    monkeypatch.setenv("FAKE_PACK_LOG", str(log))

    def use(mode: str) -> pathlib.Path:
        monkeypatch.setenv("FAKE_PACK", mode)
        return log

    return use


def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    ws = tmp_path / "ws"
    write(ws / "charmcraft.yaml", "name: test-charm\ntype: charm\n")
    write(ws / "src/charm.py", "")
    write(ws / ".wof/prompt.md", "p\n")
    write(ws / ".git/HEAD", "ref: refs/heads/main\n")
    write(ws / ".venv/bin/python", "")
    return ws


def test_pass(tmp_path, fake_charmcraft):
    log = fake_charmcraft("ok")
    ws = workspace(tmp_path)
    out = pack.run_pack(ws)
    assert out["status"] == "pass"
    assert out["reason"] is None
    assert out["charms"] == ["test-charm_amd64.charm"]
    assert out["charmcraft_version"] == "charmcraft 9.9.9"
    # Packed in a copy, without the harness's files, git or the venv...
    ran_in, *listing = log.read_text().split()
    assert pathlib.Path(ran_in) != ws
    assert "charmcraft.yaml" in listing and "src" in listing
    assert not {".wof", ".git", ".venv"} & set(listing)
    # ...so the workspace is untouched.
    assert not list(ws.glob("*.charm"))


def test_build_failure(tmp_path, fake_charmcraft):
    fake_charmcraft("build")
    out = pack.run_pack(workspace(tmp_path))
    assert out["status"] == "fail"
    assert out["reason"] == "charmcraft pack exited 1"
    assert "not a valid charm type" in out["output_tail"]


def test_exit_zero_without_a_charm_fails(tmp_path, fake_charmcraft):
    fake_charmcraft("nocharm")
    out = pack.run_pack(workspace(tmp_path))
    assert out["status"] == "fail"
    assert "no .charm" in out["reason"]


def test_backend_failure_is_an_error_not_the_charm(tmp_path, fake_charmcraft):
    fake_charmcraft("lxd")
    out = pack.run_pack(workspace(tmp_path))
    assert out["status"] == "error"
    assert "lxd is not installed" in out["reason"]


def test_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert shutil.which("charmcraft") is None
    out = pack.run_pack(workspace(tmp_path))
    assert out == {"status": "not_run", "reason": pack.NOT_INSTALLED}


class NoLint:
    """Static's lint runners, faked: no findings."""

    def __call__(self, cmd, cwd, env=None, timeout=600):
        import subprocess

        if cmd[0] == "charmcraft":  # pack goes through the same runner
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        stdout = "[]" if "ruff" in cmd and "check" in cmd and "--output-format" in cmd else ""
        if cmd[0] == "uvx":
            stdout = '{"total": 0, "errors": 0, "warnings": 0, "diagnostics": []}'
        return subprocess.CompletedProcess(cmd, 0, stdout, "")


def run_static(ws, **kw):
    return static.run_static(ws, StaticChecks(), charmlint="0.3.0", runner=NoLint(), **kw)


def test_static_tier_fails_and_tags_a_failed_pack(tmp_path, fake_charmcraft):
    fake_charmcraft("build")
    out = run_static(workspace(tmp_path))
    assert out.status == "fail"
    assert out.failure_tags == ["pack-build-failed"]
    assert out.metrics["packed"] is False
    assert out.details["checks"]["pack"]["status"] == "fail"


def test_static_tier_passes_with_a_pack(tmp_path, fake_charmcraft):
    fake_charmcraft("ok")
    out = run_static(workspace(tmp_path))
    assert out.status == "pass"
    assert out.failure_tags == []
    assert out.metrics["packed"] is True


def test_static_tier_backend_error_is_not_a_failure(tmp_path, fake_charmcraft):
    fake_charmcraft("lxd")
    out = run_static(workspace(tmp_path))
    assert out.status == "pass"  # the other sub-checks passed; pack couldn't run
    assert out.failure_tags == []
    assert out.metrics["packed"] is None


def test_static_tier_pack_turned_off(tmp_path, fake_charmcraft):
    log = fake_charmcraft("ok")
    out = run_static(workspace(tmp_path), pack=False)
    assert out.details["checks"]["pack"]["status"] == "not_run"
    assert out.metrics["packed"] is None
    assert not log.exists()


def test_scoring_code_version_is_3():
    assert scoring_code_version("0.3.0") == "3+charmlint-0.3.0"


def test_pack_build_failed_is_in_the_vocabulary(config):
    assert "pack-build-failed" in config.tags_for("static")
