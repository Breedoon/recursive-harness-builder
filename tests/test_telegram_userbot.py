from __future__ import annotations

from pathlib import Path

from obs_agent.telegram_userbot import append_profile_bot_token


def test_append_profile_bot_token_seeds_primary_and_appends(monkeypatch, tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_TELEGRAM_BOT_TOKEN=primary-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OBS_PROFILE", "test")
    monkeypatch.delenv("OBS_TEST_TELEGRAM_BOT_TOKENS", raising=False)
    monkeypatch.delenv("OBS_TELEGRAM_BOT_TOKENS", raising=False)
    monkeypatch.delenv("OBS_TEST_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OBS_TELEGRAM_BOT_TOKEN", raising=False)

    env_key, tokens = append_profile_bot_token(
        "secondary-token",
        env_path=env_path,
    )

    assert env_key == "OBS_TEST_TELEGRAM_BOT_TOKENS"
    assert tokens == ["primary-token", "secondary-token"]
    assert "OBS_TEST_TELEGRAM_BOT_TOKENS=primary-token,secondary-token" in env_path.read_text(
        encoding="utf-8"
    )


def test_append_profile_bot_token_dedupes_existing_values(monkeypatch, tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OBS_TEST_TELEGRAM_BOT_TOKEN=primary-token",
                "OBS_TEST_TELEGRAM_BOT_TOKENS=primary-token,secondary-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OBS_PROFILE", "test")
    monkeypatch.delenv("OBS_TEST_TELEGRAM_BOT_TOKENS", raising=False)
    monkeypatch.delenv("OBS_TELEGRAM_BOT_TOKENS", raising=False)
    monkeypatch.delenv("OBS_TEST_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OBS_TELEGRAM_BOT_TOKEN", raising=False)

    _, tokens = append_profile_bot_token(
        "secondary-token",
        env_path=env_path,
    )

    assert tokens == ["primary-token", "secondary-token"]
    assert env_path.read_text(encoding="utf-8").count("secondary-token") == 1
