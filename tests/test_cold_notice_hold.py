"""vault-ruoi: AgentTask notices to a cold idle parent are held in its inbox, not delivered.

Unit checks only; the proof is the obs-e2e scenario suite (scripts/e2e_cold_notice).
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from obs_agent import control
from obs_agent.queueing import QueuedMessage
from obs_agent.telegram import TelegramBot

from tests.test_cache_readiness import _jsonl
from tests.test_telegram import _TEST_GAP, _state

TEAM = "2026-08-12-08-05-root"
AGENT = "parent-agent"
NOTICE = "<task-notification><task-id>task-9</task-id><status>idle</status></task-notification>"


def _parent(config, tmp_path, monkeypatch, *, age_seconds, busy=False):
    monkeypatch.setattr("obs_agent.telegram.Path.home", lambda: tmp_path)
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    parent = _state(bot, thread_id=77)
    parent.session_manager.set_session_id("sid-parent")
    parent.busy = busy
    parent.last_bot = MagicMock()
    bot._route_inbox_targets[(TEAM, AGENT)] = parent.route
    bot._route_inbox_target_keys_by_route[parent.route] = (TEAM, AGENT)
    bot._run_and_send = AsyncMock()
    path = _jsonl(tmp_path, model="claude-opus-5-5", age_seconds=age_seconds)
    return bot, parent, path


def _inbox(tmp_path):
    path = tmp_path / ".claude" / "teams" / TEAM / "inboxes" / f"{AGENT}.json"
    return json.loads(path.read_text()) if path.exists() else []


class TestIdleDeliveryHold:
    async def test_cold_idle_parent_notice_is_held_not_delivered(self, config, tmp_path, monkeypatch, caplog):
        bot, parent, path = _parent(config, tmp_path, monkeypatch, age_seconds=3 * 3600)
        parent.hook_state.message_queue.put_nowait(QueuedMessage(text=NOTICE, fork_task_id="task-9"))
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path), caplog.at_level(
            logging.INFO, logger="obs_agent.telegram"
        ):
            await bot._deliver_background_queue(parent)
        bot._run_and_send.assert_not_awaited()
        rows = _inbox(tmp_path)
        assert len(rows) == 1 and rows[0]["wake"] is False and rows[0]["read"] is False
        assert rows[0]["text"] == NOTICE and "held" in rows[0]["summary"]
        assert "[cache_readiness] held agent-task notice" in caplog.text
        assert parent.hook_state.message_queue.empty() and not parent.pending_messages
        # The inbox poller never turns a held row into a wake.
        assert bot._latest_unread_team_inbox_message(team_name=TEAM, agent_name=AGENT) is None

    async def test_warm_parent_notice_is_delivered(self, config, tmp_path, monkeypatch):
        bot, parent, path = _parent(config, tmp_path, monkeypatch, age_seconds=60)
        parent.hook_state.message_queue.put_nowait(QueuedMessage(text=NOTICE, fork_task_id="task-9"))
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            await bot._deliver_background_queue(parent)
        bot._run_and_send.assert_awaited_once()
        assert _inbox(tmp_path) == []

    async def test_cold_parent_with_another_queued_item_is_delivered(self, config, tmp_path, monkeypatch):
        bot, parent, path = _parent(config, tmp_path, monkeypatch, age_seconds=3 * 3600)
        parent.hook_state.message_queue.put_nowait(QueuedMessage(text=NOTICE, fork_task_id="task-9"))
        parent.hook_state.message_queue.put_nowait(QueuedMessage(text="a person wrote this"))
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            await bot._deliver_background_queue(parent)
        bot._run_and_send.assert_awaited_once()
        assert _inbox(tmp_path) == []

    async def test_unknown_inbox_identity_delivers(self, config, tmp_path, monkeypatch):
        bot, parent, path = _parent(config, tmp_path, monkeypatch, age_seconds=3 * 3600)
        bot._route_inbox_target_keys_by_route.pop(parent.route)
        parent.hook_state.message_queue.put_nowait(QueuedMessage(text=NOTICE, fork_task_id="task-9"))
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            await bot._deliver_background_queue(parent)
        bot._run_and_send.assert_awaited_once()

    async def test_next_turn_hint_once(self, config, tmp_path, monkeypatch):
        bot, parent, path = _parent(config, tmp_path, monkeypatch, age_seconds=3 * 3600)
        parent.hook_state.message_queue.put_nowait(QueuedMessage(text=NOTICE, fork_task_id="task-9"))
        with patch("obs_agent.telegram.find_session_jsonl", return_value=path):
            await bot._deliver_background_queue(parent)
        hint = bot._held_inbox_hint(parent)
        assert hint is not None and "1 notice(s)" in hint.text and f"agent={AGENT}" in hint.text
        assert bot._held_inbox_hint(parent) is None

    def test_kill_switch_start_keeps_held_rows_unread(self, config, tmp_path, monkeypatch):
        bot, _, _ = _parent(config, tmp_path, monkeypatch, age_seconds=60)
        inbox = tmp_path / ".claude" / "teams" / TEAM / "inboxes" / f"{AGENT}.json"
        inbox.parent.mkdir(parents=True)
        old = "2020-01-01T00:00:00Z"
        inbox.write_text(json.dumps([
            {"from": "a", "text": "old", "timestamp": old, "read": False},
            {"from": "b", "text": "held", "timestamp": old, "read": False, "wake": False},
        ]))
        bot._outage_inbox_floor = None
        bot._mark_restored_team_inboxes_read({(TEAM, AGENT)})
        rows = json.loads(inbox.read_text())
        assert rows[0]["read"] is True and rows[1]["read"] is False


class TestHoldIfCold:
    async def test_hold_if_cold_writes_no_wake_row_and_skips_notifier(self, config, tmp_path, monkeypatch):
        from obs_agent.hooks import HookState
        from obs_agent.tools import create_obs_tools

        captured = {}

        def _fake_server(name, tools):
            captured["tools"] = tools
            return {"type": "fake-server", "tools": tools}

        monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", _fake_server)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        hook_state = HookState()

        async def validator(payload):
            return {"deliverable": False, "cold": True, "reason": "cold-cache guard: test"}

        hook_state.inbox_recipient_validator = validator
        hook_state.inbox_message_notifier = AsyncMock()
        create_obs_tools(config, lambda: "sid-1", hook_state=hook_state)
        send = next(t.handler for t in captured["tools"] if t.name == "SendInboxMessage")
        inbox = tmp_path / ".claude" / "teams" / "t" / "inboxes" / "x.json"
        refused = await send({"team_name": "t", "recipient": "x", "content": "hello"})
        assert refused["tool_use_result"]["outcome"] == "cold_blocked" and not inbox.exists()
        held = await send({"team_name": "t", "recipient": "x", "content": "hello", "hold_if_cold": True})
        assert held["tool_use_result"]["outcome"] == "held" and not held.get("is_error")
        rows = json.loads(inbox.read_text())
        assert rows[-1]["wake"] is False and rows[-1]["read"] is False
        hook_state.inbox_message_notifier.assert_not_awaited()
        schema = next(t for t in captured["tools"] if t.name == "SendInboxMessage").input_schema
        assert "hold_if_cold" not in schema["properties"]

    def test_control_audit_records_held(self):
        assert control._audit_outcome({"ok": True, "result": {"outcome": "held"}}) == "held"
        assert control._audit_outcome({"ok": True, "result": {"outcome": "reached"}}) == "ok"
