from __future__ import annotations

import pytest

from charmer_wof.adapters.base import AdapterResult, NotImplementedAdapter, ToolEvent
from charmer_wof.scoring.oracle import outcome_from_junit, parse_junit
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
