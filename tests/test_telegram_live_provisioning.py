"""Live Telegram provisioning tests for /new-group and /new-bot."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.chatlists import GetExportedInvitesRequest
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import InputChatlistDialogFilter

from obs_agent.runtime_env import bootstrap_runtime_env
from tests.live_test_vault import ensure_live_test_vault
from tests.test_telegram_live_forum_topics import _start_bot, _stop_bot


_CREATED_BOT_RE = re.compile(r"created bot:\s+([A-Za-z0-9_]+)")
_CHAT_ID_RE = re.compile(r"chat_id=(-?\d+)")
_REQUIRED_ENV = [
    "OBS_TEST_TELEGRAM_API_ID",
    "OBS_TEST_TELEGRAM_API_HASH",
    "OBS_TEST_TELEGRAM_SESSION",
    "OBS_TEST_TELEGRAM_BOT_USERNAME",
    "OBS_TEST_TELEGRAM_BOT_TOKEN",
    "OBS_TEST_TELEGRAM_USERBOT_API_ID",
    "OBS_TEST_TELEGRAM_USERBOT_API_HASH",
    "OBS_TEST_TELEGRAM_USERBOT_SESSION",
]


def _has_provisioning_credentials() -> bool:
    bootstrap_runtime_env(argv=["--test"])
    return all(os.environ.get(name) for name in _REQUIRED_ENV)


async def _latest_dm_id(client: TelegramClient, bot_entity) -> int:
    async for msg in client.iter_messages(bot_entity, limit=1):
        return int(msg.id)
    return 0


async def _wait_for_dm_reply(
    client: TelegramClient,
    bot_entity,
    *,
    after_id: int,
    prefixes: tuple[str, ...],
    timeout: float = 120.0,
) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async for msg in client.iter_messages(bot_entity, limit=20):
            if int(msg.id) <= after_id:
                continue
            text = str(msg.message or "")
            if text.startswith(prefixes):
                return text
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for DM reply prefixes={prefixes!r}")
        await asyncio.sleep(1.0)


def _dialog_filter_title_text(dialog_filter) -> str | None:
    title = getattr(dialog_filter, "title", None)
    text = getattr(title, "text", title)
    if not isinstance(text, str):
        return None
    normalized = text.strip()
    return normalized or None


@pytest.mark.integration
@pytest.mark.telegram
@pytest.mark.skipif(
    not _has_provisioning_credentials(),
    reason="Telegram provisioning credentials not configured in .env",
)
class TestTelegramLiveProvisioning:
    async def test_live_new_bot_ack_and_two_sequential_creations(self, monkeypatch):
        if (os.environ.get("OBS_TEST_TELEGRAM_ALLOW_BOT_CREATION") or "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            pytest.skip("Set OBS_TEST_TELEGRAM_ALLOW_BOT_CREATION=1 to run live bot-creation tests")

        monkeypatch.setenv("OBS_TEST_TELEGRAM_KILL_EXISTING_DAEMONS", "1")
        monkeypatch.setenv("OBS_PROFILE", "test")
        bootstrap_runtime_env(argv=["--test"])

        vault_path = ensure_live_test_vault()
        run_root = Path(tempfile.mkdtemp(prefix="obs-live-provisioning-"))
        temp_root = run_root / "telegram-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        state_db_path = run_root / "telegram-state.sqlite3"

        proc = None
        client = None
        try:
            proc, log_file = _start_bot(vault_path, temp_root, state_db_path=state_db_path)
            client = TelegramClient(
                StringSession(os.environ["OBS_TEST_TELEGRAM_SESSION"]),
                int(os.environ["OBS_TEST_TELEGRAM_API_ID"]),
                os.environ["OBS_TEST_TELEGRAM_API_HASH"],
            )
            await client.connect()
            bot_entity = await client.get_entity(os.environ["OBS_TEST_TELEGRAM_BOT_USERNAME"])

            baseline = await _latest_dm_id(client, bot_entity)
            await client.send_message(bot_entity, "/new-bot")
            first_started = await _wait_for_dm_reply(
                client,
                bot_entity,
                after_id=baseline,
                prefixes=("new bot started:",),
            )
            first_done = await _wait_for_dm_reply(
                client,
                bot_entity,
                after_id=baseline,
                prefixes=("created bot:", "new bot failed:"),
                timeout=240.0,
            )

            assert "waiting on BotFather" in first_started
            if first_done.startswith("new bot failed:"):
                assert "botfather rate limit" in first_done.lower()
                assert "too many attempts" in first_done.lower()
                return

            second_baseline = await _latest_dm_id(client, bot_entity)
            await client.send_message(bot_entity, "/new-bot")
            second_started = await _wait_for_dm_reply(
                client,
                bot_entity,
                after_id=second_baseline,
                prefixes=("new bot started:",),
            )
            second_done = await _wait_for_dm_reply(
                client,
                bot_entity,
                after_id=second_baseline,
                prefixes=("created bot:", "new bot failed:"),
                timeout=240.0,
            )

            first_match = _CREATED_BOT_RE.search(first_done)
            assert first_match is not None, first_done
            assert "waiting on BotFather" in second_started
            if second_done.startswith("new bot failed:"):
                assert "botfather rate limit" in second_done.lower()
                assert "too many attempts" in second_done.lower()
                return
            second_match = _CREATED_BOT_RE.search(second_done)
            assert second_match is not None, second_done
            assert first_match.group(1) != second_match.group(1), (first_done, second_done)
        finally:
            if client is not None:
                await client.disconnect()
            if proc is not None:
                _stop_bot(proc)

    async def test_live_new_group_adds_chat_to_claudia_folder_and_addlist(self, monkeypatch):
        monkeypatch.setenv("OBS_TEST_TELEGRAM_KILL_EXISTING_DAEMONS", "1")
        monkeypatch.setenv("OBS_PROFILE", "test")
        monkeypatch.setenv("OBS_TEST_TELEGRAM_GROUP_FOLDER_TITLE", "Claudia")
        monkeypatch.setenv("OBS_TEST_TELEGRAM_GROUP_ADDLIST_URL", "https://t.me/addlist/sPnRtk8389lhNjQ0")
        bootstrap_runtime_env(argv=["--test"])

        vault_path = ensure_live_test_vault()
        run_root = Path(tempfile.mkdtemp(prefix="obs-live-provisioning-group-"))
        temp_root = run_root / "telegram-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        state_db_path = run_root / "telegram-state.sqlite3"

        proc = None
        client = None
        try:
            proc, log_file = _start_bot(vault_path, temp_root, state_db_path=state_db_path)
            client = TelegramClient(
                StringSession(os.environ["OBS_TEST_TELEGRAM_SESSION"]),
                int(os.environ["OBS_TEST_TELEGRAM_API_ID"]),
                os.environ["OBS_TEST_TELEGRAM_API_HASH"],
            )
            await client.connect()
            bot_entity = await client.get_entity(os.environ["OBS_TEST_TELEGRAM_BOT_USERNAME"])

            baseline = await _latest_dm_id(client, bot_entity)
            group_title = f"Folder Proof {next(tempfile._get_candidate_names())[:8]}"
            await client.send_message(bot_entity, f"/new-group {group_title}")
            started = await _wait_for_dm_reply(
                client,
                bot_entity,
                after_id=baseline,
                prefixes=("new group started:",),
            )
            done = await _wait_for_dm_reply(
                client,
                bot_entity,
                after_id=baseline,
                prefixes=("created group:",),
                timeout=240.0,
            )

            assert "waiting on Telegram" in started
            assert "folder=Claudia:added" in done
            assert "addlist=https://t.me/addlist/sPnRtk8389lhNjQ0:added" in done
            assert "userbot stayed as owner" in done

            chat_id_match = _CHAT_ID_RE.search(done)
            assert chat_id_match is not None, done
            chat_id = int(chat_id_match.group(1))
            channel = await client.get_entity(chat_id)

            filters_result = await client(GetDialogFiltersRequest())
            claudia_filter = next(
                dialog_filter
                for dialog_filter in list(getattr(filters_result, "filters", []) or [])
                if _dialog_filter_title_text(dialog_filter) == "Claudia"
            )
            include_ids = {
                int(getattr(peer, "channel_id"))
                for peer in list(getattr(claudia_filter, "include_peers", []) or [])
                if isinstance(getattr(peer, "channel_id", None), int)
            }
            assert int(getattr(channel, "id")) in include_ids

            exported = await client(GetExportedInvitesRequest(chatlist=InputChatlistDialogFilter(filter_id=int(claudia_filter.id))))
            invite = next(
                existing
                for existing in list(getattr(exported, "invites", []) or [])
                if str(getattr(existing, "url", "") or "").endswith("/sPnRtk8389lhNjQ0")
            )
            invite_ids = {
                int(getattr(peer, "channel_id"))
                for peer in list(getattr(invite, "peers", []) or [])
                if isinstance(getattr(peer, "channel_id", None), int)
            }
            assert int(getattr(channel, "id")) in invite_ids
        finally:
            if client is not None:
                await client.disconnect()
            if proc is not None:
                _stop_bot(proc)
