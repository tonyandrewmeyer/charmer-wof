Write a Kubernetes charm, `pushgateway-k8s`, for the Prometheus Pushgateway (https://github.com/prometheus/pushgateway), which lets ephemeral and batch jobs push their metrics so Prometheus can scrape them, using the `ops` library. This directory is empty apart from the context file; create the charm here. Requirements:

- A `charmcraft.yaml` for a Kubernetes charm on `ubuntu@24.04`, with a workload container named `pushgateway`, an OCI image resource named `pushgateway-image` (upstream `ubuntu/prometheus-pushgateway:1.11-24.04_stable`, whose binary is `/bin/pushgateway`), and filesystem storage named `pushgateway-store` mounted in the container at `/data`.
- The workload runs as a Pebble service named `pushgateway` that runs `/bin/pushgateway` with `--persistence.file=/data/metrics` (so pushed metrics survive restarts) and starts automatically. It listens on its default port, 9091; open that port on the unit.
- Prometheus must be able to scrape the Pushgateway: provide a `metrics-endpoint` endpoint (interface `prometheus_scrape`) using the `charms.prometheus_k8s.v0.prometheus_scrape` charm library's `MetricsEndpointProvider`, with a scrape job whose target is this unit's address on port 9091.
- Unit status: active when the service is running; waiting while the container isn't reachable yet.
- A uv-managed Python project (`pyproject.toml`) with the charm in `src/charm.py` and charm libraries under `lib/`, unit tests with `ops.testing` (Scenario) in `tests/unit/`, and ruff for linting. Make the unit tests pass, and run the linters and `uvx charmlint .` and fix what they report.

Do not ask questions; make reasonable decisions and finish the task.
