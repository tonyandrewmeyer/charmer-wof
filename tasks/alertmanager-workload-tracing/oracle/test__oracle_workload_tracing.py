# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Feature: alertmanager sends workload traces to a related tempo."""

import dataclasses
import json

import pytest
import yaml
from helpers import workload_file
from ops.testing import Context, Relation, State


TEMPO_HOST = "tempo-0.tempo-endpoints.cos.svc.cluster.local:4318"


def receivers(scheme: str) -> str:
    return json.dumps(
        [
            {
                "protocol": {"name": "otlp_http", "type": "http"},
                "url": f"{scheme}://{TEMPO_HOST}/v1/traces",
            }
        ]
    )


def tracing_relation(scheme: str) -> Relation:
    return Relation(
        "tracing", remote_app_name="tempo", remote_app_data={"receivers": receivers(scheme)}
    )


def rendered_config(context: Context, state: State, alertmanager_charm) -> dict:
    return yaml.safe_load(workload_file(context, state, alertmanager_charm._config_path))


def test_oracle_workload_config_has_no_tracing_section_without_a_tracing_relation(
    context: Context, base_state: State, alertmanager_charm
):
    # GIVEN a charm with no tracing relation
    # WHEN the charm reconciles
    state = context.run(context.on.config_changed(), base_state)

    # THEN the workload config has no tracing section
    assert "tracing" not in rendered_config(context, state, alertmanager_charm)


def test_oracle_an_http_tempo_endpoint_is_written_to_the_workload_config(
    context: Context, base_state: State, alertmanager_charm
):
    # GIVEN a charm related to a tempo serving OTLP over HTTP
    relation = tracing_relation("http")
    state_in = dataclasses.replace(base_state, relations=[*base_state.relations, relation])

    # WHEN the charm reconciles
    state = context.run(context.on.relation_changed(relation), state_in)

    # THEN the workload traces to tempo over an insecure connection
    tracing = rendered_config(context, state, alertmanager_charm)["tracing"]
    assert tracing["client_type"] == "http"
    assert tracing["endpoint"] == TEMPO_HOST
    assert tracing["insecure"] is True
    assert "tls_config" not in tracing


def test_oracle_an_https_tempo_endpoint_is_written_to_the_workload_config(
    context: Context, base_state: State, alertmanager_charm
):
    # GIVEN a charm related to a tempo serving OTLP over HTTPS
    relation = tracing_relation("https")
    state_in = dataclasses.replace(base_state, relations=[*base_state.relations, relation])

    # WHEN the charm reconciles
    state = context.run(context.on.relation_changed(relation), state_in)

    # THEN the workload traces to tempo securely, verifying against a CA certificate
    tracing = rendered_config(context, state, alertmanager_charm)["tracing"]
    assert tracing["endpoint"] == TEMPO_HOST
    assert tracing["insecure"] is False
    assert "ca_file" in tracing["tls_config"]


def test_oracle_tracing_is_removed_from_the_workload_config_when_tempo_goes_away(
    context: Context, base_state: State, alertmanager_charm
):
    # GIVEN a charm that is tracing to tempo
    relation = tracing_relation("http")
    state_in = dataclasses.replace(base_state, relations=[*base_state.relations, relation])
    state = context.run(context.on.relation_changed(relation), state_in)
    assert "tracing" in rendered_config(context, state, alertmanager_charm)

    # WHEN the tracing relation is broken, which it departs during, not before
    state = context.run(context.on.relation_broken(state.get_relation(relation.id)), state)

    # THEN the workload no longer traces
    assert "tracing" not in rendered_config(context, state, alertmanager_charm)


@pytest.mark.parametrize(
    "topology_key",
    ["juju_application", "juju_model", "juju_model_uuid", "juju_unit", "juju_charm"],
)
def test_oracle_workload_traces_are_labelled_with_juju_topology(
    context: Context, base_state: State, topology_key: str
):
    # GIVEN a ready charm
    # WHEN the charm reconciles
    state = context.run(context.on.config_changed(), base_state)

    # THEN the workload is started with juju topology in its OTEL resource attributes
    environment = state.get_container("alertmanager").plan.services["alertmanager"].environment
    assert f"{topology_key}=" in environment["OTEL_RESOURCE_ATTRIBUTES"]
