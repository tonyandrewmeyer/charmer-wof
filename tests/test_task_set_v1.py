"""What task set v1 (2026-11-17) needed from the package: actions and config
option checks, ruff exclusions, monorepo golds and new-charm workspaces."""

from __future__ import annotations

import pytest
from conftest import git, write
from test_static import FakeRunner, no_charmcraft

from charmer_wof import cli, ladder, workspace
from charmer_wof.config import OracleSpec, StaticChecks, Task, Treatment
from charmer_wof.scoring import static


def _static(tmp_path, checks, runner=None):
    return static.run_static(
        tmp_path, checks, charmlint="0.3.0", runner=runner or FakeRunner(), which=no_charmcraft
    )


def test_actions_and_config_options_from_charmcraft_yaml(tmp_path):
    write(
        tmp_path / "charmcraft.yaml",
        """
        name: c
        actions:
          get-configuration: {}
        config:
          options:
            client-ip-hash-salt: {type: secret}
        """,
    )
    out = _static(
        tmp_path,
        StaticChecks(actions=("get-configuration",), config_options=("client-ip-hash-salt",)),
    )
    assert out.status == "pass"
    assert out.metrics["actions_ok"] and out.metrics["config_options_ok"]


def test_actions_and_config_options_from_separate_files(tmp_path):
    write(tmp_path / "charmcraft.yaml", "name: c\n")
    write(tmp_path / "actions.yaml", "get-configuration: {}\n")
    write(tmp_path / "config.yaml", "options:\n  port: {type: int}\n")
    out = _static(tmp_path, StaticChecks(actions=("get-configuration",), config_options=("port",)))
    assert out.status == "pass"


def test_missing_action_and_option_are_tagged(tmp_path):
    write(tmp_path / "charmcraft.yaml", "name: c\nconfig:\n  options:\n    other: {}\n")
    out = _static(tmp_path, StaticChecks(actions=("a",), config_options=("b",)))
    assert out.status == "fail"
    assert {"action-missing", "config-option-missing"} <= set(out.failure_tags)
    assert out.details["checks"]["actions"]["missing"] == ["a"]
    assert out.details["checks"]["config_options"]["missing"] == ["b"]


def test_ruff_exclude_reaches_both_ruff_commands(tmp_path):
    write(tmp_path / "charmcraft.yaml", "name: c\n")
    calls = []

    class Recording(FakeRunner):
        def __call__(self, cmd, cwd, env=None, **kw):
            calls.append(cmd)
            return super().__call__(cmd, cwd, env, **kw)

    _static(tmp_path, StaticChecks(ruff_exclude=("lib",)), runner=Recording())
    ruff = [c for c in calls if "ruff" in c]
    assert len(ruff) == 2
    assert all(c[c.index("--extend-exclude") + 1] == "lib" for c in ruff)


@pytest.fixture
def monorepo(tmp_path):
    repo = tmp_path / "mono"
    write(repo / "README.md", "the monorepo\n")
    write(repo / "charm/src/charm.py", "BASE = 1\nFEATURE = 2\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "gold")
    sha = git(repo, "rev-parse", "HEAD")
    write(repo / "charm/src/charm.py", "BASE = 1\n")
    diff = git(repo, "diff", "--relative=charm") + "\n"
    git(repo, "checkout", "-q", ".")
    write(tmp_path / "assets/removal.diff", diff)
    write(tmp_path / "assets/prompt.md", "Add it.\n")
    return repo, sha


def test_gold_subdir_is_the_whole_workspace(tmp_path, monorepo):
    repo, sha = monorepo
    task = Task(
        id="mono",
        version="1",
        kind="feature",
        gold_repo="https://example.invalid/mono",
        gold_sha=sha,
        gold_subdir="charm",
        removal_diff="assets/removal.diff",
        prompt="assets/prompt.md",
        oracle=OracleSpec(files=(), command=("pytest",)),
        root=tmp_path,
    )
    ws = workspace.prepare(
        task, Treatment(id="none", version="1", root=tmp_path), repo, tmp_path / "ws"
    )
    assert (ws.path / "src/charm.py").read_text() == "BASE = 1\n"
    assert not (ws.path / "README.md").exists()
    assert not (ws.path / "charm").exists()


def _new_charm(tmp_path, **kw) -> Task:
    write(tmp_path / "brief.md", "Write a charm.\n")
    write(tmp_path / "starter/CLAUDE.md", "# new\n")
    return Task(
        id="brief",
        version="1",
        kind="new-charm",
        prompt="brief.md",
        starter="starter",
        context_file="CLAUDE.md",
        root=tmp_path,
        **kw,
    )


def test_new_charm_workspace_is_the_starter_alone(tmp_path):
    task = _new_charm(tmp_path)
    treatment = Treatment(id="t", version="1", root=tmp_path, context_append="More.\n")
    ws = workspace.prepare(task, treatment, None, tmp_path / "ws")
    files = sorted(p.name for p in ws.path.iterdir() if p.name not in (".git", ".wof"))
    assert files == ["CLAUDE.md"]
    assert (ws.path / "CLAUDE.md").read_text() == "# new\nMore.\n"
    assert ws.prompt == "Write a charm."
    write(ws.path / "src/charm.py", "import ops\n")
    workspace.snapshot(ws.path)
    assert workspace.changed_paths(ws.path, ws.base_sha) == ["src/charm.py"]


def test_new_charm_cannot_have_a_removal(tmp_path):
    with pytest.raises(ValueError, match="nothing to remove"):
        _new_charm(tmp_path, remove_paths=("x",))


def test_feature_task_still_needs_a_gold_checkout(tmp_path, config):
    task = config.tasks["hedgedoc-s3"]
    with pytest.raises(workspace.WorkspaceError, match="needs a gold checkout"):
        workspace.prepare(task, config.treatments["none"], None, tmp_path / "ws")


def test_hidden_paths_without_a_gold(config):
    task = config.tasks["new-gotify-k8s"]
    paths = ladder.hidden_paths(None, task, config.treatments["none"])
    assert paths and all(p.is_dir() for p in paths)


def test_package_tasks_are_wired(config):
    for task in config.tasks.values():
        assert task.prompt_text()
        for f in task.oracle.files if task.oracle else ():
            assert task.path(f).exists(), f
            assert task.oracle.marker in str(f)
        if task.kind == "feature":
            assert task.path(task.removal_diff).exists()
            assert task.path(task.judge_criteria).exists()
        else:
            assert task.path(task.starter / task.context_file).exists()
    tt = config.treatments["tests-target"]
    feature = {t for t, task in config.tasks.items() if task.kind == "feature"}
    assert set(tt.applies_to) == feature


def test_cli_has_an_oracle_command(tmp_path):
    args = cli.parser().parse_args(["oracle", str(tmp_path), "--task", "new-gotify-k8s"])
    assert args.func is cli.cmd_oracle


def test_build_droppings_stay_out_of_the_diff(tmp_path):
    """A monorepo subdirectory brings no .gitignore; pytest's caches must not count."""
    task = _new_charm(tmp_path)
    ws = workspace.prepare(
        task, Treatment(id="t", version="1", root=tmp_path), None, tmp_path / "w"
    )
    write(ws.path / "src/charm.py", "import ops\n")
    write(ws.path / "src/__pycache__/charm.cpython-312.pyc", "x")
    write(ws.path / ".coverage", "x")
    workspace.snapshot(ws.path)
    assert workspace.changed_paths(ws.path, ws.base_sha) == ["src/charm.py"]
    _, stats = workspace.agent_diff(ws.path, ws.base_sha)
    assert stats["files_changed"] == 1
