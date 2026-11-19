You are grading a code change to a Juju Kubernetes charm (Python, the `ops` library) made by an AI coding agent: workload tracing for alertmanager-k8s. Grade ONLY the diff; be strict and consistent.

Rubric (integers; total 0-20):
1. correctness (0-8): A `requires: tracing` endpoint (interface tracing, limit 1) with `TracingEndpointRequirer(protocols=["otlp_http"])`; the relation's otlp_http URL becomes a `tracing` section in alertmanager.yml with `client_type: http`, `endpoint` as host:port (no scheme, no path), `sampling_fraction` 1.0, `insecure` true for http and false for https, `tls_config.ca_file` set to the charm's CA path for https; the section disappears on relation-broken; relation changed/broken events trigger the normal reconcile; `OTEL_RESOURCE_ATTRIBUTES` carries the five juju_* topology keys in the Pebble layer. Subtract roughly 2 per substantive defect.
2. ops_idiom_structure (0-6): Fits the charm's structure (config rendering in `ConfigBuilder`, the layer in `WorkloadManager`, events funnelled to the common exit hook); reads the endpoint from the library (`is_ready`/`get_endpoint`) rather than caching event data; doesn't disturb `charm-tracing`; no dead code or duplication.
3. tests (0-3): ops.testing tests cover http, https, relation removal and the topology attributes, through State and the rendered config file rather than private attributes.
4. no_collateral_damage (0-3): Existing behaviour and tests intact, no unrelated refactors, no edits to other vendored libs, no weakened lint or test config, no stray files.

Respond with a single JSON object and nothing else, of the form:
{"correctness": n, "ops_idiom_structure": n, "tests": n, "no_collateral_damage": n, "total": n, "notes": "<at most 60 words on the main defects>"}
