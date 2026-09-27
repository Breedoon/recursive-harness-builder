"""Trunks and children share one claude.ai connector policy for cache parity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute
from tests.test_env_persistence import _make_child, _record

pytestmark = pytest.mark.asyncio

KEY = "ENABLE_CLAUDEAI_MCP_SERVERS"


async def _launch(bot, route, thread_id, env):
    parent = bot._get_state(route)
    parent.last_bot = MagicMock()
    parent.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=thread_id))
    parent.last_bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=i) for i in range(940, 950)])
    parent.session_manager.set_session_id("parent-id")
    bot._bind_state_session(parent)
    bot._set_session_head(session_id="parent-id", jsonl_uuid="uuid-parent-head")
    with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
        await bot._launch_fork_task(route=route, args={
            "prompt": "Test child", "description": "Worker", "fork": False,
            "env": env, "task_tool_name": "AgentTask",
        })
    return parent, bot._get_state(TelegramRoute(chat_id=route.chat_id, thread_id=thread_id))


async def test_parent_and_child_disable_connectors_with_identical_tool_policy(config, tmp_path, monkeypatch):
    monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
    monkeypatch.setenv(KEY, "true")
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    try:
        parent, child = await _launch(bot, route, 336, None)
        assert parent.session_manager.create_options().env[KEY] == "false"
        assert child.session_manager.create_options().env[KEY] == "false"
        assert KEY not in parent.session_manager.sdk_env_overrides
        assert KEY not in child.session_manager.sdk_env_overrides
        assert KEY not in (child.session_manager.explicit_env_overrides or {})
    finally:
        await bot.shutdown()


async def test_child_explicit_override_cannot_change_tool_prefix(config, tmp_path, monkeypatch):
    monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    try:
        parent, child = await _launch(bot, route, 337, {KEY: "true"})
        assert parent.session_manager.create_options().env[KEY] == "false"
        assert child.session_manager.create_options().env[KEY] == "false"
        assert child.session_manager.explicit_env_overrides == {KEY: "true"}
    finally:
        await bot.shutdown()


@pytest.mark.parametrize("override", [None, "true"])
async def test_restored_child_retains_uniform_connector_policy(config, override):
    first = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
    child_route = TelegramRoute(chat_id=67890, thread_id=660)
    child = _make_child(first, child_route, team="team-alpha", agent="worker-env")
    if override:
        child.session_manager.explicit_env_overrides[KEY] = override
        first._persist_state_for_route(child_route)
    record = _record(child_route, team="team-alpha", agent="worker-env", is_fork=False)
    first._fork_tasks_by_id[record.task_id] = record
    first._register_team_worker_record(record)
    first._fork_task_by_child_route[child_route] = record.task_id
    await first.shutdown()

    restored = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
    await restored.initialize_runtime()
    state = restored._get_state(child_route)
    assert state.session_manager.create_options().env[KEY] == "false"
    await restored.shutdown()


async def test_team_identity_env_has_no_tool_policy(config):
    bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
    try:
        assert KEY not in bot._build_team_worker_env(team_name="t", agent_name="a")
        assert bot._build_team_worker_env(team_name=None) == {}
    finally:
        await bot.shutdown()
