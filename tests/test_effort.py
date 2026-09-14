"""Effort regressions: policy, SDK wire options, task delegation, and transports.

No live provider credentials or model requests are used by this suite.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from obs_agent.config import OBSConfig
from obs_agent.effort import (
    EFFORT_ENV, EFFORT_LEVELS, EXTRA_BODY_ENV, build_effort_env,
    child_effort_override, normalize_effort, resolve_effort,
)
from obs_agent.session import SessionManager
from obs_agent.telegram import TelegramBot, TelegramRoute, create_telegram_app, _set_bot_commands
from obs_agent.telegram_state_store import TelegramStateStore


@pytest.fixture(autouse=True)
def isolated_effort_env(monkeypatch):
    for key in (EFFORT_ENV, EXTRA_BODY_ENV, "OBS_EFFORT_LEVEL", "OBS_MODEL_EFFORT_LEVELS",
                "MAX_THINKING_TOKENS", "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("level", (*EFFORT_LEVELS, "auto"))
def test_normalize_case_and_whitespace(level):
    assert normalize_effort(f"  {level.upper()} ") == level


@pytest.mark.parametrize("value", ["", "highest", "none", "inherit", 4, True, {}, None])
def test_reject_invalid_effort(value):
    with pytest.raises(ValueError, match="Effort"):
        normalize_effort(value)


@pytest.mark.parametrize("model,expected", [
    ("gpt", "medium"), ("sol[200k]", "medium"), ("claude", "high"),
    ("sonnet[1m]", "high"), ("claude-opus-4-7", "xhigh"),
    ("haiku", "auto"), ("local-qwen", "auto"), ("custom-model", "auto"),
])
def test_defaults_resolve_model_aliases_independently_of_context(model, expected):
    assert resolve_effort(model) == expected


def test_precedence_and_auto_model_default():
    kwargs = dict(
        model="gpt[200k]", override="max", session_env={EFFORT_ENV: "xhigh"},
        configured="high", environ={EFFORT_ENV: "low"},
        model_defaults={"gpt-5.6-sol": "medium"},
    )
    assert resolve_effort(**kwargs) == "max"
    kwargs["override"] = None
    assert resolve_effort(**kwargs) == "xhigh"
    kwargs["session_env"] = {}
    assert resolve_effort(**kwargs) == "high"
    kwargs["configured"] = None
    assert resolve_effort(**kwargs) == "low"
    kwargs["override"] = "auto"
    assert resolve_effort(**kwargs) == "medium"


def test_config_env_supports_per_model_defaults(monkeypatch):
    monkeypatch.setenv("OBS_EFFORT_LEVEL", " HIGH ")
    monkeypatch.setenv("OBS_MODEL_EFFORT_LEVELS", '{"sol":"max","sonnet[200k]":"low"}')
    config = OBSConfig.from_env()
    assert config.effort_level == "high"
    assert config.model_effort_levels == {"gpt-5.6-sol": "max", "claude-sonnet-5": "low"}
    manager = SessionManager(config=config)
    assert manager.effective_effort == "high"
    manager.effort_override = "auto"
    assert manager.effective_effort == "max"


@pytest.mark.parametrize("raw", ["[1]", '"high"', '{"gpt":"bad"}', '{"gpt":5}', '{'])
def test_invalid_model_effort_config_fails_early(monkeypatch, raw):
    monkeypatch.setenv("OBS_MODEL_EFFORT_LEVELS", raw)
    with pytest.raises(ValueError):
        OBSConfig.from_env()


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_openai_wire_options_all_levels_with_context_and_no_global_mutation(config, level):
    manager = SessionManager(config=config)
    manager.model_override = "gpt[200k]"
    manager.effort_override = level
    original_env = {"CUSTOM_SESSION_KEY": "keep", EXTRA_BODY_ENV: json.dumps({
        "metadata": {"user_id": "test"}, "output_config": {"format": {"type": "json_schema"}},
        "thinking": {"type": "enabled", "budget_tokens": 8192, "display": "summarized"},
    })}
    manager.set_sdk_env_overrides(original_env)
    before = dict(os.environ)
    options = manager.create_options()
    assert options.env[EFFORT_ENV] == level
    body = json.loads(options.env[EXTRA_BODY_ENV])
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"]["effort"] == level
    assert body["output_config"]["format"] == {"type": "json_schema"}
    assert body["metadata"] == {"user_id": "test"}
    assert options.env["CUSTOM_SESSION_KEY"] == "keep"
    settings_env = json.loads(options.settings)["env"]
    assert settings_env[EFFORT_ENV] == level
    assert settings_env[EXTRA_BODY_ENV] == options.env[EXTRA_BODY_ENV]
    assert options.env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "200000"
    assert options.model == "gpt-5.6-sol[200k]"
    assert manager.sdk_env_overrides == original_env
    assert dict(os.environ) == before


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_native_claude_uses_effort_env_without_proxy_thinking_override(config, level):
    manager = SessionManager(config=config)
    manager.model_override = "opus"
    manager.effort_override = level
    options = manager.create_options()
    assert options.env[EFFORT_ENV] == level
    assert json.loads(options.settings)["env"][EFFORT_ENV] == level
    assert EXTRA_BODY_ENV not in options.env


def test_shell_extra_body_is_merged(monkeypatch, config):
    monkeypatch.setenv(EXTRA_BODY_ENV, '{"metadata":{"user_id":"shell"}}')
    options = SessionManager(config=config).create_options()
    body = json.loads(options.env[EXTRA_BODY_ENV])
    assert body["metadata"] == {"user_id": "shell"}
    assert body["output_config"]["effort"] == "medium"


@pytest.mark.parametrize("body,extra_env", [
    ({"temperature": 0.5, "thinking": {"type": "disabled"}}, {}),
    ({"thinking": {"type": "disabled"}}, {}),
    ({}, {"MAX_THINKING_TOKENS": "0"}),
    ({}, {"CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1"}),
])
def test_never_reenable_explicitly_disabled_thinking(body, extra_env):
    env = {EXTRA_BODY_ENV: json.dumps(body), **extra_env}
    result = build_effort_env("gpt", "max", env)
    assert result[EFFORT_ENV] == "max"
    assert EXTRA_BODY_ENV not in result
    assert json.loads(env[EXTRA_BODY_ENV]) == body


@pytest.mark.parametrize("raw", ["{", "[]", '"x"', '{"thinking":false}', '{"output_config":[]}'])
def test_bad_extra_body_rejected_before_session_mutation(config, raw):
    manager = SessionManager(config=config)
    manager.set_sdk_env_overrides({EXTRA_BODY_ENV: raw})
    with pytest.raises(ValueError, match="CLAUDE_CODE_EXTRA_BODY"):
        manager.create_options()
    assert manager.effort_override is None


async def test_change_reconnects_lazily_keeps_session_and_does_not_leak(config):
    first = SessionManager(config=config)
    other = SessionManager(config=config)
    first.set_session_id("kept-session")
    first._client = MagicMock(disconnect=AsyncMock())
    client = first._client
    first._connected = True
    assert await first.set_effort("max") == "max"
    client.disconnect.assert_awaited_once()
    assert first.session_id == "kept-session"
    assert first.create_options().resume == "kept-session"
    assert first.effective_effort == "max"
    assert other.effective_effort == "medium"
    assert config.effort_level is None
    await first.set_effort("auto")
    assert first.effective_effort == "medium"
    assert json.loads(first.create_options().env[EXTRA_BODY_ENV])["output_config"]["effort"] == "medium"
    assert first.sdk_env_overrides == {}


async def test_invalid_selection_does_not_disconnect(config):
    manager = SessionManager(config=config)
    manager.disconnect = AsyncMock()
    with pytest.raises(ValueError):
        await manager.set_effort("bad")
    manager.disconnect.assert_not_awaited()
    assert manager.effort_override is None


@pytest.mark.parametrize("requested,model,env,expected", [
    (None, None, {}, "max"), (None, "sonnet", {}, None),
    ("inherit", "sonnet", {}, "max"), ("auto", None, {}, "auto"),
    ("low", None, {EFFORT_ENV: "max"}, "low"),
    (None, None, {EFFORT_ENV: "low"}, "low"),
    (None, "sonnet", {EFFORT_ENV: "xhigh"}, "xhigh"),
])
def test_child_inheritance_policy(requested, model, env, expected):
    assert child_effort_override(requested, parent_effort="max", model=model, session_env=env) == expected


@pytest.mark.parametrize("level", (*EFFORT_LEVELS, "auto", "inherit"))
async def test_agenttask_schema_and_payload(config, monkeypatch, level):
    from obs_agent.hooks import HookState
    from obs_agent.tools import create_obs_tools

    captured = {}
    def capture(name, **kwargs):
        captured.update(kwargs)
        return {}
    monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", capture)
    state = HookState()
    state.fork_task_launcher = AsyncMock(return_value={"content": []})
    create_obs_tools(config, lambda: "parent", hook_state=state)
    tool = next(t for t in captured["tools"] if t.name == "AgentTask")
    assert level in tool.input_schema["properties"]["effort"]["enum"]
    await tool.handler({"prompt": "test", "display_name": "Worker", "effort": level})
    assert state.fork_task_launcher.await_args.args[0]["effort"] == level


@pytest.mark.parametrize("args", [{"effort": "bad"}, {"effort": 42},
                                    {"effort": "max", "resume": "child"},
                                    {"env": {EFFORT_ENV: "bad"}}])
async def test_agenttask_invalid_effort_never_launches(config, monkeypatch, args):
    from obs_agent.hooks import HookState
    from obs_agent.tools import create_obs_tools

    captured = {}
    def capture(name, **kwargs):
        captured.update(kwargs)
        return {}
    monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", capture)
    state = HookState()
    state.fork_task_launcher = AsyncMock()
    create_obs_tools(config, lambda: "parent", hook_state=state)
    tool = next(t for t in captured["tools"] if t.name == "AgentTask")
    result = await tool.handler({"prompt": "test", "display_name": "Worker", **args})
    assert result.get("is_error") or result.get("isError")
    state.fork_task_launcher.assert_not_awaited()


def update_and_context(args, *, user_id=12345):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.chat_id = 67890
    update.effective_message.message_thread_id = 321
    update.effective_message.message_id = 1
    update.effective_message.text = "/effort " + " ".join(args)
    ctx = MagicMock()
    ctx.args = args
    ctx.bot.send_message = AsyncMock()
    return update, ctx


async def test_telegram_effort_persists_and_restores_with_conversation(config):
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    route = TelegramRoute(chat_id=67890, thread_id=321)
    state = bot._get_state(route)
    state.session_manager.set_session_id("kept-id")
    update, ctx = update_and_context(["XHIGH"])
    try:
        await bot.handle_effort(update, ctx)
        assert state.session_manager.effective_effort == "xhigh"
        assert state.session_id == "kept-id"
        assert "next turn" in ctx.bot.send_message.call_args.kwargs["text"]
        entry = next(e for e in bot._state_store.load_snapshot().route_states if e.thread_id == 321)
        assert entry.effort_override == "xhigh"
    finally:
        await bot.shutdown()
    restored = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    try:
        state = restored._get_state(route)
        assert state.session_manager.effective_effort == "xhigh"
        assert state.session_id == "kept-id"
        update, ctx = update_and_context([])
        await restored.handle_effort(update, ctx)
        assert "effort: xhigh" in ctx.bot.send_message.call_args.kwargs["text"]
        assert "effort: xhigh" in await restored._build_context_lines(state)
    finally:
        await restored.shutdown()


@pytest.mark.parametrize("blocking", ["busy", "pending", "queue"])
async def test_telegram_rejects_effort_during_active_or_queued_work(config, blocking):
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    state = bot._get_state(TelegramRoute(chat_id=67890, thread_id=321))
    state.session_manager.disconnect = AsyncMock()
    if blocking == "busy":
        state.busy = True
    elif blocking == "pending":
        state.pending_messages.append("pending")
    else:
        state.hook_state.message_queue.put_nowait("queued")
    try:
        update, ctx = update_and_context(["low"])
        await bot.handle_effort(update, ctx)
        assert "effort unchanged" in ctx.bot.send_message.call_args.kwargs["text"]
        assert state.session_manager.effort_override is None
        state.session_manager.disconnect.assert_not_awaited()
    finally:
        state.busy = False
        state.pending_messages.clear()
        while not state.hook_state.message_queue.empty():
            state.hook_state.message_queue.get_nowait()
        await bot.shutdown()


@pytest.mark.parametrize("args", [["invalid"], ["low", "high"]])
async def test_telegram_invalid_args(config, args):
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    state = bot._get_state(TelegramRoute(chat_id=67890, thread_id=321))
    try:
        update, ctx = update_and_context(args)
        await bot.handle_effort(update, ctx)
        assert "usage:" in ctx.bot.send_message.call_args.kwargs["text"]
        assert state.session_manager.effort_override is None
    finally:
        await bot.shutdown()


async def test_telegram_unauthorized_and_registration(config):
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    try:
        update, ctx = update_and_context(["max"], user_id=999)
        await bot.handle_effort(update, ctx)
        ctx.bot.send_message.assert_not_awaited()
        assert bot._states_by_route == {}
    finally:
        await bot.shutdown()
    app = MagicMock()
    app.bot.set_my_commands = AsyncMock()
    await _set_bot_commands(app)
    assert "effort" in {c.command for c in app.bot.set_my_commands.await_args.args[0]}


@pytest.mark.parametrize("model,effort,env,expected", [
    (None, None, None, "max"), ("sonnet", None, None, "high"),
    ("gpt[200k]", "xhigh", None, "xhigh"), ("sonnet", "inherit", None, "max"),
    (None, "auto", None, "medium"), (None, None, {EFFORT_ENV: "low"}, "low"),
])
async def test_actual_child_launch_receives_effort(config, tmp_path, monkeypatch, model, effort, env, expected):
    monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    route = TelegramRoute(chat_id=-10067890, thread_id=None)
    parent = bot._get_state(route)
    parent.session_manager.effort_override = "max"
    parent.last_bot = MagicMock()
    parent.last_bot.create_forum_topic = AsyncMock(return_value=MagicMock(message_thread_id=335))
    parent.last_bot.send_message = AsyncMock(side_effect=[MagicMock(message_id=i) for i in range(940, 950)])
    parent.session_manager.set_session_id("parent-id")
    try:
        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock):
            await bot._launch_fork_task(route=route, args={
                "prompt": "Test child", "description": "Worker", "fork": False,
                "model": model, "effort": effort, "env": env, "task_tool_name": "AgentTask",
            })
        child = bot._get_state(TelegramRoute(chat_id=-10067890, thread_id=335))
        assert child.session_manager.effective_effort == expected
        assert child.session_manager.create_options().env[EFFORT_ENV] == expected
        assert parent.session_manager.effective_effort == "max"
        if model == "gpt[200k]":
            assert child.session_manager.create_options().env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] == "200000"
    finally:
        await bot.shutdown()


def test_state_store_additive_migration_and_roundtrip(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = TelegramStateStore(path)
    store.initialize()
    values = dict(chat_id=1, thread_id=2, session_id="kept", topic_title="topic",
                  topic_icon_custom_emoji_id=None, child_fork_count=0, child_fork_base_title=None,
                  notify_on_completion=True, last_inbound_message_id=None)
    store.upsert_route_state(**values)
    store.close()
    # Exact pre-feature table, with a real existing row.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE route_state DROP COLUMN effort_override")
    migrated = TelegramStateStore(path)
    migrated.initialize()
    assert migrated.load_snapshot().route_states[0].session_id == "kept"
    assert migrated.load_snapshot().route_states[0].effort_override is None
    migrated.upsert_route_state(**values, effort_override="max")
    assert migrated.load_snapshot().route_states[0].effort_override == "max"
    migrated.upsert_route_state(**values, effort_override="auto")
    assert migrated.load_snapshot().route_states[0].effort_override == "auto"
    migrated.close()


def test_daemon_show_change_and_invalid_effort(config):
    from obs_agent.daemon import create_app
    app = create_app(config)
    manager = app.state.session_manager
    manager.set_session_id("kept")
    with TestClient(app) as client:
        assert client.get("/effort").json()["effort"] == "medium"
        response = client.post("/effort", json={"effort": "max"})
        assert response.status_code == 200
        assert response.json()["effort"] == "max"
        assert manager.session_id == "kept"
        assert client.post("/effort", json={"effort": "bad"}).status_code == 422
        assert manager.effective_effort == "max"
        assert "effort" in {c["name"] for c in client.get("/commands").json()["commands"]}


async def test_daemon_busy_guard(config):
    from obs_agent.daemon import create_app
    app = create_app(config)
    app.state.session_manager.disconnect = AsyncMock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with app.state.turn_lock:
            assert (await client.post("/effort", json={"effort": "max"})).status_code == 409
            assert (await client.get("/effort")).status_code == 200
        app.state.hook_state.message_queue.put_nowait("queued")
        assert (await client.post("/effort", json={"effort": "max"})).status_code == 409
    app.state.session_manager.disconnect.assert_not_awaited()


async def test_cli_effort_uses_control_endpoint_and_validates(monkeypatch):
    from obs_agent.cli import execute_effort_command
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"effort": "medium", "model": "gpt"})
        return httpx.Response(200, json={"message": "effort selected: max"})
    client_type = httpx.AsyncClient
    monkeypatch.setattr("obs_agent.cli.httpx.AsyncClient", lambda **kw: client_type(
        transport=httpx.MockTransport(handler), **kw))
    assert "medium" in await execute_effort_command("/effort", base_url="http://test")
    assert "max" in await execute_effort_command("/effort max", base_url="http://test")
    assert json.loads(requests[-1].content) == {"effort": "max"}
    assert [r.url.path for r in requests] == ["/effort", "/effort"]
    assert "usage" in await execute_effort_command("/effort low high", base_url="http://test")
    assert "usage" in await execute_effort_command("/effort invalid", base_url="http://test")
    assert len(requests) == 2
