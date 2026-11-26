"""The CLI the GHA workflows use (2026-11-19): plan --matrix-json, fetch-gold, and
the agent / score / store split of run_cell."""

from __future__ import annotations

import json
import pathlib

import pytest
from conftest import git

from charmer_wof import cli, gold, ladder, planner, store, workspace
from charmer_wof.adapters.base import AdapterResult, ToolEvent

# --- plan --matrix-json -------------------------------------------------------


def matrix(capsys, *args: str) -> tuple[int, dict | None, str]:
    code = cli.main(["plan", "--matrix-json", *args])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if code == 0 else None), out.err


def test_matrix_shape(capsys, config):
    code, m, _ = matrix(capsys, "--task", "hedgedoc-s3,new-gotify-k8s", "--harness", "pi",
                        "--model", "glm-5-3", "--treatment", "none", "--reps", "1")  # fmt: skip
    assert code == 0
    assert set(m) == {"include"}  # anything else would become a matrix dimension
    cells = {c["cell"]: c for c in m["include"]}
    assert set(cells) == {"hedgedoc-s3/pi/glm-5-3/none/0", "new-gotify-k8s/pi/glm-5-3/none/0"}
    s3 = cells["hedgedoc-s3/pi/glm-5-3/none/0"]
    assert s3["slug"] == "hedgedoc-s3__pi__glm-5-3__none__0"
    assert s3["gold_private"] is True and s3["kind"] == "feature"
    assert s3["harness_version"] == config.harnesses["pi"].version
    assert s3["python"] == "3.12"
    assert cells["new-gotify-k8s/pi/glm-5-3/none/0"]["gold_private"] is False


def test_matrix_hard_cap(capsys, config):
    code, _, err = matrix(capsys)  # the full matrix
    assert code == 2
    assert f"cap of {config.plan.gha.max_cells}" in err
    code, _, err = matrix(capsys, "--anchors-only", "--max-cells", "2")
    assert code == 2 and "cap of 2" in err
    # --max-cells can only lower the configured cap.
    code, m, _ = matrix(capsys, "--sample", "3", "--max-cells", "1000")
    assert code == 0 and len(m["include"]) == 3


def test_matrix_sample_is_seeded(capsys):
    _, a, _ = matrix(capsys, "--sample", "8", "--seed", "2026-11-19")
    _, b, _ = matrix(capsys, "--sample", "8", "--seed", "2026-11-19")
    _, c, _ = matrix(capsys, "--sample", "8", "--seed", "2026-11-26")
    assert a == b and len(a["include"]) == 8
    assert a != c


def test_weekly_sample_is_feature_tasks_only(capsys, config):
    code, m, _ = matrix(
        capsys, "--kind", "feature", "--sample", "--seed", "2026-11-30", "--reps", "1"
    )
    assert code == 0 and len(m["include"]) == config.plan.gha.scheduled_sample
    assert {c["kind"] for c in m["include"]} == {"feature"}


def test_monthly_new_charm_slot(capsys, config):
    assert config.plan.gha.new_charm_sample == 2
    code, m, _ = matrix(
        capsys, "--kind", "new-charm", "--sample", "--seed", "2026-12-17", "--reps", "1"
    )
    assert code == 0 and len(m["include"]) == config.plan.gha.new_charm_sample
    assert {c["kind"] for c in m["include"]} == {"new-charm"}
    _, again, _ = matrix(
        capsys, "--kind", "new-charm", "--sample", "--seed", "2026-12-17", "--reps", "1"
    )
    assert again == m
    # The hard cap still applies to the slot.
    code, _, err = matrix(
        capsys, "--kind", "new-charm", "--sample", "--seed", "x", "--max-cells", "1"
    )
    assert code == 2 and "cap of 1" in err


def test_a_new_charm_cell_still_runs_by_name(capsys):
    code, m, _ = matrix(capsys, "--cells", "new-pushgateway-k8s/pi/glm-5-3/none/0")
    assert code == 0 and m["include"][0]["kind"] == "new-charm"


def test_unknown_kind_is_refused(capsys):
    code, _, err = matrix(capsys, "--kind", "rewrite", "--sample")
    assert code == 2 and "unknown task kind" in err


def test_matrix_explicit_cells(capsys):
    code, m, _ = matrix(
        capsys,
        "--cells",
        "hedgedoc-ingress/pi/glm-5-3/none/0, new-gotify-k8s/omp/glm-5-3/none/1",
    )
    assert code == 0
    assert [c["cell"] for c in m["include"]] == [
        "hedgedoc-ingress/pi/glm-5-3/none/0",
        "new-gotify-k8s/omp/glm-5-3/none/1",
    ]
    code, _, err = matrix(capsys, "--cells", "no-such-task/pi/glm-5-3/none/0")
    assert code == 2 and "unknown task" in err


def test_plan_needs_a_mode(capsys):
    assert cli.main(["plan"]) == 2


# --- fetch-gold ---------------------------------------------------------------


@pytest.fixture
def gold_repo(tmp_path):
    src = tmp_path / "gold-src"
    src.mkdir()
    git(src, "init", "-q")
    (src / "charmcraft.yaml").write_text("name: g\n")
    git(src, "add", "-A")
    git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one")
    sha = git(src, "rev-parse", "HEAD").strip()
    (src / "later.txt").write_text("later\n")
    git(src, "add", "-A")
    git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")
    return src, sha


def _task(config, repo: pathlib.Path, sha: str, private: bool):
    return config.tasks["hedgedoc-s3"].model_copy(
        update={"gold_repo": f"file://{repo}", "gold_sha": sha, "gold_private": private}
    )


def test_fetch_gold_at_its_sha(config, gold_repo, tmp_path):
    repo, sha = gold_repo
    dest = gold.fetch(_task(config, repo, sha, private=False), tmp_path / "g", env={})
    assert git(dest, "rev-parse", "HEAD").strip() == sha
    assert not (dest / "later.txt").exists()


def test_fetch_private_gold_needs_the_token(config, gold_repo, tmp_path):
    repo, sha = gold_repo
    task = _task(config, repo, sha, private=True)
    with pytest.raises(gold.GoldTokenMissing):
        gold.fetch(task, tmp_path / "g", env={})
    assert not (tmp_path / "g").exists()
    # With one, it's passed as an http header in GIT_CONFIG_* (unused by file://).
    dest = gold.fetch(task, tmp_path / "g2", env={gold.TOKEN_ENV: "tok"})
    assert git(dest, "rev-parse", "HEAD").strip() == sha


def test_auth_env_keeps_the_token_off_the_command_line():
    env = gold._auth_env("https://github.com/o/r", "tok")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
    assert "tok" not in env["GIT_CONFIG_VALUE_0"]  # base64'd


def test_cli_fetch_gold_skips_without_token(config, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv(gold.TOKEN_ENV, raising=False)
    assert cli.main(["fetch-gold", "hedgedoc-s3", "--dest", str(tmp_path / "g")]) == 3
    assert "skipped" in capsys.readouterr().err


# --- agent / score / store ----------------------------------------------------

CELL = "new-gotify-k8s/pi/glm-5-3/none/0"


class FakeAgent:
    name = "pi"

    def version(self) -> str:
        return "1.5.2"

    def context_file(self) -> str:
        return "CLAUDE.md"

    def run(self, ws, prompt, model, caps, key_env, *, env=None, hide=()):
        (ws / "src").mkdir(exist_ok=True)
        (ws / "src/charm.py").write_text("import ops\n")
        return AdapterResult(
            exit_reason="completed",
            cost_usd=0.25,
            turns=4,
            wall_seconds=12.0,
            provider="Z",
            events=[ToolEvent("shell", "bash", {"command": "curl https://example.com/x"})],
            transcript_path=ws / ".wof/pi-events.jsonl",
        )


@pytest.fixture
def agent_done(config, tmp_path):
    planned = planner.resolve(config, CELL)
    task, treatment = config.tasks[planned.cell.task], config.treatments["none"]
    ws = workspace.prepare(task, treatment, None, tmp_path / "ws")
    ladder.run_agent(config, planned, ws, adapter=FakeAgent(), hide=[])
    return ws


def test_agent_leaves_its_result_for_scoring(config, agent_done):
    path = agent_done.path / ".wof" / ladder.AGENT_RESULT
    data = json.loads(path.read_text())
    assert data["details"]["harness_version"] == "1.5.2"
    assert data["details"]["cell"] == CELL
    back = ladder.load_agent_result(workspace.load(agent_done.path), CELL)
    assert back.events == [ToolEvent("shell", "bash", {"command": "curl https://example.com/x"})]
    assert back.transcript_path == agent_done.path / ".wof/pi-events.jsonl"
    with pytest.raises(ladder.RunError, match="is from new-gotify"):
        ladder.load_agent_result(workspace.load(agent_done.path), CELL.replace("gotify", "x"))


def test_score_in_another_process(agent_done, tmp_path, capsys):
    results = tmp_path / "out.jsonl"
    code = cli.main(
        [
            "score",
            CELL,
            "--workspace",
            str(agent_done.path),
            "--tiers",
            "process",
            "--results",
            str(results),
        ]
    )
    assert code == 0, capsys.readouterr().err
    (record,) = store.read(results)
    assert record.harness.version == "1.5.2"
    assert record.tiers.process.cost_usd == 0.25
    assert record.tiers.process.details["fetches"]
    assert record.tiers.static.status == "not_run"
    assert record.tiers.oracle.status == "not_run"
    assert record.scoring.scoring_code_version.startswith("5+")
    assert "src/charm.py" in record.artefacts.agent_diff


def test_score_without_agent_result(config, tmp_path, capsys):
    planned = planner.resolve(config, CELL)
    workspace.prepare(
        config.tasks[planned.cell.task], config.treatments["none"], None, tmp_path / "ws"
    )
    code = cli.main(
        ["score", CELL, "--workspace", str(tmp_path / "ws"), "--results", str(tmp_path / "r")]
    )
    assert code == 2
    assert "did the agent job run" in capsys.readouterr().err


def test_score_rejects_unknown_tiers(config, agent_done):
    planned = planner.resolve(config, CELL)
    result = ladder.load_agent_result(agent_done, CELL)
    with pytest.raises(ladder.RunError, match="unknown tiers"):
        ladder.score(config, planned, agent_done, result, tiers=("live",))


def test_store_appends_once(agent_done, tmp_path, capsys):
    incoming = tmp_path / "incoming.jsonl"
    args = ["--tiers", "process", "--results", str(incoming)]
    assert cli.main(["score", CELL, "--workspace", str(agent_done.path), *args]) == 0
    main = tmp_path / "results.jsonl"
    assert cli.main(["store", str(incoming), "--results", str(main)]) == 0
    assert cli.main(["store", str(incoming), "--results", str(main)]) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()[-2:]]
    assert out[0]["added"] == 1 and out[1] == {**out[1], "added": 0, "already_stored": 1}
    assert len(store.read(main)) == 1


def test_store_refuses_dry_runs(tmp_path, capsys):
    dry = tmp_path / "dry.jsonl"
    assert cli.main(["run", "--dry-run", CELL, "--results", str(dry)]) == 0
    assert cli.main(["store", str(dry), "--results", str(tmp_path / "r.jsonl")]) == 1
    assert "dry run" in capsys.readouterr().err


def test_workspace_load_refuses_an_unprepared_dir(tmp_path):
    with pytest.raises(workspace.WorkspaceError, match="not a prepared workspace"):
        workspace.load(tmp_path)


def test_fetch_libs_needs_charmcraft(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(workspace.WorkspaceError, match="charmcraft"):
        workspace.fetch_libs(tmp_path)


def test_fetch_libs_runs_charmcraft(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "charmcraft"
    fake.write_text(
        '#!/bin/sh\n[ "$1" = fetch-libs ] && mkdir -p lib/charms/x && : > lib/charms/x/y.py\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    workspace.fetch_libs(tmp_path)
    assert (tmp_path / "lib/charms/x/y.py").exists()
