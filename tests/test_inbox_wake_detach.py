"""A1 (vault-aay.1) — the route-target inbox wake must not block the sender.

``SendInboxMessage`` is an in-process SDK MCP tool.  On the route-target path
(recipient has a route binding but no live worker record: trunk/root topic
agents, and any child whose fork task already ended) the delivery notifier used
to ``await`` ``_start_idle_route_inbox_wake`` -> ``_run_and_send``, which drives
the RECIPIENT'S ENTIRE TURN.  The sender's tool call could therefore not return
until the recipient finished — measured in production at up to 56 minutes
(20,222 sends over 7 days; 573 over 60 s, 58 over 5 min; 508/573 and 58/58 of
those on this exact path).

Every fix assertion here is paired with a POSITIVE CONTROL that first
demonstrates the block against the pre-fix call shape, so a passing test cannot
be satisfied by code that never had the defect.
"""

import asyncio
import logging

from unittest.mock import AsyncMock, MagicMock, patch

from obs_agent.telegram import TelegramBot, TelegramRoute, _RunOutcome

_TEST_GAP = 0.05
_CHAT_ID = -1009876543


def _make_bot(config) -> TelegramBot:
    return TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)


def _make_route_target(bot: TelegramBot, *, thread_id: int, team_name: str, agent_name: str):
    """Register a route-bound agent that has NO live worker record.

    This is exactly the population that takes the (formerly blocking) route-
    target path in ``_handle_inbox_message_notification``: a route binding
    exists, but ``_resolve_team_worker_record`` returns ``None``.
    """
    route = TelegramRoute(chat_id=_CHAT_ID, thread_id=thread_id)
    state = bot._get_state(route, topic_title=f"General - {agent_name}")
    assert state is not None
    fake_bot = MagicMock()
    sent = MagicMock()
    sent.message_id = 1000 + thread_id
    fake_bot.send_message = AsyncMock(return_value=sent)
    state.last_bot = fake_bot
    state.session_manager.set_session_id(f"sid-{agent_name}")
    bot._route_inbox_targets[(team_name, agent_name)] = route
    return state


async def _notify(bot: TelegramBot, *, team_name: str, recipient: str):
    """Drive the exact coroutine ``SendInboxMessage`` awaits at ``tools.py:1476``."""
    return await bot._handle_inbox_message_notification(
        sender_route=TelegramRoute(chat_id=_CHAT_ID, thread_id=999),
        payload={
            "team_name": team_name,
            "recipient": recipient,
            "sender": "sender-x",
            "summary": "handoff",
            "content": "please process item 7",
        },
    )


class _SlowTurn:
    """A ``_run_and_send`` stand-in whose completion the test controls."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.entered = 0
        self.exited = 0

    async def __call__(self, *, state, user_text, bot, **kwargs):
        self.entered += 1
        state.busy = True
        self.started.set()
        try:
            await self.release.wait()
        finally:
            state.busy = False
            self.exited += 1
        return _RunOutcome(assistant_text="OK")


async def _drain_detached(bot: TelegramBot, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while bot._detached_wake_tasks and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)


class TestA1PositiveControl:
    async def test_inline_wake_blocks_the_notifier(self, config):
        """POSITIVE CONTROL — reproduce the defect against the pre-fix call shape.

        ``_maybe_wake_route_inbox_target`` is replaced with its original body
        (lock, recheck, inline ``await`` of the wake).  The notifier must NOT
        complete while the recipient's turn is still running.  If this stops
        blocking, every fix assertion in this file proves nothing.
        """
        bot = _make_bot(config)
        _make_route_target(bot, thread_id=101, team_name="t", agent_name="recipient")
        slow = _SlowTurn()

        async def _pre_fix_maybe_wake(*, state, team_name, agent_name, sender, summary, content):
            if state.busy or state.hook_state.pause_queue_delivery:
                return False
            if bot._chat_pending_ops.get(state.route.chat_id, 0) > 0:
                return False
            lock = bot._get_route_lock(state.route)
            if lock.locked():
                return False
            async with lock:
                if state.busy or state.hook_state.pause_queue_delivery:
                    return False
                await bot._start_idle_route_inbox_wake(
                    state=state,
                    team_name=team_name,
                    agent_name=agent_name,
                    sender=sender,
                    summary=summary,
                    content=content,
                )
            return True

        with patch.object(bot, "_run_and_send", slow), patch.object(
            bot, "_maybe_wake_route_inbox_target", _pre_fix_maybe_wake
        ):
            notify = asyncio.create_task(_notify(bot, team_name="t", recipient="recipient"))
            await asyncio.wait_for(slow.started.wait(), timeout=5)
            await asyncio.sleep(0.2)
            assert not notify.done(), (
                "POSITIVE CONTROL FAILED: the inline wake did not block the notifier"
            )
            assert slow.exited == 0
            slow.release.set()
            result = await asyncio.wait_for(notify, timeout=5)

        assert result == {"delivered": True}
        assert slow.entered == 1
        await bot.shutdown()


class TestA1Fix:
    async def test_notifier_returns_while_recipient_turn_still_running(self, config):
        """The sender returns promptly; the recipient's turn continues detached."""
        bot = _make_bot(config)
        state = _make_route_target(bot, thread_id=102, team_name="t", agent_name="recipient")
        slow = _SlowTurn()

        with patch.object(bot, "_run_and_send", slow):
            result = await asyncio.wait_for(
                _notify(bot, team_name="t", recipient="recipient"), timeout=5
            )
            await asyncio.wait_for(slow.started.wait(), timeout=5)
            assert slow.entered == 1
            assert slow.exited == 0, "the notifier waited for the recipient's turn"
            assert state.busy is True

            slow.release.set()
            await _drain_detached(bot)
            assert slow.exited == 1

        assert result == {"delivered": True}
        await bot.shutdown()

    async def test_detached_wake_is_strongly_referenced_then_released(self, config):
        """A bare fire-and-forget task can be garbage-collected mid-flight."""
        bot = _make_bot(config)
        _make_route_target(bot, thread_id=103, team_name="t", agent_name="recipient")
        slow = _SlowTurn()

        with patch.object(bot, "_run_and_send", slow):
            await _notify(bot, team_name="t", recipient="recipient")
            await asyncio.wait_for(slow.started.wait(), timeout=5)
            assert len(bot._detached_wake_tasks) == 1, (
                "the detached wake must be held in a strong-reference set"
            )
            slow.release.set()
            await _drain_detached(bot)
            assert bot._detached_wake_tasks == set(), (
                "the done-callback must drop the reference once the wake completes"
            )

        await bot.shutdown()

    async def test_exception_inside_detached_wake_is_logged_not_swallowed(self, config, caplog):
        """Converting a blocking bug into a silently-disappearing one is a defect."""
        bot = _make_bot(config)
        _make_route_target(bot, thread_id=104, team_name="t", agent_name="recipient")

        async def _explode(**kwargs):
            raise RuntimeError("wake exploded")

        with caplog.at_level(logging.WARNING, logger="obs_agent.telegram"):
            with patch.object(bot, "_start_idle_route_inbox_wake", _explode):
                result = await asyncio.wait_for(
                    _notify(bot, team_name="t", recipient="recipient"), timeout=5
                )
                await _drain_detached(bot)

        assert result == {"delivered": True}
        blob = "\n".join(
            (record.getMessage() + "\n" + (record.exc_text or "")) for record in caplog.records
        )
        assert "wake exploded" in blob, (
            f"detached-wake exception was swallowed; captured log was: {blob!r}"
        )
        await bot.shutdown()

    async def test_positive_control_bare_create_task_swallows_the_exception(self, config, caplog):
        """POSITIVE CONTROL for the logging assertion above.

        A bare ``asyncio.create_task`` with no done-callback produces no WARNING
        record, so the previous test is detecting the callback, not asyncio's
        own behaviour.
        """
        async def _explode():
            raise RuntimeError("bare task exploded")

        with caplog.at_level(logging.WARNING, logger="obs_agent.telegram"):
            task = asyncio.create_task(_explode())
            await asyncio.sleep(0.05)
            assert task.done() and task.exception() is not None
        assert not any(
            "bare task exploded" in record.getMessage() for record in caplog.records
        ), "POSITIVE CONTROL FAILED: a bare task already logs, so the callback test is vacuous"


class TestA1DeliverySemanticsPreserved:
    async def test_dead_recipient_still_reports_underdelivered(self, config):
        """Detaching must not turn an undeliverable message into a false delivery.

        ``delivered: False`` is what drives the rollback at
        ``tools.py:1481-1495`` (``message underdelivered`` + message removed
        from the inbox JSON).  A recipient with neither a worker record nor a
        route binding must still produce it.
        """
        bot = _make_bot(config)
        result = await asyncio.wait_for(_notify(bot, team_name="t", recipient="ghost"), timeout=5)
        assert result == {
            "delivered": False,
            "reason": "recipient has no current route binding",
        }
        assert bot._detached_wake_tasks == set()
        await bot.shutdown()

    async def test_live_recipient_reports_delivered(self, config):
        bot = _make_bot(config)
        _make_route_target(bot, thread_id=105, team_name="t", agent_name="recipient")
        with patch.object(
            bot, "_run_and_send", AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        ):
            result = await asyncio.wait_for(
                _notify(bot, team_name="t", recipient="recipient"), timeout=5
            )
            await _drain_detached(bot)
        assert result == {"delivered": True}
        await bot.shutdown()

    async def test_underdelivered_rollback_round_trip_through_send_inbox_message(
        self, monkeypatch, config, tmp_path
    ):
        """End-to-end: a notifier reporting ``delivered: False`` must still make
        ``SendInboxMessage`` return ``message underdelivered`` AND remove the
        written message from the inbox JSON (``tools.py:1481-1495``).

        Paired with the delivered case below so this is not a one-sided check.
        """
        import json
        from obs_agent.tools import create_obs_tools
        from obs_agent.hooks import HookState

        captured = {}

        def _fake_server(name, tools):
            captured["tools"] = tools
            return {"type": "fake-server", "tools": tools}

        monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", _fake_server)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)

        hook_state = HookState()

        async def _notifier(payload):
            return {"delivered": False, "reason": "recipient has no current route binding"}

        hook_state.inbox_message_notifier = _notifier
        create_obs_tools(config, lambda: "sid-123", hook_state=hook_state)
        handler = next(t.handler for t in captured["tools"] if t.name == "SendInboxMessage")

        result = await handler(
            {
                "team_name": "team-x",
                "recipient": "ghost",
                "sender": "me",
                "content": "hello",
                "summary": "s",
            }
        )

        assert result.get("is_error") is True
        assert "underdelivered" in result["content"][0]["text"]
        assert result["tool_use_result"]["outcome"] == "underdelivered"
        inbox = tmp_path / ".claude" / "teams" / "team-x" / "inboxes" / "ghost.json"
        assert not inbox.exists() or json.loads(inbox.read_text(encoding="utf-8")) == [], (
            "the underdelivered rollback did not remove the written message"
        )

    async def test_delivered_notifier_keeps_the_written_message(
        self, monkeypatch, config, tmp_path
    ):
        """Counterpart control: a ``delivered: True`` notifier must NOT roll back."""
        import json
        from obs_agent.tools import create_obs_tools
        from obs_agent.hooks import HookState

        captured = {}

        def _fake_server(name, tools):
            captured["tools"] = tools
            return {"type": "fake-server", "tools": tools}

        monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", _fake_server)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)

        hook_state = HookState()

        async def _notifier(payload):
            return {"delivered": True}

        hook_state.inbox_message_notifier = _notifier
        create_obs_tools(config, lambda: "sid-123", hook_state=hook_state)
        handler = next(t.handler for t in captured["tools"] if t.name == "SendInboxMessage")

        result = await handler(
            {
                "team_name": "team-x",
                "recipient": "live",
                "sender": "me",
                "content": "hello",
                "summary": "s",
            }
        )
        assert result.get("is_error") is not True
        inbox = tmp_path / ".claude" / "teams" / "team-x" / "inboxes" / "live.json"
        persisted = json.loads(inbox.read_text(encoding="utf-8"))
        assert persisted and persisted[-1]["text"] == "hello"

    async def test_wake_that_loses_the_busy_race_is_queued_not_dropped(self, config):
        """If the recipient becomes busy between the pre-check and the lock, the
        message must fall back to the existing deferral path, not vanish."""
        bot = _make_bot(config)
        state = _make_route_target(bot, thread_id=106, team_name="t", agent_name="recipient")
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        lock = bot._get_route_lock(state.route)

        with patch.object(bot, "_run_and_send", run_mock):
            await lock.acquire()
            try:
                result = await asyncio.wait_for(
                    _notify(bot, team_name="t", recipient="recipient"), timeout=5
                )
                state.busy = True  # lose the race while the detached task waits
            finally:
                lock.release()
            await _drain_detached(bot)

        assert result == {"delivered": True}
        run_mock.assert_not_awaited()
        assert state.hook_state.message_queue.qsize() == 1, (
            "a wake that lost the busy race must be queued, not dropped"
        )
        await bot.shutdown()
