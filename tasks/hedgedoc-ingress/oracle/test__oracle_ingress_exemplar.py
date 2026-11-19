# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.

"""The exemplar's own ingress tests, used as the T2 oracle."""

import dataclasses
import json
import urllib.parse

import ops
import pytest
from ops import testing

import hedgedoc
from charm import HedgeDocCharm


@pytest.fixture
def ctx():
    return testing.Context(HedgeDocCharm)


@pytest.fixture
def container():
    # A connectable container that accepts the stat, chown and manage_users execs.
    # stat reports root ownership so the (guarded) chown still runs.
    return testing.Container(
        "hedgedoc",
        can_connect=True,
        execs={
            testing.Exec(["stat"], return_code=0, stdout="0:0\n"),
            testing.Exec(["chown"], return_code=0),
            testing.Exec(["node", hedgedoc.MANAGE_USERS], return_code=0, stdout="ok"),
        },
    )


@pytest.fixture
def peer():
    return testing.PeerRelation("hedgedoc")


def _hedgedoc_layer(state: testing.State) -> dict:
    plan = state.get_container("hedgedoc").layers["hedgedoc"]
    return plan.services["hedgedoc"].environment


def _oauth_relation(client_secret_id: str) -> testing.Relation:
    return testing.Relation(
        "oauth",
        interface="oauth",
        remote_app_data={
            "issuer_url": "https://idp.example.com",
            "authorization_endpoint": "https://idp.example.com/oauth2/auth",
            "token_endpoint": "https://idp.example.com/oauth2/token",
            "introspection_endpoint": "https://idp.example.com/admin/oauth2/introspect",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
            "jwks_endpoint": "https://idp.example.com/.well-known/jwks.json",
            "scope": "openid email profile",
            "client_id": "hedgedoc-client",
            "client_secret_id": client_secret_id,
        },
    )


def test_ingress_url_sets_domain_protocol_and_path(ctx, container, peer):
    ingress = testing.Relation(
        "ingress",
        interface="ingress",
        remote_app_data={"ingress": json.dumps({"url": "https://pad.example.com/hedgedoc-k8s"})},
    )
    state = testing.State(leader=True, containers={container}, relations={peer, ingress})
    out = ctx.run(ctx.on.config_changed(), state)
    env = _hedgedoc_layer(out)
    assert env["CMD_DOMAIN"] == "pad.example.com"
    assert env["CMD_PROTOCOL_USESSL"] == "true"
    assert env["CMD_URL_PATH"] == "hedgedoc-k8s"


def test_catalogue_item_carries_the_ingress_url(ctx, container, peer):
    ingress = testing.Relation(
        "ingress",
        interface="ingress",
        remote_app_data={"ingress": json.dumps({"url": "https://pad.example.com/hedgedoc"})},
    )
    catalogue = testing.Relation("catalogue", interface="catalogue")
    state = testing.State(
        leader=True, containers={container}, relations={peer, ingress, catalogue}
    )
    out = ctx.run(ctx.on.config_changed(), state)
    item = out.get_relation(catalogue.id).local_app_data
    assert item["name"] == "HedgeDoc"
    assert item["url"] == "https://pad.example.com/hedgedoc"


def test_oauth_redirect_uri_is_registered_from_the_ingress_url(ctx, container, peer):
    client_secret = testing.Secret({"secret": "s3cr3t"})
    oauth = _oauth_relation(client_secret.id)
    ingress = testing.Relation(
        "ingress",
        interface="ingress",
        remote_app_data={"ingress": json.dumps({"url": "https://pad.example.com/hedgedoc"})},
    )
    state = testing.State(
        leader=True,
        containers={container},
        relations={peer, oauth, ingress},
        secrets={client_secret},
    )
    out = ctx.run(ctx.on.config_changed(), state)
    published = out.get_relation(oauth.id).local_app_data
    assert published["redirect_uri"] == ("https://pad.example.com/hedgedoc/auth/oauth2/callback")


def test_ingress_takes_precedence_over_the_cluster_fallback(ctx, container, peer):
    ingress = testing.Relation(
        "ingress",
        remote_app_data={"ingress": json.dumps({"url": "https://pad.example.com/hedgedoc"})},
    )
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(leader=True, containers={container}, relations={peer, ingress}),
    )
    assert _hedgedoc_layer(out)["CMD_DOMAIN"] == "pad.example.com"
