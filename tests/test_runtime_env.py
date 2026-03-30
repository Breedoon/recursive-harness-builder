"""Tests for shared runtime env bootstrap and profile resolution."""

from __future__ import annotations

import os
from pathlib import Path

from obs_agent.config import OBSConfig
from obs_agent.runtime_env import bootstrap_runtime_env


def _clear_runtime_env(monkeypatch) -> None:
    for key in (
        "OBS_PROFILE",
        "OBS_AGENT_MODEL",
        "OBS_MODEL",
        "OBS_TEST_VAULT_PATH",
        "OBS_TEST_DAEMON_PORT",
        "OBS_TEST_TELEGRAM_BOT_TOKEN",
        "OBS_TEST_TELEGRAM_BOT_TOKEN_2",
        "OBS_TEST_TELEGRAM_BOT_TOKENS",
        "OBS_TEST_TELEGRAM_ALLOWED_USERS",
        "OBS_TEST_TELEGRAM_NOTIFY_USERNAME",
        "OBS_PROD_TELEGRAM_BOT_TOKEN",
        "OBS_PROD_TELEGRAM_ALLOWED_USERS",
        "OBS_TELEGRAM_BOT_TOKEN",
        "OBS_TELEGRAM_BOT_TOKENS",
        "OBS_TELEGRAM_ALLOWED_USERS",
        "OBS_TELEGRAM_NOTIFY_USERNAME",
        "OBS_TELEGRAM_TEST_BOT_TOKEN",
        "OBS_TELEGRAM_TEST_BOT_TOKEN_2",
        "OBS_TELEGRAM_TEST_SECOND_BOT_TOKEN",
        "OBS_TELEGRAM_TEST_NOTIFY_USERNAME",
        "OBS_TELEGRAM_PROD_BOT_TOKEN",
        "OBS_TELEGRAM_PROD_ALLOWED_USERS",
        "TELEGRAM_TEST_USER_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def test_test_profile_maps_prefixed_env_and_sets_haiku(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_TELEGRAM_BOT_TOKEN=test-primary",
                "OBS_TEST_TELEGRAM_BOT_TOKENS=test-primary,test-secondary",
                "OBS_TEST_TELEGRAM_ALLOWED_USERS=12345",
                "OBS_TEST_TELEGRAM_NOTIFY_USERNAME=@notify_test",
            ]
        ),
        encoding="utf-8",
    )

    profile = bootstrap_runtime_env(
        argv=["--test"],
        env_path=env_path,
        mutate_argv=False,
    )

    assert profile == "test"
    assert OBSConfig.from_env().model == "haiku"
    assert bootstrap_runtime_env(
        argv=["--test"],
        env_path=env_path,
        mutate_argv=False,
    ) == "test"
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "test-primary"
    assert os.environ["OBS_TELEGRAM_BOT_TOKENS"] == "test-primary,test-secondary"
    assert os.environ["OBS_TELEGRAM_ALLOWED_USERS"] == "12345"
    assert os.environ["OBS_TELEGRAM_NOTIFY_USERNAME"] == "@notify_test"


def test_prefixed_profile_env_maps_to_generic_keys(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_VAULT_PATH=/tmp/test-vault",
                "OBS_TEST_DAEMON_PORT=9999",
            ]
        ),
        encoding="utf-8",
    )

    bootstrap_runtime_env(
        argv=["--profile", "test"],
        env_path=env_path,
        mutate_argv=False,
    )

    assert OBSConfig.from_env().vault_path == Path("/tmp/test-vault")
    assert OBSConfig.from_env().daemon_port == 9999


def test_explicit_generic_env_wins_over_profile_mapping(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OBS_TEST_TELEGRAM_BOT_TOKEN=test-primary\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OBS_TELEGRAM_BOT_TOKEN", "explicit-generic")

    bootstrap_runtime_env(
        argv=["--test"],
        env_path=env_path,
        mutate_argv=False,
    )

    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "explicit-generic"


def test_legacy_profile_keys_are_not_mapped(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OBS_TELEGRAM_TEST_BOT_TOKEN=legacy-primary\n",
        encoding="utf-8",
    )

    bootstrap_runtime_env(
        argv=["--test"],
        env_path=env_path,
        mutate_argv=False,
    )

    assert OBSConfig.from_env().model == "haiku"
    assert "OBS_TELEGRAM_BOT_TOKEN" not in os.environ
