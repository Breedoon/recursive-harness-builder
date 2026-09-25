"""vault-u3b.20: explicit AgentTask env must survive daemon restore and task resume."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute, _ForkTaskRecord

pytestmark = pytest.mark.asyncio

_TEST_GAP = 0.05

EXPLICIT = {
    "DISABLE_AUTO_COMPACT": "1",
    "OBS_COMPACT_POLICY": "handoff",
    "LEVEL_CONTEXT_THRESHOLD": "0.62",
}


def _team_env(team: str, agent: str) -> dict[str, str]:
    return {
        "CLAUDE_CODE_ENABLE_TASKS": "1",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        "CLAUDE_CODE_TASK_LIST_ID": team,
        "CLAUDE_CODE_TEAM_NAME": team,
        "CLAUDE_CODE_AGENT_NAME": agent,
    }


def _make_child(bot: TelegramBot, child_route: TelegramRoute, *, team: str | None, agent: str | None):
    child_state = bot._get_state(child_route, topic_title="General - Env Worker")
    assert child_state is not None
    child_state.session_manager.set_session_id("sid-env-child")
    bot._bind_state_session(child_state)
    bot._set_session_head(session_id="sid-env-child", jsonl_uuid="uuid-env-child")
    base = _team_env(team, agent) if team and agent else {}
    child_state.session_manager.explicit_env_overrides = dict(EXPLICIT)
    child_state.session_manager.set_sdk_env_overrides({**base, **EXPLICIT})
    child_state.session_manager.model_override = "gpt-6-luna[120k]"
    bot._persist_state_for_route(child_route)
    return child_state


def _record(child_route: TelegramRoute, *, team: str | None, agent: str | None, is_fork: bool):
    return _ForkTaskRecord(
        task_id="task-env-1",
        parent_route=TelegramRoute(chat_id=67890, thread_id=None),
        parent_session_id_at_launch="sid-parent",
        parent_source_uuid="uuid-parent",
        child_route=child_route,
        child_session_id="sid-env-child",
        prompt="",
        description="Env worker",
        status="completed",
        is_fork=is_fork,
        launch_tool_name="AgentTask",
        team_name=team,
        agent_name=agent,
        idle_ready=True,
        emit_parent_callback=False,
    )


@pytest.mark.parametrize("is_fork", [True, False])
async def test_team_worker_restore_keeps_explicit_env(config, is_fork):
    first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    child_route = TelegramRoute(chat_id=67890, thread_id=654)
    _make_child(first, child_route, team="team-alpha", agent="worker-env")
    record = _record(child_route, team="team-alpha", agent="worker-env", is_fork=is_fork)
    first._fork_tasks_by_id[record.task_id] = record
    first._register_team_worker_record(record)
    first._fork_task_by_child_route[child_route] = record.task_id
    await first.shutdown()

    restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    await restored.initialize_runtime()
    state = restored._get_state(child_route)
    assert state is not None
    env = state.session_manager.sdk_env_overrides
    for key, value in EXPLICIT.items():
        assert env.get(key) == value, key
    assert env.get("CLAUDE_CODE_TEAM_NAME") == "team-alpha"
    assert env.get("CLAUDE_CODE_AGENT_NAME") == "worker-env"
    assert state.session_manager.explicit_env_overrides == EXPLICIT
    assert state.session_manager.model_override == "gpt-6-luna[120k]"
    await restored.shutdown()


async def test_non_team_route_restore_keeps_explicit_env(config):
    first = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    child_route = TelegramRoute(chat_id=67890, thread_id=655)
    _make_child(first, child_route, team=None, agent=None)
    await first.shutdown()

    restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    await restored.initialize_runtime()
    state = restored._get_state(child_route)
    assert state is not None
    for key, value in EXPLICIT.items():
        assert state.session_manager.sdk_env_overrides.get(key) == value, key
    await restored.shutdown()


async def test_resume_keeps_explicit_env(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    parent_route = TelegramRoute(chat_id=67890, thread_id=None)
    child_route = TelegramRoute(chat_id=67890, thread_id=656)
    parent_state = bot._get_state(parent_route)
    assert parent_state is not None
    parent_state.session_manager.set_session_id("sid-parent")
    bot._bind_state_session(parent_state)
    child_state = _make_child(bot, child_route, team="team-alpha", agent="worker-env")
    record = _record(child_route, team="team-alpha", agent="worker-env", is_fork=False)
    bot._fork_tasks_by_id[record.task_id] = record
    bot._register_team_worker_record(record)
    bot._fork_task_by_child_route[child_route] = record.task_id
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=931), MagicMock(message_id=932)])
    parent_state.last_bot = fake_bot

    with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
        launched = await bot._launch_fork_task(
            route=parent_route,
            args={
                "prompt": "continue",
                "description": "resume",
                "fork": False,
                "resume": "task-env-1",
                "task_tool_name": "AgentTask",
            },
        )
    assert "launched" in launched["content"][0]["text"]
    env = child_state.session_manager.sdk_env_overrides
    for key, value in EXPLICIT.items():
        assert env.get(key) == value, key
    assert env.get("CLAUDE_CODE_AGENT_NAME") == "worker-env"
    await bot.shutdown()


async def test_session_switch_carries_route_configuration(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    child_route = TelegramRoute(chat_id=67890, thread_id=657)
    state = _make_child(bot, child_route, team="team-alpha", agent="worker-env")
    state.session_manager.user_hooks = {"Stop": "x.py::y"}
    await bot._activate_route_session(state, "sid-other")
    sm = state.session_manager
    assert sm.session_id == "sid-other"
    assert sm.model_override == "gpt-6-luna[120k]"
    assert sm.user_hooks == {"Stop": "x.py::y"}
    assert sm.explicit_env_overrides == EXPLICIT
    for key, value in EXPLICIT.items():
        assert sm.sdk_env_overrides.get(key) == value
    await bot.shutdown()


async def test_explicit_env_persisted_as_json(config):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    child_route = TelegramRoute(chat_id=67890, thread_id=658)
    _make_child(bot, child_route, team="team-alpha", agent="worker-env")
    snapshot = bot._state_store.load_snapshot()
    rows = [r for r in snapshot.route_states if r.thread_id == 658]
    assert rows and json.loads(rows[0].explicit_env_json) == EXPLICIT
    await bot.shutdown()


async def test_session_lineage_reports_hook_session_id_for_fresh_session(config, monkeypatch):
    """B27: a fresh session's manager id is unset mid-turn; fall back to the hook's id."""
    from obs_agent.hooks import HookState
    from obs_agent.lineage import build_obs_bootstrap_xml
    from obs_agent.tools import create_obs_tools

    captured = {}

    def capture(name, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", capture)
    state = HookState()
    state.session_id = "sid-from-hook"
    state.pending_obs_bootstrap_xml = build_obs_bootstrap_xml(
        lineage=("Trunk", "Fresh Subject"),
        origin="agent_task_fresh",
        is_fork=False,
        session_id=None,
        root_team_key="2026-09-25-09-50-local-fleet",
        agent_name="fresh-subject",
    )
    create_obs_tools(config, lambda: None, hook_state=state)
    tool = next(t for t in captured["tools"] if t.name == "session_lineage")
    result = await tool.handler({"include_xml": False})
    payload = json.loads(result["content"][0]["text"])
    assert payload["session_id"] == "sid-from-hook"
