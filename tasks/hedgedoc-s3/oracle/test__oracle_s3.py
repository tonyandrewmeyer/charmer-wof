# Copyright 2026 Tony Meyer
# See LICENSE file for licensing details.

"""The charm's own S3 (object storage) tests, from tests/unit/test_charm.py."""

import dataclasses

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



def test_oracle_s3_relation_sets_upload_environment(ctx, container, peer):
    s3 = testing.Relation(
        "s3",
        interface="s3",
        remote_app_data={"access-key": "AK", "secret-key": "SK", "bucket": "mybucket"},
    )
    state = testing.State(leader=True, containers={container}, relations={peer, s3})
    out = ctx.run(ctx.on.config_changed(), state)
    env = _hedgedoc_layer(out)
    assert env["CMD_IMAGE_UPLOAD_TYPE"] == "s3"
    assert env["CMD_S3_ACCESS_KEY_ID"] == "AK"
    assert env["CMD_S3_SECRET_ACCESS_KEY"] == "SK"
    assert env["CMD_S3_BUCKET"] == "mybucket"


def test_oracle_incomplete_s3_relation_does_not_fall_back_to_the_filesystem(ctx, container, peer):
    """Uploads written locally are invisible once the credentials arrive."""
    s3 = testing.Relation("s3", interface="s3", remote_app_data={"bucket": "mybucket"})
    state = testing.State(leader=True, containers={container}, relations={peer, s3})
    out = ctx.run(ctx.on.config_changed(), state)
    # No configuration is pushed at all, rather than one naming the filesystem.
    assert "hedgedoc" not in out.get_container("hedgedoc").layers


def test_oracle_s3_credentials_arriving_configures_object_storage(ctx, container, peer):
    s3 = testing.Relation("s3", interface="s3", remote_app_data={"bucket": "mybucket"})
    state = ctx.run(
        ctx.on.config_changed(),
        testing.State(leader=True, containers={container}, relations={peer, s3}),
    )
    ready = dataclasses.replace(
        state.get_relation(s3.id),
        remote_app_data={"access-key": "AK", "secret-key": "SK", "bucket": "mybucket"},
    )
    peer_out = state.get_relations("hedgedoc")[0]
    out = ctx.run(
        ctx.on.relation_changed(ready), dataclasses.replace(state, relations={peer_out, ready})
    )
    assert _hedgedoc_layer(out)["CMD_IMAGE_UPLOAD_TYPE"] == "s3"
