"""New tree in a chosen existing group chat (obs-15.5 / ops-61 / ops-1.11.6).

No test posts to Telegram: topic creation, sends and the first turn are stubbed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from obs_agent import chat_registry, control
from obs_agent.lineage import parse_obs_bootstrap_xml
from obs_agent.telegram import TelegramBot, TelegramRoute
from tests.test_telegram import _TEST_GAP, _state

PROC_CHAT = -1003780712622
AC_CHAT = -1004447899968


def _registry(tmp_path):
    path = tmp_path / "chat-registry.json"
    path.write_text(json.dumps({"chats": [
        {"chat_id": PROC_CHAT, "name": "procedures", "aliases": ["obs", "recording"], "purpose": "OBS + procedures"},
        {"chat_id": AC_CHAT, "name": "autonomous-companies", "aliases": ["ac"]},
    ]}), encoding="utf-8")
    return path


class TestChatRegistry:
    def test_name_and_alias_resolve(self, tmp_path):
        entries = chat_registry.load_registry(_registry(tmp_path))
        assert chat_registry.resolve_chat("procedures", known_chat_ids=[], entries=entries).chat_id == PROC_CHAT
        assert chat_registry.resolve_chat("AC", known_chat_ids=[], entries=entries).chat_id == AC_CHAT

    def test_known_or_declared_id_resolves(self, tmp_path):
        entries = chat_registry.load_registry(_registry(tmp_path))
        assert chat_registry.resolve_chat(str(AC_CHAT), known_chat_ids=[], entries=entries).chat_id == AC_CHAT
        assert chat_registry.resolve_chat(-555, known_chat_ids=[-555], entries=[]).chat_id == -555

    def test_unknown_chat_is_refused_never_created(self, tmp_path):
        entries = chat_registry.load_registry(_registry(tmp_path))
        res = chat_registry.resolve_chat(-999, known_chat_ids=[PROC_CHAT], entries=entries)
        assert res.chat_id is None and "never created" in res.error
        res = chat_registry.resolve_chat("finance", known_chat_ids=[PROC_CHAT], entries=entries)
        assert res.chat_id is None and "procedures" in res.error

    def test_default_only_when_one_chat_known(self):
        assert chat_registry.resolve_chat(None, known_chat_ids=[7], entries=[]).chat_id == 7
        res = chat_registry.resolve_chat(None, known_chat_ids=[7, 8], entries=[])
        assert res.chat_id is None and "--chat-id" in res.error

    def test_missing_or_malformed_file_is_empty(self, tmp_path):
        assert chat_registry.load_registry(tmp_path / "nope.json") == []
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert chat_registry.load_registry(bad) == []

    def test_env_overrides_default_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv(chat_registry.REGISTRY_ENV, str(tmp_path / "x.json"))
        assert chat_registry.registry_path(tmp_path / "vault") == tmp_path / "x.json"
        monkeypatch.delenv(chat_registry.REGISTRY_ENV)
        assert chat_registry.registry_path(tmp_path) == tmp_path / chat_registry.DEFAULT_RELATIVE_PATH


@pytest.fixture
def bot(config, tmp_path, monkeypatch):
    monkeypatch.setenv(chat_registry.REGISTRY_ENV, str(_registry(tmp_path)))
    b = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    b._primary_bot = object()
    b._enqueue_create_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=4242))
    b._send_system_message = AsyncMock(return_value=None)
    b._run_and_send = AsyncMock(return_value=None)
    return b


class TestCreateNewTree:
    async def test_new_root_in_named_chat_returns_identity(self, bot, tmp_path):
        with patch("obs_agent.telegram.Path.home", return_value=tmp_path):
            result = await bot._create_new_tree(
                trigger_label="t", notice_text="n", chat="procedures", title="Budget to Dot",
                prompt="do it", hooks={"PreToolUse": "/abs/guard.py::check"},
                model="haiku", effort="high", env={"FOO": "bar"},
                origin={"kind": "agent", "origin_team": "2026-10-17-11-36-proc-work", "origin_agent": "entry"},
            )
        assert result["ok"] is True
        res = result["result"]
        assert res["chat_id"] == PROC_CHAT and res["chat_name"] == "procedures" and res["thread_id"] == 4242
        # The topic is created in the EXISTING chat; nothing creates a chat.
        created_route = bot._enqueue_create_topic.await_args.kwargs["route"]
        assert created_route == TelegramRoute(chat_id=PROC_CHAT, thread_id=None)
        state = bot._get_state(TelegramRoute(chat_id=PROC_CHAT, thread_id=4242), create=False)
        bootstrap = parse_obs_bootstrap_xml(state.pending_obs_bootstrap)
        assert bootstrap.origin == "new_trunk" and not bootstrap.is_fork
        assert res["root_team_key"] == bootstrap.root_team_key and res["agent_name"] == bootstrap.agent_name
        assert res["agent_name"] == res["root_team_key"]  # a trunk's agent_name is its team key
        assert state.session_manager.user_hooks == {"PreToolUse": "/abs/guard.py::check"}
        assert state.session_manager.model_override and "haiku" in state.session_manager.model_override
        assert state.session_manager.effort_override == "high"
        assert state.session_manager.sdk_env_overrides["FOO"] == "bar"
        origin = json.loads((tmp_path / ".claude" / "teams" / res["root_team_key"] / "origin.json").read_text())
        assert origin["origin_agent"] == "entry" and origin["kind"] == "agent"
        first_turn = bot._run_and_send.call_args.kwargs["user_text"]
        assert first_turn.startswith("[t]\n") and "do it" in first_turn

    async def test_no_hooks_keeps_default_new_chat_hooks(self, bot, tmp_path):
        bot._default_new_chat_hooks = {"PreToolUse": "/abs/new_chat_guard.py::check"}
        with patch("obs_agent.telegram.Path.home", return_value=tmp_path):
            result = await bot._create_new_tree(trigger_label="t", notice_text="n", chat=AC_CHAT,
                                                title="X", prompt="p")
        state = bot._get_state(TelegramRoute(chat_id=AC_CHAT, thread_id=4242), create=False)
        assert result["ok"] and state.session_manager.user_hooks == {"PreToolUse": "/abs/new_chat_guard.py::check"}

    async def test_unknown_chat_refused_before_any_topic(self, bot, tmp_path):
        with patch("obs_agent.telegram.Path.home", return_value=tmp_path):
            result = await bot._create_new_tree(trigger_label="t", notice_text="n", chat="finance",
                                                title="X", prompt="p")
        assert result["ok"] is False and "chat registry" in result["error"]
        bot._enqueue_create_topic.assert_not_awaited()
        bot._run_and_send.assert_not_called()

    async def test_prompt_file_is_wrapped_like_agenttask(self, bot, tmp_path):
        with patch("obs_agent.telegram.Path.home", return_value=tmp_path):
            result = await bot._create_new_tree(
                trigger_label="t", notice_text="n", chat=PROC_CHAT, title="X", prompt="go",
                prompt_file="procedures/x.md", prompt_file_content="FILE BODY",
            )
        assert result["ok"]
        text = bot._run_and_send.call_args.kwargs["user_text"]
        assert '<prompt_file_context path="procedures/x.md">' in text and "FILE BODY" in text and text.endswith("go")

    async def test_bad_hooks_refused(self, bot, tmp_path):
        result = await bot._create_new_tree(trigger_label="t", notice_text="n", chat=PROC_CHAT,
                                            title="X", prompt="p", hooks={"PreToolUse": "nofunction"})
        assert result["ok"] is False and "file_path::function_name" in result["error"]
        bot._enqueue_create_topic.assert_not_awaited()


class TestControlNewTree:
    async def test_cli_args_reach_core(self, bot, tmp_path):
        bot._create_new_tree = AsyncMock(return_value={"ok": True, "text": "x", "result": {}})
        await bot._control_dispatch("new_tree", "nightly", {
            "title": "T", "prompt": "p", "chat": "procedures", "model": "opus",
            "hooks": '{"PreToolUse": "a.py::check"}', "env": '{"K": "V"}'})
        kw = bot._create_new_tree.await_args.kwargs
        assert kw["chat"] == "procedures" and kw["model"] == "opus"
        assert kw["hooks"] == {"PreToolUse": "a.py::check"} and kw["env"] == {"K": "V"}
        assert kw["origin"] == {"kind": "external", "caller": "nightly"}
        assert kw["trigger_label"] == "external trigger: nightly"

    async def test_chat_id_still_accepted(self, bot):
        bot._create_new_tree = AsyncMock(return_value={"ok": True, "text": "x", "result": {}})
        await bot._control_dispatch("new_tree", "nightly", {"title": "T", "prompt": "p", "chat_id": AC_CHAT})
        assert bot._create_new_tree.await_args.kwargs["chat"] == AC_CHAT

    def test_cli_parser(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(control, "send_request",
                            lambda op, caller, args, **kw: (sent.update(op=op, args=args) or (0, {"ok": True, "text": "x"})))
        assert control.main(["--as", "cron", "new-tree", "--title", "T", "--prompt-file", "p.md",
                             "--chat", "procedures", "--model", "opus", "--hooks", "{}"]) == 0
        assert sent["op"] == "new_tree"
        assert sent["args"] == {"title": "T", "prompt_file": "p.md", "chat": "procedures", "model": "opus", "hooks": "{}"}

    def test_validation_needs_prompt_or_file(self):
        assert "prompt or prompt_file" in control.validate_request(
            {"op": "new_tree", "caller": "cron", "args": {"title": "t"}})
        assert control.validate_request(
            {"op": "new_tree", "caller": "cron", "args": {"title": "t", "prompt_file": "x.md"}}) is None


class TestAgentTaskNewTree:
    async def test_launch_fork_task_routes_to_new_tree_with_origin(self, bot):
        caller = _state(bot, chat_id=PROC_CHAT, thread_id=77)
        caller.last_bot = object()
        caller.session_manager.set_session_id("sid-caller")
        team = "2026-10-17-11-36-proc-work"
        bot._route_inbox_targets[(team, "entry-agent")] = caller.route
        bot._route_inbox_target_keys_by_route[caller.route] = (team, "entry-agent")
        bot._state_inbox_projection = lambda state, **kw: (team, "entry-agent")
        bot._create_new_tree = AsyncMock(return_value={
            "ok": True, "text": "new tree started",
            "result": {"link": "L", "root_team_key": "2026-10-20-01-00-x", "agent_name": "2026-10-20-01-00-x"}})
        out = await bot._launch_fork_task(route=caller.route, args={
            "new_tree_chat": "ac", "description": "Budget", "prompt": "go", "model": "opus", "fork": False})
        assert out["tool_use_result"]["root_team_key"] == "2026-10-20-01-00-x" and not out.get("is_error")
        kw = bot._create_new_tree.await_args.kwargs
        assert kw["chat"] == "ac" and kw["title"] == "Budget" and kw["model"] == "opus"
        assert kw["origin"]["origin_agent"] == "entry-agent" and kw["origin"]["origin_team"] == team
        assert "SendInboxMessage team_name=2026-10-17-11-36-proc-work" in kw["trigger_label"]

    async def test_launch_failure_is_tool_error(self, bot):
        caller = _state(bot, chat_id=PROC_CHAT, thread_id=78)
        caller.last_bot = object()
        bot._create_new_tree = AsyncMock(return_value={"ok": False, "error": "no chat named 'x'"})
        out = await bot._launch_fork_task(route=caller.route, args={"new_tree_chat": "x", "description": "B", "prompt": "p"})
        assert out["is_error"] and "no chat named" in out["content"][0]["text"]


class TestAgentTaskToolArgs:
    def _tools(self, config, launcher):
        from obs_agent.hooks import HookState
        from obs_agent.tools import create_obs_tools

        hs = HookState()
        hs.session_id = "sid"
        hs.fork_task_launcher = launcher
        tools = create_obs_tools(config, lambda: "sid", hook_state=hs, return_tools=True)
        return {tool.name: tool.handler for tool in tools}

    async def test_new_tree_chat_passed_and_fork_forced_false(self, config):
        launcher = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
        tools = self._tools(config, launcher)
        await tools["AgentTask"]({"display_name": "N", "prompt": "p", "new_tree_chat": "procedures"})
        payload = launcher.await_args.args[0]
        assert payload["new_tree_chat"] == "procedures" and payload["fork"] is False

    async def test_new_tree_chat_with_fork_true_refused(self, config):
        launcher = AsyncMock()
        tools = self._tools(config, launcher)
        out = await tools["AgentTask"]({"display_name": "N", "prompt": "p", "new_tree_chat": "x", "fork": True})
        assert out.get("is_error") and "fresh root" in out["content"][0]["text"]
        launcher.assert_not_awaited()

    def test_schema_declares_new_tree_chat(self, config):
        from obs_agent.hooks import HookState
        from obs_agent.tools import create_obs_tools

        tools = create_obs_tools(config, lambda: "sid", hook_state=HookState(), return_tools=True)
        schema = {tool.name: tool.input_schema for tool in tools}["AgentTask"]
        assert "new_tree_chat" in schema["properties"]
