"""External control channel (bead vault-h667): auth, validation and agent-tool reuse."""
from __future__ import annotations

import asyncio
import json
import os
import socket
from unittest.mock import AsyncMock, patch

import pytest

from obs_agent import control
from obs_agent.telegram import TelegramBot, TelegramRoute
from tests.test_cache_readiness import _jsonl
from tests.test_telegram import _TEST_GAP, _state

TEAM = "2026-08-12-08-05-root"


@pytest.fixture
def ctl_dir(tmp_path, monkeypatch):
    # AF_UNIX paths are limited to ~107 bytes: use a short dir.
    import tempfile
    d = tempfile.mkdtemp(prefix="obsctl-", dir="/tmp")
    monkeypatch.setenv("OBS_CONTROL_DIR", d)
    yield d


async def _with_server(dispatch, fn):
    server = control.ControlServer(dispatch)
    await server.start()
    try:
        return await fn()
    finally:
        await server.close()


async def _client(*args, **kw):
    return await asyncio.to_thread(control.send_request, *args, **kw)


class TestValidation:
    @pytest.mark.parametrize("req,why", [
        ([], "JSON object"),
        ({"op": "rm", "caller": "x"}, "unknown op"),
        ({"op": "ping"}, "caller is required"),
        ({"op": "ping", "caller": "Bad Name!"}, "caller is required"),
        ({"op": "message", "caller": "cron", "args": {"team_name": TEAM}}, "recipient, content"),
        ({"op": "launch", "caller": "cron", "args": {"parent_team": TEAM, "parent_agent": "a", "display_name": "x"}},
         "prompt or prompt_file"),
        ({"op": "new_tree", "caller": "cron", "args": {"title": "t"}}, "prompt"),
    ])
    def test_rejects(self, req, why):
        assert why in control.validate_request(req)

    def test_accepts(self):
        assert control.validate_request({"op": "message", "caller": "error-monitor",
                                         "args": {"team_name": TEAM, "recipient": "a", "content": "hi"}}) is None


class TestServerAuthAndAudit:
    async def test_socket_and_token_are_private(self, ctl_dir):
        async def check():
            assert oct(os.stat(control.socket_path()).st_mode & 0o777) == "0o600"
            assert oct(os.stat(control.token_path()).st_mode & 0o777) == "0o600"
            assert oct(os.stat(ctl_dir).st_mode & 0o777) == "0o700"
        await _with_server(AsyncMock(), check)

    async def test_ping_and_dispatch(self, ctl_dir):
        dispatch = AsyncMock(return_value={"ok": True, "text": "sent"})

        async def run():
            code, resp = await _client("ping", "cron", {})
            assert code == 0 and resp["pong"]
            code, resp = await _client("message", "cron", {"team_name": TEAM, "recipient": "a", "content": "x"})
            assert code == 0 and resp["text"] == "sent"
        await _with_server(dispatch, run)
        dispatch.assert_awaited_once_with("message", "cron", {"team_name": TEAM, "recipient": "a", "content": "x"})
        audit = [json.loads(l) for l in control.audit_path().read_text().splitlines()]
        assert [a["op"] for a in audit] == ["ping", "message"] and audit[1]["outcome"] == "ok"

    async def test_wrong_token_refused_and_audited(self, ctl_dir):
        dispatch = AsyncMock()

        async def run():
            def raw():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(str(control.socket_path()))
                    s.sendall(b'{"op":"ping","caller":"intruder","token":"nope"}\n')
                    return json.loads(s.recv(65536))
            return await asyncio.to_thread(raw)
        resp = await _with_server(dispatch, run)
        assert resp["ok"] is False and resp["auth"] is False and "token" in resp["error"]
        dispatch.assert_not_awaited()
        assert json.loads(control.audit_path().read_text().splitlines()[-1])["reason"] == "token"

    async def test_other_uid_refused(self, ctl_dir, monkeypatch):
        monkeypatch.setattr(control, "_peer_uid", lambda writer: os.getuid() + 1)
        dispatch = AsyncMock()

        async def run():
            return await _client("ping", "cron", {})
        code, resp = await _with_server(dispatch, run)
        assert code == 2 and "not the OBS user" in resp["error"]
        dispatch.assert_not_awaited()

    async def test_idempotency_key_replays(self, ctl_dir):
        dispatch = AsyncMock(return_value={"ok": True, "text": "launched"})

        async def run():
            args = {"title": "t", "prompt": "p"}
            first = await _client("new_tree", "cron", args, idempotency_key="k1")
            second = await _client("new_tree", "cron", args, idempotency_key="k1")
            return first, second
        first, second = await _with_server(dispatch, run)
        assert first[1]["text"] == second[1]["text"] == "launched" and second[1]["replayed"]
        assert dispatch.await_count == 1

    def test_client_without_running_bot_exits_2(self, ctl_dir):
        code, resp = control.send_request("ping", "cron", {})
        assert code == 2 and "token" in resp["error"]


class TestBotDispatchReusesAgentTools:
    def _bot(self, config):
        return TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)

    async def test_message_goes_through_cold_guard(self, config, tmp_path):
        bot = self._bot(config)
        target = _state(bot, thread_id=55)
        target.session_manager.set_session_id("sid-target")
        bot._route_inbox_targets[(TEAM, "target-agent")] = target.route
        bot._route_inbox_target_keys_by_route[target.route] = (TEAM, "target-agent")
        path = _jsonl(tmp_path, model="claude-opus-5-5", age_seconds=3 * 3600)
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            result = await bot._control_dispatch("message", "error-monitor", {
                "team_name": TEAM, "recipient": "target-agent", "content": "boom"})
        assert result["ok"] is False and str(path) in result["error"]
        assert result["result"]["outcome"] == "cold_blocked"

    async def test_message_warm_target_delivered_as_external_sender(self, config, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        bot = self._bot(config)
        target = _state(bot, thread_id=56)
        target.session_manager.set_session_id("sid-warm")
        bot._route_inbox_targets[(TEAM, "warm-agent")] = target.route
        bot._route_inbox_target_keys_by_route[target.route] = (TEAM, "warm-agent")
        path = _jsonl(tmp_path, model="claude-opus-5-5", age_seconds=60)
        bot._handle_inbox_message_notification = AsyncMock(return_value={"delivered": True})
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path), \
                patch("obs_agent.tools.Path.home", return_value=tmp_path):
            result = await bot._control_dispatch("message", "error-monitor", {
                "team_name": TEAM, "recipient": "warm-agent", "content": "boom"})
        assert result["ok"] is True
        inbox = json.loads((tmp_path / ".claude" / "teams" / TEAM / "inboxes" / "warm-agent.json").read_text())
        assert inbox[-1]["from"] == "external:error-monitor" and inbox[-1]["text"] == "boom"
        payload = bot._handle_inbox_message_notification.await_args.kwargs["payload"]
        assert payload["sender"] == "external:error-monitor"

    async def test_launch_unknown_parent(self, config):
        bot = self._bot(config)
        result = await bot._control_dispatch("launch", "cron", {
            "parent_team": TEAM, "parent_agent": "ghost", "display_name": "x", "prompt": "p"})
        assert result["ok"] is False and "no current route binding" in result["error"]

    def _parent(self, bot):
        parent = _state(bot, thread_id=57)
        parent.session_manager.set_session_id("sid-parent")
        bot._route_inbox_targets[(TEAM, "parent-agent")] = parent.route
        bot._route_inbox_target_keys_by_route[parent.route] = (TEAM, "parent-agent")
        return parent

    async def test_launch_fork_of_cold_parent_refused(self, config, tmp_path):
        bot = self._bot(config)
        self._parent(bot)
        bot._launch_fork_task = AsyncMock()
        path = _jsonl(tmp_path, model="claude-opus-5-5", age_seconds=3 * 3600)
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            result = await bot._control_dispatch("launch", "cron", {
                "parent_team": TEAM, "parent_agent": "parent-agent", "display_name": "x", "prompt": "p", "fork": True})
        assert result["ok"] is False and result["cold"] and str(path) in result["error"]
        bot._launch_fork_task.assert_not_awaited()

    async def test_launch_runs_agenttask_as_parent(self, config, tmp_path):
        bot = self._bot(config)
        parent = self._parent(bot)
        bot._launch_fork_task = AsyncMock(return_value={"content": [{"type": "text", "text": "AgentTask launched."}]})
        path = _jsonl(tmp_path, model="claude-opus-5-5", age_seconds=60)
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            result = await bot._control_dispatch("launch", "cron", {
                "parent_team": TEAM, "parent_agent": "parent-agent", "display_name": "Fixer",
                "prompt": "fix it", "fork": False})
        assert result["ok"] is True and "AgentTask launched." in result["text"]
        kwargs = bot._launch_fork_task.await_args.kwargs
        assert kwargs["route"] == parent.route
        assert kwargs["args"]["prompt"].startswith("[external trigger: cron]\nfix it")
        assert kwargs["args"]["fork"] is False and kwargs["args"]["session_id"] == "sid-parent"

    async def test_launch_fork_of_warm_parent_runs_agenttask_fork(self, config, tmp_path):
        """`obs-agentctl launch --fork` goes through the AgentTask handler as a fork of the parent."""
        bot = self._bot(config)
        parent = self._parent(bot)
        bot._launch_fork_task = AsyncMock(return_value={"content": [{"type": "text", "text": "AgentTask launched."}]})
        path = _jsonl(tmp_path, model="claude-opus-5-5", age_seconds=60)
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            result = await bot._control_dispatch("launch", "cron", {
                "parent_team": TEAM, "parent_agent": "parent-agent", "display_name": "Forker",
                "prompt": "look", "fork": True})
        assert result["ok"] is True and "AgentTask launched." in result["text"]
        kwargs = bot._launch_fork_task.await_args.kwargs
        assert kwargs["route"] == parent.route
        args = kwargs["args"]
        assert args["fork"] is True and args["session_id"] == "sid-parent"
        assert args["model"] is None and args["session_source"] is None and args["allow_cold_wake"] is False
        assert args["prompt"].startswith("[external trigger: cron]\nlook")

    def test_cli_launch_defaults_to_fork(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(control, "send_request", lambda op, caller, args, **kw: (sent.update(op=op, args=args) or (0, {"ok": True, "text": "x"})))
        assert control.main(["--as", "cron", "launch", "--parent-team", TEAM, "--parent", "p", "--name", "n", "--prompt", "q"]) == 0
        assert sent["op"] == "launch" and sent["args"]["fork"] is True
        assert control.main(["--as", "cron", "launch", "--parent-team", TEAM, "--parent", "p", "--name", "n", "--prompt", "q", "--fresh"]) == 0
        assert sent["args"]["fork"] is False

    async def test_new_tree_requires_chat_when_ambiguous(self, config):
        bot = self._bot(config)
        bot._primary_bot = object()
        _state(bot, thread_id=1)
        bot._states_by_route[TelegramRoute(chat_id=111, thread_id=5)] = bot._states_by_route[
            next(iter(bot._states_by_route))]
        result = await bot._control_dispatch("new_tree", "cron", {"title": "t", "prompt": "p"})
        assert result["ok"] is False and "--chat-id" in result["error"]
