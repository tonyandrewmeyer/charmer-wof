# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.

"""Hidden acceptance tests for ingress support (the scoring oracle).

These go through ``ops.testing.State``, the Pebble plan and relation databags
only, so they do not depend on how the charm is structured internally. They
encode what the real Traefik v2 ``ingress`` provider publishes:
``{"ingress": json.dumps({"url": "<scheme>://<host>[:port]/<model>-<app>"})}``
in its application databag.
"""

import json
import pathlib

import ops
import pytest
from ops import testing

from charm import HedgeDocCharm


@pytest.fixture
def ctx():
    return testing.Context(HedgeDocCharm)


@pytest.fixture
def container():
    # Accept any exec (chown, stat, ...) the charm may issue while reconciling.
    return testing.Container(
        "hedgedoc",
        can_connect=True,
        execs={
            testing.Exec(["chown"], return_code=0),
            testing.Exec(["stat"], return_code=0, stdout="0:0\n"),
        },
    )


def _peers() -> set:
    """Peer relations the charm declares; some charms wait on them before reconciling."""
    try:
        import yaml  # type: ignore

        meta = yaml.safe_load(pathlib.Path("charmcraft.yaml").read_text()) or {}
        return {testing.PeerRelation(name) for name in (meta.get("peers") or {})}
    except Exception:  # noqa: BLE001
        return set()


def _ingress(url: str) -> testing.Relation:
    return testing.Relation(
        endpoint="ingress",
        interface="ingress",
        remote_app_name="traefik",
        remote_app_data={"ingress": json.dumps({"url": url})},
    )


def _env(state: testing.State) -> dict:
    """The environment of the HedgeDoc service, whatever the layer is called."""
    plan = state.get_container("hedgedoc").plan
    services = plan.services
    assert services, "no Pebble services in the plan"
    service = services.get("hedgedoc") or next(iter(services.values()))
    return dict(service.environment)


def _bool(value: object) -> bool:
    """HedgeDoc accepts true/false (any case) and 1/0 for boolean settings."""
    return str(value).strip().lower() in {"true", "1", "yes"}


def _bag(value: str):
    """Requirer databag values are JSON-encoded by the library; tolerate raw strings too."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def test_oracle_http_url_with_subpath_sets_domain_scheme_and_path(ctx, container):
    ingress = _ingress("http://traefik.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(leader=True, containers={container}, relations={ingress, *_peers()})
    out = ctx.run(ctx.on.config_changed(), state)
    env = _env(out)
    assert env["CMD_DOMAIN"] == "traefik.example.com"
    assert not _bool(env["CMD_PROTOCOL_USESSL"])
    assert env["CMD_URL_PATH"] == "mymodel-hedgedoc-k8s"
    if "CMD_URL_ADDPORT" in env:
        assert not _bool(env["CMD_URL_ADDPORT"])


def test_oracle_https_url_sets_usessl(ctx, container):
    ingress = _ingress("https://pad.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(leader=True, containers={container}, relations={ingress, *_peers()})
    out = ctx.run(ctx.on.config_changed(), state)
    env = _env(out)
    assert env["CMD_DOMAIN"] == "pad.example.com"
    assert _bool(env["CMD_PROTOCOL_USESSL"])
    assert env["CMD_URL_PATH"] == "mymodel-hedgedoc-k8s"


def test_oracle_non_default_port_is_carried_in_the_domain(ctx, container):
    ingress = _ingress("http://10.0.0.5:8080/mymodel-hedgedoc-k8s")
    state = testing.State(leader=True, containers={container}, relations={ingress, *_peers()})
    out = ctx.run(ctx.on.config_changed(), state)
    env = _env(out)
    assert env["CMD_DOMAIN"] == "10.0.0.5:8080"
    if "CMD_URL_ADDPORT" in env:
        assert not _bool(env["CMD_URL_ADDPORT"])


def test_oracle_ingress_overrides_external_hostname_config(ctx, container):
    ingress = _ingress("https://pad.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(
        leader=True,
        containers={container},
        relations={ingress, *_peers()},
        config={"external-hostname": "configured.example.org", "external-tls": False},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    env = _env(out)
    assert env["CMD_DOMAIN"] == "pad.example.com"
    assert _bool(env["CMD_PROTOCOL_USESSL"])
    assert env["CMD_URL_PATH"] == "mymodel-hedgedoc-k8s"


def test_oracle_ingress_ready_event_reconciles(ctx, container):
    """The library's relation-changed handling must reach the workload, not only config-changed."""
    ingress = _ingress("https://pad.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(leader=True, containers={container}, relations={ingress, *_peers()})
    out = ctx.run(ctx.on.relation_changed(ingress), state)
    env = _env(out)
    assert env["CMD_DOMAIN"] == "pad.example.com"
    assert env["CMD_URL_PATH"] == "mymodel-hedgedoc-k8s"


def test_oracle_relation_broken_falls_back_to_config(ctx, container):
    ingress = _ingress("https://pad.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(
        leader=True,
        containers={container},
        relations={ingress, *_peers()},
        config={"external-hostname": "configured.example.org", "external-tls": True},
    )
    out = ctx.run(ctx.on.relation_broken(ingress), state)
    env = _env(out)
    assert env["CMD_DOMAIN"] == "configured.example.org"
    assert _bool(env["CMD_PROTOCOL_USESSL"])
    assert env.get("CMD_URL_PATH", "") == ""


def test_oracle_requirer_publishes_model_name_and_port(ctx, container):
    ingress = _ingress("http://traefik.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(
        leader=True, containers={container}, relations={ingress, *_peers()}, model=testing.Model("mymodel")
    )
    out = ctx.run(ctx.on.relation_changed(ingress), state)
    data = {k: _bag(v) for k, v in out.get_relation(ingress.id).local_app_data.items()}
    assert data.get("model") == "mymodel"
    assert data.get("name") == "hedgedoc-k8s"
    assert int(data.get("port")) == 3000


def test_oracle_requirer_asks_traefik_to_strip_the_prefix(ctx, container):
    ingress = _ingress("http://traefik.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(leader=True, containers={container}, relations={ingress, *_peers()})
    out = ctx.run(ctx.on.relation_changed(ingress), state)
    data = {k: _bag(v) for k, v in out.get_relation(ingress.id).local_app_data.items()}
    assert _bool(data.get("strip-prefix", False))


def test_oracle_status_is_active_with_ingress(ctx, container):
    ingress = _ingress("https://pad.example.com/mymodel-hedgedoc-k8s")
    state = testing.State(leader=True, containers={container}, relations={ingress, *_peers()})
    out = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(out.unit_status, ops.ActiveStatus)


def test_oracle_ingress_without_url_yet_does_not_break_the_workload(ctx, container):
    """Traefik writes its URL some time after the relation is created."""
    ingress = testing.Relation(endpoint="ingress", interface="ingress", remote_app_name="traefik")
    state = testing.State(
        leader=True,
        containers={container},
        relations={ingress, *_peers()},
        config={"external-hostname": "configured.example.org"},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    env = _env(out)
    assert env["CMD_DOMAIN"] == "configured.example.org"
    assert isinstance(out.unit_status, (ops.ActiveStatus, ops.WaitingStatus))
