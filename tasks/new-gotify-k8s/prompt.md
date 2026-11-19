Write a Kubernetes charm, `gotify-k8s`, for Gotify server (https://gotify.net, source https://github.com/gotify/server), a self-hosted push-notification server, using the `ops` library. This directory is empty apart from the context file; create the charm here. Requirements:

- A `charmcraft.yaml` for a Kubernetes charm on `ubuntu@24.04`, with a workload container named `gotify`, an OCI image resource named `gotify-image` (upstream `ghcr.io/gotify/server:2.6.3`), and filesystem storage named `data` mounted in the container at `/app/data`.
- The workload runs as a Pebble service named `gotify` that runs `/app/gotify-app` (working directory `/app`) and starts automatically. Gotify reads its configuration from environment variables:
  - `GOTIFY_SERVER_PORT` from a `port` config option (integer, default 8080);
  - `GOTIFY_REGISTRATION` (`true` or `false`) from a `registration` config option (boolean, default false), which allows users to sign themselves up;
  - `GOTIFY_DEFAULTUSER_NAME` set to `admin`, and `GOTIFY_DEFAULTUSER_PASS` from an `admin-password` config option of type `secret`: the ID of a Juju user secret whose `password` key holds the password;
  - `GOTIFY_DATABASE_DIALECT` `sqlite3` and `GOTIFY_DATABASE_CONNECTION` `/app/data/gotify.db`, so the data lives on the storage.
- Unit status: active when the service is configured and running; blocked, with a message that names `admin-password`, when that option is unset or the secret can't be read; waiting while the container isn't reachable yet.
- Changing configuration must update the running service.
- A uv-managed Python project (`pyproject.toml`) with the charm in `src/charm.py`, unit tests with `ops.testing` (Scenario) in `tests/unit/`, and ruff for linting. Make the unit tests pass, and run the linters and `uvx charmlint .` and fix what they report.

Do not ask questions; make reasonable decisions and finish the task.
