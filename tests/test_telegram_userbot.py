from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.functions.channels import CreateChannelRequest, LeaveChannelRequest, ToggleForumRequest
from telethon.tl.types import Channel, ChatPhotoEmpty

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


async def test_create_group_keeps_userbot_in_group_and_only_invites_bots(monkeypatch, tmp_path: Path):
    provisioner = TelegramUserbotProvisioner(config=None, env_path=tmp_path / ".env")
    created_channel = Channel(
        id=777,
        title="NCS",
        photo=ChatPhotoEmpty(),
        date=None,
        creator=True,
        megagroup=True,
        forum=True,
        forum_tabs=False,
    )
    refreshed_channel = Channel(
        id=777,
        title="NCS",
        photo=ChatPhotoEmpty(),
        date=None,
        creator=True,
        megagroup=True,
        forum=True,
        forum_tabs=False,
    )
    class _FakeClient:
        def __init__(self) -> None:
            self.requests: list[object] = []

        async def __call__(self, request):
            self.requests.append(request)
            if isinstance(request, CreateChannelRequest):
                return SimpleNamespace(chats=[created_channel])
            if isinstance(request, ToggleForumRequest):
                return None
            if isinstance(request, LeaveChannelRequest):
                raise AssertionError("create_group should not leave the channel")
            raise AssertionError(f"unexpected request: {request!r}")

        async def get_entity(self, entity):
            if isinstance(entity, Channel):
                return refreshed_channel
            return entity

    class _FakeClientContext:
        def __init__(self, client) -> None:
            self._client = client

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = _FakeClient()
    monkeypatch.setattr(provisioner, "_ensure_available", lambda: None)
    monkeypatch.setattr(provisioner, "_client", lambda: _FakeClientContext(client))
    safe_invite = AsyncMock()
    promote_admin = AsyncMock()
    bot_entities = [SimpleNamespace(id=901, username="bot_a"), SimpleNamespace(id=902, username="bot_b")]
    discover_bots = AsyncMock(return_value=(bot_entities, ["bot_a", "bot_b"]))
    maybe_add_to_chatlist = AsyncMock(return_value=(None, None, False, False))
    monkeypatch.setattr(provisioner, "_safe_invite", safe_invite)
    monkeypatch.setattr(provisioner, "_promote_admin", promote_admin)
    monkeypatch.setattr(provisioner, "_discover_bot_entities", discover_bots)
    monkeypatch.setattr(provisioner, "_maybe_add_group_to_chatlist", maybe_add_to_chatlist)

    result = await provisioner.create_group(
        title="NCS",
        default_user_id=222,
        default_username="produser",
    )

    assert result.creator_left is False
    assert result.chat_id == -1000000000777
    assert result.target_user_id == 222
    assert result.target_username == "produser"
    assert safe_invite.await_count == 2
    assert promote_admin.await_count == 2
    assert [call.args[2] for call in safe_invite.await_args_list] == bot_entities
    assert [call.args[2] for call in promote_admin.await_args_list] == bot_entities


async def test_finalize_joined_allowed_user_promotes_then_leaves(monkeypatch, tmp_path: Path):
    provisioner = TelegramUserbotProvisioner(config=None, env_path=tmp_path / ".env")
    channel = SimpleNamespace(id=777)
    joined_user = SimpleNamespace(id=222, username="produser")
    me = SimpleNamespace(id=111)

    class _FakeClient:
        def __init__(self) -> None:
            self.requests: list[object] = []

        async def get_me(self):
            return me

        async def __call__(self, request):
            self.requests.append(request)
            if isinstance(request, LeaveChannelRequest):
                return None
            raise AssertionError(f"unexpected request: {request!r}")

        async def get_entity(self, entity):
            if entity == -1000000000777:
                return channel
            return entity

    class _FakeClientContext:
        def __init__(self, client) -> None:
            self._client = client

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = _FakeClient()
    monkeypatch.setattr(provisioner, "_ensure_available", lambda: None)
    monkeypatch.setattr(provisioner, "_client", lambda: _FakeClientContext(client))
    resolve_target = AsyncMock(return_value=joined_user)
    promote_admin = AsyncMock()
    monkeypatch.setattr(provisioner, "_resolve_target_user", resolve_target)
    monkeypatch.setattr(provisioner, "_promote_admin", promote_admin)

    left = await provisioner.finalize_joined_allowed_user(
        chat_id=-1000000000777,
        joined_user_id=222,
        joined_username="produser",
    )

    assert left is True
    promote_admin.assert_awaited_once_with(client, channel, joined_user, rank="obs-owner-user")
    assert any(isinstance(request, LeaveChannelRequest) for request in client.requests)


async def test_finalize_joined_allowed_user_skips_self_userbot(monkeypatch, tmp_path: Path):
    provisioner = TelegramUserbotProvisioner(config=None, env_path=tmp_path / ".env")
    me = SimpleNamespace(id=222)

    class _FakeClient:
        async def get_me(self):
            return me

    class _FakeClientContext:
        def __init__(self, client) -> None:
            self._client = client

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, exc_type, exc, tb):
            return False

    client = _FakeClient()
    monkeypatch.setattr(provisioner, "_ensure_available", lambda: None)
    monkeypatch.setattr(provisioner, "_client", lambda: _FakeClientContext(client))
    resolve_target = AsyncMock()
    promote_admin = AsyncMock()
    monkeypatch.setattr(provisioner, "_resolve_target_user", resolve_target)
    monkeypatch.setattr(provisioner, "_promote_admin", promote_admin)

    left = await provisioner.finalize_joined_allowed_user(
        chat_id=-1000000000777,
        joined_user_id=222,
        joined_username="produser",
    )

    assert left is False
    resolve_target.assert_not_called()
    promote_admin.assert_not_called()
