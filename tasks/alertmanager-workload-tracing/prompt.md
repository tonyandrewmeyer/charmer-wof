Add workload tracing to this charm: Alertmanager should send its own traces to a tracing backend such as Tempo. Use the `tempo_coordinator_k8s` `tracing` charm library (charms.tempo_coordinator_k8s.v0.tracing) on a new `tracing` endpoint (interface `tracing`, at most one relation), and request the `otlp_http` protocol. Requirements:

- When the tracing relation provides an OTLP HTTP endpoint, the rendered `alertmanager.yml` must have a `tracing` section that sends traces to it: `client_type` `http`, `endpoint` as `host:port` (Alertmanager doesn't accept a scheme or path there), and a sampling fraction of 1.0.
- For a plain `http` endpoint, set `insecure: true`. For `https`, set `insecure: false` and verify the backend against the CA certificate the charm already uses (`tls_config.ca_file`).
- When the relation goes away, the `tracing` section must go too.
- Label the workload's traces with the Juju topology: start Alertmanager with an `OTEL_RESOURCE_ATTRIBUTES` environment variable carrying `juju_application`, `juju_model`, `juju_model_uuid`, `juju_unit` and `juju_charm`.
- The existing `charm-tracing` endpoint (the charm's own traces) stays as it is.
- Keep the existing unit tests passing, and add unit tests for the new behaviour.
- Run the linters and fix anything they report.

Follow the charm's existing structure and conventions. Work in this directory. Do not ask questions; make reasonable decisions and finish the task.
