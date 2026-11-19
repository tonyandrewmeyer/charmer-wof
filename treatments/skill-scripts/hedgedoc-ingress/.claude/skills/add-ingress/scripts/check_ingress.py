#!/usr/bin/env python3
# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.
"""Verify that the charm's ingress support behaves correctly behind Traefik.

Run from the charm root with the project's environment, so that ops and the
charm's dependencies are importable:

    uv run python .claude/skills/add-ingress/scripts/check_ingress.py

Prints one line per check, then PASS or FAIL. Exit status 0 only on PASS.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import traceback

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record and print one check result."""
    CHECKS.append((name, ok, detail))
    print(f"[{'ok' if ok else 'FAIL'}] {name}{(': ' + detail) if detail and not ok else ''}")


def _decode(value: str) -> object:
    """Databag values are JSON-encoded by the library; tolerate raw strings too."""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def main() -> int:
    """Run every check and return 0 only if all pass."""
    root = pathlib.Path.cwd()
    charmcraft = root / "charmcraft.yaml"
    text = charmcraft.read_text() if charmcraft.exists() else ""
    check(
        "charmcraft.yaml declares requires.ingress",
        bool(re.search(r"^requires:(?:\n(?:  .*|))*?\n  ingress:\s*$", text, re.M))
        or bool(re.search(r"^  ingress:\s*\n(?:    .*\n)*?    interface:\s*ingress", text, re.M)),
    )
    check(
        "charmcraft.yaml lists charm-libs traefik_k8s.ingress",
        bool(re.search(r"lib:\s*traefik_k8s\.ingress\b", text)),
    )
    lib = root / "lib/charms/traefik_k8s/v2/ingress.py"
    check("lib/charms/traefik_k8s/v2/ingress.py exists", lib.exists())

    sys.path[:0] = [str(root / "src"), str(root / "lib")]
    try:
        import ops
        from ops import testing

        import charm as charm_module
    except Exception as e:
        check("charm imports", False, f"{type(e).__name__}: {e}")
        return _finish()
    charm_cls = next(
        (
            c
            for c in vars(charm_module).values()
            if isinstance(c, type) and issubclass(c, ops.CharmBase) and c is not ops.CharmBase
        ),
        None,
    )
    check("charm class found", charm_cls is not None)
    if charm_cls is None:
        return _finish()

    def container():
        return testing.Container(
            "hedgedoc",
            can_connect=True,
            execs={
                testing.Exec(["chown"], return_code=0),
                testing.Exec(["stat"], return_code=0, stdout="0:0\n"),
            },
        )

    def peers():
        try:
            import yaml  # type: ignore

            meta = yaml.safe_load(charmcraft.read_text()) or {}
            return {testing.PeerRelation(name) for name in (meta.get("peers") or {})}
        except Exception:
            return set()

    def ingress(url):
        return testing.Relation(
            endpoint="ingress",
            interface="ingress",
            remote_app_name="traefik",
            remote_app_data={"ingress": json.dumps({"url": url})},
        )

    def env_of(state):
        services = state.get_container("hedgedoc").plan.services
        if not services:
            return {}
        service = services.get("hedgedoc") or next(iter(services.values()))
        return dict(service.environment)

    try:
        ctx = testing.Context(charm_cls)
        rel = ingress("http://traefik.example.com/mymodel-hedgedoc-k8s")
        out = ctx.run(
            ctx.on.config_changed(),
            testing.State(
                leader=True,
                containers={container()},
                relations={rel, *peers()},
                config={"external-hostname": "configured.example.org"},
            ),
        )
        env = env_of(out)
        check(
            "CMD_DOMAIN is the ingress host (overriding external-hostname)",
            env.get("CMD_DOMAIN") == "traefik.example.com",
            repr(env.get("CMD_DOMAIN")),
        )
        check(
            "CMD_PROTOCOL_USESSL is false for http",
            not _bool(env.get("CMD_PROTOCOL_USESSL", "false")),
            repr(env.get("CMD_PROTOCOL_USESSL")),
        )
        check(
            "CMD_URL_PATH is the prefix without slashes",
            env.get("CMD_URL_PATH") == "mymodel-hedgedoc-k8s",
            repr(env.get("CMD_URL_PATH")),
        )
        check(
            "status is active",
            isinstance(out.unit_status, ops.ActiveStatus),
            repr(out.unit_status),
        )

        rel2 = ingress("https://pad.example.com:8443/mymodel-hedgedoc-k8s")
        out = ctx.run(
            ctx.on.config_changed(),
            testing.State(leader=True, containers={container()}, relations={rel2, *peers()}),
        )
        env = env_of(out)
        check(
            "CMD_PROTOCOL_USESSL is true for https",
            _bool(env.get("CMD_PROTOCOL_USESSL", "false")),
            repr(env.get("CMD_PROTOCOL_USESSL")),
        )
        check(
            "non-default port is carried in CMD_DOMAIN",
            env.get("CMD_DOMAIN") == "pad.example.com:8443",
            repr(env.get("CMD_DOMAIN")),
        )

        out = ctx.run(
            ctx.on.relation_broken(rel),
            testing.State(
                leader=True,
                containers={container()},
                relations={rel, *peers()},
                config={"external-hostname": "configured.example.org", "external-tls": True},
            ),
        )
        env = env_of(out)
        check(
            "relation-broken falls back to external-hostname",
            env.get("CMD_DOMAIN") == "configured.example.org",
            repr(env.get("CMD_DOMAIN")),
        )
        check(
            "relation-broken clears CMD_URL_PATH",
            not env.get("CMD_URL_PATH"),
            repr(env.get("CMD_URL_PATH")),
        )

        out = ctx.run(
            ctx.on.relation_changed(rel),
            testing.State(
                leader=True,
                containers={container()},
                relations={rel, *peers()},
                model=testing.Model("mymodel"),
            ),
        )
        bag = {k: _decode(v) for k, v in out.get_relation(rel.id).local_app_data.items()}
        check(
            "requirer publishes model/name/port",
            bag.get("model") == "mymodel" and bag.get("name") and str(bag.get("port")) == "3000",
            repr(bag),
        )
        check(
            "requirer asks Traefik to strip the prefix",
            _bool(bag.get("strip-prefix", False)),
            repr(bag.get("strip-prefix")),
        )
    except Exception as e:
        check("scenario checks ran without error", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    return _finish()


def _finish() -> int:
    failed = [c for c in CHECKS if not c[1]]
    print("PASS" if not failed else f"FAIL ({len(failed)} of {len(CHECKS)} checks failed)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
