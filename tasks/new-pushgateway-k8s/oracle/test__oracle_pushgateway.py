"""Generic checks for a new pushgateway-k8s charm, through ops.testing only.

They find the charm class in ``src/charm.py`` and go through ``State``, the
Pebble plan, opened ports, the unit status and the ``metrics-endpoint``
databag, so they don't depend on how the charm is structured beyond what the
brief asks for.
"""

import importlib
import inspect
import json
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

VERSION = "pushgateway, version 1.11.0 (branch: HEAD, revision: 0000000)\n"


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
    """charmcraft.yaml, with metadata.yaml merged in if present."""
    meta = {}
    for name in ("charmcraft.yaml", "metadata.yaml"):
        if (ROOT / name).exists():
            meta.update(yaml.safe_load((ROOT / name).read_text()) or {})
    return meta


@pytest.fixture
def ctx():
    return testing.Context(_charm_class())


def _container(can_connect=True):
    # Accept a version probe, which a charm may run to set the workload version.
    return testing.Container(
        "pushgateway",
        can_connect=can_connect,
        execs={testing.Exec(["/bin/pushgateway"], return_code=0, stdout=VERSION)},
    )


def _service(out):
    plan = out.get_container("pushgateway").plan
    assert "pushgateway" in plan.services, f"no 'pushgateway' service in {sorted(plan.services)}"
    return plan.services["pushgateway"]


def test_oracle_metadata_declares_container_resource_storage_and_endpoint():
    meta = _meta()
    container = meta.get("containers", {}).get("pushgateway", {})
    assert container.get("resource") == "pushgateway-image"
    assert meta.get("resources", {}).get("pushgateway-image", {}).get("type") == "oci-image"
    assert "pushgateway-store" in meta.get("storage", {})
    assert {"storage": "pushgateway-store", "location": "/data"} in [
        {"storage": m.get("storage"), "location": m.get("location")}
        for m in container.get("mounts", [])
    ]
    endpoint = meta.get("provides", {}).get("metrics-endpoint", {})
    assert endpoint.get("interface") == "prometheus_scrape"


def test_oracle_pebble_ready_starts_pushgateway_with_persistence(ctx):
    container = _container()
    out = ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))
    svc = _service(out)
    assert "/bin/pushgateway" in svc.command
    assert "--persistence.file=/data/metrics" in svc.command
    assert svc.startup == "enabled"
    assert out.get_container("pushgateway").service_statuses.get("pushgateway") == (
        ops.pebble.ServiceStatus.ACTIVE
    )
    assert isinstance(out.unit_status, testing.ActiveStatus)


def test_oracle_port_9091_is_opened(ctx):
    container = _container()
    out = ctx.run(ctx.on.pebble_ready(container), testing.State(containers={container}))
    assert testing.TCPPort(9091) in out.opened_ports


def test_oracle_scrape_job_targets_port_9091(ctx):
    container = _container()
    metrics = testing.Relation("metrics-endpoint", interface="prometheus_scrape")
    state = testing.State(leader=True, containers={container}, relations={metrics})
    out = ctx.run(ctx.on.relation_joined(metrics, remote_unit=0), state)
    container = out.get_container("pushgateway")
    out = ctx.run(ctx.on.pebble_ready(container), out)
    data = out.get_relation(metrics.id).local_app_data
    assert "scrape_jobs" in data, f"no scrape_jobs in {sorted(data)}"
    jobs = json.loads(data["scrape_jobs"])
    targets = [
        target
        for job in jobs
        for static in job.get("static_configs", [])
        for target in static.get("targets", [])
    ]
    assert any(t.endswith(":9091") for t in targets), targets


def test_oracle_unreachable_container_waits(ctx):
    container = _container(can_connect=False)
    out = ctx.run(ctx.on.config_changed(), testing.State(containers={container}))
    assert isinstance(out.unit_status, testing.WaitingStatus)
