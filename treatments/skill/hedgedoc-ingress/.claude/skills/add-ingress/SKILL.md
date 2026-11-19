---
name: add-ingress
description: Add ingress support to this charm with the traefik_k8s ingress (v2) charm library, so that HedgeDoc is reachable behind Traefik and generates correct links. Use when asked to add ingress, integrate with Traefik, expose the app externally, give it an external URL, or make it work behind a reverse proxy or under a sub-path.
---

# Add ingress

HedgeDoc computes a single `serverURL` at startup from `CMD_DOMAIN`,
`CMD_PROTOCOL_USESSL` and `CMD_URL_PATH`, and derives every generated link,
its stylesheet URLs, the websocket origin and the OAuth callbacks from it.
These three settings have to describe the address in the user's browser
exactly, so the charm reads them from the URL the ingress assigns.

## Steps

1. **Fetch the library.** Run `charmcraft fetch-lib charms.traefik_k8s.v2.ingress`
   in the charm directory. It writes `lib/charms/traefik_k8s/v2/ingress.py`.

2. **Record the library in `charmcraft.yaml`** so `charmcraft pack` keeps it up
   to date:

   ```yaml
   charm-libs:
     - lib: traefik_k8s.ingress
       version: "2"
   ```

3. **Add the library's dependency.** The library needs `pydantic` (see `PYDEPS`
   at the top of the file). Add `pydantic >= 2` to `dependencies` in
   `pyproject.toml`.

4. **Declare the endpoint** in `charmcraft.yaml`:

   ```yaml
   requires:
     ingress:
       interface: ingress
       optional: true
       limit: 1
   ```

5. **Instantiate the requirer** in `HedgeDocCharm.__init__`. Pass the port
   HedgeDoc listens on so the request is published as soon as the relation
   exists, and ask Traefik to strip the path prefix:

   ```python
   from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer

   self.ingress = IngressPerAppRequirer(
       self, relation_name="ingress", port=hedgedoc.HEDGEDOC_PORT, strip_prefix=True
   )
   ```

   Traefik routes each application under `/<model>-<app>` (for example
   `https://traefik.example.com/prod-hedgedoc-k8s`) unless configured with
   per-application hostnames. With `strip_prefix=True` Traefik removes that
   prefix before forwarding, so HedgeDoc's routes stay mounted at `/` and only
   its *generated* links need the prefix, which `CMD_URL_PATH` supplies.
   Without `strip_prefix`, HedgeDoc would have to serve its routes under the
   prefix, which it cannot do: `CMD_URL_PATH` affects link generation only.

   The library publishes, in the relation's application databag, the model
   name, application name, port and the `strip-prefix` flag (each value
   JSON-encoded), and in the unit databag the unit's hostname and IP. It does
   this when the relation changes and again on `upgrade-charm` and
   `leader-elected`.

6. **Observe the library's events.** It emits `ready` when Traefik publishes
   a URL (or it changes) and `revoked` when the relation goes away. Add
   `self.ingress.on.ready` and `self.ingress.on.revoked` to the events that
   call `_reconcile`. Do not read the URL from the event: read
   `self.ingress.url` inside the reconciler so every code path sees the same
   value. It is `None` with no relation, before Traefik writes its URL, and
   during `relation-broken`.

7. **Give the ingress URL precedence** over `external-hostname` and
   `external-tls`: those describe a proxy outside Juju, and an ingress
   relation is the more specific statement. Keep the config options as the
   fallback when the relation is absent or revoked.

8. **Map the URL to HedgeDoc's environment.** Put the parsing in
   `src/hedgedoc.py` next to the existing `ExternalUrl` mapping, free of
   `ops`; `urllib.parse.urlsplit` gives scheme, hostname, port and path.

   | Ingress URL | `CMD_DOMAIN` | `CMD_PROTOCOL_USESSL` | `CMD_URL_PATH` |
   |---|---|---|---|
   | `https://pad.example.com` | `pad.example.com` | `true` | *(unset)* |
   | `https://traefik.example.com/prod-hedgedoc-k8s` | `traefik.example.com` | `true` | `prod-hedgedoc-k8s` |
   | `http://10.0.0.5:8080/prod-hedgedoc-k8s` | `10.0.0.5:8080` | `false` | `prod-hedgedoc-k8s` |

   - `CMD_PROTOCOL_USESSL` is `true` exactly when the scheme is `https`.
     Besides generating `https://` links, this marks the session cookie
     `Secure` and makes HedgeDoc honour `X-Forwarded-*` headers.
   - `CMD_URL_PATH` is a path *segment*: HedgeDoc joins it with its own
     slash, so strip the surrounding slashes (`prod-hedgedoc-k8s`, never
     `/prod-hedgedoc-k8s`). A leading slash produces double-slash links and
     404s on newly created notes. Leave it unset when the path is empty.
   - A non-default port belongs in `CMD_DOMAIN` (`10.0.0.5:8080`). Keep
     `CMD_URL_ADDPORT` at `false`: it appends HedgeDoc's own listen port
     (3000), never the port the outside world uses.

9. **Test with Scenario** (`ops.testing`). The real provider writes its
   application databag as the key `ingress` holding JSON with a `url` key:

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

   Run a reconciling event with the relation in the state and check the
   HedgeDoc service's environment in the Pebble plan:

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

   Also cover: an `http://` URL (`CMD_PROTOCOL_USESSL=false`); a relation plus
   `external-hostname` set, where the relation wins;
   `ctx.on.relation_broken(ingress)`, after which the config options (or the
   in-cluster address) are used again and `CMD_URL_PATH` is absent; and
   `ctx.on.relation_changed(ingress)` on the leader, after which
   `out.get_relation(ingress.id).local_app_data` holds the request. The
   library JSON-encodes each value, so expect
   `{"model": '"<model>"', "name": '"hedgedoc-k8s"', "port": "3000", "strip-prefix": "true", ...}`.

10. **Run the unit tests and the linters** as described in `CLAUDE.md`.
