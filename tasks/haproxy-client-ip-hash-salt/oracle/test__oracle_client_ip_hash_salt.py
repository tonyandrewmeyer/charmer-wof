# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""The gold's own unit tests for client-ip-hash-salt (from tests/unit/test_charm.py)."""

from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

from charm import HAProxyCharm

from .helper import rendered_file_content

TEST_LOG_HASH_SALT = "test-salt"
TEST_LOG_HASH_CLIENT_ADDRESS = (
    f'%[src,concat(,,),regsub(^::ffff:,),concat(,,\\"{TEST_LOG_HASH_SALT}\\"),sha2(256),hex]:%cp'
)


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
@pytest.mark.parametrize(
    "configure_salt,expected_present",
    [(False, False), (True, True)],
    ids=["salt_removed", "salt_configured"],
)
def test_oracle_config_changed_client_ip_hash_salt(
    monkeypatch: pytest.MonkeyPatch, peer_relation, configure_salt, expected_present
):
    """
    arrange: prepare a state with or without a client IP hash salt.
    act: run config-changed.
    assert: the rendered config hashes client IPs only when the salt is configured.
    """
    render_file_mock = MagicMock()
    monkeypatch.setattr("haproxy.render_file", render_file_mock)
    context = ops.testing.Context(HAProxyCharm)
    log_hash_salt = ops.testing.Secret(tracked_content={"salt": TEST_LOG_HASH_SALT})
    state = ops.testing.State(relations=[peer_relation], leader=True)
    if configure_salt:
        state = ops.testing.State(
            relations=[peer_relation],
            leader=True,
            config={"client-ip-hash-salt": log_hash_salt.id},
            secrets=[log_hash_salt],
        )

    context.run(context.on.config_changed(), state)

    render_file_mock.assert_called_once()
    config_content = rendered_file_content(render_file_mock)
    assert (f'log-format "{TEST_LOG_HASH_CLIENT_ADDRESS}' in config_content) is expected_present
    assert (
        f'error-log-format "{TEST_LOG_HASH_CLIENT_ADDRESS}' in config_content
    ) is expected_present


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
def test_oracle_config_changed_client_ip_hash_salt_rejects_inaccessible_secret(
    monkeypatch: pytest.MonkeyPatch, peer_relation
):
    """
    arrange: configure an ID for a salt secret that is not available to the charm.
    act: run config-changed.
    assert: the unit is blocked and the HAProxy config is not rendered.
    """
    render_file_mock = MagicMock()
    monkeypatch.setattr("haproxy.render_file", render_file_mock)
    context = ops.testing.Context(HAProxyCharm)
    inaccessible_secret = ops.testing.Secret(tracked_content={"salt": TEST_LOG_HASH_SALT})
    state = ops.testing.State(
        relations=[peer_relation],
        leader=True,
        config={"client-ip-hash-salt": inaccessible_secret.id},
    )

    out = context.run(context.on.config_changed(), state)

    assert out.unit_status == ops.testing.BlockedStatus(
        "The client-ip-hash-salt secret does not exist or cannot be accessed."
    )
    render_file_mock.assert_not_called()


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
@pytest.mark.parametrize(
    "secret_content",
    [
        pytest.param({"other": "value"}, id="missing_key"),
        pytest.param({"salt": ""}, id="empty"),
        pytest.param({"salt": "   "}, id="whitespace"),
    ],
)
def test_oracle_config_changed_client_ip_hash_salt_rejects_blank_value(
    monkeypatch: pytest.MonkeyPatch,
    peer_relation,
    secret_content: dict[str, str],
):
    """
    arrange: prepare a state with hashing enabled and a missing or blank salt.
    act: run config-changed.
    assert: the unit is blocked and the HAProxy config is not rendered.
    """
    render_file_mock = MagicMock()
    monkeypatch.setattr("haproxy.render_file", render_file_mock)
    context = ops.testing.Context(HAProxyCharm)
    log_hash_salt = ops.testing.Secret(tracked_content=secret_content)
    state = ops.testing.State(
        relations=[peer_relation],
        leader=True,
        config={"client-ip-hash-salt": log_hash_salt.id},
        secrets=[log_hash_salt],
    )

    out = context.run(context.on.config_changed(), state)

    assert out.unit_status == ops.testing.BlockedStatus(
        "The client-ip-hash-salt secret must contain a non-empty 'salt' value."
    )
    render_file_mock.assert_not_called()


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
@pytest.mark.parametrize(
    "salt",
    [
        pytest.param('unsafe"salt', id="quote"),
        pytest.param("unsafe\\salt", id="backslash"),
        pytest.param("unsafe\nsalt", id="control_character"),
        pytest.param("unsafe$salt", id="dollar_sign"),
    ],
)
def test_oracle_config_changed_client_ip_hash_salt_rejects_unsafe_value(
    monkeypatch: pytest.MonkeyPatch,
    peer_relation,
    salt: str,
):
    """
    arrange: prepare a state with hashing enabled and a salt containing an unsafe character.
    act: run config-changed.
    assert: the unit is blocked and the HAProxy config is not rendered.
    """
    render_file_mock = MagicMock()
    monkeypatch.setattr("haproxy.render_file", render_file_mock)
    context = ops.testing.Context(HAProxyCharm)
    log_hash_salt = ops.testing.Secret(tracked_content={"salt": salt})
    state = ops.testing.State(
        relations=[peer_relation],
        leader=True,
        config={"client-ip-hash-salt": log_hash_salt.id},
        secrets=[log_hash_salt],
    )

    out = context.run(context.on.config_changed(), state)

    assert out.unit_status == ops.testing.BlockedStatus(
        "The client-ip-hash-salt secret must not contain control characters, "
        "double quotes, backslashes, or dollar signs."
    )
    render_file_mock.assert_not_called()


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
def test_oracle_get_configuration_redacts_log_hash_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    arrange: mock on-disk formats containing a salt different from the current secret.
    act: trigger the get-configuration action.
    assert: every on-disk salt is redacted without changing the rest of the configuration.
    """
    content = "\n".join(
        [
            f'log-format "{TEST_LOG_HASH_CLIENT_ADDRESS} HTTP"',
            f'error-log-format "{TEST_LOG_HASH_CLIENT_ADDRESS} ERROR"',
            f'log-format "{TEST_LOG_HASH_CLIENT_ADDRESS} TCP"',
        ]
    )
    monkeypatch.setattr("charm.file_exists", MagicMock(return_value=True))
    monkeypatch.setattr("charm.read_file", MagicMock(return_value=content))
    context = ops.testing.Context(HAProxyCharm)
    log_hash_salt = ops.testing.Secret(tracked_content={"salt": "different-current-salt"})
    state = ops.testing.State(
        leader=True,
        config={"client-ip-hash-salt": log_hash_salt.id},
        secrets=[log_hash_salt],
    )

    context.run(context.on.action("get-configuration"), state)

    results = context.action_results
    assert results is not None
    configuration = results["configuration"]
    assert configuration == content.replace(TEST_LOG_HASH_SALT, "<redacted>")
    assert TEST_LOG_HASH_SALT not in configuration
    assert configuration.count("<redacted>") == 3
