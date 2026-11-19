# How to add ingress

See first:

- [Traefik K8s charm on Charmhub](https://charmhub.io/traefik-k8s)
- [How to manage relations](https://canonical.com/juju/docs/ops/latest/howto/manage-relations/)

To expose HedgeDoc outside the cluster, integrate it with an ingress provider such as Traefik over the `ingress` interface. The charm requests ingress with the `charms.traefik_k8s.v2.ingress` library, reads back the URL Traefik assigns, and configures HedgeDoc from it. HedgeDoc computes a single `serverURL` at startup from `CMD_DOMAIN`, `CMD_PROTOCOL_USESSL` and `CMD_URL_PATH`, and derives every generated link, its stylesheet URLs, the websocket origin and the OAuth callbacks from it, so these three settings have to describe the address in the user's browser exactly.

## Implement the feature

### Fetch the library

In the charm directory, fetch the library with Charmcraft:

```bash
charmcraft fetch-lib charms.traefik_k8s.v2.ingress
```

This writes `lib/charms/traefik_k8s/v2/ingress.py`. Record it in `charmcraft.yaml` so that `charmcraft pack` keeps it up to date:

```yaml
charm-libs:
  - lib: traefik_k8s.ingress
    version: "2"
```

The library depends on `pydantic` (see `PYDEPS` at the top of the file), so add `pydantic >= 2` to the `dependencies` list in `pyproject.toml`.

### Declare the relation endpoint

In `charmcraft.yaml`, declare a `requires` endpoint named `ingress` using the `ingress` interface. One ingress is enough, so limit it to one relation:

```yaml
requires:
  ingress:
    interface: ingress
    optional: true
    limit: 1
```

### Request ingress from the provider

In `src/charm.py`, import `IngressPerAppRequirer` and instantiate it in `__init__`. Pass the port HedgeDoc listens on (3000) so that the library publishes the request as soon as the relation exists, and ask Traefik to strip the path prefix:

```python
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer


class HedgeDocCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)
        ...
        self.ingress = IngressPerAppRequirer(
            self, relation_name="ingress", port=hedgedoc.HEDGEDOC_PORT, strip_prefix=True
        )
```

Traefik routes each application under a path prefix of `/<model>-<app>` (for example `https://traefik.example.com/prod-hedgedoc-k8s`) unless it is configured with per-application hostnames. With `strip_prefix=True`, Traefik removes that prefix before forwarding, so HedgeDoc's routes stay mounted at `/` and only its *generated* links need the prefix, which `CMD_URL_PATH` supplies. Without `strip_prefix`, HedgeDoc would have to serve its routes under the prefix, which it cannot do: `CMD_URL_PATH` affects link generation only.

The library publishes, in the application databag of the relation, the model name, application name, port and the `strip-prefix` flag (each value JSON-encoded), and in the unit databag the unit's hostname and IP. It does this when the relation changes and again on `upgrade-charm` and `leader-elected`.

### Observe the library's events

The library emits `ready` when Traefik publishes a URL (or the URL changes) and `revoked` when the relation goes away. Both change HedgeDoc's desired configuration, so add them to the events that reconcile the workload:

```python
        for event in (
            self.on.config_changed,
            ...
            self.ingress.on.ready,
            self.ingress.on.revoked,
        ):
            framework.observe(event, self._reconcile)
```

Do not read the URL from the event: read `self.ingress.url` from inside the reconciler, so that every code path sees the same value. It is `None` when there is no relation, when Traefik has not written its URL yet, and during `relation-broken`.

### Derive HedgeDoc's settings from the URL

The ingress URL takes precedence over the `external-hostname` and `external-tls` configuration options: those describe a proxy that is outside Juju, and an ingress relation is the more specific statement. Keep the configuration options as the fallback for when the relation is absent or revoked.

Map the URL to HedgeDoc's environment as follows:

| Ingress URL | `CMD_DOMAIN` | `CMD_PROTOCOL_USESSL` | `CMD_URL_PATH` |
|---|---|---|---|
| `https://pad.example.com` | `pad.example.com` | `true` | *(unset)* |
| `https://traefik.example.com/prod-hedgedoc-k8s` | `traefik.example.com` | `true` | `prod-hedgedoc-k8s` |
| `http://10.0.0.5:8080/prod-hedgedoc-k8s` | `10.0.0.5:8080` | `false` | `prod-hedgedoc-k8s` |

- `CMD_PROTOCOL_USESSL` is `true` exactly when the scheme is `https`. Besides generating `https://` links, this marks the session cookie `Secure` and makes HedgeDoc honour `X-Forwarded-*` headers from the proxy.
- `CMD_URL_PATH` is a path *segment*, not a path: HedgeDoc joins it with a slash of its own, so strip the surrounding slashes (`prod-hedgedoc-k8s`, never `/prod-hedgedoc-k8s`). A leading slash produces double-slash links and 404s on newly created notes. Leave it unset when the path is empty.
- A non-default port belongs in `CMD_DOMAIN` (`10.0.0.5:8080`). Keep `CMD_URL_ADDPORT` set to `false`: it appends HedgeDoc's own listen port (3000), which is never the port the outside world uses.

Put the parsing in `src/hedgedoc.py`, next to the existing `ExternalUrl` mapping, so that it stays free of `ops` and can be tested on its own. `urllib.parse.urlsplit` gives you the scheme, hostname, port and path.

## Test the feature

Scenario (`ops.testing`) can simulate Traefik without a cluster. The real provider writes its application databag as the key `ingress` holding a JSON document with a `url` key:

```python
import json

from ops import testing


def _ingress(url: str) -> testing.Relation:
    return testing.Relation(
        endpoint="ingress",
        interface="ingress",
        remote_app_name="traefik",
        remote_app_data={"ingress": json.dumps({"url": url})},
    )
```

Run a reconciling event with the relation in the state and check the environment of the HedgeDoc service in the Pebble plan:

```python
def test_ingress_url_sets_domain_protocol_and_path(ctx, container):
    ingress = _ingress("https://traefik.example.com/prod-hedgedoc-k8s")
    state = testing.State(leader=True, containers={container}, relations={ingress})
    out = ctx.run(ctx.on.config_changed(), state)
    env = out.get_container("hedgedoc").plan.services["hedgedoc"].environment
    assert env["CMD_DOMAIN"] == "traefik.example.com"
    assert env["CMD_PROTOCOL_USESSL"] == "true"
    assert env["CMD_URL_PATH"] == "prod-hedgedoc-k8s"
```

Also cover:

- an `http://` URL, which must give `CMD_PROTOCOL_USESSL=false`;
- a relation plus `external-hostname` set, where the relation must win;
- `ctx.on.relation_broken(ingress)`, after which the configuration options (or the in-cluster address) must be used again and `CMD_URL_PATH` must be absent;
- `ctx.on.relation_changed(ingress)` on the leader, after which `out.get_relation(ingress.id).local_app_data` holds the request. The library JSON-encodes each value, so expect `{"model": '"<model>"', "name": '"hedgedoc-k8s"', "port": "3000", "strip-prefix": "true", ...}`.

Run the unit tests and the linters as described in `CLAUDE.md`.
