from __future__ import annotations

import pytest
from conftest import copy_package_tree, write

from charmer_wof.config import PACKAGE_ROOT, Config, ConfigError, load_config, tree_digest


def test_package_config_loads(config):
    assert set(config.tasks) == {
        "hedgedoc-ingress",
        "alertmanager-workload-tracing",
        "haproxy-client-ip-hash-salt",
        "haproxy-get-configuration",
        "hedgedoc-s3",
        "new-gotify-k8s",
        "new-pushgateway-k8s",
    }
    assert set(config.harnesses) == {"pi", "omp", "opencode"}
    assert set(config.treatments) == {"none", "howto", "skill", "skill-scripts", "tests-target"}
    assert {m.tier for m in config.models.values()} == {"open-weight"}


def test_task_records_what_phase_0_asks_for(config):
    task = config.tasks["hedgedoc-ingress"]
    assert task.kind == "feature"
    assert task.held_out is False
    assert len(task.gold_sha) == 40
    assert task.path(task.removal_diff).is_file()
    assert task.path(task.prompt).is_file()
    assert all(task.path(f).is_file() for f in task.oracle.files)
    assert task.oracle.command[0] == "uv"


def test_models_are_pinned_where_dated(config):
    deepseek = config.models["deepseek-v4-pro"]
    assert deepseek.dated
    assert deepseek.openrouter_id.endswith("-0813")


def test_treatment_artefacts_carry_versions(config):
    task = config.tasks["hedgedoc-ingress"]
    arts = config.treatments["howto"].artefact_versions(task)
    assert arts["charmlint"] == "0.3.0"
    assert arts["overlay"].startswith("sha256:")
    assert config.treatments["none"].artefact_versions(task) == {"charmlint": "0.3.0"}
    tt = config.treatments["tests-target"].artefact_versions(task)
    assert "generated:tests/unit/test_acceptance_ingress.py" in tt
    assert "generated:tests/unit/test_acceptance_ingress_exemplar.py" in tt


def test_tree_digest_tracks_content_not_mtime(tmp_path):
    write(tmp_path / "a" / "x.md", "one")
    first = tree_digest(tmp_path / "a")
    (tmp_path / "a" / "x.md").touch()
    assert tree_digest(tmp_path / "a") == first
    write(tmp_path / "a" / "x.md", "two")
    assert tree_digest(tmp_path / "a") != first


def test_held_out_tree_merges(private_tree):
    config = load_config([PACKAGE_ROOT, private_tree])
    task = config.tasks["secret-new-charm"]
    assert task.held_out
    assert task.root == private_tree.resolve()
    assert task.prompt_text() == "Do the secret thing."


def test_private_tree_from_env(monkeypatch, private_tree):
    monkeypatch.setenv("WOF_PRIVATE_CONFIG", str(private_tree))
    assert "secret-new-charm" in load_config().tasks


def test_private_tree_from_legacy_env(monkeypatch, private_tree):
    # The variable's name before the 2026-11-19 rename still works.
    monkeypatch.delenv("WOF_PRIVATE_CONFIG", raising=False)
    monkeypatch.setenv("CWE_PRIVATE_CONFIG", str(private_tree))
    assert "secret-new-charm" in load_config().tasks


def test_duplicate_ids_across_trees_rejected(tmp_path):
    other = copy_package_tree(tmp_path / "other")
    with pytest.raises(ConfigError, match="duplicate"):
        load_config([PACKAGE_ROOT, other])


def test_feature_task_needs_gold_and_oracle(tmp_path):
    root = copy_package_tree(tmp_path / "t")
    write(
        root / "config" / "tasks.toml",
        """
        [tasks.half]
        version = "1"
        kind = "feature"
        prompt = "p.md"
        """,
    )
    (root / "config" / "treatments.toml").write_text('[treatments.none]\nversion = "1"\n')
    (root / "config" / "plan.toml").write_text("reps = 1\n")
    with pytest.raises(ConfigError, match="gold_repo, gold_sha, removal_diff, oracle"):
        load_config([root])


def test_unknown_reference_in_treatment_rejected(tmp_path):
    root = copy_package_tree(tmp_path / "t")
    with (root / "config" / "treatments.toml").open("a") as f:
        f.write('\n[treatments.bad]\nversion = "1"\napplies_to = ["nope"]\n')
    with pytest.raises(ConfigError, match="unknown task 'nope'"):
        load_config([root])


def test_unknown_anchor_rejected(tmp_path):
    root = copy_package_tree(tmp_path / "t")
    with (root / "config" / "plan.toml").open("a") as f:
        f.write('\n[[anchors]]\ntask = "hedgedoc-ingress"\nharness = "pi"\n')
        f.write('model = "gpt-9"\ntreatment = "none"\n')
    with pytest.raises(ConfigError, match="unknown model 'gpt-9'"):
        load_config([root])


def test_failure_tags_per_tier(config):
    assert "prefix-not-stripped" in config.tags_for("live")
    assert "prefix-not-stripped" in config.tags_for("judge")
    assert "tests-shallow" not in config.tags_for("live")
    assert config.tags_for("live") and config.tags_for("judge")


def test_judge_models_sorted_and_unique(tmp_path):
    root = copy_package_tree(tmp_path / "t")
    path = root / "config" / "scoring.toml"
    text = path.read_text().replace(
        '"anthropic/claude-sonnet-5.5",', '"b/judge", "a/judge", "b/judge",'
    )
    path.write_text(text)
    with pytest.raises(ConfigError, match="duplicates"):
        load_config([root])


def test_generating_treatment_must_name_its_files(config):
    data = config.model_dump()
    tt = data["treatments"]["tests-target"]
    tt["prompt_append"] = "Make the acceptance tests pass."
    with pytest.raises(ValueError, match=r"\{generated\}"):
        Config.model_validate(data)
    tt["prompt_append"] = "{generated}"
    tt["from_oracle"]["task_replace"] = {"no-such-task": []}
    with pytest.raises(ValueError, match="unknown task 'no-such-task'"):
        Config.model_validate(data)
