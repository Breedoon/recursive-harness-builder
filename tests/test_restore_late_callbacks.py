"""Late parent callbacks after a restart (vault-u3b.35) and crash-snapshot coverage.

A child that was mid-run when OBS went down by a maintenance restart or crash,
and that was not resumed then, owes its launching parent a completion callback.
Its next run (e.g. an inbox wake) delivers it exactly once. The kill switch and
stopped parents keep the old behaviour: no callback, nothing woken.

The crash snapshot also records the turn's input for a route whose session id
is not known yet, so the resume re-sends the original request.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import maintenance_restart as maint
from obs_agent.telegram import TelegramBot, TelegramRoute, _ForkTaskRecord, _RunOutcome

_GAP = 0.05
PARENT = TelegramRoute(chat_id=67890, thread_id=500)


def _bot(config) -> TelegramBot:
    return TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)


async def _restored(config) -> TelegramBot:
    bot = _bot(config)
    await bot.initialize_runtime()  # restore happens here, before any resume file is consumed
    return bot


def _child_record(child: TelegramRoute, *, task_id: str, status: str = "launched") -> _ForkTaskRecord:
    return _ForkTaskRecord(
        task_id=task_id,
        parent_route=PARENT,
        parent_session_id_at_launch="sid-parent",
        parent_source_uuid="uuid-parent",
        child_route=child,
        child_session_id=f"sid-{task_id}",
        prompt="original prompt",
        description="worker",
        status=status,
        is_fork=True,
        launch_tool_name="AgentTask",
        team_name="team-lc",
        agent_name=f"agent-{task_id}",
        launch_parent_message_id=4242,
        emit_parent_callback=True,
    )


async def _first_daemon(
    config,
    records: list[_ForkTaskRecord],
    *,
    crash: bool = True,
    parent_waiting: bool = True,
) -> None:
    """Persist parent + children as a daemon would, then 'die' (optionally leaving a crash snapshot).

    ``parent_waiting`` puts each unfinished child in the parent's active set, as
    ``_schedule_fork_task`` does for a run in flight.
    """
    first = _bot(config)
    parent = first._get_state(PARENT, topic_title="Parent")
    parent.session_manager.set_session_id("sid-parent")
    first._persist_state_for_route(PARENT)
    for rec in records:
        if parent_waiting and rec.status == "launched":
            parent.active_fork_task_ids.add(rec.task_id)
        child = first._get_state(rec.child_route, topic_title="Child")
        child.session_manager.set_session_id(rec.child_session_id)
        first._fork_tasks_by_id[rec.task_id] = rec
        first._fork_task_by_child_route[rec.child_route] = rec.task_id
        first._register_team_worker_record(rec)
        first._persist_state_for_route(rec.child_route)
    if crash:
        first.write_crash_snapshot()
    await first.shutdown()


# --- which restored records owe a callback ------------------------------------------


@pytest.mark.asyncio
async def test_crash_restore_marks_midrun_child_as_owing_callback(config):
    child = TelegramRoute(chat_id=67890, thread_id=501)
    await _first_daemon(config, [_child_record(child, task_id="t-501")])
    restored = await _restored(config)
    rec = restored._fork_tasks_by_id["t-501"]
    assert rec.callback_owed is True
    assert rec.emit_parent_callback is False  # nothing is sent until the child runs again
    assert rec.parent_route == PARENT


@pytest.mark.asyncio
async def test_maintenance_restore_marks_midrun_child_as_owing_callback(config):
    child = TelegramRoute(chat_id=67890, thread_id=502)
    await _first_daemon(config, [_child_record(child, task_id="t-502")], crash=False)
    maint.write_marker(
        maint.marker_path(Path(config.telegram_state_db_path).parent),
        maint.ResumeMarker(requested_at=time.time(), source="test", entries=[], inflight_task_ids=["t-502"]),
    )
    restored = await _restored(config)
    assert restored._fork_tasks_by_id["t-502"].callback_owed is True


@pytest.mark.asyncio
async def test_kill_switch_restore_owes_nothing(config, tmp_path, monkeypatch):
    child = TelegramRoute(chat_id=67890, thread_id=503)
    await _first_daemon(config, [_child_record(child, task_id="t-503")])
    sentinel = tmp_path / "ks"
    sentinel.write_text("")
    monkeypatch.setenv(maint.KILLSWITCH_SENTINEL_ENV, str(sentinel))
    restored = await _restored(config)
    assert restored._fork_tasks_by_id["t-503"].callback_owed is False


@pytest.mark.asyncio
async def test_plain_start_without_any_resume_file_owes_nothing(config):
    child = TelegramRoute(chat_id=67890, thread_id=504)
    await _first_daemon(config, [_child_record(child, task_id="t-504")], crash=False)
    restored = await _restored(config)
    assert restored._fork_tasks_by_id["t-504"].callback_owed is False


@pytest.mark.asyncio
async def test_finished_stopped_called_back_and_self_parented_records_owe_nothing(config):
    done = _child_record(TelegramRoute(chat_id=67890, thread_id=505), task_id="t-done", status="completed")
    stopped = _child_record(TelegramRoute(chat_id=67890, thread_id=506), task_id="t-stop", status="stopped")
    stopped.terminal_request = "stopped"
    called = _child_record(TelegramRoute(chat_id=67890, thread_id=507), task_id="t-called")
    called.parent_callback_message_id = 99
    selfwake_route = TelegramRoute(chat_id=67890, thread_id=508)
    selfwake = _child_record(selfwake_route, task_id="t-self")
    selfwake.parent_route = selfwake_route  # an inbox-wake run owes no parent
    await _first_daemon(config, [done, stopped, called, selfwake])
    restored = await _restored(config)
    for task_id in ("t-done", "t-stop", "t-called", "t-self"):
        assert restored._fork_tasks_by_id[task_id].callback_owed is False, task_id


# --- delivering the owed callback -----------------------------------------------------


def _schedule_without_running(*, task_id, parent_state, **_kwargs):
    # What _schedule_fork_task does before the turn starts: the run is in flight.
    parent_state.active_fork_task_ids.add(task_id)


async def _wake(bot: TelegramBot, rec: _ForkTaskRecord, *, run_turn: bool) -> None:
    sent = AsyncMock(return_value=[])
    run = AsyncMock(return_value=_RunOutcome(assistant_text="DONE"))
    with (
        patch.object(bot, "_send_system_html_message", sent),
        patch.object(bot, "_send_system_message", AsyncMock(return_value=None)),
        patch.object(bot, "_run_and_send", run),
        patch.object(bot, "_prune_idle_claude_processes", AsyncMock()),
    ):
        if not run_turn:
            with patch.object(bot, "_schedule_fork_task", AsyncMock(side_effect=_schedule_without_running)):
                await bot._start_idle_team_worker_wake(record=rec, sender="peer", summary="s", content="c")
            return
        await bot._start_idle_team_worker_wake(record=rec, sender="peer", summary="s", content="c")
        await asyncio.gather(*[t for t in list(bot._fork_task_tasks.values())])
        await asyncio.sleep(0)
    bot._last_wake_html_calls = [call.kwargs for call in sent.await_args_list]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_owed_callback_is_delivered_once_on_the_next_wake(config):
    child = TelegramRoute(chat_id=67890, thread_id=510)
    await _first_daemon(config, [_child_record(child, task_id="t-510")])
    restored = await _restored(config)
    restored._primary_bot = MagicMock()
    rec = restored._fork_tasks_by_id["t-510"]
    parent = restored._get_state(PARENT, create=False)
    assert parent is not None and parent.last_bot is None  # restored parent has no bot yet

    await _wake(restored, rec, run_turn=True)

    # The restored parent's queue got exactly one completion payload, sent to the
    # original parent as a reply to its launch message.
    assert parent.hook_state.message_queue.qsize() == 1
    parent_markers = [c for c in restored._last_wake_html_calls if c.get("route") == PARENT]
    assert len(parent_markers) == 1
    assert parent_markers[0]["reply_to_message_id"] == 4242
    assert rec.callback_owed is False
    assert rec.status == "completed"

    # A second wake of the same child is an ordinary inbox wake: no callback.
    await _wake(restored, rec, run_turn=True)
    assert parent.hook_state.message_queue.qsize() == 1
    assert rec.parent_route == child


@pytest.mark.asyncio
async def test_owed_callback_is_not_replayed_after_another_crash(config):
    child = TelegramRoute(chat_id=67890, thread_id=511)
    await _first_daemon(config, [_child_record(child, task_id="t-511")])
    restored = await _restored(config)
    restored._primary_bot = MagicMock()
    await _wake(restored, restored._fork_tasks_by_id["t-511"], run_turn=True)
    restored.write_crash_snapshot()
    await restored.shutdown()

    again = await _restored(config)
    # The finished run persisted a terminal status: nothing is owed twice.
    assert again._fork_tasks_by_id["t-511"].callback_owed is False


@pytest.mark.asyncio
async def test_crash_during_owed_wake_keeps_the_debt(config):
    child = TelegramRoute(chat_id=67890, thread_id=512)
    await _first_daemon(config, [_child_record(child, task_id="t-512")])
    restored = await _restored(config)
    restored._primary_bot = MagicMock()
    rec = restored._fork_tasks_by_id["t-512"]
    await _wake(restored, rec, run_turn=False)  # wake scheduled, turn never finishes
    assert rec.emit_parent_callback is True and rec.parent_route == PARENT
    restored.write_crash_snapshot()
    await restored.shutdown()

    again = await _restored(config)
    owed = again._fork_tasks_by_id["t-512"]
    assert owed.callback_owed is True and owed.parent_route == PARENT


@pytest.mark.asyncio
async def test_owed_callback_not_delivered_to_stopped_parent(config):
    child = TelegramRoute(chat_id=67890, thread_id=513)
    await _first_daemon(config, [_child_record(child, task_id="t-513")])
    restored = await _restored(config)
    restored._primary_bot = MagicMock()
    # The parent is itself a child of a grandparent and was /stop_tree'd.
    parent_rec = _child_record(PARENT, task_id="t-parent")
    parent_rec.parent_route = TelegramRoute(chat_id=67890, thread_id=None)
    parent_rec.terminal_request = "stopped"
    parent_rec.status = "stopped"
    restored._fork_tasks_by_id[parent_rec.task_id] = parent_rec
    restored._fork_task_by_child_route[PARENT] = parent_rec.task_id
    rec = restored._fork_tasks_by_id["t-513"]
    await _wake(restored, rec, run_turn=False)
    assert rec.emit_parent_callback is False
    assert rec.parent_route == child
    assert rec.callback_owed is False


@pytest.mark.asyncio
async def test_resume_paths_clear_the_debt(config):
    child = TelegramRoute(chat_id=67890, thread_id=514)
    await _first_daemon(config, [_child_record(child, task_id="t-514")])
    restored = await _restored(config)
    restored._primary_bot = MagicMock()
    rec = restored._fork_tasks_by_id["t-514"]
    entry = maint.ResumeEntry(
        chat_id=child.chat_id, thread_id=child.thread_id, session_id=rec.child_session_id, task_id=rec.task_id
    )
    with (
        patch.object(restored, "_send_system_html_message", AsyncMock(return_value=[])),
        patch.object(restored, "_schedule_fork_task", AsyncMock()),
    ):
        await restored._resume_maintenance_entry(entry, requested_at=time.time(), kind="crash")
    assert rec.emit_parent_callback is True and rec.callback_owed is False


# --- crash snapshot coverage for routes without a session id ---------------------------


def test_snapshot_keeps_route_without_session_id_and_records_its_input(config):
    bot = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=520)
    state = bot._get_state(route, topic_title="Fresh")
    state.busy = True
    state.inflight_user_text = "please do the thing"
    entries = bot._collect_maintenance_entries(allow_jsonl_lookup=False)
    assert [(e.thread_id, e.session_id, e.inflight_prompt) for e in entries] == [
        (520, None, "please do the thing")
    ]


def test_snapshot_uses_fork_prompt_before_the_turn_starts(config):
    bot = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=521)
    state = bot._get_state(route, topic_title="Queued fork")
    state.busy = True  # reserved, waiting for its child lock; no session yet
    rec = _child_record(route, task_id="t-521")
    rec.child_session_id = ""
    bot._fork_tasks_by_id[rec.task_id] = rec
    bot._fork_task_by_child_route[route] = rec.task_id
    (entry,) = bot._collect_maintenance_entries(allow_jsonl_lookup=False)
    assert entry.session_id is None and entry.inflight_prompt == "original prompt"


def test_snapshot_omits_input_when_session_exists(config):
    bot = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=522)
    state = bot._get_state(route, topic_title="Known")
    state.session_manager.set_session_id("sid-522")
    state.busy = True
    state.inflight_user_text = "not needed"
    (entry,) = bot._collect_maintenance_entries(allow_jsonl_lookup=False)
    assert entry.session_id == "sid-522" and entry.inflight_prompt is None


@pytest.mark.asyncio
async def test_run_and_send_sets_and_clears_inflight_text(config):
    bot = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=523)
    state = bot._get_state(route, topic_title="T")
    seen: list[str | None] = []

    def boom(*args, **kwargs):
        seen.append(state.inflight_user_text)
        raise RuntimeError("stop here")

    with (
        patch.object(bot, "_resolve_session_for_trigger", AsyncMock(return_value=(True, None))),
        patch.object(bot, "_recover_route_session_if_needed", AsyncMock()),
        patch.object(bot, "_ensure_state_lineage", side_effect=boom),
    ):
        try:
            await bot._run_and_send(state=state, user_text="hello there", bot=AsyncMock())
        except Exception:
            pass
    assert seen == ["hello there"]
    assert state.inflight_user_text is None


@pytest.mark.asyncio
async def test_resume_without_session_resends_original_request(config):
    bot = _bot(config)
    bot._primary_bot = MagicMock()
    route = TelegramRoute(chat_id=67890, thread_id=524)
    bot._get_state(route, topic_title="Fresh")
    entry = maint.ResumeEntry(
        chat_id=route.chat_id, thread_id=route.thread_id, session_id=None, inflight_prompt="build the widget"
    )
    run = AsyncMock()
    with (
        patch.object(bot, "_send_system_html_message", AsyncMock(return_value=[])),
        patch.object(bot, "_run_and_send", run),
    ):
        task = await bot._resume_maintenance_entry(entry, requested_at=time.time(), kind="crash")
        await task
    text = run.await_args.kwargs["user_text"]
    assert "before a session transcript existed" in text
    assert text.rstrip().endswith("build the widget")


@pytest.mark.asyncio
async def test_resume_with_session_keeps_continue_prompt(config):
    bot = _bot(config)
    bot._primary_bot = MagicMock()
    route = TelegramRoute(chat_id=67890, thread_id=525)
    state = bot._get_state(route, topic_title="Known")
    state.session_manager.set_session_id("sid-525")
    entry = maint.ResumeEntry(
        chat_id=route.chat_id, thread_id=route.thread_id, session_id="sid-525", inflight_prompt="ignored"
    )
    run = AsyncMock()
    with (
        patch.object(bot, "_send_system_html_message", AsyncMock(return_value=[])),
        patch.object(bot, "_run_and_send", run),
    ):
        task = await bot._resume_maintenance_entry(entry, requested_at=time.time(), kind="maintenance")
        await task
    text = run.await_args.kwargs["user_text"]
    assert "Continue your task from where you left off." in text and "ignored" not in text


def test_inflight_prompt_round_trips_and_old_markers_parse(tmp_path: Path):
    path = maint.crash_snapshot_path(tmp_path)
    maint.write_marker(
        path,
        maint.ResumeMarker(
            requested_at=time.time(),
            source="periodic",
            entries=[
                maint.ResumeEntry(chat_id=1, thread_id=2, session_id=None, inflight_prompt="x"),
                maint.ResumeEntry(chat_id=1, thread_id=3, session_id="s"),
            ],
        ),
    )
    marker, reason = maint.consume_marker(path)
    assert reason == "ok"
    assert [e.inflight_prompt for e in marker.entries] == ["x", None]


@pytest.mark.parametrize("rev", ["19ddec9", "6a2b730"])
def test_marker_with_inflight_prompt_is_readable_by_rollback_readers(tmp_path: Path, rev: str):
    """Rollback safety: older readers ignore the new inflight_prompt field."""
    import importlib.util
    import subprocess
    import sys

    source = subprocess.run(
        ["git", "show", f"{rev}:src/obs_agent/maintenance_restart.py"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    if source.returncode != 0:
        pytest.skip(f"{rev} not reachable from this checkout")
    old_file = tmp_path / f"maint_{rev}.py"
    old_file.write_text(source.stdout)
    name = f"maint_{rev}"
    spec = importlib.util.spec_from_file_location(name, old_file)
    old = importlib.util.module_from_spec(spec)
    sys.modules[name] = old
    try:
        spec.loader.exec_module(old)
        path = maint.marker_path(tmp_path)
        maint.write_marker(
            path,
            maint.ResumeMarker(
                requested_at=time.time(),
                source="test",
                entries=[maint.ResumeEntry(chat_id=1, thread_id=2, session_id=None, inflight_prompt="x")],
            ),
        )
        marker, reason = old.consume_marker(path)
        assert reason == "ok" and marker.entries[0].thread_id == 2
    finally:
        sys.modules.pop(name, None)


# --- F1 bound: only the outage that cut the run off owes a callback (vault-u3b.81) ------


def _db_rows(config) -> dict[str, tuple]:
    import sqlite3

    conn = sqlite3.connect(str(config.telegram_state_db_path))
    try:
        return {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT task_id, status, parent_chat_id, parent_thread_id, terminal_request, "
                "parent_callback_message_id, launch_parent_message_id, created_at, completed_at "
                "FROM task_handle_state ORDER BY task_id"
            )
        }
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_child_from_two_outages_ago_is_not_owed(config):
    """Over-fire guard: a child cut off by an older outage and never woken owes nothing now."""
    child = TelegramRoute(chat_id=67890, thread_id=520)
    await _first_daemon(config, [_child_record(child, task_id="t-520")])
    second = await _restored(config)
    assert second._fork_tasks_by_id["t-520"].callback_owed is True  # owed for the outage that cut it off
    # The second daemon never wakes the child, then crashes too.
    second.write_crash_snapshot()
    await second.shutdown()

    third = await _restored(config)
    assert third._fork_tasks_by_id["t-520"].callback_owed is False


@pytest.mark.asyncio
async def test_child_the_parent_no_longer_waits_on_is_not_owed(config):
    """Over-fire guard: an unfinished record that no in-flight set lists owes nothing."""
    child = TelegramRoute(chat_id=67890, thread_id=521)
    await _first_daemon(config, [_child_record(child, task_id="t-521")], parent_waiting=False)
    restored = await _restored(config)
    assert restored._fork_tasks_by_id["t-521"].callback_owed is False


@pytest.mark.asyncio
async def test_child_launched_after_the_last_snapshot_is_owed(config):
    """Under-fire guard: a run launched after the last 15 s snapshot write still owes its callback."""
    first = _bot(config)
    parent = first._get_state(PARENT, topic_title="Parent")
    parent.session_manager.set_session_id("sid-parent")
    first._persist_state_for_route(PARENT)
    first.write_crash_snapshot()  # the last periodic write before the crash
    time.sleep(0.01)
    child = TelegramRoute(chat_id=67890, thread_id=522)
    rec = _child_record(child, task_id="t-522")  # created now, after the snapshot
    child_state = first._get_state(child, topic_title="Child")
    child_state.session_manager.set_session_id(rec.child_session_id)
    first._fork_tasks_by_id[rec.task_id] = rec
    first._fork_task_by_child_route[child] = rec.task_id
    first._register_team_worker_record(rec)
    first._persist_state_for_route(child)
    await first.shutdown()

    restored = await _restored(config)
    assert restored._fork_tasks_by_id["t-522"].callback_owed is True


@pytest.mark.asyncio
async def test_last_outage_child_with_waiting_parent_is_called_back_exactly_once_across_restarts(config):
    """Two-direction check: owed once, delivered once, then never owed again."""
    child = TelegramRoute(chat_id=67890, thread_id=523)
    await _first_daemon(config, [_child_record(child, task_id="t-523")])
    restored = await _restored(config)
    restored._primary_bot = MagicMock()
    rec = restored._fork_tasks_by_id["t-523"]
    assert rec.callback_owed is True
    await _wake(restored, rec, run_turn=True)
    parent = restored._get_state(PARENT, create=False)
    assert parent.hook_state.message_queue.qsize() == 1
    restored.write_crash_snapshot()
    await restored.shutdown()

    again = await _restored(config)
    assert again._fork_tasks_by_id["t-523"].callback_owed is False


@pytest.mark.asyncio
async def test_historical_backlog_stays_unowed_and_untouched_across_starts(config):
    """Neutralisation: old unfinished records are never owed, and restores do not rewrite them."""
    records = [_child_record(TelegramRoute(chat_id=67890, thread_id=530 + i), task_id=f"t-old-{i}") for i in range(3)]
    await _first_daemon(config, records, crash=False, parent_waiting=False)
    before = _db_rows(config)
    assert set(before) >= {"t-old-0", "t-old-1", "t-old-2"}
    for _ in range(3):
        # Each start follows a crash whose snapshot lists nothing in flight.
        maint.write_marker(
            maint.crash_snapshot_path(Path(config.telegram_state_db_path).parent),
            maint.ResumeMarker(requested_at=time.time(), source="periodic", entries=[]),
        )
        bot = await _restored(config)
        assert not any(bot._fork_tasks_by_id[f"t-old-{i}"].callback_owed for i in range(3))
        await bot.shutdown()
    after = _db_rows(config)
    assert {k: before[k] for k in ("t-old-0", "t-old-1", "t-old-2")} == {
        k: after[k] for k in ("t-old-0", "t-old-1", "t-old-2")
    }


def test_inflight_task_ids_exclude_self_parented_and_terminal_runs(config):
    bot = _bot(config)
    parent = bot._get_state(PARENT, topic_title="Parent")
    live = _child_record(TelegramRoute(chat_id=67890, thread_id=540), task_id="t-live")
    self_route = TelegramRoute(chat_id=67890, thread_id=541)
    selfwake = _child_record(self_route, task_id="t-selfwake")
    selfwake.parent_route = self_route
    stopping = _child_record(TelegramRoute(chat_id=67890, thread_id=542), task_id="t-stopping")
    stopping.terminal_request = "stopped"
    for rec in (live, selfwake, stopping):
        bot._fork_tasks_by_id[rec.task_id] = rec
        parent.active_fork_task_ids.add(rec.task_id)
    parent.active_fork_task_ids.add("t-unknown")
    assert bot._collect_inflight_task_ids() == ["t-live"]
    snapshot = bot.write_crash_snapshot()
    marker = maint.peek_marker(snapshot, max_age_seconds=900)
    assert marker is not None and marker.inflight_task_ids == ["t-live"]


def test_inflight_task_ids_round_trip_and_old_markers_default_empty(tmp_path: Path):
    import json

    path = maint.marker_path(tmp_path)
    maint.write_marker(
        path,
        maint.ResumeMarker(requested_at=time.time(), source="t", entries=[], inflight_task_ids=["a", "b"]),
    )
    assert maint.peek_marker(path, max_age_seconds=900).inflight_task_ids == ["a", "b"]
    marker, reason = maint.consume_marker(path)
    assert reason == "ok" and marker.inflight_task_ids == ["a", "b"]
    old = {"version": maint.MARKER_VERSION, "requested_at": time.time(), "source": "old", "entries": []}
    path.write_text(json.dumps(old))
    assert maint.peek_marker(path, max_age_seconds=900).inflight_task_ids == []
    path.write_text("{not json")
    assert maint.peek_marker(path, max_age_seconds=900) is None
    stale = dict(old, requested_at=time.time() - 10_000)
    path.write_text(json.dumps(stale))
    assert maint.peek_marker(path, max_age_seconds=900) is None


@pytest.mark.parametrize("rev", ["19ddec9", "6a2b730"])
def test_marker_with_inflight_task_ids_is_readable_by_rollback_readers(tmp_path: Path, rev: str):
    """Rollback safety: older readers ignore the new inflight_task_ids field."""
    import importlib.util
    import subprocess
    import sys

    source = subprocess.run(
        ["git", "show", f"{rev}:src/obs_agent/maintenance_restart.py"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    if source.returncode != 0:
        pytest.skip(f"{rev} not reachable from this checkout")
    old_file = tmp_path / f"maint_ids_{rev}.py"
    old_file.write_text(source.stdout)
    name = f"maint_ids_{rev}"
    spec = importlib.util.spec_from_file_location(name, old_file)
    old = importlib.util.module_from_spec(spec)
    sys.modules[name] = old
    try:
        spec.loader.exec_module(old)
        path = maint.marker_path(tmp_path)
        maint.write_marker(
            path,
            maint.ResumeMarker(
                requested_at=time.time(),
                source="test",
                entries=[maint.ResumeEntry(chat_id=1, thread_id=2, session_id="s")],
                inflight_task_ids=["t-1"],
            ),
        )
        marker, reason = old.consume_marker(path)
        assert reason == "ok" and marker.entries[0].thread_id == 2
    finally:
        sys.modules.pop(name, None)
