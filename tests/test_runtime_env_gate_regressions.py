"""Live-launch safety gates must not use last-option-wins semantics."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obs_agent.runtime_env import (
    LegacyFormalTestRedirect,
    assert_live_entrypoint_allowed,
    bootstrap_runtime_env,
)


@pytest.mark.parametrize("test_arguments", [
    ["--test"], ["--test-instance"], ["--profile", "test"],
    ["--profile=test"], ["--profile", " TEST "],
])
@pytest.mark.parametrize(
    "production_arguments", [["--prod"], ["--profile", "prod"], ["--profile=prod"]]
)
def test_later_profile_cannot_erase_test_launch_request(
    test_arguments: list[str], production_arguments: list[str]
) -> None:
    arguments = test_arguments + production_arguments
    original_arguments = list(arguments)

    with pytest.raises(LegacyFormalTestRedirect, match="obs-live-test"):
        assert_live_entrypoint_allowed(argv=arguments, environ={})

    assert arguments == original_arguments


@pytest.mark.parametrize(
    "arguments", [[], ["--prod"], ["--profile", "prod"], ["--profile=prod"]]
)
def test_explicit_production_does_not_override_test_environment(arguments: list[str]) -> None:
    environment = {"OBS_PROFILE": " TEST "}

    with pytest.raises(LegacyFormalTestRedirect, match="host-preflight"):
        assert_live_entrypoint_allowed(argv=arguments, environ=environment)

    assert environment == {"OBS_PROFILE": " TEST "}


@pytest.mark.parametrize(
    "arguments", [[], ["--prod"], ["--profile", "prod"], ["--profile=prod"]]
)
def test_production_launch_without_test_selection_remains_allowed(arguments: list[str]) -> None:
    assert_live_entrypoint_allowed(argv=arguments, environ={"OBS_PROFILE": "prod"})


def test_library_only_test_bootstrap_remains_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in tuple(os.environ):
        if key.startswith("OBS_"):
            monkeypatch.delenv(key)
    environment_file = tmp_path / ".env"
    environment_file.write_text("OBS_TEST_DAEMON_PORT=29999\n", encoding="utf-8")

    profile = bootstrap_runtime_env(
        argv=["--test"], env_path=environment_file, mutate_argv=False
    )

    assert profile == "test"
    assert os.environ["OBS_DAEMON_PORT"] == "29999"
    assert os.environ["OBS_AGENT_MODEL"] == "haiku"
