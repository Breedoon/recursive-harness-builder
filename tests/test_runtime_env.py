"""Authored tests for profile parsing and fail-closed live entry points; unrun."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obs_agent.config import OBSConfig
from obs_agent.runtime_env import (
    FORMAL_TEST_REDIRECT,
    LegacyFormalTestRedirect,
    _resolve_profile,
    assert_live_entrypoint_allowed,
    bootstrap_runtime_env,
)


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("OBS_TEST_") or key.startswith("OBS_PROD_"):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "OBS_PROFILE",
        "OBS_AGENT_MODEL",
        "OBS_MODEL",
        "OBS_VAULT_PATH",
        "OBS_DAEMON_PORT",
        "OBS_CACHE_PROXY_PORT",
        "OBS_TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_pure_profile_parser_retains_unit_library_compatibility() -> None:
    assert _resolve_profile(["--test", "value"])[0] == "test"
    assert _resolve_profile(["--test-instance"])[0] == "test"
    assert _resolve_profile(["--profile", "test"])[0] == "test"
    assert _resolve_profile(["--profile=test"])[0] == "test"


@pytest.mark.parametrize(
    "argv",
    [
        ["--test"],
        ["--test-instance"],
        ["--profile", "test"],
        ["--profile=test"],
    ],
)
def test_live_gate_rejects_every_legacy_test_argument_before_bootstrap(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_runtime_env(monkeypatch)
    with pytest.raises(LegacyFormalTestRedirect, match="obs-live-test"):
        assert_live_entrypoint_allowed(argv=argv, environ={})
    assert "OBS_PROFILE" not in os.environ


def test_live_gate_rejects_environment_test_profile() -> None:
    with pytest.raises(LegacyFormalTestRedirect, match="host-preflight"):
        assert_live_entrypoint_allowed(argv=[], environ={"OBS_PROFILE": "test"})


def test_redirect_is_exact_and_operator_actionable() -> None:
    assert "legacy test-profile live launch is disabled" in FORMAL_TEST_REDIRECT
    assert "docs/testing.md" in FORMAL_TEST_REDIRECT
    assert "obs-live-test" in FORMAL_TEST_REDIRECT


def test_library_bootstrap_defaults_to_production_and_maps_prod_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OBS_PROD_TELEGRAM_BOT_TOKEN=prod-primary\n"
        "OBS_PROD_CACHE_PROXY_PORT=18923\n",
        encoding="utf-8",
    )
    profile = bootstrap_runtime_env(
        argv=[], env_path=env_path, mutate_argv=False
    )
    assert profile == "prod"
    assert os.environ["OBS_PROFILE"] == "prod"
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "prod-primary"
    assert OBSConfig.from_env().cache_proxy_port == 18923


def test_library_bootstrap_test_profile_remains_available_without_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OBS_TEST_VAULT_PATH=/tmp/unit-fixture\n"
        "OBS_TEST_DAEMON_PORT=29999\n",
        encoding="utf-8",
    )
    profile = bootstrap_runtime_env(
        argv=["--profile", "test"],
        env_path=env_path,
        mutate_argv=False,
    )
    assert profile == "test"
    assert OBSConfig.from_env().vault_path == Path("/tmp/unit-fixture")
    assert OBSConfig.from_env().daemon_port == 29999
    assert OBSConfig.from_env().model == "claude-haiku-4-5"


def test_explicit_generic_env_wins_over_profile_mapping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_runtime_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OBS_PROD_TELEGRAM_BOT_TOKEN=file-value\n", encoding="utf-8"
    )
    monkeypatch.setenv("OBS_TELEGRAM_BOT_TOKEN", "explicit-value")
    bootstrap_runtime_env(argv=["--prod"], env_path=env_path, mutate_argv=False)
    assert os.environ["OBS_TELEGRAM_BOT_TOKEN"] == "explicit-value"
