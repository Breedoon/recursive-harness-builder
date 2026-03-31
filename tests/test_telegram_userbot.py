from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from obs_agent.telegram_userbot import TelegramUserbotProvisioner, append_profile_bot_token


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


@dataclass
class _FakeBotfatherMessage:
    id: int
    message: str
    out: bool = False


class _FakeBotfatherClient:
    def __init__(self, messages: list[_FakeBotfatherMessage]) -> None:
        self._messages = messages

    async def iter_messages(self, peer, limit: int = 12):
        _ = peer, limit
        for message in self._messages:
            yield message


async def test_wait_for_bot_reply_ignores_non_matching_stale_messages(tmp_path: Path):
    provisioner = TelegramUserbotProvisioner(config=None, env_path=tmp_path / ".env")
    client = _FakeBotfatherClient(
        [
            _FakeBotfatherMessage(id=14, message="Choose a name for your bot."),
            _FakeBotfatherMessage(id=13, message="Privacy mode disabled for @oldbot."),
            _FakeBotfatherMessage(id=12, message="Older baseline"),
        ]
    )

    response = await provisioner._wait_for_bot_reply(
        client,
        peer=object(),
        after_id=12,
        timeout=0.1,
        match_text=lambda text: "name for your bot" in text.lower(),
        settle_seconds=0.0,
    )

    assert response == "Choose a name for your bot."


async def test_wait_for_bot_reply_surfaces_botfather_rate_limit(tmp_path: Path):
    provisioner = TelegramUserbotProvisioner(config=None, env_path=tmp_path / ".env")
    client = _FakeBotfatherClient(
        [
            _FakeBotfatherMessage(
                id=14,
                message="Sorry, too many attempts. Please try again in 54722 seconds.",
            ),
            _FakeBotfatherMessage(id=12, message="Older baseline"),
        ]
    )

    try:
        await provisioner._wait_for_bot_reply(
            client,
            peer=object(),
            after_id=12,
            timeout=0.1,
            settle_seconds=0.0,
        )
    except RuntimeError as exc:
        assert "BotFather rate limit" in str(exc)
        assert "too many attempts" in str(exc).lower()
    else:
        raise AssertionError("Expected BotFather rate limit to raise RuntimeError")


def test_extract_addlist_slug_accepts_full_url(tmp_path: Path):
    provisioner = TelegramUserbotProvisioner(config=None, env_path=tmp_path / ".env")

    assert provisioner._extract_addlist_slug("https://t.me/addlist/sPnRtk8389lhNjQ0") == "sPnRtk8389lhNjQ0"


def test_append_unique_input_peer_dedupes_channel_ids(tmp_path: Path):
    provisioner = TelegramUserbotProvisioner(config=None, env_path=tmp_path / ".env")

    class _Peer:
        def __init__(self, channel_id: int) -> None:
            self.channel_id = channel_id

    peers = [_Peer(123)]

    changed = provisioner._append_unique_input_peer(peers, _Peer(123))

    assert changed is False
    assert len(peers) == 1
