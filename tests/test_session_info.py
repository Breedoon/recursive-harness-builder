"""Offline agent/session metadata regressions; no SDK/provider/Telegram calls."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from obs_agent.context_stats import build_context_snapshot, format_context_snapshot_compact
from obs_agent.hooks import create_hook_matchers
from obs_agent.lineage import build_obs_bootstrap_xml
from obs_agent.session import SessionManager
from obs_agent.session_info import (
    BUILTIN_HOOK_EVENTS, SessionViewContext, build_session_info,
    format_session_info_lines, session_context_window,
)
from obs_agent.telegram import (
    TelegramBot, TelegramRoute, _ForkTaskRecord, _TELEGRAM_HELP_TEXT,
    create_telegram_app, _set_bot_commands,
)


@pytest.fixture(autouse=True)
def isolated_home_and_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_EXTRA_BODY", "OBS_EFFORT_LEVEL"):
        monkeypatch.delenv(name, raising=False)


def transcript(tmp_path, rows, sid="session-1"):
    path = tmp_path / ".claude" / "projects" / "-fixture" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def usage(input_tokens=1_000, cache=23_000):
    return {"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": input_tokens, "cache_read_input_tokens": cache,
        "cache_creation_input_tokens": 0, "output_tokens": 900,
    }}}


def update_and_context(*, user=12345, args=()):
    update = MagicMock()
    update.effective_user.id = user
    update.effective_message.chat_id = 67890
    update.effective_message.message_thread_id = 321
    update.effective_message.reply_text = AsyncMock()
    context = MagicMock(args=list(args))
    return update, context


def rendered(info):
    return "\n".join(format_session_info_lines(info))


def test_cold_session_is_unknown_not_an_invented_agent(config):
    manager = SessionManager(config=config)
    manager.create_options = Mock(side_effect=AssertionError("must not create SDK options"))
    info = build_session_info(manager)
    assert info["session"]["session_id"] is None
    assert info["agent"]["agent_name"] is None
    assert info["agent"]["agent_id"] is None
    assert info["agent"]["task_id"] is None
    assert info["context"]["used_tokens"] is None
    assert "context: unavailable" in rendered(info)
    assert not info["session"]["connected"]
    assert not info["session"]["resumable"]
    assert not manager.hook_state.effective_model
    manager.create_options.assert_not_called()


@pytest.mark.parametrize("model,expected", [("gpt[200k]", 200_000), ("haiku", 200_000), ("local-qwen", 262_000)])
def test_cold_override_uses_its_own_model_window(config, model, expected):
    manager = SessionManager(config=config)
    manager.model_override = model
    assert session_context_window(manager, 400_000) == expected
    assert build_session_info(manager)["model"]["context_window_tokens"] == expected


def test_single_context_signal_matches_completion_and_preserves_zero_tail_fallback(config, tmp_path):
    manager = SessionManager(config=config)
    manager.set_session_id("session-1")
    path = transcript(tmp_path, [usage(), usage(0, 0)])
    manager.hook_state.last_result_data = {
        "session_id": "session-1", "num_turns": 7,
        "usage": {"input_tokens": 1_500_000, "cache_read_input_tokens": 8_000_000},
    }
    info = build_session_info(manager)
    expected = build_context_snapshot(
        session_id=manager.session_id, data=manager.hook_state.last_result_data,
        context_window_estimate_tokens=session_context_window(manager, 400_000), cwd=config.vault_path,
    )
    assert info["context"]["summary"] == format_context_snapshot_compact(expected)
    assert info["context"]["used_tokens"] == 24_000
    assert info["files"]["jsonl_session_file"] == str(path)
    text = rendered(info)
    assert sum(line.startswith("context:") for line in text.splitlines()) == 1
    for old_diagnostic in ("sdk_input", "cache_read_input", "estimated_context", "peak_context", "context_estimate_source"):
        assert old_diagnostic not in text


def test_text_estimate_remains_the_primary_fallback_without_extra_diagnostics(config, tmp_path):
    manager = SessionManager(config=config)
    manager.set_session_id("session-1")
    transcript(tmp_path, [{"type": "user", "message": {"role": "user", "content": "a" * 400}}])
    info = build_session_info(manager)
    assert info["context"]["used_tokens"] == 100
    assert "context: 100 /" in rendered(info)


@pytest.mark.parametrize("new_id", [None, "new-session"])
def test_stale_result_never_selects_an_old_transcript(config, tmp_path, new_id):
    transcript(tmp_path, [usage()], sid="old-session")
    manager = SessionManager(config=config)
    if new_id:
        manager.set_session_id(new_id)
    manager.hook_state.last_result_data = {"session_id": "old-session", "usage": {"input_tokens": 42}}
    info = build_session_info(manager)
    assert info["session"]["session_id"] == new_id
    assert info["files"]["jsonl_session_file"] is None
    assert info["context"]["used_tokens"] is None


@pytest.mark.parametrize("source", ["pending", "jsonl"])
def test_lineage_and_identifiers_are_distinct(config, tmp_path, source):
    manager = SessionManager(config=config)
    manager.set_session_id("session-1")
    xml = build_obs_bootstrap_xml(
        lineage=("Root", "Reviewer"), origin="AgentTask", is_fork=True,
        session_id="session-1", agent_id="task-uuid", parent_session_id="parent-session",
        root_team_key="2026-09-16-10-00-root", agent_name="machine-reviewer",
        parent_agent_name="parent-name", parent_display_name="Root",
    )
    if source == "jsonl":
        transcript(tmp_path, [{"type": "user", "message": {"role": "user", "content": xml}}, usage()])
    view = SessionViewContext(
        pending_bootstrap=xml if source == "pending" else None,
        task_id="task-uuid", parent_source_uuid="message-uuid", topic_title="Renamed topic",
    )
    info = build_session_info(manager, view=view)
    assert info["agent"]["lineage"] == ["Root", "Reviewer"]
    assert info["agent"]["display_name"] == "Reviewer"
    assert info["agent"]["agent_name"] == "machine-reviewer"
    assert info["agent"]["agent_id"] == "task-uuid"
    assert info["agent"]["task_id"] == "task-uuid"
    assert info["session"]["session_id"] == "session-1"
    assert info["session"]["parent_session_id"] == "parent-session"
    assert info["session"]["parent_source_uuid"] == "message-uuid"
    assert "not a separate file" in rendered(info)


@pytest.mark.parametrize("corrupt", ["xml", "encoding", "missing"])
def test_missing_and_corrupt_transcripts_do_not_break_inspection(config, tmp_path, corrupt):
    manager = SessionManager(config=config)
    manager.set_session_id("session-1")
    if corrupt != "missing":
        path = transcript(tmp_path, [{"type": "user", "message": {
            "role": "user", "content": "<obs-bootstrap><broken></obs-bootstrap>",
        }}])
        if corrupt == "encoding":
            path.write_bytes(b"\xff\xfe")
    info = build_session_info(manager)
    assert info["session"]["session_id"] == "session-1"
    assert "Agent" in rendered(info)
    if corrupt != "missing":
        assert info["warnings"]


def test_environment_values_are_not_in_either_projection_or_text(config):
    manager = SessionManager(config=config)
    manager.set_sdk_env_overrides({
        "ANTHROPIC_AUTH_TOKEN": "SECRET_AUTH_VALUE",
        "ANTHROPIC_BASE_URL": "https://SECRET_ENDPOINT_VALUE?key=SECRET_QUERY_VALUE",
        "CLAUDE_CODE_EXTRA_BODY": '{"metadata":{"key":"SECRET_BODY_VALUE"}}',
        "CUSTOM_KEY": "SECRET_CUSTOM_VALUE", "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
    })
    info = build_session_info(manager)
    assert info["environment"]["session_override_count"] == 5
    assert info["model"]["effort"] == "xhigh"
    assert "SECRET_" not in json.dumps(info)
    assert "SECRET_" not in rendered(info)
    assert manager.sdk_env_overrides["ANTHROPIC_AUTH_TOKEN"] == "SECRET_AUTH_VALUE"


def test_hook_inventory_matches_sdk_and_reading_never_imports_user_code(config, monkeypatch):
    manager = SessionManager(config=config)
    assert set(BUILTIN_HOOK_EVENTS) == set(create_hook_matchers(config, manager.hook_state))
    manager.user_hooks = {"PostToolUse": "danger.py::check", "UnknownEvent": "elsewhere.py::check"}
    loader = Mock(side_effect=AssertionError("must not load user hooks"))
    monkeypatch.setattr("obs_agent.hooks.load_hook_function", loader)
    info = build_session_info(manager)
    assert info["hooks"]["configured"] == manager.user_hooks
    assert info["hooks"]["unsupported_events"] == ["UnknownEvent"]
    assert "not proof of successful loading" in rendered(info)
    loader.assert_not_called()


def test_sdk_parameters_are_described_without_building_options(config):
    manager = SessionManager(config=config)
    info = build_session_info(manager)
    # Options creation is allowed in this test only, to detect drift in the
    # read-only inventory; the application getter itself must never call it.
    options = manager.create_options()
    assert info["runtime"]["permission_mode"] == options.permission_mode
    assert info["runtime"]["setting_sources"] == options.setting_sources
    assert info["runtime"]["mcp_servers"] == list(options.mcp_servers)
    assert info["runtime"]["max_buffer_size_bytes"] == options.max_buffer_size


async def test_telegram_cold_session_does_not_create_route_schedule_or_client(config, monkeypatch):
    config.context_probe_claude_cli = True
    probe = AsyncMock(side_effect=AssertionError("inspection must not probe CLI"))
    monkeypatch.setattr("obs_agent.context_probe.probe_context_via_claude_cli", probe)
    bot = TelegramBot(config, enable_background_poller=False)
    bot._default_new_chat_hooks = {"Stop": "stop.py::check"}
    bot._persist_state_for_route = Mock(side_effect=AssertionError("must not persist"))
    bot._maybe_seed_default_schedule = Mock(side_effect=AssertionError("must not seed schedules"))
    update, ctx = update_and_context()
    try:
        await bot.handle_session(update, ctx)
        text = "\n".join(call.args[0] for call in update.effective_message.reply_text.await_args_list)
        assert "session_id: (none)" in text
        assert "configured Stop: stop.py::check" in text
        assert not bot._states_by_route
        assert not bot._schedule_ids_by_route
        probe.assert_not_awaited()
        for call in update.effective_message.reply_text.await_args_list:
            assert call.kwargs["parse_mode"] is None
    finally:
        await bot.shutdown()


@pytest.mark.parametrize("condition", ["unauthorized", "no_user", "no_message", "args"])
async def test_telegram_guards(config, condition):
    bot = TelegramBot(config, enable_background_poller=False)
    update, ctx = update_and_context(user=999 if condition == "unauthorized" else 12345)
    message = update.effective_message
    if condition == "no_user":
        update.effective_user = None
    elif condition == "no_message":
        update.effective_message = None
    elif condition == "args":
        ctx.args = ["extra"]
    try:
        await bot.handle_session(update, ctx)
        assert not bot._states_by_route
        if condition == "args":
            assert "Usage: /session" in message.reply_text.await_args.args[0]
        else:
            message.reply_text.assert_not_awaited()
    finally:
        await bot.shutdown()


async def test_active_telegram_session_reports_task_and_keeps_queues(config, tmp_path):
    bot = TelegramBot(config, enable_background_poller=False)
    route = TelegramRoute(chat_id=67890, thread_id=321)
    state = bot._get_state(route)
    state.session_manager.set_session_id("session-1")
    transcript(tmp_path, [usage()])
    state.busy = True
    state.pending_messages.append("private pending prompt")
    state.hook_state.message_queue.put_nowait("private queued message")
    record = _ForkTaskRecord(
        task_id="task-id", parent_route=TelegramRoute(67890, 123),
        parent_session_id_at_launch="parent-id", parent_source_uuid="parent-message-id",
        child_route=route, child_session_id="session-1", prompt="private task prompt",
        team_name="team", agent_name="worker", timeout_ms=60_000, max_turns=4,
    )
    bot._fork_tasks_by_id[record.task_id] = record
    bot._fork_task_by_child_route[route] = record.task_id
    try:
        text = "\n".join(bot._build_session_lines(state))
        assert "status: working" in text
        assert "task_id: task-id" in text
        assert "agent_name: worker" in text
        assert "parent_session_id: parent-id" in text
        assert "timeout_ms: 60000" in text
        assert "private" not in text
        primary = next(line for line in text.splitlines() if line.startswith("context:"))
        assert primary in bot._build_completion_summary(state)
        assert state.hook_state.message_queue.qsize() == 1
        assert len(state.pending_messages) == 1
        assert state.session_manager.get_client.await_count == 0
    finally:
        state.busy = False
        state.pending_messages.clear()
        state.hook_state.message_queue.get_nowait()
        await bot.shutdown()


async def test_long_hook_list_is_split_and_metadata_stays_plain_text(config):
    bot = TelegramBot(config, enable_background_poller=False)
    state = bot._get_state(TelegramRoute(67890, 321))
    state.topic_title = "<b>not HTML</b>\nnot-a-new-field"
    state.session_manager.user_hooks = {f"Event{i}": "x" * 120 + ".py::check" for i in range(70)}
    update, ctx = update_and_context()
    try:
        await bot.handle_session(update, ctx)
        calls = update.effective_message.reply_text.await_args_list
        assert len(calls) > 1
        assert all(len(call.args[0]) <= 4096 for call in calls)
        assert all(call.kwargs["parse_mode"] is None for call in calls)
        text = "\n".join(call.args[0] for call in calls)
        assert "<b>not HTML</b> not-a-new-field" in text
    finally:
        await bot.shutdown()


async def test_help_menu_and_registered_handler_replace_context(config, monkeypatch):
    config.telegram_bot_token = "123:test-token"
    application = MagicMock()
    application.bot_data = {}
    builder = MagicMock()
    builder.token.return_value = builder
    builder.concurrent_updates.return_value = builder
    builder.build.return_value = application
    monkeypatch.setattr("obs_agent.telegram.Application.builder", lambda: builder)
    assert create_telegram_app(config) is application
    handlers = [call.args[0] for call in application.add_handler.call_args_list]
    commands = set().union(*(getattr(h, "commands", frozenset()) for h in handlers))
    assert "session" in commands and "context" not in commands
    application.bot.set_my_commands = AsyncMock()
    await _set_bot_commands(application)
    menu = {c.command for c in application.bot.set_my_commands.await_args.args[0]}
    assert "session" in menu and "context" not in menu
    assert "/session" in _TELEGRAM_HELP_TEXT and "/context" not in _TELEGRAM_HELP_TEXT
    await application.bot_data["obs_telegram_bot"].shutdown()


async def test_daemon_get_session_is_read_only_even_during_a_turn(config):
    from obs_agent.daemon import create_app
    app = create_app(config)
    manager = app.state.session_manager
    manager.model_override = "haiku"
    async with app.state.turn_lock:
        with TestClient(app) as client:
            response = client.get("/session")
            assert response.status_code == 200
            info = response.json()
            assert info["runtime"]["busy"]
            assert info["model"]["context_window_tokens"] == 200_000
            assert info["session"]["session_id"] is None
            assert "session" in {c["name"] for c in client.get("/commands").json()["commands"]}
    manager.get_client.assert_not_awaited()
    assert not manager.has_connected_client()


async def test_cli_session_fetches_control_endpoint_and_handles_errors(config, monkeypatch):
    from obs_agent.cli import execute_session_command
    requests = []
    payload = build_session_info(SessionManager(config=config))
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    client_type = httpx.AsyncClient
    monkeypatch.setattr("obs_agent.cli.httpx.AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs))
    assert "session_id:" in await execute_session_command("/session", base_url="http://test")
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/session")]
    assert "Usage" in await execute_session_command("/session extra", base_url="http://test")
    assert len(requests) == 1
    payload.clear()
    assert "invalid session information" in await execute_session_command("/session", base_url="http://test")


async def test_cli_session_available_during_stream_without_enqueuing(monkeypatch):
    from obs_agent import cli
    stdin = io.StringIO("/session\n/quit\n")
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    monkeypatch.setattr(cli.select, "select", lambda *args: ([stdin], [], []))
    execute = AsyncMock(return_value="Session snapshot")
    monkeypatch.setattr(cli, "execute_session_command", execute)
    post = Mock()
    monkeypatch.setattr(cli.httpx, "post", post)
    channel = MagicMock()
    assert await cli._handle_input_during_stream("http://test", asyncio.Event(), channel) == "/quit"
    execute.assert_awaited_once_with("/session", base_url="http://test")
    channel.print_output.assert_called_with("Session snapshot\n")
    assert all(call.args[0].endswith("/chat/interrupt") for call in post.call_args_list)


async def test_persisted_route_identity_is_used_without_generating_names(config):
    bot = TelegramBot(config, enable_background_poller=False)
    route = TelegramRoute(67890, 321)
    state = bot._get_state(route)
    bot._route_inbox_target_keys_by_route[route] = ("existing-team", "existing-agent")
    try:
        text = "\n".join(bot._build_session_lines(state))
        assert "team_name: existing-team" in text
        assert "agent_name: existing-agent" in text
        assert state.session_manager.sdk_env_overrides == {}
    finally:
        await bot.shutdown()


async def test_cli_session_available_at_idle_prompt(config, monkeypatch, capsys):
    from obs_agent import cli
    channel = MagicMock()
    channel.read_input = AsyncMock(side_effect=["/session", "/quit"])
    monkeypatch.setattr("obs_agent.input.SimpleChannel", lambda: channel)
    monkeypatch.setenv("OBS_SIMPLE_INPUT", "1")
    monkeypatch.setattr(cli.sys, "argv", ["obs-agent"])
    monkeypatch.setattr(cli, "bootstrap_runtime_env", lambda: None)
    monkeypatch.setattr(cli, "assert_live_entrypoint_allowed", lambda: None)
    monkeypatch.setattr(cli.OBSConfig, "from_env", lambda: config)
    monkeypatch.setattr(cli, "check_daemon", lambda *args: True)
    execute = AsyncMock(return_value="Idle session snapshot")
    monkeypatch.setattr(cli, "execute_session_command", execute)
    stream = AsyncMock(side_effect=AssertionError("must not send a session prompt"))
    monkeypatch.setattr(cli, "stream_with_input", stream)
    await cli.async_main()
    execute.assert_awaited_once_with("/session", base_url=config.base_url)
    stream.assert_not_awaited()
    assert "Idle session snapshot" in capsys.readouterr().out
