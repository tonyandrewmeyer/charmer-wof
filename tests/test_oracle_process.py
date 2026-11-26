from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

from charmer_wof.adapters.base import AdapterResult, NotImplementedAdapter, ToolEvent
from charmer_wof.schema import json_schema
from charmer_wof.scoring import oracle
from charmer_wof.scoring.oracle import failure_details, outcome_from_junit, parse_junit
from charmer_wof.scoring.process import process_outcome

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite>
  <testcase classname="tests.unit.test__oracle_ingress" name="test_oracle_url"/>
  <testcase classname="tests.unit.test__oracle_ingress_exemplar"
            name="test_oracle_requirer_asks_traefik_to_strip_the_prefix">
    <failure message="strip_prefix missing"/>
  </testcase>
  <testcase classname="tests.unit.test_charm" name="test_install"/>
  <testcase classname="tests.unit.test_charm" name="test_skipped"><skipped/></testcase>
</testsuite></testsuites>
"""


def test_parse_junit_splits_oracle_from_agent():
    oracle, agent, collection_error = parse_junit(JUNIT, "test__oracle_")
    assert sorted(oracle.values()) == ["fail", "pass"]
    assert agent == {
        "tests.unit.test_charm::test_install": "pass",
        "tests.unit.test_charm::test_skipped": "skip",
    }
    assert not collection_error


def test_oracle_outcome(config):
    out = outcome_from_junit(JUNIT, config.tasks["hedgedoc-ingress"].oracle)
    assert (out.passed, out.total) == (1, 2)
    assert (out.agent_tests_passed, out.agent_tests_total) == (1, 2)
    assert out.status == "fail"
    assert out.key_tests == {"test_oracle_requirer_asks_traefik_to_strip_the_prefix": "fail"}


def test_oracle_collection_error_tagged(config):
    xml = """<testsuites><testsuite>
      <testcase classname="" name="tests.unit.test__oracle_ingress">
        <error message="ImportError"/>
      </testcase>
    </testsuite></testsuites>"""
    out = outcome_from_junit(xml, config.tasks["hedgedoc-ingress"].oracle)
    assert out.failure_tags == ["oracle-collection-error"]
    assert out.key_tests == {"test_oracle_requirer_asks_traefik_to_strip_the_prefix": "missing"}


def result(events, exit_reason="completed"):
    return AdapterResult(
        exit_reason=exit_reason,
        cost_usd=0.21,
        turns=12,
        wall_seconds=64.0,
        provider="Anthropic",
        events=events,
    )


def test_process_help_and_scripts(config):
    events = [
        ToolEvent("read", "read", {"path": ".claude/skills/add-ingress/SKILL.md"}),
        ToolEvent(
            "shell",
            "bash",
            {"command": "python .claude/skills/add-ingress/scripts/check_ingress.py"},
        ),
        ToolEvent("edit", "edit", {"path": "src/charm.py"}),
        ToolEvent("edit", "edit", {"path": "charmcraft.yaml"}),
    ]
    out = process_outcome(result(events), config.treatments["skill-scripts"], config.plan.caps)
    assert out.read_help is True
    assert out.ran_scripts == ["check_ingress.py"]
    assert out.tool_calls == {"bash": 1, "edit": 2, "read": 1}
    assert out.tool_calls_total == 4
    assert (out.cost_usd, out.turns, out.wall_seconds) == (0.21, 12, 64.0)
    assert out.status == "pass" and out.failure_tags == []


def test_process_editing_the_help_is_not_reading_it(config):
    events = [ToolEvent("edit", "edit", {"path": "docs/how-to/add-ingress.md"})]
    out = process_outcome(result(events), config.treatments["howto"], config.plan.caps)
    assert out.read_help is False


def test_process_no_help_offered(config):
    out = process_outcome(result([]), config.treatments["none"], config.plan.caps)
    assert out.read_help is None


def test_process_cap_tagged(config):
    out = process_outcome(result([], "turn_cap"), config.treatments["none"], config.plan.caps)
    assert out.failure_tags == ["turn-cap"]


def test_adapters_are_stubs():
    with pytest.raises(NotImplementedError, match="phase 1"):
        NotImplementedAdapter("pi").version()


# --- the oracle's failure output in the record (2026-11-26) ---------------------

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "oracle-junit-failures.xml"
WS = pathlib.Path("/home/runner/work/_temp/wof/ws/new-pushgateway-k8s__opencode__glm-5-3__none__0")


def test_failure_output_is_kept_without_paths():
    details = failure_details(FIXTURE.read_text(), "test__oracle_", workspace=WS)
    failures = details["failures"]
    assert set(failures) == {
        "tests.unit.test__oracle_x::test_oracle_port_is_rendered",
        "tests.unit.test__oracle_x::test_oracle_raises",
    }  # the agent's own failing test isn't kept
    assert failures["tests.unit.test__oracle_x::test_oracle_raises"] == {
        "type": "KeyError",
        "message": "'missing'",
        "lines": [
            '>       {}["missing"]',
            "E       KeyError: 'missing'",
            "tests/unit/test__oracle_x.py:12: KeyError",
        ],
    }
    (err,) = details["collection_errors"].values()
    assert err["type"] == "AttributeError" and "StartedEvent" in err["message"]
    assert "src/charm.py:5: in Charm" in err["lines"]
    text = json.dumps(details)
    assert "/home/runner" not in text and "_temp" not in text
    # A path that's a value in the charm's own config isn't a machine path.
    port = failures["tests.unit.test__oracle_x::test_oracle_port_is_rendered"]
    assert '"/var/lib/thing"' in port["lines"][0]


def test_a_private_gold_keeps_type_and_message_only():
    details = failure_details(FIXTURE.read_text(), "test__oracle_", workspace=WS, private=True)
    entries = [*details["failures"].values(), *details["collection_errors"].values()]
    assert all(set(e) == {"type", "message"} for e in entries)
    assert details["failure_output"].startswith("type and message only")
    assert "class Charm" not in json.dumps(details)


def test_failure_output_is_capped():
    long_tb = "\n".join(f"E   line {i} " + "x" * 400 for i in range(100))
    cases = "".join(
        f'<testcase classname="tests.unit.test__oracle_a" name="test_oracle_{i}">'
        f'<failure message="AssertionError: {i}">{long_tb}</failure></testcase>'
        for i in range(oracle.FAILURES_MAX + 3)
    )
    details = failure_details(
        f"<testsuites><testsuite>{cases}</testsuite></testsuites>", "test__oracle_"
    )
    assert len(details["failures"]) == oracle.FAILURES_MAX
    assert details["failures_omitted"] == 3
    for entry in details["failures"].values():
        assert len(entry["lines"]) <= oracle.FAILURE_LINES
        assert all(len(x) <= oracle.FAILURE_LINE_CHARS for x in entry["lines"])
    total = sum(len(x) for e in details["failures"].values() for x in e["lines"])
    assert total <= oracle.FAILURE_CHARS_MAX


def test_a_passing_oracle_keeps_nothing(config):
    out = outcome_from_junit(
        '<testsuites><testsuite><testcase classname="tests.unit.test__oracle_s3" name="t"/>'
        "</testsuite></testsuites>",
        config.tasks["hedgedoc-s3"].oracle,
    )
    assert out.details == {}


def test_the_outcome_carries_the_failures_and_validates(config, record):
    out = outcome_from_junit(FIXTURE.read_text(), config.tasks["new-pushgateway-k8s"].oracle)
    assert "failures" in out.details and "collection_errors" in out.details
    data = json.loads(record.model_dump_json())
    data["tiers"]["oracle"] = json.loads(out.model_dump_json())
    jsonschema.validate(data, json_schema())
    bad = {**data["tiers"]["oracle"], "details": {"failures": {"t": {"message": 1}}}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**data, "tiers": {**data["tiers"], "oracle": bad}}, json_schema())
