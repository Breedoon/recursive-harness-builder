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


class TestCliFlagPlacementAndAuditOutcome:
    """ops-1.11.5 / ops-1.11.13."""

    def _capture(self, monkeypatch):
        seen = {}

        def fake_send(op, caller, args, *, idempotency_key=None, timeout=120.0):
            seen.update(op=op, caller=caller, args=args, key=idempotency_key)
            return 0, {"ok": True, "text": "pong"}
        monkeypatch.setattr(control, "send_request", fake_send)
        return seen

    def test_global_flags_after_the_op(self, monkeypatch, capsys):
        seen = self._capture(monkeypatch)
        assert control.main(["ping", "--as", "cron", "--json", "--idempotency-key", "k1"]) == 0
        assert seen == {"op": "ping", "caller": "cron", "args": {}, "key": "k1"}
        assert '"ok": true' in capsys.readouterr().out

    def test_global_flags_before_the_op_still_work_and_are_not_overwritten(self, monkeypatch, capsys):
        seen = self._capture(monkeypatch)
        assert control.main(["--as", "cron", "--json", "--idempotency-key", "k2", "message",
                             "--team", TEAM, "--to", "x", "--text", "hi"]) == 0
        assert seen["caller"] == "cron" and seen["key"] == "k2"
        assert seen["args"] == {"team_name": TEAM, "recipient": "x", "content": "hi", "allow_cold_wake": False}
        assert '"ok": true' in capsys.readouterr().out

    def test_missing_caller_is_a_clear_usage_error(self, monkeypatch, capsys):
        self._capture(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            control.main(["ping"])
        assert exc.value.code == 2
        assert "--as NAME is required" in capsys.readouterr().err

    def test_audit_outcome_labels(self):
        assert control._audit_outcome({"ok": True}) == "ok"
        assert control._audit_outcome({"ok": False, "cold": True, "error": "cold-cache guard"}) == "cold_blocked"
        assert control._audit_outcome({"ok": False, "result": {"outcome": "cold_blocked"}}) == "cold_blocked"
        assert control._audit_outcome({"ok": False, "result": {"outcome": "underdelivered"}}) == "failed"
        assert control._audit_outcome({"ok": False, "error": "boom"}) == "failed"


class TestScopedStop:
    def _setup(self, config, monkeypatch, tmp_path):
        from types import SimpleNamespace
        from obs_agent.telegram import _ForkTaskRecord
        monkeypatch.setenv("OBS_CONTROL_DIR", str(tmp_path / "control"))
        monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0")
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        parent = TestBotDispatchReusesAgentTools()._parent(bot)
        parent.session_manager.set_sdk_env_overrides({"CLAUDE_CODE_TEAM_NAME": TEAM})
        child = _state(bot, thread_id=58)
        child.session_manager.set_session_id("sid-child")
        child.session_manager._client = SimpleNamespace(_transport=SimpleNamespace(_process=SimpleNamespace(pid=123456)))
        child.busy = True
        record = _ForkTaskRecord(task_id="exact-task", parent_route=parent.route,
            parent_session_id_at_launch="sid-parent", parent_source_uuid="parent-head",
            child_route=child.route, child_session_id="sid-child", prompt="check", description="item1",
            team_name=TEAM, agent_name="0123456789-check-child", is_fork=False,
            launch_child_message_id=901, launch_parent_message_id=900)
        bot._fork_tasks_by_id[record.task_id] = record
        bot._fork_task_by_child_route[child.route] = record.task_id
        bot._team_worker_records[(TEAM, record.agent_name)] = record.task_id
        monkeypatch.setattr("obs_agent.telegram._maint.capture_process_tree", lambda pid: {123456: 100, 123457: 101})
        monkeypatch.setattr("obs_agent.telegram._maint.process_identity_matches", lambda pid, start: True)
        bot._interrupt_live_client = AsyncMock()
        args = dict(parent_team=TEAM, parent_agent="parent-agent", team_name=TEAM,
                    agent_name="0123456789-check-child", expected_task_id="exact-task", expected_launch_parent_message_id=900)
        return bot, child, record, args

    async def test_native_stop_once_then_cleanup_not_status_alone(self, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        first = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert first["ok"] and not first["result"]["cleanup_complete"], first
        assert record.terminal_request == "stopped"
        assert first["result"]["owned"] == {123456: 100, 123457: 101}
        bot._interrupt_live_client.assert_awaited_once()
        child.busy = False
        record.status = "stopped"
        second = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not second["result"]["cleanup_complete"]  # retained tool identity still lives
        assert second["result"]["survivors"] == [123457]  # idle CLI itself is not working
        monkeypatch.setattr("obs_agent.telegram._maint.process_identity_matches", lambda pid, start: False)
        third = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert third["result"]["cleanup_complete"]
        assert bot._interrupt_live_client.await_count == 1  # no reset of the native grace

    @pytest.mark.parametrize("change", ["wrong-team", "wrong-parent", "new-generation", "inbox-new-turn"])
    async def test_stale_timer_does_not_stop_unowned_generation(self, change, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        if change == "wrong-team":
            args["team_name"] = "other-team"
        elif change == "wrong-parent":
            record.parent_route = TelegramRoute(chat_id=1, thread_id=999)
        elif change == "new-generation":
            record.launch_parent_message_id = 902
        else:
            record.parent_route = child.route
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not result["ok"]
        bot._interrupt_live_client.assert_not_awaited()
        assert record.terminal_request is None

    @pytest.mark.parametrize("stop_first", [False, True])
    async def test_runs_without_parent_notices_get_distinct_generations(
        self, stop_first, config, monkeypatch, tmp_path,
    ):
        # obs-54 V1 re-check: two explicit runs of one task whose parent notices both
        # failed shared generation None, so a stale timer for the first could stop the
        # second, and a second stop reused the first's receipt.
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        record.launch_parent_message_id = None  # a legacy run without a parent message
        args["expected_launch_parent_message_id"] = "None"
        if stop_first:
            assert (await bot._control_dispatch("stop", "rounds-watchdog", dict(args)))["ok"]
            assert bot._interrupt_live_client.await_count == 1
        else:
            record.status = "completed"
            record.idle_ready = True
        child.busy = False
        parent = bot._control_agent_state(TEAM, "parent-agent")
        parent.last_bot = object()
        monkeypatch.setattr(bot, "_send_task_launch_notice", AsyncMock(return_value=[]))
        monkeypatch.setattr(bot, "_schedule_fork_task", AsyncMock())
        resumed = await bot._resume_fork_task(
            route=parent.route, state=parent, task_id=record.task_id,
            args={"prompt": "a different explicit run", "task_tool_name": "AgentTask"},
        )
        assert not resumed.get("is_error"), resumed
        generation = record.launch_parent_message_id
        assert isinstance(generation, int) and generation < 0  # synthetic, not None
        # Durable before the run starts: a restart restores the generation stops pin.
        persisted = {row.task_id: row for row in bot._state_store.load_snapshot().task_handle_states}
        assert persisted[record.task_id].launch_parent_message_id == generation
        bot._clear_stop_latch(child, "test_explicit_resume")
        child.busy = True
        before = bot._interrupt_live_client.await_count
        stale = await bot._control_dispatch("stop", "rounds-watchdog", dict(args))  # the old "None" timer
        assert not stale["ok"] and "generation mismatch" in str(stale)
        assert bot._interrupt_live_client.await_count == before
        current = await bot._control_dispatch(
            "stop", "rounds-watchdog", {**args, "expected_launch_parent_message_id": str(generation)},
        )
        assert current["ok"], current
        assert record.terminal_request == "stopped"
        assert bot._interrupt_live_client.await_count == before + 1

    def test_synthetic_generations_are_distinct_negative_and_never_reply_targets(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        generations = [bot._run_generation(None) for _ in range(1000)]
        assert len(set(generations)) == 1000 and all(g < 0 for g in generations)
        assert generations == sorted(generations, reverse=True)
        assert bot._run_generation(902) == 902
        assert bot._reply_anchor(generations[0]) is None and bot._reply_anchor(902) == 902
        assert bot._reply_anchor(None) is None

    @pytest.mark.parametrize("spelling", ["None", None])
    async def test_launch_without_a_parent_message_is_stoppable_by_its_none_generation(
        self, spelling, config, monkeypatch, tmp_path,
    ):
        # A record from before synthetic generations (or another path) can still
        # hold None; obs-agentctl/rounds pass str(None).
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        record.launch_parent_message_id = None
        stale = await bot._control_dispatch("stop", "rounds-watchdog", args)  # integer 900
        assert not stale["ok"] and "generation mismatch" in str(stale)
        args["expected_launch_parent_message_id"] = spelling
        assert control.validate_request({"op": "stop", "caller": "rounds-watchdog", "args": args}) is None
        absent = {key: value for key, value in args.items() if key != "expected_launch_parent_message_id"}
        assert "needs expected_launch_parent_message_id" in control.validate_request(
            {"op": "stop", "caller": "rounds-watchdog", "args": absent})
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert result["ok"], result
        assert record.terminal_request == "stopped"

    @pytest.mark.parametrize("parent_sid", ["sid-parent", None])
    async def test_maintenance_session_change_does_not_reset_generation(self, config, monkeypatch, tmp_path, parent_sid):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        bot._control_agent_state(TEAM, "parent-agent").session_manager.set_session_id(parent_sid)
        child.session_manager.set_session_id("maintenance-new-sid")
        record.child_session_id = "maintenance-new-sid"
        record.launch_child_message_id = 903
        record.emit_parent_callback = False  # restored handles default false; not a generation token
        assert (await bot._control_dispatch("stop", "rounds-watchdog", args))["ok"]
        assert record.terminal_request == "stopped"

    async def test_restart_after_intent_before_stop_merges_resumed_cli_ownership(self, config, monkeypatch, tmp_path):
        old_bot, old_child, old_record, args = self._setup(config, monkeypatch, tmp_path)
        # First instance durably captures OLD ownership but crashes before any native stop.
        await old_bot._control_dispatch("stop", "rounds-watchdog", {**args, "observe_only": True})
        old_bot._interrupt_live_client.assert_not_awaited()
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        record.created_at = old_record.created_at
        child.session_manager._client._transport._process.pid = 234567
        monkeypatch.setattr("obs_agent.telegram._maint.capture_process_tree", lambda pid: {234567: 200, 234568: 201})
        monkeypatch.setattr("obs_agent.telegram._maint.process_identity_matches", lambda pid, start: pid == 234568)
        async def stop_new_route(state):
            child.busy = False
            record.status = "stopped"
            record.terminal_request = "stopped"
        bot._interrupt_live_client.side_effect = stop_new_route
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not result["result"]["cleanup_complete"], result
        assert result["result"]["survivors"] == [234568]
        owned = {int(pid): start for pid, start in result["result"]["owned"].items()}
        assert owned == {123456: 100, 123457: 101, 234567: 200, 234568: 201}
        bot._interrupt_live_client.assert_awaited_once()

    async def test_retry_merge_preserves_unknown_capture_and_pid_reuse_history(self, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        await bot._control_dispatch("stop", "rounds-watchdog", {**args, "observe_only": True})
        path = next(control.control_dir().glob("stop-*.json"))
        receipt = json.loads(path.read_text())
        receipt.update(capture_known=False, capture_error="prior-unknown")
        path.write_text(json.dumps(receipt))
        monkeypatch.setattr("obs_agent.telegram._maint.capture_process_tree", lambda pid: {123456: 200, 234568: 201})
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not result["result"]["capture_known"]
        assert result["result"]["capture_error"] == "prior-unknown"
        assert [123456, 100] in result["result"]["retired_owned_identities"]
        assert len(result["result"]["ownership_snapshots"]) == 2
        assert not result["result"]["cleanup_complete"]

    async def test_generation_is_rechecked_at_actual_async_stop_dispatch(self, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        create = asyncio.create_task
        def changed_before_dispatch(coro, *a, **kw):
            record.launch_parent_message_id = 902
            return create(coro, *a, **kw)
        monkeypatch.setattr(asyncio, "create_task", changed_before_dispatch)
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not result["ok"] and "generation changed" in result["error"]
        bot._interrupt_live_client.assert_not_awaited()
        assert record.terminal_request is None

    async def test_lost_ack_restart_keeps_owned_identities_and_does_not_relatch(self, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        await bot._control_dispatch("stop", "rounds-watchdog", args)
        path = next(control.control_dir().glob("stop-*.json"))
        receipt = json.loads(path.read_text())
        receipt["stop_requested_at"] = None  # crash after native latch, before durable ack
        path.write_text(json.dumps(receipt))
        bot.__dict__.pop("_control_stop_locks")
        monkeypatch.setattr("obs_agent.telegram._maint.capture_process_tree", lambda pid: {})
        child.busy = False
        record.status = "stopped"
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not result["result"]["cleanup_complete"]
        assert result["result"]["survivors"] == [123457]
        assert bot._interrupt_live_client.await_count == 1
        assert result["result"]["request_timestamp_basis"] == "native_latch"

    async def test_concurrent_stop_calls_share_one_native_latch(self, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        async def interrupt(state):
            await asyncio.sleep(0)
        bot._interrupt_live_client.side_effect = interrupt
        result = await asyncio.gather(*(bot._control_dispatch("stop", "rounds-watchdog", args) for _ in range(2)))
        assert all(r["ok"] for r in result)
        assert bot._interrupt_live_client.await_count == 1

    @pytest.mark.parametrize("pid", [0, 1, True, "123", os.getpid()])
    async def test_invalid_or_self_pid_never_becomes_capture_authority(self, pid, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        child.session_manager._client._transport._process.pid = pid
        capture = __import__("unittest.mock", fromlist=["Mock"]).Mock()
        monkeypatch.setattr("obs_agent.telegram._maint.capture_process_tree", capture)
        first = await bot._control_dispatch("stop", "rounds-watchdog", args)
        capture.assert_not_called()
        child.busy = False
        record.status = "stopped"
        second = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not second["result"]["cleanup_complete"]  # missing capture is unknown, never empty-tree proof
        assert not first["result"]["capture_known"]

    @pytest.mark.parametrize("native_status", ["completed", "stopped"])
    async def test_observation_requires_runtime_and_tool_cleanup_not_native_flag(self, native_status, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        record.status = native_status
        record.terminal_request = "stopped" if native_status == "stopped" else None
        args["observe_only"] = True
        first = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not first["result"]["cleanup_complete"] and first["result"]["route_active"]
        bot._interrupt_live_client.assert_not_awaited()
        child.busy = False
        assert not (await bot._control_dispatch("stop", "rounds-watchdog", args))["result"]["cleanup_complete"]
        monkeypatch.setattr("obs_agent.telegram._maint.process_identity_matches", lambda *a: False)
        assert (await bot._control_dispatch("stop", "rounds-watchdog", args))["result"]["cleanup_complete"]
        bot._interrupt_live_client.assert_not_awaited()

    async def test_external_stop_without_retained_capture_is_unknown_not_empty_tree_proof(self, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        record.status = "stopped"
        record.terminal_request = "stopped"
        child.busy = False
        child.session_manager._client = None
        args["observe_only"] = True
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert not result["result"]["capture_known"] and not result["result"]["cleanup_complete"]

    @pytest.mark.parametrize("error", [OSError("process exited"), ValueError("process identity vanished")])
    async def test_capture_exit_race_still_stops_logical_child_without_claiming_cleanup(self, error, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        def vanished(pid):
            raise error
        monkeypatch.setattr("obs_agent.telegram._maint.capture_process_tree", vanished)
        result = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert result["ok"] and record.terminal_request == "stopped"
        assert result["result"]["capture_error"] == type(error).__name__
        assert not result["result"]["capture_known"] and not result["result"]["cleanup_complete"]
        bot._interrupt_live_client.assert_awaited_once()

    async def test_slow_native_interrupt_cannot_monopolize_deadline_watchdog(self, config, monkeypatch, tmp_path):
        bot, child, record, args = self._setup(config, monkeypatch, tmp_path)
        release = asyncio.Event()
        async def interrupt(state):
            await release.wait()
        bot._interrupt_live_client.side_effect = interrupt
        first = await bot._control_dispatch("stop", "rounds-watchdog", args)
        assert first["result"]["request_timestamp_basis"] == "native_latch"
        assert not first["result"]["cleanup_complete"]
        callback = next(iter(bot._control_stop_callbacks.values()))
        assert not callback.done()
        release.set()
        await callback
        assert record.terminal_request == "stopped"
        assert bot._interrupt_live_client.await_count == 1

    async def test_stop_observation_bypasses_generic_cached_ack(self, ctl_dir):
        args = dict(parent_team=TEAM, parent_agent="p", team_name=TEAM, agent_name="c",
                    expected_task_id="task", expected_launch_parent_message_id=1)
        dispatch = AsyncMock(side_effect=[{"ok": True, "result": {"cleanup_complete": False}},
                                         {"ok": True, "result": {"cleanup_complete": True}}])
        async def run():
            first = await _client("stop", "rounds-watchdog", args, idempotency_key="same-stop")
            second = await _client("stop", "rounds-watchdog", args, idempotency_key="same-stop")
            return first, second
        first, second = await _with_server(dispatch, run)
        assert not first[1]["result"]["cleanup_complete"] and second[1]["result"]["cleanup_complete"]
        assert dispatch.await_count == 2

    def test_cli_stop_preserves_generation(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(control, "send_request", lambda op, caller, args, **kw: (seen.update(op=op, args=args) or (0, {"ok": True})))
        assert control.main(["--as", "rounds-watchdog", "stop", "--parent-team", TEAM, "--parent", "p",
                             "--team", TEAM, "--agent", "c", "--expected-task-id", "task",
                             "--expected-launch-parent-message-id", "900"]) == 0
        assert seen["op"] == "stop" and seen["args"]["expected_launch_parent_message_id"] == 900
