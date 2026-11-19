from __future__ import annotations

import pathlib

import pytest
from conftest import git, write

from charmer_wof import workspace
from charmer_wof.config import FromOracle, OracleSpec, Task, Treatment


@pytest.fixture
def gold(tmp_path) -> tuple[pathlib.Path, str, pathlib.Path]:
    """A tiny gold charm repo with a feature, and the diff that removes it."""
    repo = tmp_path / "gold"
    write(repo / "src/charm.py", "BASE = 1\nFEATURE = 2\n")
    write(repo / "lib/charms/x/v0/feature.py", "# vendored\n")
    write(repo / "tests/unit/test_charm.py", "def test_ok():\n    pass\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "gold")
    sha = git(repo, "rev-parse", "HEAD")
    write(repo / "src/charm.py", "BASE = 1\n")
    write(repo / "AGENTS.md", "# Context\n")
    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", "HEAD") + "\n"
    git(repo, "reset", "-q", "--hard", sha)
    removal = write(tmp_path / "assets/removal.diff", diff)
    return repo, sha, removal


def make_task(root, sha, removal, **kw) -> Task:
    write(root / "prompt.md", "Add the feature.\n")
    write(root / "oracle/test__oracle_feature.py", "def test_oracle_feature():\n    pass\n")
    write(
        root / "oracle/test__oracle_feature_more.py",
        '"""The hidden oracle."""\n\n\ndef test_oracle_more():\n    pass\n',
    )
    return Task(
        id="feat",
        version="1",
        kind="feature",
        gold_repo="https://example.invalid/gold",
        gold_sha=sha,
        removal_diff=removal.relative_to(root),
        remove_paths=("lib/charms/x/v0/feature.py",),
        prompt="prompt.md",
        oracle=OracleSpec(
            files=("oracle/test__oracle_feature.py", "oracle/test__oracle_feature_more.py"),
            command=("pytest",),
        ),
        root=root,
        **kw,
    )


def make_treatment(root, **kw) -> Treatment:
    return Treatment(id="t", version="1", root=root, **kw)


def test_prepare_builds_base(tmp_path, gold):
    repo, sha, removal = gold
    root = tmp_path
    write(root / "treatments/howto/feat/docs/how-to/feature.md", "Do X.\n")
    task = make_task(root, sha, removal)
    treatment = make_treatment(
        root,
        overlay="treatments/howto",
        context_append="\nSee docs/how-to/.\n",
        prompt_append="\nExtra sentence: {generated}.",
        from_oracle=FromOracle(
            replace=(("test_oracle_", "test_"),),
            task_replace={"feat": (("The hidden oracle.", "Acceptance tests."),)},
        ),
    )
    ws = workspace.prepare(task, treatment, repo, tmp_path / "ws")
    d = ws.path
    assert (d / "src/charm.py").read_text() == "BASE = 1\n"
    assert not (d / "lib/charms/x/v0/feature.py").exists()
    assert (d / "docs/how-to/feature.md").exists()
    assert (d / "AGENTS.md").read_text() == "# Context\n\nSee docs/how-to/.\n"
    # Every oracle file is generated, renamed and de-oracled.
    assert "def test_feature" in (d / "tests/unit/test_acceptance_feature.py").read_text()
    more = (d / "tests/unit/test_acceptance_feature_more.py").read_text()
    assert more == '"""Acceptance tests."""\n\n\ndef test_more():\n    pass\n'
    assert not (d / "tests/unit/test__oracle_feature.py").exists()  # the oracle stays hidden
    assert not (d / "tests/unit/test__oracle_feature_more.py").exists()
    assert ws.prompt == (
        "Add the feature.\nExtra sentence: tests/unit/test_acceptance_feature.py, "
        "tests/unit/test_acceptance_feature_more.py."
    )
    assert (d / ".wof/prompt.md").read_text() == ws.prompt + "\n"
    assert git(d, "status", "--porcelain") == ""  # .wof/ is excluded
    assert git(d, "rev-parse", "HEAD") == ws.base_sha


def test_agent_diff(tmp_path, gold):
    repo, sha, removal = gold
    task = make_task(tmp_path, sha, removal)
    ws = workspace.prepare(task, make_treatment(tmp_path), repo, tmp_path / "ws")
    write(ws.path / "src/charm.py", "BASE = 1\nFEATURE = 3\n")
    write(ws.path / "lib/charms/x/v0/feature.py", "# fetched\n")  # vendored: excluded
    write(ws.path / ".wof/transcript.jsonl", "{}\n")  # harness: excluded
    workspace.snapshot(ws.path)
    diff, stats = workspace.agent_diff(ws.path, ws.base_sha)
    assert "FEATURE = 3" in diff
    assert "fetched" not in diff
    assert stats == {"files_changed": 1, "insertions": 1, "deletions": 0}


def test_wrong_sha_is_an_error(tmp_path, gold):
    repo, _, removal = gold
    task = make_task(tmp_path, "0" * 40, removal)
    with pytest.raises(workspace.WorkspaceError, match="does not have"):
        workspace.prepare(task, make_treatment(tmp_path), repo, tmp_path / "ws")


def test_inapplicable_treatment_is_an_error(tmp_path, gold):
    repo, sha, removal = gold
    task = make_task(tmp_path, sha, removal)
    treatment = make_treatment(tmp_path, applies_to=("other",))
    with pytest.raises(workspace.WorkspaceError, match="does not apply"):
        workspace.prepare(task, treatment, repo, tmp_path / "ws")


def test_package_task_prompt(config):
    task = config.tasks["hedgedoc-ingress"]
    prompt = workspace.build_prompt(task, config.treatments["tests-target"])
    assert prompt.startswith("Add ingress support")
    assert prompt.endswith(
        "are in tests/unit/test_acceptance_ingress.py, "
        "tests/unit/test_acceptance_ingress_exemplar.py; "
        "make them pass without modifying them."
    )


def test_generated_file_cannot_overwrite_the_base(tmp_path, gold):
    repo, sha, removal = gold
    task = make_task(tmp_path, sha, removal)
    treatment = make_treatment(
        tmp_path,
        prompt_append="{generated}",
        from_oracle=FromOracle(rename=("test__oracle_feature", "test_charm")),
    )
    with pytest.raises(workspace.WorkspaceError, match="would overwrite"):
        workspace.prepare(task, treatment, repo, tmp_path / "ws")


def test_from_oracle_applies_to(tmp_path, gold):
    _repo, sha, removal = gold
    task = make_task(tmp_path, sha, removal)
    other = make_treatment(tmp_path, from_oracle=FromOracle(applies_to=("other",)))
    assert other.generated_files(task) == []
    every = make_treatment(tmp_path, from_oracle=FromOracle(to_dir="tests/acceptance/"))
    assert [dest for _, dest in every.generated_files(task)] == [
        "tests/acceptance/test_acceptance_feature.py",
        "tests/acceptance/test_acceptance_feature_more.py",
    ]


def test_package_tests_target_shows_every_oracle_file(config, tmp_path):
    """PLAN §8 2026-11-17: the strip-prefix test is visible in tests-target."""
    task = config.tasks["hedgedoc-ingress"]
    tt = config.treatments["tests-target"]
    files = tt.generated_files(task)
    assert len(files) == len(task.oracle.files) == 2
    texts = {dest: tt.from_oracle.text(task, src) for src, dest in files}
    visible = "".join(texts.values())
    assert "def test_requirer_asks_traefik_to_strip_the_prefix" in visible
    assert "test_oracle_" not in visible
    assert "oracle" not in visible.lower()
