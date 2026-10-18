"""AgentTaskStop durably stops the route, not only the task record (obs-2).

Live incident 2026-10-16: AgentTaskStop interrupted the running turn, but three
queued updates were auto-delivered seven seconds later as a new turn, which
cleared the per-turn stop mark; the "stopped" agent ran tools for an hour and
a SIGTERM of its CLI reconnected it with "Resume the interrupted response".
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ProcessError, TextBlock

from obs_agent.hooks import HookState
from obs_agent.queueing import QueuedMessage
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent
from obs_agent.telegram import TelegramBot, TelegramRoute, _ForkTaskRecord

PARENT = TelegramRoute(chat_id=67890, thread_id=None)


@pytest.fixture(autouse=True)
def _no_real_process_kills(monkeypatch):
    """Fail loudly instead of killing real processes (a mock pid once became PID 1)."""

    def _refuse(pid, *args, **kwargs):
        raise AssertionError(f"test attempted a real kill_process_tree({pid!r})")

    monkeypatch.setattr("obs_agent.maintenance_restart.kill_process_tree", _refuse)
    monkeypatch.setattr("os.kill", lambda pid, sig: (_ for _ in ()).throw(AssertionError(f"test attempted os.kill({pid!r}, {sig!r})")))
    monkeypatch.setattr("os.killpg", lambda pid, sig: (_ for _ in ()).throw(AssertionError(f"test attempted os.killpg({pid!r}, {sig!r})")))


def _bot(config) -> TelegramBot:
    return TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)


def _child(bot: TelegramBot, thread_id: int, *, busy: bool):
    route = TelegramRoute(chat_id=67890, thread_id=thread_id)
    state = bot._get_state(route, topic_title="Worker")
    state.session_manager.set_session_id(f"sid-{thread_id}")
    bot._bind_state_session(state)
    state.busy = busy
    state.hook_state.execution_active = busy
    client = MagicMock()
    client.interrupt = AsyncMock()
    client._transport = None  # never let a MagicMock pid reach a real process kill
    state.session_manager._client = client
    state.session_manager._connected = True
    return state, client


def _record(route: TelegramRoute, *, task_id: str, status: str = "launched", idle_ready: bool = False):
    return _ForkTaskRecord(
        task_id=task_id,
        parent_route=PARENT,
        parent_session_id_at_launch="sid-parent",
        parent_source_uuid="uuid-parent",
        child_route=route,
        child_session_id="sid-child",
        prompt="original prompt",
        description="worker",
        status=status,
        is_fork=False,
        team_name="2026-10-16-00-00-team",
        agent_name="worker",
        idle_ready=idle_ready,
    )


def _register(bot, monkeypatch, record, *, running: bool):
    bot._fork_tasks_by_id[record.task_id] = record
    bot._fork_task_by_child_route[record.child_route] = record.task_id
    if running:
        bot._fork_task_tasks[record.task_id] = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(bot, "_resolve_task_lookup", lambda **_kw: record)


async def _stop(bot, record):
    result = await bot._fork_task_stop(route=PARENT, args={"task_id": record.task_id})
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_stop_of_idle_looking_record_interrupts_running_route(config, monkeypatch):
    """The record was restored idle, but a maintenance-resume turn is running."""
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0")
    bot = _bot(config)
    state, client = _child(bot, 61, busy=True)
    rec = _record(state.route, task_id="t-61", status="completed", idle_ready=True)
    _register(bot, monkeypatch, rec, running=False)
    await _stop(bot, rec)
    client.interrupt.assert_awaited_once()
    assert state.hook_state.stop_requested_at is not None
    assert state.hook_state.stop_latched_at is not None
    assert state.hook_state.interrupt_requested is True


@pytest.mark.asyncio
async def test_repeat_stop_of_terminal_record_still_stops_live_route(config, monkeypatch):
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0")
    bot = _bot(config)
    state, client = _child(bot, 62, busy=True)
    rec = _record(state.route, task_id="t-62", status="stopped")
    _register(bot, monkeypatch, rec, running=False)
    payload = await _stop(bot, rec)
    assert payload["status"] == "stopping"
    assert payload["idempotent"] is False
    client.interrupt.assert_awaited_once()
    assert state.hook_state.stop_requested_at is not None


@pytest.mark.asyncio
async def test_repeat_stop_of_idle_terminal_record_stays_idempotent(config, monkeypatch):
    bot = _bot(config)
    state, client = _child(bot, 63, busy=False)
    rec = _record(state.route, task_id="t-63", status="stopped")
    _register(bot, monkeypatch, rec, running=False)
    payload = await _stop(bot, rec)
    assert payload["status"] == "stopped" and payload["idempotent"] is True
    assert state.hook_state.stop_requested_at is None  # nothing running


@pytest.mark.asyncio
async def test_stop_discards_queued_work_and_pending_wakes(config, monkeypatch):
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0")
    bot = _bot(config)
    state, _client = _child(bot, 64, busy=True)
    state.hook_state.message_queue.put_nowait(QueuedMessage(text="queued before stop"))
    state.pending_messages = [QueuedMessage(text="pending before stop")]
    state.inbox_wake_pending = True
    rec = _record(state.route, task_id="t-64")
    rec.wake_requested = True
    _register(bot, monkeypatch, rec, running=True)
    payload = await _stop(bot, rec)
    assert payload["status"] == "stopping"
    assert state.hook_state.message_queue.empty()
    assert state.pending_messages == []
    assert state.inbox_wake_pending is False
    assert rec.wake_requested is False


@pytest.mark.asyncio
async def test_stop_does_not_start_a_cli_for_an_idle_route(config, monkeypatch):
    bot = _bot(config)
    state, _client = _child(bot, 65, busy=False)
    state.session_manager._client = None
    state.session_manager._connected = False
    state.session_manager.get_client = AsyncMock()
    rec = _record(state.route, task_id="t-65", status="completed", idle_ready=True)
    _register(bot, monkeypatch, rec, running=False)
    await _stop(bot, rec)
    state.session_manager.get_client.assert_not_awaited()
    assert state.hook_state.stop_latched_at is not None


@pytest.mark.asyncio
async def test_latched_route_is_not_auto_delivered(config, monkeypatch):
    """The queued-update delivery that revived the stopped agent on 2026-10-16."""
    bot = _bot(config)
    state, _client = _child(bot, 66, busy=False)
    bot._bot_for_state = lambda _state: MagicMock()
    bot._latch_task_stop(state)
    state.hook_state.message_queue.put_nowait(QueuedMessage(text="internal callback after stop"))
    run = AsyncMock()
    monkeypatch.setattr(bot, "_run_and_send", run)
    monkeypatch.setattr(bot, "_poll_team_worker_inbox_wakes", AsyncMock())
    await bot._poll_background_queues_once()
    assert state.route not in bot._background_delivery_tasks
    await bot._deliver_background_queue(state)
    run.assert_not_awaited()
    # Kept, not dropped: the next explicit wake delivers it.
    assert state.hook_state.message_queue.qsize() == 1


@pytest.mark.asyncio
async def test_latched_route_is_left_out_of_restart_snapshots(config):
    bot = _bot(config)
    state, _client = _child(bot, 67, busy=False)
    bot._latch_task_stop(state)
    state.busy = True  # a turn started anyway
    assert bot._collect_maintenance_entries() == []


@pytest.mark.asyncio
async def test_teammate_message_after_stop_clears_latch_and_is_delivered(config, monkeypatch):
    """Agents are idle, not dead: a new message still wakes a stopped agent."""
    bot = _bot(config)
    state, _client = _child(bot, 68, busy=False)
    bot._bot_for_state = lambda _state: MagicMock()
    bot._latch_task_stop(state)
    bot._queue_running_team_worker_notice(
        state=state,
        team_name="2026-10-16-00-00-team",
        agent_name="worker",
        sender="parent",
        summary="new task",
        content="please continue",
    )
    assert state.hook_state.stop_latched_at is None
    run = AsyncMock()
    monkeypatch.setattr(bot, "_run_and_send", run)
    await bot._deliver_background_queue(state)
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_message_clears_latch(config, monkeypatch):
    from tests.test_telegram import _make_context, _make_update

    bot = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=None)
    state = bot._get_state(route)
    bot._latch_task_stop(state)
    run = AsyncMock()
    monkeypatch.setattr(bot, "_run_and_send", run)
    monkeypatch.setattr(bot, "_send_received_marker", AsyncMock(return_value=[]))
    await bot._process_message("hello", _make_update("hello"), _make_context())
    assert state.hook_state.stop_latched_at is None
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_start_keeps_latched_stop_armed_and_explicit_turn_clears(config):
    bot = _bot(config)
    state, _client = _child(bot, 69, busy=False)
    bot._latch_task_stop(state)
    seen: list[float | None] = []
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))

    with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
        instance = mock_runner.return_value

        async def mock_run(msg):
            seen.append(state.hook_state.stop_requested_at)
            yield DoneEvent()

        instance.run = mock_run
        instance.remaining_pending = []
        await bot._run_and_send(state=state, user_text="auto", bot=fake_bot)
        bot._clear_stop_latch(state, "test")
        await bot._run_and_send(state=state, user_text="explicit", bot=fake_bot)

    assert seen[0] is not None  # a turn that slips through stays stopped
    assert seen[1] is None  # after explicit input it is an ordinary turn


@pytest.mark.asyncio
async def test_agent_task_run_clears_latch(config, monkeypatch):
    bot = _bot(config)
    state, _client = _child(bot, 70, busy=False)
    rec = _record(state.route, task_id="t-70")
    bot._fork_tasks_by_id[rec.task_id] = rec
    bot._latch_task_stop(state)
    state.last_bot = None
    bot._primary_bot = None
    monkeypatch.setattr(bot, "_persist_task_handle_record", lambda _r: None)
    await bot._execute_fork_task(rec.task_id)  # exits early (no bot) after clearing
    assert state.hook_state.stop_latched_at is None


@pytest.mark.asyncio
@patch("obs_agent.session.SessionManager.reconnect")
@patch("obs_agent.session.SessionManager.get_client")
async def test_runner_does_not_reconnect_when_latched_stop_armed_at_turn_start(
    mock_get_client, mock_reconnect, config
):
    hook_state = HookState()
    hook_state.stop_latched_at = 1.0
    hook_state.stop_requested_at = hook_state.stop_latched_at  # as _run_and_send arms it
    failing_client = AsyncMock()
    failing_client.query = AsyncMock()

    async def failing_receive():
        raise ProcessError("CLI killed", exit_code=-15)
        yield  # noqa: unreachable

    failing_client.receive_response = failing_receive
    mock_get_client.return_value = failing_client
    mock_reconnect.return_value = MagicMock(content=[TextBlock(text="resumed")], session_id="x")
    from obs_agent.session import SessionManager

    session_mgr = SessionManager(config=config, hook_state=hook_state)
    session_mgr.recover_poisoned_session_if_needed = AsyncMock(return_value=None)
    runner = ConversationRunner(session_mgr, hook_state, config)
    events = [event async for event in runner.run("hello")]
    mock_reconnect.assert_not_called()
    assert not any(isinstance(e, TextEvent) and "resumed" in e.text for e in events)


@pytest.mark.asyncio
async def test_latched_stop_escalates_to_kill_when_turn_ignores_interrupt(config, monkeypatch):
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0.05")
    bot = _bot(config)
    state, _client = _child(bot, 71, busy=True)
    kill = AsyncMock(return_value=True)
    monkeypatch.setattr(state.session_manager, "_direct_kill_client_process_unlocked", kill)
    rec = _record(state.route, task_id="t-71", status="completed", idle_ready=True)
    _register(bot, monkeypatch, rec, running=False)
    await _stop(bot, rec)
    await asyncio.sleep(0.15)
    kill.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_teammate_message_during_stopped_turn_survives_the_discard(config, monkeypatch):
    """A notice that lands while the stopped turn winds down is new input, not leftover."""
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0")
    bot = _bot(config)
    state, _client = _child(bot, 72, busy=False)
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=778))

    with patch("obs_agent.telegram.ConversationRunner") as mock_runner:
        instance = mock_runner.return_value

        async def mock_run(msg):
            state.hook_state.message_queue.put_nowait(QueuedMessage(text="queued before stop"))
            bot._latch_task_stop(state)
            state.hook_state.message_queue.put_nowait(QueuedMessage(text="leftover"))
            bot._queue_running_team_worker_notice(
                state=state,
                team_name="2026-10-16-00-00-team",
                agent_name="worker",
                sender="parent",
                summary="after stop",
                content="new work",
            )
            yield DoneEvent()

        instance.run = mock_run
        instance.remaining_pending = [QueuedMessage(text="leftover")]
        await bot._run_and_send(state=state, user_text="turn", bot=fake_bot)

    texts = [m.text for m in state.pending_messages]
    assert len(texts) == 1 and "after stop" in texts[0]
    assert state.hook_state.stop_latched_at is None
    assert state.hook_state.post_stop_messages == []


@pytest.mark.parametrize(
    "pid,ok",
    [(None, False), (True, False), (False, False), (0, False), (1, False), (-5, False),
     ("1234", False), (1234.0, False), (MagicMock(), False), (4242, True)],
)
def test_killable_cli_pid_is_strict(pid, ok):
    from obs_agent.telegram import _is_killable_cli_pid

    assert _is_killable_cli_pid(pid) is ok


def test_killable_cli_pid_refuses_own_pid():
    import os

    from obs_agent.telegram import _is_killable_cli_pid

    assert _is_killable_cli_pid(os.getpid()) is False
