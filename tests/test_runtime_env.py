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
        "OBS_TEST_CACHE_PROXY_PORT",
        "OBS_TEST_TELEGRAM_BOT_TOKEN",
        "OBS_TEST_TELEGRAM_BOT_TOKEN_2",
        "OBS_TEST_TELEGRAM_BOT_TOKENS",
        "OBS_TEST_TELEGRAM_ALLOWED_USERS",
        "OBS_TEST_TELEGRAM_NOTIFY_USERNAME",
        "OBS_TEST_TELEGRAM_GROUP_FOLDER_TITLE",
        "OBS_TEST_TELEGRAM_GROUP_ADDLIST_URL",
        "OBS_TEST_TELEGRAM_USERBOT_API_ID",
        "OBS_TEST_TELEGRAM_USERBOT_API_HASH",
        "OBS_TEST_TELEGRAM_USERBOT_SESSION",
        "OBS_PROD_TELEGRAM_BOT_TOKEN",
        "OBS_PROD_CACHE_PROXY_PORT",
        "OBS_PROD_TELEGRAM_ALLOWED_USERS",
        "OBS_PROD_TELEGRAM_GROUP_FOLDER_TITLE",
        "OBS_PROD_TELEGRAM_GROUP_ADDLIST_URL",
        "OBS_PROD_TELEGRAM_USERBOT_API_ID",
        "OBS_PROD_TELEGRAM_USERBOT_API_HASH",
        "OBS_PROD_TELEGRAM_USERBOT_SESSION",
        "OBS_VAULT_PATH",
        "OBS_DAEMON_PORT",
        "OBS_CACHE_PROXY_PORT",
        "OBS_TELEGRAM_BOT_TOKEN",
        "OBS_TELEGRAM_BOT_TOKENS",
        "OBS_TELEGRAM_ALLOWED_USERS",
        "OBS_TELEGRAM_NOTIFY_USERNAME",
        "OBS_TELEGRAM_GROUP_FOLDER_TITLE",
        "OBS_TELEGRAM_GROUP_ADDLIST_URL",
        "OBS_TELEGRAM_USERBOT_API_ID",
        "OBS_TELEGRAM_USERBOT_API_HASH",
        "OBS_TELEGRAM_USERBOT_SESSION",
        "OBS_TELEGRAM_TEST_BOT_TOKEN",
        "OBS_TELEGRAM_TEST_BOT_TOKEN_2",
        "OBS_TELEGRAM_TEST_SECOND_BOT_TOKEN",
        "OBS_TELEGRAM_TEST_NOTIFY_USERNAME",
        "OBS_TELEGRAM_PROD_BOT_TOKEN",
        "OBS_TELEGRAM_PROD_ALLOWED_USERS",
        "TELEGRAM_TEST_USER_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_profile_is_test_and_maps_prefixed_env(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_TELEGRAM_BOT_TOKEN=test-primary",
                "OBS_PROD_TELEGRAM_BOT_TOKEN=prod-primary",
            ]
        ),
        encoding="utf-8",
    )

    profile = bootstrap_runtime_env(
        argv=[],
        env_path=env_path,
        mutate_argv=False,
    )

    assert profile == "test"
    assert os.environ["OBS_PROFILE"] == "test"
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "test-primary"


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
                "OBS_TEST_TELEGRAM_GROUP_FOLDER_TITLE=Claudia",
                "OBS_TEST_TELEGRAM_GROUP_ADDLIST_URL=https://t.me/addlist/sPnRtk8389lhNjQ0",
                "OBS_TEST_TELEGRAM_USERBOT_API_ID=111111",
                "OBS_TEST_TELEGRAM_USERBOT_API_HASH=userbot-hash",
                "OBS_TEST_TELEGRAM_USERBOT_SESSION=userbot-session",
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
    assert OBSConfig.from_env().model == "claude-haiku-4-5"
    assert bootstrap_runtime_env(
        argv=["--test"],
        env_path=env_path,
        mutate_argv=False,
    ) == "test"
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "test-primary"
    assert os.environ["OBS_TELEGRAM_BOT_TOKENS"] == "test-primary,test-secondary"
    assert os.environ["OBS_TELEGRAM_ALLOWED_USERS"] == "12345"
    assert os.environ["OBS_TELEGRAM_NOTIFY_USERNAME"] == "@notify_test"
    assert os.environ["OBS_TELEGRAM_GROUP_FOLDER_TITLE"] == "Claudia"
    assert os.environ["OBS_TELEGRAM_GROUP_ADDLIST_URL"] == "https://t.me/addlist/sPnRtk8389lhNjQ0"
    assert os.environ["OBS_TELEGRAM_USERBOT_API_ID"] == "111111"
    assert os.environ["OBS_TELEGRAM_USERBOT_API_HASH"] == "userbot-hash"
    assert os.environ["OBS_TELEGRAM_USERBOT_SESSION"] == "userbot-session"


def test_prefixed_profile_env_maps_to_generic_keys(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_VAULT_PATH=/tmp/test-vault",
                "OBS_TEST_DAEMON_PORT=9999",
                "OBS_TEST_CACHE_PROXY_PORT=28923",
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
    assert OBSConfig.from_env().cache_proxy_port == 28923


def test_profile_mapping_overrides_generic_env_file_defaults(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_CACHE_PROXY_PORT=18923",
                "OBS_TEST_CACHE_PROXY_PORT=28923",
            ]
        ),
        encoding="utf-8",
    )

    bootstrap_runtime_env(
        argv=[],
        env_path=env_path,
        mutate_argv=False,
    )

    assert OBSConfig.from_env().cache_proxy_port == 28923


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


def test_env_profile_prod_does_not_map_prod_prefixed_env(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_PROFILE=prod",
                "OBS_TEST_TELEGRAM_BOT_TOKEN=test-primary",
                "OBS_PROD_TELEGRAM_BOT_TOKEN=prod-primary",
            ]
        ),
        encoding="utf-8",
    )

    profile = bootstrap_runtime_env(
        argv=[],
        env_path=env_path,
        mutate_argv=False,
    )

    assert profile == "test"
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "test-primary"
    assert "OBS_PROD_TELEGRAM_BOT_TOKEN" not in os.environ


def test_profile_prod_arg_does_not_map_prod_prefixed_env(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_TELEGRAM_BOT_TOKEN=test-primary",
                "OBS_PROD_TELEGRAM_BOT_TOKEN=prod-primary",
            ]
        ),
        encoding="utf-8",
    )

    profile = bootstrap_runtime_env(
        argv=["--profile", "prod"],
        env_path=env_path,
        mutate_argv=False,
    )

    assert profile == "test"
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "test-primary"


def test_prod_flag_maps_prod_prefixed_env(monkeypatch, tmp_path: Path) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_TELEGRAM_BOT_TOKEN=test-primary",
                "OBS_PROD_TELEGRAM_BOT_TOKEN=prod-primary",
                "OBS_PROD_CACHE_PROXY_PORT=18923",
            ]
        ),
        encoding="utf-8",
    )

    profile = bootstrap_runtime_env(
        argv=["--prod"],
        env_path=env_path,
        mutate_argv=False,
    )

    assert profile == "prod"
    assert os.environ["OBS_PROD_TELEGRAM_BOT_TOKEN"] == "prod-primary"
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "prod-primary"
    assert OBSConfig.from_env().cache_proxy_port == 18923


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

    assert OBSConfig.from_env().model == "claude-haiku-4-5"
    assert "OBS_TELEGRAM_BOT_TOKEN" not in os.environ
