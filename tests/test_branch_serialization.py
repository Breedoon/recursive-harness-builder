"""Tier A (vault-aay.2) — per-branch gating of wake-class turn starts.

A "wake-class" turn start is one nobody explicitly asked for right now: an
inbox wake, a team-worker wake, or the background poller's queued
auto-delivery.  Over 91 h of production logs, 97.5 % of inbox wakes
(1223/1254 across 8 trees) started a turn while a same-tree sibling was
already mid-turn.  Tier A defers those.

Explicit ``AgentTask`` launches, resumes, user messages, and scheduled runs
are deliberately NOT gated — see ``test_explicit_paths_are_not_gated`` for the
reasoning.  (User-facing documentation lands with ``vault-aay.4``.)

Every gated behaviour below is paired with a POSITIVE CONTROL that first shows
the un-gated path producing genuinely concurrent same-branch turn starts.  A
serialization assertion against code that never had concurrency proves nothing.
"""

import asyncio
import json

from unittest.mock import AsyncMock, MagicMock, patch

from obs_agent.queueing import QueuedMessage
from obs_agent.telegram import (
    TelegramBot,
    TelegramRoute,
    _ForkTaskRecord,
    _RunOutcome,
)

_TEST_GAP = 0.05
_CHAT_ID = -1009876543


def _make_bot(config) -> TelegramBot:
    return TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)


def _make_route_target(
    bot: TelegramBot,
    *,
    thread_id: int,
    team_name: str,
    agent_name: str,
    lineage: tuple[str, ...],
):
    route = TelegramRoute(chat_id=_CHAT_ID, thread_id=thread_id)
    state = bot._get_state(route, topic_title=f"General - {agent_name}")
    assert state is not None
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=1000 + thread_id))
    state.last_bot = fake_bot
    state.session_manager.set_session_id(f"sid-{agent_name}")
    state.agent_lineage = lineage
    bot._route_inbox_targets[(team_name, agent_name)] = route
    return state


async def _notify(bot: TelegramBot, *, team_name: str, recipient: str):
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


async def _drain_detached(bot: TelegramBot, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while bot._detached_wake_tasks and loop.time() < deadline:
        await asyncio.sleep(0.01)


class _SlowTurn:
    """``_run_and_send`` stand-in that records overlap between turns."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.entered = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def __call__(self, *, state, user_text, bot, **kwargs):
        self.entered += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        state.busy = True
        self.started.set()
        try:
            await self.release.wait()
        finally:
            state.busy = False
            self.concurrent -= 1
        return _RunOutcome(assistant_text="OK")


class TestInboxWakeGating:
    async def test_positive_control_ungated_wakes_run_concurrently(self, config):
        """POSITIVE CONTROL — with the gate disabled, two same-branch agents
        woken by inbox messages hold GENUINELY OVERLAPPING turns."""
        config.max_concurrent_turns = 0
        bot = _make_bot(config)
        _make_route_target(
            bot, thread_id=201, team_name="t", agent_name="a", lineage=("Trunk", "A")
        )
        _make_route_target(
            bot, thread_id=202, team_name="t", agent_name="b", lineage=("Trunk", "B")
        )
        slow = _SlowTurn()

        with patch.object(bot, "_run_and_send", slow):
            await _notify(bot, team_name="t", recipient="a")
            await asyncio.wait_for(slow.started.wait(), timeout=5)
            await _notify(bot, team_name="t", recipient="b")
            for _ in range(200):
                if slow.entered >= 2:
                    break
                await asyncio.sleep(0.01)
            assert slow.entered == 2, "POSITIVE CONTROL FAILED: second wake never started"
            assert slow.max_concurrent == 2, (
                "POSITIVE CONTROL FAILED: the ungated path did not overlap two "
                "same-branch turns, so the gating test below proves nothing"
            )
            slow.release.set()
            await _drain_detached(bot)

        await bot.shutdown()

    async def test_gate_defers_second_same_branch_wake(self, config):
        """Same scenario, gate on: the second wake is deferred, not started."""
        bot = _make_bot(config)
        _make_route_target(
            bot, thread_id=211, team_name="t", agent_name="a", lineage=("Trunk", "A")
        )
        state_b = _make_route_target(
            bot, thread_id=212, team_name="t", agent_name="b", lineage=("Trunk", "B")
        )
        slow = _SlowTurn()

        with patch.object(bot, "_run_and_send", slow):
            await _notify(bot, team_name="t", recipient="a")
            await asyncio.wait_for(slow.started.wait(), timeout=5)
            result = await _notify(bot, team_name="t", recipient="b")
            await asyncio.sleep(0.2)
            assert slow.entered == 1, "the gate let a second same-branch wake start a turn"
            assert slow.max_concurrent == 1
            assert result == {"delivered": True}
            assert state_b.hook_state.message_queue.qsize() == 1, (
                "a deferred wake must take the existing per-agent deferral path"
            )
            slow.release.set()
            await _drain_detached(bot)

        await bot.shutdown()

    async def test_gate_does_not_cross_branches(self, config):
        """An agent in a different tree must not be gated by this branch's load."""
        bot = _make_bot(config)
        _make_route_target(
            bot, thread_id=221, team_name="t", agent_name="a", lineage=("TrunkOne", "A")
        )
        _make_route_target(
            bot, thread_id=222, team_name="t", agent_name="b", lineage=("TrunkTwo", "B")
        )
        slow = _SlowTurn()

        with patch.object(bot, "_run_and_send", slow):
            await _notify(bot, team_name="t", recipient="a")
            await asyncio.wait_for(slow.started.wait(), timeout=5)
            await _notify(bot, team_name="t", recipient="b")
            for _ in range(200):
                if slow.entered >= 2:
                    break
                await asyncio.sleep(0.01)
            assert slow.entered == 2, "an unrelated tree was wrongly gated"
            slow.release.set()
            await _drain_detached(bot)

        await bot.shutdown()

    async def test_gate_fails_open_without_lineage(self, config):
        """A state with no lineage cannot be classified, so it is not gated."""
        bot = _make_bot(config)
        busy = _make_route_target(
            bot, thread_id=231, team_name="t", agent_name="busy", lineage=("Trunk", "B")
        )
        busy.busy = True
        target = _make_route_target(
            bot, thread_id=232, team_name="t", agent_name="target", lineage=("Trunk", "T")
        )
        target.agent_lineage = None
        assert bot._branch_anchor_for(target) is None
        assert bot._branch_turn_load(target) == 0
        assert bot._branch_wake_gate_blocks(target) is False
        await bot.shutdown()

    async def test_cap_above_one_admits_that_many(self, config):
        """The knob is a number, not a mode: cap 2 admits two, defers the third."""
        config.max_concurrent_turns = 2
        bot = _make_bot(config)
        for index, name in enumerate(("a", "b", "c")):
            _make_route_target(
                bot,
                thread_id=240 + index,
                team_name="t",
                agent_name=name,
                lineage=("Trunk", name.upper()),
            )
        slow = _SlowTurn()

        with patch.object(bot, "_run_and_send", slow):
            await _notify(bot, team_name="t", recipient="a")
            await asyncio.wait_for(slow.started.wait(), timeout=5)
            await _notify(bot, team_name="t", recipient="b")
            for _ in range(200):
                if slow.entered >= 2:
                    break
                await asyncio.sleep(0.01)
            assert slow.entered == 2
            await _notify(bot, team_name="t", recipient="c")
            await asyncio.sleep(0.2)
            assert slow.entered == 2, "cap=2 admitted a third concurrent wake"
            assert slow.max_concurrent == 2
            slow.release.set()
            await _drain_detached(bot)

        await bot.shutdown()


class TestBackgroundPollerGating:
    """A2 named the background poller (the E3 auto-delivery path) as the most
    likely implementation hole.  Gating the wake sites but not the poller only
    delays the parallel turn by one poll interval."""

    def _setup(self, config, *, base_thread: int):
        bot = _make_bot(config)
        busy = _make_route_target(
            bot,
            thread_id=base_thread,
            team_name="t",
            agent_name="busy",
            lineage=("Trunk", "Busy"),
        )
        idle = _make_route_target(
            bot,
            thread_id=base_thread + 1,
            team_name="t",
            agent_name="idle",
            lineage=("Trunk", "Idle"),
        )
        busy.busy = True  # a same-branch sibling is mid-turn
        idle.hook_state.message_queue.put_nowait(QueuedMessage(text="queued update"))
        return bot, busy, idle

    async def test_positive_control_ungated_poller_starts_a_turn(self, config):
        """POSITIVE CONTROL — ungated, the poller starts a turn for the idle
        agent even though a same-branch sibling is busy."""
        config.max_concurrent_turns = 0
        bot, _busy, idle = self._setup(config, base_thread=301)
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await bot._poll_background_queues_once()
        assert run_mock.await_count == 1, (
            "POSITIVE CONTROL FAILED: the ungated poller did not start a turn while a "
            "same-branch sibling was busy — the gated assertion below would be vacuous"
        )
        assert idle.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_gated_poller_defers_and_keeps_the_queue(self, config):
        """Gated, the same poller pass must start nothing and lose nothing."""
        config.max_concurrent_turns = 1
        bot, _busy, idle = self._setup(config, base_thread=311)
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await bot._poll_background_queues_once()
        run_mock.assert_not_awaited()
        assert idle.hook_state.message_queue.qsize() == 1, (
            "the poller dropped the queued update instead of deferring it"
        )
        await bot.shutdown()

    async def test_gated_poller_delivers_once_the_branch_quiets_down(self, config):
        """Deferral must be temporary, not a permanent stall."""
        config.max_concurrent_turns = 1
        bot, busy, idle = self._setup(config, base_thread=321)
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await bot._poll_background_queues_once()
            run_mock.assert_not_awaited()
            busy.busy = False  # the sibling's turn ends
            await bot._poll_background_queues_once()
        assert run_mock.await_count == 1, (
            "the deferred queue was never delivered after the branch quieted down"
        )
        assert idle.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_background_poller_loop_uses_the_gated_pass(self, config):
        """The extracted pass is what the real loop runs — not a test-only path."""
        bot = _make_bot(config)
        called = asyncio.Event()

        async def _once():
            called.set()

        bot._background_poll_seconds = 0.01
        with patch.object(bot, "_poll_background_queues_once", _once):
            task = asyncio.create_task(bot._background_poller_loop())
            await asyncio.wait_for(called.wait(), timeout=2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.shutdown()


class TestTeamWorkerWakeGating:
    def _setup(self, config, *, base_thread: int):
        bot = _make_bot(config)
        sibling = _make_route_target(
            bot,
            thread_id=base_thread,
            team_name="t",
            agent_name="sibling",
            lineage=("Trunk", "S"),
        )
        sibling.busy = True

        child_route = TelegramRoute(chat_id=_CHAT_ID, thread_id=base_thread + 1)
        child_state = bot._get_state(child_route, topic_title="General - Worker")
        assert child_state is not None
        child_state.agent_lineage = ("Trunk", "W")
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=7))
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-worker")

        record = _ForkTaskRecord(
            task_id="task-w",
            parent_route=TelegramRoute(chat_id=_CHAT_ID, thread_id=None),
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-uuid",
            child_route=child_route,
            child_session_id="sid-worker",
            prompt="old",
            description="Worker",
            team_name="t",
            agent_name="worker",
            is_fork=False,
            status="completed",
            idle_ready=True,
        )
        bot._fork_tasks_by_id["task-w"] = record
        bot._fork_task_by_child_route[child_route] = "task-w"
        bot._team_worker_records[("t", "worker")] = "task-w"
        return bot, child_state, record

    async def test_positive_control_ungated_team_worker_wake_launches(self, config):
        """POSITIVE CONTROL — ungated, an idle team-worker wake launches."""
        config.max_concurrent_turns = 0
        bot, child_state, _record = self._setup(config, base_thread=401)
        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            result = await _notify(bot, team_name="t", recipient="worker")
        schedule_mock.assert_awaited_once()
        assert result == {"delivered": True}
        assert child_state.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_gated_team_worker_wake_is_deferred(self, config):
        config.max_concurrent_turns = 1
        bot, child_state, record = self._setup(config, base_thread=411)
        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            result = await _notify(bot, team_name="t", recipient="worker")
        schedule_mock.assert_not_awaited()
        assert result == {"delivered": True}
        assert child_state.hook_state.message_queue.qsize() == 1
        assert record.status == "completed", "a deferred wake must not mutate the record"
        await bot.shutdown()


class TestExplicitPathsAreNotGated:
    async def test_explicit_paths_are_not_gated(self, config):
        """DOCUMENTED EXCEPTION (requirement 3).

        Tier A gates only wake-class starts.  Explicit ``AgentTask`` launches
        and resumes (``_schedule_fork_task`` / ``_execute_fork_task``), user
        messages (``_process_message``), and scheduled runs contain NO call to
        the gate predicate.

        Rationale: gating an explicit launch reintroduces the deadlock A2
        identified — a parent blocked in ``AgentTaskOutput(block=true)`` holds
        the branch's only slot while waiting for a child that can never be
        admitted, and silently returns ``retrieval_status: timeout``.  Because
        explicit launches are never gated, Tier A is deadlock-free by
        construction.  Tier B, which does count them, must add the slot-yield.

        This test asserts the *absence* structurally, so it fails loudly if a
        later change adds gating to those paths without revisiting the
        deadlock analysis.
        """
        import inspect

        from obs_agent import telegram as telegram_mod

        gate_names = ("_branch_wake_gate_blocks", "_branch_turn_load")
        for fn_name in (
            "_schedule_fork_task",
            "_execute_fork_task",
            "_process_message",
            "_resume_fork_task",
        ):
            source = inspect.getsource(getattr(telegram_mod.TelegramBot, fn_name))
            for gate in gate_names:
                assert gate not in source, (
                    f"{fn_name} now consults {gate}. Tier A must NOT gate explicit "
                    f"launches or user messages — see the AgentTaskOutput deadlock in "
                    f"A2 §4/D1. If this is intentional, it is Tier B and needs a "
                    f"slot-yield around the blocking await."
                )

    async def test_gate_predicate_is_true_for_the_launcher_itself(self, config):
        """Control for the test above: the predicate WOULD block, if consulted.

        Without this, the structural assertion could pass merely because the
        branch happened to be quiet.
        """
        bot = _make_bot(config)
        sibling = _make_route_target(
            bot, thread_id=501, team_name="t", agent_name="sibling", lineage=("Trunk", "S")
        )
        sibling.busy = True
        launcher = _make_route_target(
            bot, thread_id=502, team_name="t", agent_name="launcher", lineage=("Trunk", "L")
        )
        assert bot._branch_turn_load(launcher) == 1
        assert bot._branch_wake_gate_blocks(launcher) is True
        await bot.shutdown()


class TestDeferredWakeDurability:
    """Requirement 2 says VERIFY that deferred wakes are never dropped, rather
    than assuming inbox persistence guarantees it.  It does not fully: the
    in-memory notice queue is lost on restart.  The durable inbox JSON is what
    actually carries the message across, and that is what is proven here."""

    async def test_deferred_message_stays_unread_on_disk_across_a_restart(
        self, config, monkeypatch, tmp_path
    ):
        from obs_agent.tools import create_obs_tools
        from obs_agent.hooks import HookState

        captured = {}

        def _fake_server(name, tools):
            captured["tools"] = tools
            return {"type": "fake-server", "tools": tools}

        monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", _fake_server)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)

        config.max_concurrent_turns = 1
        bot = _make_bot(config)
        sibling = _make_route_target(
            bot, thread_id=601, team_name="t", agent_name="sibling", lineage=("Trunk", "S")
        )
        sibling.busy = True
        recipient = _make_route_target(
            bot, thread_id=602, team_name="t", agent_name="recipient", lineage=("Trunk", "R")
        )

        hook_state = HookState()
        hook_state.inbox_message_notifier = lambda payload: _notify(
            bot, team_name=payload["team_name"], recipient=payload["recipient"]
        )
        create_obs_tools(config, lambda: "sid-1", hook_state=hook_state)
        send = next(t.handler for t in captured["tools"] if t.name == "SendInboxMessage")

        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            result = await send(
                {
                    "team_name": "t",
                    "recipient": "recipient",
                    "sender": "sender-x",
                    "content": "please process item 7",
                    "summary": "handoff",
                }
            )
            await _drain_detached(bot)

        # The send succeeded and the gate deferred the turn.
        assert result.get("is_error") is not True, (
            "a gated wake must not be reported as underdelivered"
        )
        run_mock.assert_not_awaited()
        assert recipient.hook_state.message_queue.qsize() == 1

        inbox = tmp_path / ".claude" / "teams" / "t" / "inboxes" / "recipient.json"
        persisted = json.loads(inbox.read_text(encoding="utf-8"))
        assert persisted[-1]["text"] == "please process item 7"
        assert persisted[-1]["read"] is False, (
            "the deferred message was marked read without ever being delivered"
        )

        # --- simulated restart: drop the bot entirely, so the in-memory notice
        # queue is gone.  A fresh runtime must still be able to read the message.
        await bot.shutdown()
        del bot

        read = next(t.handler for t in captured["tools"] if t.name == "ReadInbox")
        out = await read({"team_name": "t", "agent": "recipient"})
        blob = json.dumps(out)
        assert "please process item 7" in blob, (
            "a deferred wake's message did NOT survive the simulated restart — the "
            "durability guarantee is weaker than claimed and must be reported"
        )

        # And it is marked read only once actually handed over.
        after = json.loads(inbox.read_text(encoding="utf-8"))
        assert after[-1]["read"] is True

    async def test_positive_control_unread_flag_is_a_real_signal(
        self, monkeypatch, config, tmp_path
    ):
        """POSITIVE CONTROL for the durability test: ReadInbox returns nothing
        when the inbox is empty, so the assertion above is not trivially true."""
        from obs_agent.tools import create_obs_tools

        captured = {}

        def _fake_server(name, tools):
            captured["tools"] = tools
            return {"type": "fake-server", "tools": tools}

        monkeypatch.setattr("obs_agent.tools.create_sdk_mcp_server", _fake_server)
        monkeypatch.setattr("obs_agent.tools.Path.home", lambda: tmp_path)
        create_obs_tools(config, lambda: "sid-1")
        read = next(t.handler for t in captured["tools"] if t.name == "ReadInbox")
        out = await read({"team_name": "t", "agent": "nobody"})
        assert "please process item 7" not in json.dumps(out)
