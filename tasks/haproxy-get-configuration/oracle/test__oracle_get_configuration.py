# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""The gold's own unit tests for the get-configuration action (from tests/unit/test_charm.py)."""

from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

from charm import CharmStateValidationBaseError, HAProxyCharm

TEST_LOG_HASH_SALT = "test-salt"
TEST_LOG_HASH_CLIENT_ADDRESS = (
    f'%[src,concat(,,),regsub(^::ffff:,),concat(,,\\"{TEST_LOG_HASH_SALT}\\"),sha2(256),hex]:%cp'
)


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
def test_oracle_get_configuration_returns_disk_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    arrange: mock the config file on disk with known content.
    act: trigger the get-configuration action.
    assert: the on-disk file content is returned unchanged.
    """
    content = "global\n    maxconn 4096\n\nfrontend default\n    bind :80\n"
    monkeypatch.setattr("charm.file_exists", MagicMock(return_value=True))
    monkeypatch.setattr("charm.read_file", MagicMock(return_value=content))
    context = ops.testing.Context(HAProxyCharm)
    state = ops.testing.State(leader=True)

    context.run(context.on.action("get-configuration"), state)

    assert context.action_results == {"configuration": content, "source": "disk"}


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


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
def test_oracle_get_configuration_missing_file_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    arrange: mock the config file as absent from disk.
    act: trigger the get-configuration action.
    assert: the action fails with a clear message rather than returning empty.
    """
    monkeypatch.setattr("charm.file_exists", MagicMock(return_value=False))
    context = ops.testing.Context(HAProxyCharm)
    state = ops.testing.State(leader=True)

    with pytest.raises(ops.testing.ActionFailed) as exc_info:
        context.run(context.on.action("get-configuration"), state)

    assert "not found" in exc_info.value.message


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
@pytest.mark.parametrize(
    "on_disk_config, rendered_default, expect_default_warning",
    [
        pytest.param(
            "global\n    maxconn 4096\n",
            "global\n    maxconn 4096\n",
            True,
            id="matches-default",
        ),
        pytest.param(
            "frontend haproxy\n    bind :80\n",
            "global\n    maxconn 4096\n",
            False,
            id="differs-from-default",
        ),
    ],
)
def test_oracle_get_configuration_default_warning(
    monkeypatch: pytest.MonkeyPatch,
    on_disk_config: str,
    rendered_default: str,
    expect_default_warning: bool,
) -> None:
    """
    arrange: mock the on-disk config to either match or differ from the rendered default.
    act: trigger the get-configuration action.
    assert: the configuration is returned, and the "matches default" warning is logged
        only when the config is the default.
    """
    monkeypatch.setattr("charm.file_exists", MagicMock(return_value=True))
    monkeypatch.setattr("charm.read_file", MagicMock(return_value=on_disk_config))
    monkeypatch.setattr(
        "charm.HAProxyService.render_default_config",
        MagicMock(return_value=rendered_default),
    )
    context = ops.testing.Context(HAProxyCharm)
    state = ops.testing.State(leader=True)

    context.run(context.on.action("get-configuration"), state)

    assert context.action_results == {"configuration": on_disk_config, "source": "disk"}
    warned = any("default configuration" in log.lower() for log in context.action_logs)
    assert warned == expect_default_warning


@pytest.mark.usefixtures("systemd_mock", "mocks_external_calls")
def test_oracle_get_configuration_notes_invalid_charm_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    arrange: the config file is present, but building the charm state raises.
    act: trigger the get-configuration action.
    assert: the configuration is still returned, and a "could not determine" note is
        logged instead of silently claiming it is not the default.
    """
    content = "frontend haproxy\n    bind :80\n"
    monkeypatch.setattr("charm.file_exists", MagicMock(return_value=True))
    monkeypatch.setattr("charm.read_file", MagicMock(return_value=content))
    monkeypatch.setattr(
        "charm.CharmState.from_charm",
        MagicMock(side_effect=CharmStateValidationBaseError("invalid config")),
    )
    context = ops.testing.Context(HAProxyCharm)
    state = ops.testing.State(leader=True)

    context.run(context.on.action("get-configuration"), state)

    results = context.action_results
    assert results is not None
    assert results == {"configuration": content, "source": "disk"}
    assert any("could not determine" in log.lower() for log in context.action_logs)
    assert not any("matches the default" in log.lower() for log in context.action_logs)
