"""Gold-copy detection (2026-11-18): the static and process signals, and their back-test."""

from __future__ import annotations

import json
import pathlib

import pytest

from charmer_wof.adapters import omp, opencode, pi
from charmer_wof.adapters.base import AdapterResult, ToolEvent
from charmer_wof.config import PACKAGE_ROOT, Caps
from charmer_wof.ladder import with_gold_copy
from charmer_wof.schema import RunRecord, TierOutcome
from charmer_wof.scoring import contamination as c
from charmer_wof.scoring.process import process_outcome

RESULTS = PACKAGE_ROOT / "results"
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HAPROXY_SALT_INVALID = RESULTS / "invalid" / "2026-11-17-haproxy-salt-copied-upstream.jsonl"
SMOKE_GOLD_VISIBLE = RESULTS / "invalid" / "2026-11-10-smoke-gold-visible.jsonl"
SALT_SHA = "1cab0e2a"  # the start of haproxy's pinned SHA, checked against the config below


def _records(path: pathlib.Path) -> list[RunRecord]:
    return [RunRecord.model_validate_json(x) for x in path.read_text().splitlines() if x.strip()]


# --- parsing ------------------------------------------------------------------


def test_parse_reads_hunks_by_count_not_by_prefix():
    diff = (
        "diff --git a/x.yaml b/x.yaml\n"
        "--- a/x.yaml\n"
        "+++ b/x.yaml\n"
        "@@ -1,3 +1,2 @@\n"
        " keep\n"
        "--- yaml document marker\n"
        "-gone line\n"
        "+new line\n"
        "diff --git a/new.py b/new.py\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('hi')\n"
        "\\ No newline at end of file\n"
    )
    files = c.parse_unified_diff(diff)
    assert files["x.yaml"].removed == ["-- yaml document marker", "gone line"]
    assert files["x.yaml"].added == ["new line"]
    assert files["new.py"].added == ["print('hi')"]


def test_parse_strips_diff_ruN_prefixes():
    diff = (
        "diff -ruN reference-t2/src/charm.py base-t2/src/charm.py\n"
        "--- reference-t2/src/charm.py\t2026-09-21 05:45:22 -0500\n"
        "+++ base-t2/src/charm.py\t2026-10-20 15:20:20 -0500\n"
        "@@ -1,2 +1 @@\n"
        "-import ingress_lib\n"
        " import ops\n"
    )
    assert c.parse_unified_diff(diff, 1)["src/charm.py"].removed == ["import ingress_lib"]


def test_gold_lines_drop_the_context_file_and_vendored_libs(config):
    gold = c.gold_added_lines(config.tasks["hedgedoc-ingress"])
    assert gold is not None and "CLAUDE.md" not in gold
    assert not any(p.startswith("lib/") for p in gold)
    assert "src/charm.py" in gold


# --- the static signal ----------------------------------------------------------


def test_backtest_no_valid_feature_run_is_tagged(config):
    """Every valid record in the store, scored with today's code: no gold-copied tag."""
    records = _records(RESULTS / "results.jsonl")
    checked = 0
    for r in records:
        check = c.gold_copy(config.tasks[r.task.id], r.artefacts.agent_diff)
        if r.task.kind == "new-charm":
            assert not check.applies
            continue
        checked += 1
        assert not check.copied, (r.cell.id, check.why)
        assert min(check.containment, check.coverage) < c.GOLD_COPY_OVERALL / 1.5
    assert checked == 20


@pytest.mark.parametrize("path", [HAPROXY_SALT_INVALID, SMOKE_GOLD_VISIBLE])
def test_backtest_invalid_copies_are_tagged(config, path):
    (r,) = _records(path)
    check = c.gold_copy(config.tasks[r.task.id], r.artefacts.agent_diff)
    assert check.copied
    assert check.containment == pytest.approx(1.0) and check.coverage == pytest.approx(1.0)


def test_new_charm_briefs_have_no_gold_to_copy(config):
    check = c.gold_copy(config.tasks["new-pushgateway-k8s"], "+++ b/src/charm.py\n")
    assert not check.applies and not check.copied
    assert check.metrics() == {} and check.details() == {"applies": False}


def _diff(path: str, lines: list[str]) -> str:
    body = "".join(f"+{x}\n" for x in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def test_one_copied_file_is_enough():
    src = [f"value_{i} = compute_thing({i})" for i in range(20)]
    tests = [f"assert check_value_{i}()" for i in range(200)]
    gold = {"src/charm.py": src, "tests/unit/test_x.py": tests}
    own_tests = [f"assert my_own_test_{i}()" for i in range(50)]
    check = c.compare_with_gold(
        _diff("src/charm.py", src) + _diff("tests/unit/test_x.py", own_tests), gold
    )
    assert check.coverage < c.GOLD_COPY_OVERALL  # the overall rule alone would miss it
    assert check.copied and check.why.startswith("src/charm.py")


def test_small_files_dont_count_on_their_own():
    yaml = [f"option_{i}: something" for i in range(6)]
    gold = {"charmcraft.yaml": yaml, "src/charm.py": [f"line number {i}" for i in range(50)]}
    check = c.compare_with_gold(_diff("charmcraft.yaml", yaml), gold)
    assert check.files["charmcraft.yaml"]["ratio"] == 1.0
    assert not check.copied


def test_whitespace_is_normalised():
    gold = {"src/a.py": [f"call_number({i}, other)" for i in range(20)]}
    reformatted = [f"    call_number( {i},   other)".replace("( ", "(") for i in range(20)]
    assert c.compare_with_gold(_diff("src/a.py", reformatted), gold).copied


def test_static_tier_gets_the_tag_metrics_and_keeps_its_status(config):
    (r,) = _records(HAPROXY_SALT_INVALID)
    task = config.tasks[r.task.id]
    out = with_gold_copy(TierOutcome(status="pass"), task, r.artefacts.agent_diff)
    assert out.status == "pass"
    assert out.failure_tags == ["gold-copied"]
    assert out.metrics["gold_line_containment"] == 1.0
    assert out.details["gold_copy"]["applies"] is True
    clean = with_gold_copy(TierOutcome(status="fail"), task, "")
    assert clean.status == "fail" and clean.failure_tags == []


# --- the process signal ----------------------------------------------------------

SEARCH = (
    "curl -s 'https://api.github.com/search/code?q=client-ip-hash-salt"
    "+repo%3Acanonical%2Fhaproxy-operator'"
)
RAW = (
    "curl -sL https://raw.githubusercontent.com/canonical/haproxy-operator/"
    "1cab0e2a0000000000000000000000000000000/haproxy-operator/src/charm.py -o src/charm.py"
)
# 2026-11-17's legitimate fetches (PLAN §7): none of them may be tagged.
TEMPO_LIB = (
    "curl -sL https://raw.githubusercontent.com/canonical/tempo-operators/main/coordinator/"
    "lib/charms/tempo_coordinator_k8s/v0/tracing.py -o lib/charms/tempo_coordinator_k8s/v0/"
    "tracing.py"
)
S3_LIB = "uvx charmcraft fetch-lib charms.data_platform_libs.v0.s3"
PUSHGATEWAY = (
    "curl -s https://raw.githubusercontent.com/canonical/prometheus-pushgateway-k8s-operator/"
    "main/src/charm.py"
)


def _shell(cmd: str) -> ToolEvent:
    return ToolEvent(kind="shell", name="bash", input={"command": cmd})


def test_gold_slug():
    assert c.gold_slug("https://github.com/canonical/haproxy-operator") == (
        "canonical/haproxy-operator"
    )
    assert c.gold_slug("https://github.com/Owner/Repo.git") == "owner/repo"
    assert c.gold_slug(None) is None and c.gold_slug("https://gitlab.com/a/b") is None


def test_fetches_keep_network_calls_only():
    events = [
        _shell("ls src"),
        _shell("grep -rn http src/"),
        _shell(S3_LIB),
        _shell("git clone https://github.com/canonical/operator"),
        ToolEvent(kind="read", name="read", input={"path": "src/charm.py"}),
        ToolEvent(kind="other", name="fetch", input={"url": "https://example.com/x"}),
    ]
    kept = c.fetches(events)
    assert [f["input"] for f in kept] == [
        S3_LIB,
        "git clone https://github.com/canonical/operator",
        '{"url": "https://example.com/x"}',
    ]


def test_fetches_are_trimmed_and_capped():
    kept = c.fetches([_shell("curl https://x.example/" + "a" * 2000)] * (c.FETCHES_MAX + 5))
    assert len(kept) == c.FETCHES_MAX
    assert all(len(f["input"]) == c.FETCH_TEXT_MAX for f in kept)


def test_salt_run_shaped_commands_are_tagged(config):
    task = config.tasks["haproxy-client-ip-hash-salt"]
    assert task.gold_sha.startswith(SALT_SHA)
    for cmd in (
        SEARCH,
        RAW,
        "gh search code client-ip-hash-salt --repo canonical/haproxy-operator",
    ):
        check = c.gold_fetch(task, c.fetches([_shell(cmd)]))
        assert check.fetched, cmd
    # A fork at a made-up SHA that only shares the first 8 characters: not the gold.
    fork = RAW.replace("canonical/haproxy-operator", "someone/fork")
    assert not c.gold_fetch(task, c.fetches([_shell(fork)])).fetched


def test_gold_sha_alone_is_enough(config):
    task = config.tasks["haproxy-get-configuration"]
    cmd = f"curl https://raw.githubusercontent.com/x/y/{task.gold_sha}/src/charm.py"
    check = c.gold_fetch(task, c.fetches([_shell(cmd)]))
    assert check.fetched and "gold SHA" in check.hits[0]


def test_legitimate_fetches_are_not_tagged(config):
    for task_id, cmd in (
        ("alertmanager-workload-tracing", TEMPO_LIB),
        ("hedgedoc-s3", S3_LIB),
        ("haproxy-get-configuration", "curl https://github.com/canonical/haproxy-operator-x"),
    ):
        check = c.gold_fetch(config.tasks[task_id], c.fetches([_shell(cmd)]))
        assert not check.fetched, (task_id, cmd)
    # A new-charm brief has no gold: reading the Canonical pushgateway charm is allowed.
    check = c.gold_fetch(config.tasks["new-pushgateway-k8s"], c.fetches([_shell(PUSHGATEWAY)]))
    assert not check.applies and not check.fetched


def test_code_search_without_the_gold_is_noted_not_tagged(config):
    task = config.tasks["hedgedoc-s3"]
    cmd = "curl 'https://api.github.com/search/code?q=S3Requirer+language:python'"
    check = c.gold_fetch(task, c.fetches([_shell(cmd)]))
    assert check.code_search and not check.fetched


# Each adapter's event shape for one tool call, so the check runs on what the
# adapters actually produce (their recorded fixtures have no network calls).


def _pi_line(cmd):
    return {"type": "tool_execution_start", "toolName": "bash", "args": {"command": cmd}}


def _opencode_line(cmd):
    return {
        "type": "tool_use",
        "part": {"tool": "shell", "state": {"status": "completed", "input": {"command": cmd}}},
    }


@pytest.mark.parametrize(
    ("module", "fixture", "line"),
    [
        (pi, "pi-1.5.2-oneline.jsonl", _pi_line),
        (omp, "omp-18.19.0-oneline.jsonl", _pi_line),
        (opencode, "opencode-2.2.6-oneline.jsonl", _opencode_line),
    ],
)
def test_adapters_events_reach_the_process_tier(config, module, fixture, line):
    task = config.tasks["haproxy-client-ip-hash-salt"]
    recorded = (FIXTURES / fixture).read_text().splitlines()
    for extra, tagged in (([], False), ([json.dumps(line(RAW))], True)):
        parsed = module.parse_lines(recorded + extra)
        result = AdapterResult(
            exit_reason="completed",
            cost_usd=0.0,
            turns=parsed.turns,
            wall_seconds=1.0,
            provider=None,
            events=parsed.events,
        )
        out = process_outcome(
            result, config.treatments["none"], Caps(), ["src/charm.py"], task=task
        )
        assert ("gold-fetched" in out.failure_tags) is tagged
        assert len(out.details["fetches"]) == (1 if tagged else 0)
        assert out.details["gold_fetch"]["applies"] is True


def test_process_without_a_task_keeps_fetches_but_checks_nothing(config):
    result = AdapterResult(
        exit_reason="completed",
        cost_usd=0.0,
        turns=1,
        wall_seconds=1.0,
        provider=None,
        events=[_shell(RAW)],
    )
    out = process_outcome(result, config.treatments["none"], Caps(), ["x"])
    assert out.failure_tags == [] and "gold_fetch" not in out.details
    assert out.details["fetches"][0]["tool"] == "bash"


def test_tags_are_in_the_vocabulary(config):
    assert "gold-copied" in config.tags_for("static")
    assert "gold-fetched" in config.tags_for("process")


def test_fetch_hosts():
    assert c.fetch_hosts(
        "curl -sSL https://github.com/canonical/x/archive/main.tar.gz; "
        "git clone git@github.com:canonical/x.git; curl https://user:pw@codeload.github.com/a"
    ) == ["codeload.github.com", "github.com"]
    assert c.fetch_hosts("gh repo clone canonical/x") == []


def _result_with(events, egress):
    return AdapterResult(
        exit_reason="completed",
        cost_usd=0.0,
        turns=1,
        wall_seconds=1.0,
        provider=None,
        events=events,
        details=egress,
    )


ALERTMANAGER_TARBALL = (
    "cd /tmp && curl -sSL -o amk.tar.gz "
    "https://github.com/canonical/alertmanager-k8s-operator/archive/refs/heads/main.tar.gz"
)


@pytest.mark.parametrize(
    ("cmd", "egress", "tag"),
    [
        # Refused at the proxy, as in run 44877482270's pi record.
        (
            ALERTMANAGER_TARBALL,
            {"egress_allowed": {"pypi.org": 1}, "egress_refused": {"github.com:443": 2}},
            "gold-fetch-refused",
        ),
        # No egress record (a run without the agent network): can't tell.
        (ALERTMANAGER_TARBALL, {}, "gold-fetched"),
        # The host was allowed.
        (
            ALERTMANAGER_TARBALL,
            {"egress_allowed": {"github.com": 1}, "egress_refused": {"github.com:443": 1}},
            "gold-fetched",
        ),
        # The proxy never saw the host: it may have gone some other way.
        (
            ALERTMANAGER_TARBALL,
            {"egress_allowed": {}, "egress_refused": {"codeload.github.com:443": 1}},
            "gold-fetched",
        ),
        # A fetch that names no host.
        (
            "gh repo clone canonical/alertmanager-k8s-operator",
            {"egress_allowed": {}, "egress_refused": {"github.com:443": 1}},
            "gold-fetched",
        ),
    ],
)
def test_gold_fetch_refused_only_when_the_proxy_refused_every_host(config, cmd, egress, tag):
    task = config.tasks["alertmanager-workload-tracing"]
    out = process_outcome(
        _result_with([_shell(cmd)], egress),
        config.treatments["none"],
        Caps(),
        ["src/charm.py"],
        task=task,
    )
    assert tag in out.failure_tags
    other = ({"gold-fetched", "gold-fetch-refused"} - {tag}).pop()
    assert other not in out.failure_tags
