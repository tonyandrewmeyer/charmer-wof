Add a `get-configuration` action to this charm, so operators can inspect the HAProxy configuration a unit is running, for debugging, without shell access to the unit. Requirements:

- The action takes no parameters. It reads the configuration currently on disk (`/etc/haproxy/haproxy.cfg`, the file HAProxy is running with), using the file helpers in `src/haproxy.py`, and returns it in the `configuration` result, with a `source` result of `disk`. It must not write to disk or reload the service.
- If the file isn't there, the action fails with a message saying it was not found.
- Client IP hash salts (see the `client-ip-hash-salt` option) must not leak: redact any salt in the returned configuration as `<redacted>`.
- If the configuration on disk is exactly the charm's default configuration (what it renders when no proxy backends are configured; `HAProxyService.render_default_config`), log a message through the action that says it matches the default configuration and that this usually means no proxy backends are configured. If the charm state needed to render the default is invalid, still return the configuration, and log that it could not determine whether this is the default configuration.
- Keep the existing unit tests passing, and add unit tests for the new behaviour.
- Run the linters and fix anything they report.

Follow the charm's existing structure and conventions. Work in this directory. Do not ask questions; make reasonable decisions and finish the task.
