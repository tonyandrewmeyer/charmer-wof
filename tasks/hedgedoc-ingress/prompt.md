Add ingress support to this charm using the `traefik_k8s` `ingress` v2 charm library (charms.traefik_k8s.v2.ingress), so that HedgeDoc works correctly behind Traefik. Traefik serves the application under the path `/<model>-<app>` on its own hostname. Requirements:

- When an ingress relation provides a URL, it must take precedence over the `external-hostname` and `external-tls` configuration options, which remain the fallback when there is no ingress.
- HedgeDoc must generate correct links behind the ingress: the hostname, the scheme (http or https) and the sub-path must all match the ingress URL.
- Keep the existing unit tests passing, and add unit tests for the new behaviour.
- Run the linters and fix anything they report.

This charm already integrates with several other charms; follow its existing structure and conventions. Work in this directory. Do not ask questions; make reasonable decisions and finish the task.
