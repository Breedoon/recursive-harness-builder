"""AgentTask children skip claude.ai connectors (Canva, Claude Docs) by default."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute
from tests.test_env_persistence import _make_child, _record

pytestmark = pytest.mark.asyncio

KEY = "ENABLE_CLAUDEAI_MCP_SERVERS"


async def _launch(bot, route, thread_id, env, *, fork=False):
    parent = bot._get_state(route)
    parent.last_bot = MagicMock()
    parent.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=thread_id))
    parent.last_bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=i) for i in range(940, 950)])
    parent.session_manager.set_session_id("parent-id")
    bot._bind_state_session(parent)
    bot._set_session_head(session_id="parent-id", jsonl_uuid="uuid-parent-head")
    with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
        await bot._launch_fork_task(route=route, args={
            "prompt": "Test child", "description": "Worker", "fork": fork,
            "env": env, "task_tool_name": "AgentTask",
        })
    return parent, bot._get_state(TelegramRoute(chat_id=route.chat_id, thread_id=thread_id))


async def test_child_launch_disables_claudeai_connectors(config, tmp_path, monkeypatch):
    # Fork and fresh launches share _create_child_fork_topic, where this env is set.
    monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    try:
        parent, child = await _launch(bot, route, 336, None)
        sm = child.session_manager
        assert sm.sdk_env_overrides[KEY] == "false"
        assert sm.create_options().env[KEY] == "false"
        # Default is not recorded as an explicit launch override.
        assert KEY not in (sm.explicit_env_overrides or {})
        # The user-facing parent route is unchanged.
        assert KEY not in parent.session_manager.sdk_env_overrides
    finally:
        await bot.shutdown()


async def test_child_launch_env_override_reenables(config, tmp_path, monkeypatch):
    monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    try:
        _, child = await _launch(bot, route, 337, {KEY: "true"})
        sm = child.session_manager
        assert sm.create_options().env[KEY] == "true"
        assert sm.explicit_env_overrides == {KEY: "true"}
    finally:
        await bot.shutdown()


@pytest.mark.parametrize("override", [None, "true"])
async def test_restore_keeps_child_default_and_override(config, override):
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
    assert state.session_manager.sdk_env_overrides[KEY] == (override or "false")
    await restored.shutdown()


async def test_non_child_team_env_has_no_default(config):
    bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
    try:
        assert KEY not in bot._build_team_worker_env(team_name="t", agent_name="a")
        assert bot._build_team_worker_env(team_name=None, agent_child=True) == {KEY: "false"}
    finally:
        await bot.shutdown()
