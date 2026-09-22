"""Independent agent wake delivery with per-route ownership and replay safety.

Cross-agent serialization is withdrawn: busy siblings, ancestors and descendants
must not prevent a route from waking. Locks and reservations still protect each
individual route against duplicate concurrent turns.
"""

import asyncio
import json

import pytest

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


async def _poll_and_drain(bot: TelegramBot) -> None:
    await bot._poll_background_queues_once()
    await _drain_detached(bot)


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


class TestInboxWakeConcurrency:
    async def test_inbox_wakes_run_concurrently_by_default(self, config):
        """Two sibling inbox wakes hold genuinely overlapping turns."""
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
            assert slow.entered == 2, "second wake never started"
            assert slow.max_concurrent == 2
            slow.release.set()
            await _drain_detached(bot)

        await bot.shutdown()

    async def test_second_same_branch_wake_starts_concurrently(self, config):
        """Default behavior permits overlapping sibling wake turns."""
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
            for _ in range(200):
                if slow.entered == 2:
                    break
                await asyncio.sleep(0.01)
            assert slow.entered == 2
            assert slow.max_concurrent == 2
            assert result == {"delivered": True}
            assert state_b.hook_state.message_queue.qsize() == 0
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

    async def test_busy_descendant_does_not_block_ancestor_inbox_wake(self, config):
        """A parent coordinator may wake while a descendant is busy."""
        bot = _make_bot(config)
        busy = _make_route_target(
            bot,
            thread_id=226,
            team_name="t",
            agent_name="busy",
            lineage=("Trunk", "Parent", "Busy"),
        )
        busy.busy = True
        _make_route_target(
            bot,
            thread_id=227,
            team_name="t",
            agent_name="parent",
            lineage=("Trunk", "Parent"),
        )
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            result = await _notify(bot, team_name="t", recipient="parent")
            await _drain_detached(bot)
        assert result == {"delivered": True}
        run_mock.assert_awaited_once()
        await bot.shutdown()

    async def test_busy_ancestor_does_not_block_descendant_inbox_wake(self, config):
        """A worker may wake while its coordinator is busy."""
        bot = _make_bot(config)
        ancestor = _make_route_target(
            bot,
            thread_id=228,
            team_name="t",
            agent_name="parent",
            lineage=("Trunk", "Parent"),
        )
        ancestor.busy = True
        _make_route_target(
            bot,
            thread_id=229,
            team_name="t",
            agent_name="child",
            lineage=("Trunk", "Parent", "Child"),
        )
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            result = await _notify(bot, team_name="t", recipient="child")
            await _drain_detached(bot)
        assert result == {"delivered": True}
        run_mock.assert_awaited_once()
        await bot.shutdown()

    async def test_same_parent_cousins_are_not_gated(self, config):
        """Different parents under one root are independent wake branches."""
        bot = _make_bot(config)
        busy = _make_route_target(
            bot,
            thread_id=228,
            team_name="t",
            agent_name="busy",
            lineage=("Trunk", "ParentA", "Busy"),
        )
        busy.busy = True
        target = _make_route_target(
            bot,
            thread_id=229,
            team_name="t",
            agent_name="target",
            lineage=("Trunk", "ParentB", "Target"),
        )
        run = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run):
            await _notify(bot, team_name="t", recipient="target")
            await _drain_detached(bot)
        run.assert_awaited_once()
        await bot.shutdown()

    async def test_busy_root_does_not_block_child(self, config):
        """A busy root has no cross-agent admission effect on its child."""
        bot = _make_bot(config)
        root = _make_route_target(
            bot, thread_id=230, team_name="t", agent_name="root", lineage=("Trunk",)
        )
        root.busy = True
        child = _make_route_target(
            bot,
            thread_id=231,
            team_name="t",
            agent_name="child",
            lineage=("Trunk", "Child"),
        )
        run = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run):
            await _notify(bot, team_name="t", recipient="child")
            await _drain_detached(bot)
        run.assert_awaited_once()
        await bot.shutdown()

    async def test_busy_agent_lineage_and_chat_do_not_block_recipient(self, config):
        """Siblings, cousins, ancestors, descendants and unknown routes stay independent."""
        bot = _make_bot(config)
        target = _make_route_target(
            bot,
            thread_id=232,
            team_name="t",
            agent_name="target",
            lineage=("Root", "P", "Target"),
        )
        busy = _make_route_target(
            bot,
            thread_id=233,
            team_name="t",
            agent_name="busy",
            lineage=("Root", "P", "Busy"),
        )
        busy.busy = True
        matrix = [
            (("Root", "P", "Busy"), -1009876543),
            (("Root", "Q", "Busy"), -1009876543),
            (("Root", "P"), -1009876543),
            (("Root", "P", "Target", "Deep"), -1009876543),
            (("Other", "P", "Busy"), -1009876543),
            (("Root", "P", "Busy"), -1005555),
            ((), -1009876543),
        ]
        for lineage, chat_id in matrix:
            busy.agent_lineage = lineage
            busy.route = TelegramRoute(chat_id=chat_id, thread_id=233)
            run = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
            with patch.object(bot, "_run_and_send", run):
                await _notify(bot, team_name="t", recipient="target")
                await _drain_detached(bot)
            run.assert_awaited_once()
            assert busy.busy
        await bot.shutdown()

    async def test_missing_lineage_does_not_block_inbox_wake(self, config):
        """Unknown lineage has no bearing on independent route delivery."""
        bot = _make_bot(config)
        busy = _make_route_target(
            bot, thread_id=231, team_name="t", agent_name="busy", lineage=("Trunk", "B")
        )
        busy.busy = True
        target = _make_route_target(
            bot, thread_id=232, team_name="t", agent_name="target", lineage=("Trunk", "T")
        )
        target.agent_lineage = None
        run = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run):
            await _notify(bot, team_name="t", recipient="target")
            await _drain_detached(bot)
        run.assert_awaited_once()
        await bot.shutdown()

    async def test_three_sibling_wakes_run_concurrently(self, config):
        """There is no cross-agent default cap, including for a third sibling."""
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
            for _ in range(200):
                if slow.entered == 3:
                    break
                await asyncio.sleep(0.01)
            assert slow.entered == 3
            assert slow.max_concurrent == 3
            slow.release.set()
            await _drain_detached(bot)

        await bot.shutdown()


class TestBackgroundPollerConcurrency:
    """The background poller delivers each idle route independently."""

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

    async def test_poller_starts_idle_recipient_while_sibling_is_busy(self, config):
        """A same-branch sibling cannot hold back an idle recipient."""
        bot, _busy, idle = self._setup(config, base_thread=301)
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await _poll_and_drain(bot)
        run_mock.assert_awaited_once()
        assert idle.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_busy_recipient_defers_and_keeps_the_queue(self, config):
        """Only the recipient's own running turn postpones its queued work."""
        bot, _busy, idle = self._setup(config, base_thread=311)
        idle.busy = True
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await _poll_and_drain(bot)
        run_mock.assert_not_awaited()
        assert idle.hook_state.message_queue.qsize() == 1, (
            "the poller dropped the queued update instead of deferring it"
        )
        await bot.shutdown()

    async def test_poller_delivers_once_the_recipient_is_idle(self, config):
        """Recipient deferral ends without waiting for its busy sibling."""
        bot, busy, idle = self._setup(config, base_thread=321)
        idle.busy = True
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await _poll_and_drain(bot)
            run_mock.assert_not_awaited()
            idle.busy = False
            assert busy.busy
            await _poll_and_drain(bot)
        assert run_mock.await_count == 1, (
            "the deferred queue was never delivered after the branch quieted down"
        )
        assert idle.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_ancestor_completion_wake_not_blocked_by_descendant(self, config):
        """Incident-shaped liveness contract for hierarchical parent/child work.

        The idle recipient is an ancestor coordinating a busy descendant. A
        withdrawn root-wide gate counted that descendant and blocked the parent;
        independent route delivery must admit the queued completion.
        """
        bot = _make_bot(config)
        busy = _make_route_target(
            bot,
            thread_id=331,
            team_name="t",
            agent_name="busy",
            lineage=("Trunk", "Parent", "Busy"),
        )
        busy.busy = True
        idle = _make_route_target(
            bot,
            thread_id=332,
            team_name="t",
            agent_name="parent",
            lineage=("Trunk", "Parent"),
        )
        idle.hook_state.message_queue.put_nowait(QueuedMessage(text="child completed"))
        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await _poll_and_drain(bot)
        assert run_mock.await_count == 1, (
            "ancestor completion wake was blocked by a descendant under root-wide gating"
        )
        assert idle.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_real_completion_callback_wakes_idle_ancestor_once(self, config):
        """A real fork completion queues the callback consumed by the poller."""
        bot = _make_bot(config)
        parent = _make_route_target(
            bot,
            thread_id=601,
            team_name="verifier",
            agent_name="parent",
            lineage=("Root", "Parent"),
        )
        completed = _make_route_target(
            bot,
            thread_id=602,
            team_name="verifier",
            agent_name="completed",
            lineage=("Root", "Parent", "Completed"),
        )
        busy = _make_route_target(
            bot,
            thread_id=603,
            team_name="verifier",
            agent_name="busy",
            lineage=("Root", "Parent", "Busy"),
        )
        busy.busy = True
        completed.last_bot.edit_message_text = AsyncMock()
        record = _ForkTaskRecord(
            task_id="verify-completion",
            parent_route=parent.route,
            parent_session_id_at_launch=parent.session_id,
            parent_source_uuid="parent-head",
            child_route=completed.route,
            child_session_id=completed.session_id,
            prompt="Complete",
            description="Complete",
            team_name="verifier",
            agent_name="completed",
            launch_parent_message_id=701,
            launch_child_message_id=702,
        )
        bot._fork_tasks_by_id[record.task_id] = record
        bot._fork_task_by_child_route[completed.route] = record.task_id
        parent.active_fork_task_ids.add(record.task_id)
        delivered = []

        async def complete_turn(*, state, **kwargs):
            if state is parent:
                delivered.extend(state.pending_messages)
                state.pending_messages = []
            return _RunOutcome(assistant_text="VERIFIER_COMPLETED")

        run = AsyncMock(side_effect=complete_turn)
        with (
            patch.object(bot, "_run_and_send", run),
            patch.object(bot, "_prune_idle_claude_processes", AsyncMock()),
            patch.object(bot, "_register_team_worker_record"),
            patch.object(bot, "_persist_task_handle_record"),
        ):
            await bot._execute_fork_task(record.task_id)
            assert run.await_count == 1
            assert parent.hook_state.message_queue.qsize() == 1
            assert parent.last_bot.send_message.await_count >= 1
            assert not parent.busy and busy.busy
            await _poll_and_drain(bot)
            assert run.await_count == 2
            assert run.await_args.kwargs["state"] is parent
            assert len(delivered) == 1
            assert "<task-notification>" in delivered[0].text
            assert "VERIFIER_COMPLETED" in delivered[0].text
            assert parent.hook_state.message_queue.empty()
            await _poll_and_drain(bot)
            assert run.await_count == 2
        await bot.shutdown()

    async def test_background_poller_loop_uses_the_delivery_pass(self, config):
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


class TestBackgroundDeliveryIsolation:
    async def test_only_child_completion_wakes_during_unrelated_turn(self, config):
        """Sep 21 incident: the global poller must not await an unrelated turn."""
        bot = _make_bot(config)
        unrelated = _make_route_target(
            bot, thread_id=1101, team_name="other", agent_name="unrelated",
            lineage=("Other", "Long worker"),
        )
        parent = _make_route_target(
            bot, thread_id=1102, team_name="case", agent_name="parent",
            lineage=("Case", "Parent"),
        )
        child = _make_route_target(
            bot, thread_id=1103, team_name="case", agent_name="child",
            lineage=("Case", "Parent", "Only child"),
        )
        child.last_bot.edit_message_text = AsyncMock()
        record = _ForkTaskRecord(
            task_id="only-child", parent_route=parent.route,
            parent_session_id_at_launch=parent.session_id, parent_source_uuid="head",
            child_route=child.route, child_session_id=child.session_id,
            prompt="Finish", description="Finish", team_name="case", agent_name="child",
            launch_parent_message_id=101, launch_child_message_id=102,
        )
        bot._fork_tasks_by_id[record.task_id] = record
        bot._fork_task_by_child_route[child.route] = record.task_id
        parent.active_fork_task_ids.add(record.task_id)
        unrelated.hook_state.message_queue.put_nowait(QueuedMessage(text="Other work"))
        entered, release, parent_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        parent_payloads = []

        async def run(*, state, **kwargs):
            pending = state.pending_messages
            state.pending_messages = []
            if state is unrelated:
                entered.set()
                await release.wait()
            elif state is parent:
                parent_payloads.extend(pending)
                parent_entered.set()
            return _RunOutcome(assistant_text="COMPLETE")

        with (
            patch.object(bot, "_run_and_send", run),
            patch.object(bot, "_prune_idle_claude_processes", AsyncMock()),
            patch.object(bot, "_register_team_worker_record"),
            patch.object(bot, "_persist_task_handle_record"),
        ):
            try:
                await asyncio.wait_for(bot._poll_background_queues_once(), 1)
                await asyncio.wait_for(entered.wait(), 1)
                await bot._execute_fork_task(record.task_id)
                assert parent.last_bot.send_message.await_count >= 1
                assert parent.hook_state.message_queue.qsize() == 1
                assert not parent.busy and not child.busy
                await asyncio.wait_for(bot._poll_background_queues_once(), 1)
                await asyncio.wait_for(parent_entered.wait(), 1)
                assert not release.is_set(), "Parent must wake before unrelated work ends"
                assert len(parent_payloads) == 1
                assert "<task-notification>" in parent_payloads[0].text
                assert "COMPLETE" in parent_payloads[0].text
                await bot._poll_background_queues_once()
                await asyncio.sleep(0)
                assert len(parent_payloads) == 1
            finally:
                release.set()
                await _drain_detached(bot)
                await bot.shutdown()

    async def test_sibling_admission_reserved_before_session_preflight(self, config):
        bot = _make_bot(config)
        states = [
            _make_route_target(
                bot, thread_id=1120 + i, team_name="t", agent_name=f"s{i}",
                lineage=("Root", f"Sibling{i}"),
            ) for i in range(2)
        ]
        for state in states:
            state.hook_state.message_queue.put_nowait(QueuedMessage(text="Work"))
        started, release = asyncio.Event(), asyncio.Event()
        entered = []

        async def preflight(*, state, **kwargs):
            # Deliberately do not set busy: real session/transport preflight
            # also precedes _run_and_send's own busy assignment.
            entered.append(state)
            started.set()
            await release.wait()
            return _RunOutcome(assistant_text="OK")

        with patch.object(bot, "_run_and_send", preflight):
            try:
                await bot._poll_background_queues_once()
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(0)
                assert entered == states
                assert all(state.busy for state in states)
                assert states[1].hook_state.message_queue.qsize() == 0
                await bot._poll_background_queues_once()
                await asyncio.sleep(0)
                assert entered == states, "Repeated poll duplicated in-flight delivery"
            finally:
                release.set()
                await _drain_detached(bot)
        await bot.shutdown()

    @pytest.mark.parametrize("block", ["busy", "paused", "transport", "lock"])
    async def test_admission_recheck_keeps_queue(self, config, block):
        bot = _make_bot(config)
        target = _make_route_target(
            bot, thread_id=1130, team_name="t", agent_name="target", lineage=("Root", "A"),
        )
        target.hook_state.message_queue.put_nowait(QueuedMessage(text="Keep me"))
        lock = bot._get_route_lock(target.route)
        run = AsyncMock()
        with patch.object(bot, "_run_and_send", run):
            await bot._poll_background_queues_once()
            if block == "busy":
                target.busy = True
            elif block == "paused":
                target.hook_state.pause_queue_delivery = True
            elif block == "transport":
                bot._chat_pending_ops[target.route.chat_id] = 1
            elif block == "lock":
                await lock.acquire()
            await _drain_detached(bot)
            run.assert_not_awaited()
            assert target.hook_state.message_queue.qsize() == 1
            if block == "lock":
                lock.release()
        await bot.shutdown()

    @pytest.mark.parametrize("cancel", [False, True])
    async def test_preflight_failure_retains_pending_and_releases_route(self, config, cancel):
        bot = _make_bot(config)
        target = _make_route_target(
            bot, thread_id=1140, team_name="t", agent_name="target", lineage=("Root",),
        )
        target.hook_state.message_queue.put_nowait(QueuedMessage(text="Keep callback"))
        entered = asyncio.Event()
        hold = asyncio.Event()

        async def preflight(**kwargs):
            entered.set()
            if cancel:
                await hold.wait()
            raise RuntimeError("preflight failure")

        with patch.object(bot, "_resolve_session_for_trigger", preflight):
            await bot._poll_background_queues_once()
            await asyncio.wait_for(entered.wait(), 1)
            if cancel:
                await bot.shutdown()
            else:
                await _drain_detached(bot)
            assert not target.busy
            assert not bot._get_route_lock(target.route).locked()
            assert [m.text for m in target.pending_messages] == ["Keep callback"]
            assert target.route not in bot._background_delivery_tasks
        if not cancel:
            await bot.shutdown()

    async def test_cancel_before_admission_keeps_queue(self, config):
        bot = _make_bot(config)
        target = _make_route_target(
            bot, thread_id=1141, team_name="t", agent_name="target", lineage=("Root",),
        )
        target.hook_state.message_queue.put_nowait(QueuedMessage(text="Keep callback"))
        run = AsyncMock()
        with patch.object(bot, "_run_and_send", run):
            await bot._poll_background_queues_once()
            bot._background_delivery_tasks[target.route].cancel()
            await _drain_detached(bot)
            run.assert_not_awaited()
            assert target.hook_state.message_queue.qsize() == 1
            assert target.route not in bot._background_delivery_tasks
        await bot.shutdown()

    async def test_user_and_inbox_queue_behind_reserved_background_turn(self, config):
        bot = _make_bot(config)
        target = _make_route_target(
            bot, thread_id=1142, team_name="t", agent_name="target", lineage=("Root",),
        )
        target.hook_state.message_queue.put_nowait(QueuedMessage(text="Callback"))
        started, hold = asyncio.Event(), asyncio.Event()

        async def run(**kwargs):
            started.set()
            await hold.wait()
            return _RunOutcome(assistant_text="OK")

        update = MagicMock()
        update.effective_message.chat_id = target.route.chat_id
        update.effective_message.message_thread_id = target.route.thread_id
        update.effective_message.message_id = 100
        update.effective_message.reply_to_message = None
        context = MagicMock(bot=target.last_bot)
        mock_run = AsyncMock(side_effect=run)
        with (
            patch.object(bot, "_run_and_send", mock_run),
            patch.object(bot, "_send_received_marker", AsyncMock()),
        ):
            try:
                await bot._poll_background_queues_once()
                await asyncio.wait_for(started.wait(), 1)
                await _notify(bot, team_name="t", recipient="target")
                await bot._process_message("User follow-up", update, context)
                await bot._poll_background_queues_once()
                await asyncio.sleep(0)
                mock_run.assert_awaited_once()
                assert target.hook_state.message_queue.qsize() == 2
                assert target.pending_messages == [QueuedMessage(text="Callback")]
            finally:
                hold.set()
                await _drain_detached(bot)
        await bot.shutdown()

    async def test_inbox_poll_transport_does_not_block_callback_dispatch(self, config):
        bot = _make_bot(config)
        target = _make_route_target(
            bot, thread_id=1150, team_name="t", agent_name="target", lineage=("Root",),
        )
        target.hook_state.message_queue.put_nowait(QueuedMessage(text="Callback"))
        started, hold = asyncio.Event(), asyncio.Event()

        async def stalled_inbox_poll():
            started.set()
            await hold.wait()

        run = AsyncMock()
        with (
            patch.object(bot, "_poll_team_worker_inbox_wakes", stalled_inbox_poll),
            patch.object(bot, "_run_and_send", run),
        ):
            try:
                await asyncio.wait_for(bot._poll_background_queues_once(), 1)
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(0)
                run.assert_awaited_once()
                assert not hold.is_set()
            finally:
                hold.set()
                await _drain_detached(bot)
        await bot.shutdown()

    async def test_slow_inbox_marker_does_not_block_sibling_poller(self, config):
        bot = _make_bot(config)
        direct = _make_route_target(
            bot, thread_id=1160, team_name="t", agent_name="direct", lineage=("Root", "A"),
        )
        sibling = _make_route_target(
            bot, thread_id=1161, team_name="t", agent_name="sibling", lineage=("Root", "B"),
        )
        started, hold = asyncio.Event(), asyncio.Event()

        async def marker(**kwargs):
            started.set()
            await hold.wait()
            return []

        run = AsyncMock()
        with (
            patch.object(bot, "_send_system_html_message", marker),
            patch.object(bot, "_run_and_send", run),
        ):
            try:
                await _notify(bot, team_name="t", recipient="direct")
                await asyncio.wait_for(started.wait(), 1)
                assert direct.busy
                sibling.hook_state.message_queue.put_nowait(QueuedMessage(text="Sibling work"))
                await bot._poll_background_queues_once()
                await asyncio.sleep(0)
                run.assert_awaited_once()
                assert run.call_args.kwargs["state"] is sibling
                assert sibling.hook_state.message_queue.qsize() == 0
            finally:
                hold.set()
                await _drain_detached(bot)
        await bot.shutdown()


class TestTeamWorkerWakeConcurrency:
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

    async def test_slow_worker_marker_reserves_own_route_not_sibling(self, config):
        bot, child, _record = self._setup(config, base_thread=1190)
        sibling = bot._get_state(TelegramRoute(chat_id=_CHAT_ID, thread_id=1190), create=False)
        sibling.busy = False
        started, hold = asyncio.Event(), asyncio.Event()

        async def marker(**kwargs):
            started.set()
            await hold.wait()
            return []

        run = AsyncMock()
        with (
            patch.object(bot, "_send_system_html_message", marker),
            patch.object(bot, "_run_and_send", run),
            patch.object(bot, "_send_system_message", AsyncMock(return_value=None)),
            patch.object(bot, "_register_team_worker_record"),
            patch.object(bot, "_persist_task_handle_record"),
            patch.object(bot, "_prune_idle_claude_processes", AsyncMock()),
        ):
            notify = asyncio.create_task(_notify(bot, team_name="t", recipient="worker"))
            try:
                await asyncio.wait_for(started.wait(), 1)
                assert child.busy
                sibling.hook_state.message_queue.put_nowait(QueuedMessage(text="Sibling work"))
                await bot._poll_background_queues_once()
                await asyncio.sleep(0)
                run.assert_awaited_once()
                assert run.call_args.kwargs["state"] is sibling
                assert sibling.hook_state.message_queue.qsize() == 0
            finally:
                hold.set()
                await notify
                await asyncio.gather(*list(bot._fork_task_tasks.values()))
                await _drain_detached(bot)
        assert not child.busy
        await bot.shutdown()

    async def test_idle_team_worker_wakes_while_sibling_is_busy(self, config):
        """A busy sibling cannot prevent scheduling the idle worker."""
        bot, child_state, _record = self._setup(config, base_thread=401)
        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            result = await _notify(bot, team_name="t", recipient="worker")
        schedule_mock.assert_awaited_once()
        assert result == {"delivered": True}
        assert child_state.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_busy_team_worker_wake_is_deferred(self, config):
        bot, child_state, record = self._setup(config, base_thread=411)
        child_state.busy = True
        with patch.object(bot, "_schedule_fork_task", new_callable=AsyncMock) as schedule_mock:
            result = await _notify(bot, team_name="t", recipient="worker")
        schedule_mock.assert_not_awaited()
        assert result == {"delivered": True}
        assert child_state.hook_state.message_queue.qsize() == 1
        assert record.status == "completed", "a deferred wake must not mutate the record"
        await bot.shutdown()


class TestWakeReservationOwnership:
    @pytest.mark.parametrize("replay", [False, True])
    async def test_worker_reservation_survives_scheduled_preflight(self, config, replay):
        bot, worker, record = TestTeamWorkerWakeConcurrency()._setup(config, base_thread=2101)
        sibling = bot._get_state(TelegramRoute(chat_id=worker.route.chat_id, thread_id=2101), create=False)
        sibling.busy = False
        started, release = asyncio.Event(), asyncio.Event()
        sibling_started = asyncio.Event()

        async def run(*, state, **kwargs):
            if state is worker:
                started.set()
                await release.wait()
            else:
                sibling_started.set()
            return _RunOutcome(assistant_text="OK")

        with (
            patch.object(bot, "_send_system_html_message", AsyncMock(return_value=[])),
            patch.object(bot, "_send_system_message", AsyncMock(return_value=None)),
            patch.object(bot, "_run_and_send", run),
            patch.object(bot, "_register_team_worker_record"),
            patch.object(bot, "_persist_task_handle_record"),
            patch.object(bot, "_prune_idle_claude_processes", AsyncMock()),
        ):
            try:
                if replay:
                    record.wake_requested = True
                    await bot._replay_pending_team_worker_wake(record)
                else:
                    await _notify(bot, team_name="t", recipient="worker")
                await asyncio.wait_for(started.wait(), 1)
                assert worker.busy
                assert bot._get_route_lock(worker.route).locked()
                sibling.hook_state.message_queue.put_nowait(QueuedMessage(text="Sibling work"))
                await bot._poll_background_queues_once()
                for _ in range(5):
                    await asyncio.sleep(0)
                assert sibling_started.is_set()
                assert sibling.hook_state.message_queue.qsize() == 0
                assert worker.busy
                assert bot._get_route_lock(worker.route).locked()
                release.set()
                await asyncio.gather(*list(bot._fork_task_tasks.values()))
                assert not worker.busy
                await bot._poll_background_queues_once()
                await _drain_detached(bot)
                assert sibling_started.is_set()
            finally:
                release.set()
                await bot.shutdown()

    @pytest.mark.parametrize("failure", ["preflight", "cancel", "before_start", "missing_record"])
    async def test_worker_reservation_released_on_failure(self, config, failure):
        bot, worker, record = TestTeamWorkerWakeConcurrency()._setup(config, base_thread=2151)
        started, release = asyncio.Event(), asyncio.Event()

        async def preflight(**kwargs):
            started.set()
            if failure == "cancel":
                await release.wait()
            raise RuntimeError("preflight failed")

        with (
            patch.object(bot, "_resolve_session_for_trigger", preflight),
            patch.object(bot, "_send_system_message", AsyncMock(return_value=None)),
            patch.object(bot, "_register_team_worker_record"),
            patch.object(bot, "_persist_task_handle_record"),
            patch.object(bot, "_prune_idle_claude_processes", AsyncMock()),
        ):
            worker.busy = True
            try:
                await bot._schedule_fork_task(task_id=record.task_id, parent_state=worker, wake_reserved=True)
                task = bot._fork_task_tasks[record.task_id]
                if failure == "missing_record":
                    del bot._fork_tasks_by_id[record.task_id]
                elif failure == "before_start":
                    task.cancel()
                elif failure == "cancel":
                    await asyncio.wait_for(started.wait(), 1)
                    assert worker.busy
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                assert not worker.busy
                assert not bot._get_route_lock(worker.route).locked()
                assert record.task_id not in bot._fork_task_tasks
                assert record.task_id not in worker.active_fork_task_ids
            finally:
                release.set()
                await bot.shutdown()

    async def test_user_retry_consumes_retained_callback_once(self, config):
        bot = _make_bot(config)
        target = _make_route_target(bot, thread_id=2201, team_name="t", agent_name="target", lineage=("Root",))
        target.hook_state.message_queue.put_nowait(QueuedMessage(text="unique callback"))
        with patch.object(bot, "_resolve_session_for_trigger", AsyncMock(side_effect=RuntimeError("preflight failed"))):
            await bot._poll_background_queues_once()
            await _drain_detached(bot)
        assert [m.text for m in target.pending_messages] == ["unique callback"]
        # Identical independent messages must survive; fix ownership, not text dedup.
        target.pending_messages.append(QueuedMessage(text="unique callback"))
        captured = []

        def capture(*args, pending_messages, **kwargs):
            captured.extend(pending_messages)
            raise RuntimeError("stop after capture")

        update = MagicMock()
        update.effective_message.chat_id = target.route.chat_id
        update.effective_message.message_thread_id = target.route.thread_id
        update.effective_message.message_id = 100
        update.effective_message.reply_to_message = None
        with (
            patch.object(bot, "_resolve_session_for_trigger", AsyncMock(return_value=(True, None))),
            patch.object(bot, "_recover_route_session_if_needed", AsyncMock()),
            patch.object(bot, "_send_received_marker", AsyncMock(return_value=[])),
            patch("obs_agent.telegram.ConversationRunner", side_effect=capture),
        ):
            try:
                with pytest.raises(RuntimeError, match="stop after capture"):
                    await bot._process_message("so?", update, MagicMock(bot=target.last_bot))
                assert [m.text for m in captured] == ["unique callback", "unique callback"]
            finally:
                await bot.shutdown()


class TestExplicitPathsAreNotGated:
    async def test_explicit_launch_still_runs_under_branch_saturation(self, config):
        """The real launch path runs while its parent and sibling are busy."""
        bot = _make_bot(config)
        sibling = _make_route_target(
            bot, thread_id=901, team_name="t", agent_name="sibling", lineage=("Trunk", "S")
        )
        sibling.busy = True

        parent_route = TelegramRoute(chat_id=_CHAT_ID, thread_id=902)
        parent_state = bot._get_state(parent_route, topic_title="Parent")
        assert parent_state is not None
        parent_state.agent_lineage = ("Trunk", "P")
        parent_state.last_bot = MagicMock()
        # The parent is mid-turn: this is the AgentTaskOutput(block=true) shape,
        # where the parent holds a slot while awaiting its child.
        parent_state.busy = True

        child_route = TelegramRoute(chat_id=_CHAT_ID, thread_id=903)
        child_state = bot._get_state(child_route, topic_title="General - Child")
        assert child_state is not None
        child_state.agent_lineage = ("Trunk", "C")
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=9))
        child_state.last_bot = fake_bot
        child_state.session_manager.set_session_id("sid-child")

        record = _ForkTaskRecord(
            task_id="task-explicit",
            parent_route=parent_route,
            parent_session_id_at_launch="sid-parent",
            parent_source_uuid="parent-uuid",
            child_route=child_route,
            child_session_id="sid-child",
            prompt="do the work",
            description="Child",
            team_name="t",
            agent_name="child",
            is_fork=False,
            status="launched",
        )
        bot._fork_tasks_by_id["task-explicit"] = record

        assert sibling.busy and parent_state.busy

        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="CHILD-RAN"))
        with patch.object(bot, "_run_and_send", run_mock):
            await bot._execute_fork_task("task-explicit")

        run_mock.assert_awaited_once()
        assert run_mock.await_args.kwargs["state"] is child_state
        await bot.shutdown()


class TestTeamWorkerWakeReplayConcurrency:
    """Replay a worker's pending wake independently of other agents."""

    def _setup(self, config, *, base_thread: int, sibling_busy: bool):
        bot = _make_bot(config)
        sibling = _make_route_target(
            bot,
            thread_id=base_thread,
            team_name="t",
            agent_name="sibling",
            lineage=("Trunk", "S"),
        )
        sibling.busy = sibling_busy

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
        # A wake arrived while this worker was mid-turn and was deferred onto
        # the record — exactly what telegram.py:9578 does.
        record.wake_requested = True
        record.wake_source_sender = "sender-x"
        record.wake_source_summary = "handoff"
        record.wake_source_content = "please process item 7"
        bot._fork_tasks_by_id["task-w"] = record
        bot._fork_task_by_child_route[child_route] = "task-w"
        bot._team_worker_records[("t", "worker")] = "task-w"
        return bot, child_state, record

    async def test_positive_control_replay_fires_when_the_branch_is_quiet(self, config):
        """A pending worker replay starts when its own recipient is ready."""
        bot, child_state, record = self._setup(config, base_thread=701, sibling_busy=False)
        with patch.object(bot, "_start_idle_team_worker_wake", new_callable=AsyncMock) as wake:
            await bot._replay_pending_team_worker_wake(record)
        wake.assert_awaited_once()
        assert child_state.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_replay_runs_while_a_same_branch_sibling_is_busy(self, config):
        """A sibling's running turn cannot suppress a worker wake replay."""
        bot, child_state, record = self._setup(config, base_thread=711, sibling_busy=True)
        with patch.object(bot, "_start_idle_team_worker_wake", new_callable=AsyncMock) as wake:
            await bot._replay_pending_team_worker_wake(record)
        wake.assert_awaited_once()
        assert wake.call_args.kwargs["content"] == "please process item 7"
        assert child_state.hook_state.message_queue.qsize() == 0
        await bot.shutdown()

    async def test_busy_recipient_replay_delivers_when_recipient_is_idle(self, config):
        """Preserve recipient deferral without waiting for its sibling."""
        bot, child_state, record = self._setup(config, base_thread=721, sibling_busy=True)
        child_state.busy = True
        sibling = bot._get_state(TelegramRoute(chat_id=_CHAT_ID, thread_id=721), create=False)
        with patch.object(bot, "_start_idle_team_worker_wake", new_callable=AsyncMock):
            await bot._replay_pending_team_worker_wake(record)
        assert child_state.hook_state.message_queue.qsize() == 1

        run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        with patch.object(bot, "_run_and_send", run_mock):
            await _poll_and_drain(bot)
            run_mock.assert_not_awaited()
            child_state.busy = False
            assert sibling.busy
            await _poll_and_drain(bot)
        assert run_mock.await_count == 1, (
            "the converted replay deferral was never delivered after the branch quieted"
        )
        kwargs = run_mock.await_args.kwargs
        carried = "".join(m.text for m in kwargs["state"].pending_messages)
        assert "process item 7" in carried, "the replayed wake lost its payload"
        await bot.shutdown()

    async def test_execute_fork_task_tail_replays_despite_busy_sibling(self, config):
        """End-to-end through the real tail, not just the extracted method."""
        bot, child_state, record = self._setup(config, base_thread=731, sibling_busy=True)
        parent_state = bot._get_state(record.parent_route, topic_title="Parent")
        assert parent_state is not None
        parent_state.last_bot = MagicMock()

        with patch.object(
            bot, "_run_and_send", AsyncMock(return_value=_RunOutcome(assistant_text="OK"))
        ), patch.object(
            bot, "_start_idle_team_worker_wake", new_callable=AsyncMock
        ) as wake:
            await bot._execute_fork_task("task-w")

        wake.assert_awaited_once()
        assert child_state.hook_state.message_queue.qsize() == 0
        await bot.shutdown()


class TestWithdrawnSerializationPolicy:
    def test_branch_cap_config_and_helpers_are_removed(self, config, monkeypatch):
        from obs_agent.config import OBSConfig

        monkeypatch.setenv("OBS_MAX_CONCURRENT_TURNS", "1")
        assert "max_concurrent_turns" not in OBSConfig.__dataclass_fields__
        assert not hasattr(OBSConfig.from_env(), "max_concurrent_turns")
        for name in ("_branch_anchor_for", "_branch_turn_load",
                     "_effective_branch_turn_cap", "_branch_wake_gate_blocks"):
            assert not hasattr(TelegramBot, name)


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

        bot = _make_bot(config)
        sibling = _make_route_target(
            bot, thread_id=601, team_name="t", agent_name="sibling", lineage=("Trunk", "S")
        )
        sibling.busy = True
        recipient = _make_route_target(
            bot, thread_id=602, team_name="t", agent_name="recipient", lineage=("Trunk", "R")
        )
        recipient.busy = True

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

        # The send succeeded while the recipient's own turn delayed delivery.
        assert result.get("is_error") is not True
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
