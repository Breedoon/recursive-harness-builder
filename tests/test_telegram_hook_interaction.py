"""Tests for Telegram-facing hook interaction helpers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute


@pytest.mark.asyncio
async def test_hook_context_can_send_user_message(config):
    bot = TelegramBot(config, enable_background_poller=False)
    route = TelegramRoute(chat_id=67890, thread_id=12)
    state = bot._get_state(route)
    state.last_bot = MagicMock()

    sent_message = MagicMock()
    sent_message.message_id = 101
    bot._send_system_message = AsyncMock(return_value=sent_message)

    result = await state.hook_state.user_message_sender({"text": "hello from hook"})

    assert result == {
        "ok": True,
        "message_ids": [101],
        "route": {"chat_id": 67890, "thread_id": 12},
    }
    bot._send_system_message.assert_awaited_once()
    kwargs = bot._send_system_message.await_args.kwargs
    assert kwargs["route"] == route
    assert kwargs["text"] == "hook message: hello from hook"
    assert kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_hook_context_can_prompt_user_with_notification(config):
    bot = TelegramBot(config, enable_background_poller=False)
    route = TelegramRoute(chat_id=67890, thread_id=None)
    state = bot._get_state(route)
    state.last_bot = MagicMock()
    bot._send_system_message = AsyncMock(return_value=[])

    result = await state.hook_state.user_prompt_sender({"text": "approve?"})

    assert result["ok"] is True
    kwargs = bot._send_system_message.await_args.kwargs
    assert kwargs["text"] == "hook prompt: approve?"
    assert kwargs["disable_notification"] is False


@pytest.mark.asyncio
async def test_hook_user_message_requires_text(config):
    bot = TelegramBot(config, enable_background_poller=False)
    route = TelegramRoute(chat_id=67890)
    state = bot._get_state(route)
    state.last_bot = MagicMock()

    result = await state.hook_state.user_message_sender({})

    assert result["ok"] is False
    assert "text is required" in result["error"]


@pytest.mark.asyncio
async def test_telegram_commands_are_logged_for_hooks(config):
    bot = TelegramBot(config, enable_background_poller=False)
    bot._normalizer.initialize()
    route = TelegramRoute(chat_id=67890, thread_id=33)
    state = bot._get_state(route)
    update = MagicMock()
    update.effective_message.text = "/context extra"
    update.effective_message.chat_id = route.chat_id
    update.effective_message.message_thread_id = route.thread_id
    update.effective_message.message_id = 44
    update.effective_user.id = 12345
    update.effective_user.username = "tester"

    bot._log_user_command(update, "context")
    result = await state.hook_state.user_command_reader({"limit": 5})

    assert result["ok"] is True
    assert result["path"].endswith("user-command-log.jsonl")
    assert len(result["commands"]) == 1
    record = result["commands"][0]
    assert record["command"] == "context"
    assert record["text"] == "/context extra"
    assert record["route"] == {"chat_id": 67890, "thread_id": 33}
    assert record["user"] == {"id": 12345, "username": "tester"}
    assert json.loads(bot._command_log_path.read_text(encoding="utf-8").strip())["message_id"] == 44


@pytest.mark.asyncio
async def test_hook_command_reader_filters_route(config):
    bot = TelegramBot(config, enable_background_poller=False)
    bot._normalizer.initialize()
    first_route = TelegramRoute(chat_id=1, thread_id=10)
    second_route = TelegramRoute(chat_id=1, thread_id=20)
    first_state = bot._get_state(first_route)
    bot._command_log_path.parent.mkdir(parents=True, exist_ok=True)
    bot._command_log_path.write_text(
        "\n".join(
            [
                json.dumps({"command": "help", "route": {"chat_id": 1, "thread_id": 10}}),
                json.dumps({"command": "stop", "route": {"chat_id": 1, "thread_id": 20}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = await first_state.hook_state.user_command_reader({"limit": 10})

    assert [record["command"] for record in result["commands"]] == ["help"]
    all_result = bot._read_user_command_log(route=second_route, payload={"all_routes": True})
    assert [record["command"] for record in all_result["commands"]] == ["help", "stop"]


@pytest.mark.asyncio
async def test_command_wrapper_only_logs_authorized_users(config):
    bot = TelegramBot(config, enable_background_poller=False)
    bot._normalizer.initialize()
    context = MagicMock()
    handler = AsyncMock()

    allowed_update = MagicMock()
    allowed_update.effective_message.text = "/help"
    allowed_update.effective_message.chat_id = 1
    allowed_update.effective_message.message_thread_id = None
    allowed_update.effective_message.message_id = 10
    allowed_update.effective_user.id = 12345
    allowed_update.effective_user.username = "allowed"

    blocked_update = MagicMock()
    blocked_update.effective_message.text = "/help secret"
    blocked_update.effective_message.chat_id = 1
    blocked_update.effective_message.message_thread_id = None
    blocked_update.effective_message.message_id = 11
    blocked_update.effective_user.id = 99999
    blocked_update.effective_user.username = "blocked"

    await bot._log_command_and_call(allowed_update, context, "help", handler)
    await bot._log_command_and_call(blocked_update, context, "help", handler)

    records = [json.loads(line) for line in bot._command_log_path.read_text(encoding="utf-8").splitlines()]
    assert [record["text"] for record in records] == ["/help"]
    assert handler.await_count == 2
