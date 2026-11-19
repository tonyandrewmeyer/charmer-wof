"""Generic checks for a new gotify-k8s charm, through ops.testing only.

They find the charm class in ``src/charm.py`` and go through ``State``, the
Pebble plan and the unit status, so they don't depend on how the charm is
structured beyond what the brief asks for.
"""

import importlib
import inspect
import pathlib
import sys

import ops
import pytest
import yaml
from ops import testing

ROOT = pathlib.Path(__file__).resolve().parents[2]
for _p in (ROOT / "lib", ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _charm_class():
    module = importlib.import_module("charm")
    found = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, ops.CharmBase) and obj.__module__ == module.__name__
    ]
    assert found, "no CharmBase subclass in src/charm.py"
    return found[0]


def _meta():
    """charmcraft.yaml, with metadata.yaml and config.yaml merged in if present."""
    meta = {}
    for name in ("charmcraft.yaml", "metadata.yaml"):
        if (ROOT / name).exists():
            meta.update(yaml.safe_load((ROOT / name).read_text()) or {})
    if (ROOT / "config.yaml").exists():
        meta["config"] = yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    return meta


@pytest.fixture
def ctx():
    return testing.Context(_charm_class())


@pytest.fixture
def secret():
    return testing.Secret(tracked_content={"password": "s3cr3t-Pa55"}, owner=None)


def _container(can_connect=True):
    return testing.Container("gotify", can_connect=can_connect)


def _run(ctx, secret, config=None, can_connect=True, event="pebble_ready"):
    container = _container(can_connect)
    cfg = {"admin-password": secret.id} if secret is not None else {}
    cfg.update(config or {})
    state = testing.State(
        leader=True,
        containers={container},
        config=cfg,
        secrets={secret} if secret is not None else set(),
    )
    if event == "pebble_ready":
        ev = ctx.on.pebble_ready(container)
    else:
        ev = ctx.on.config_changed()
    return ctx.run(ev, state)


def _service(out):
    plan = out.get_container("gotify").plan
    assert "gotify" in plan.services, f"no 'gotify' service in {sorted(plan.services)}"
    return plan.services["gotify"]


def test_oracle_metadata_declares_container_resource_and_storage():
    meta = _meta()
    assert "gotify" in meta.get("containers", {})
    assert meta.get("resources", {}).get("gotify-image", {}).get("type") == "oci-image"
    assert meta["containers"]["gotify"].get("resource") == "gotify-image"
    assert "data" in meta.get("storage", {})
    mounts = meta["containers"]["gotify"].get("mounts", [])
    assert {"storage": "data", "location": "/app/data"} in [
        {"storage": m.get("storage"), "location": m.get("location")} for m in mounts
    ]


def test_oracle_config_options_declared():
    options = (_meta().get("config") or {}).get("options", {})
    assert options.get("port", {}).get("type") == "int"
    assert options["port"].get("default") == 8080
    assert options.get("registration", {}).get("type") == "boolean"
    assert options["registration"].get("default") is False
    assert options.get("admin-password", {}).get("type") == "secret"


def test_oracle_pebble_ready_starts_gotify(ctx, secret):
    out = _run(ctx, secret)
    svc = _service(out)
    assert "/app/gotify-app" in svc.command
    assert svc.startup == "enabled"
    assert out.get_container("gotify").service_statuses.get("gotify") == (
        ops.pebble.ServiceStatus.ACTIVE
    )
    assert isinstance(out.unit_status, testing.ActiveStatus)


def test_oracle_environment_defaults(ctx, secret):
    env = _service(_run(ctx, secret)).environment
    assert str(env.get("GOTIFY_SERVER_PORT")) == "8080"
    assert str(env.get("GOTIFY_REGISTRATION")).lower() == "false"
    assert env.get("GOTIFY_DEFAULTUSER_NAME") == "admin"
    assert env.get("GOTIFY_DATABASE_DIALECT") == "sqlite3"
    assert env.get("GOTIFY_DATABASE_CONNECTION") == "/app/data/gotify.db"


def test_oracle_admin_password_comes_from_the_secret(ctx, secret):
    env = _service(_run(ctx, secret)).environment
    assert env.get("GOTIFY_DEFAULTUSER_PASS") == "s3cr3t-Pa55"


def test_oracle_config_changed_updates_port_and_registration(ctx, secret):
    out = _run(ctx, secret, {"port": 9090, "registration": True}, event="config_changed")
    env = _service(out).environment
    assert str(env.get("GOTIFY_SERVER_PORT")) == "9090"
    assert str(env.get("GOTIFY_REGISTRATION")).lower() == "true"


def test_oracle_missing_admin_password_blocks(ctx):
    out = _run(ctx, None)
    assert isinstance(out.unit_status, testing.BlockedStatus)
    assert "admin-password" in out.unit_status.message


def test_oracle_unreachable_container_waits(ctx, secret):
    out = _run(ctx, secret, can_connect=False, event="config_changed")
    assert isinstance(out.unit_status, testing.WaitingStatus)
