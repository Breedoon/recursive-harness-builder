"""Opt-in maintenance restart: snapshot running agents, consume-before-act resume.

Plain restart/stop is the kill switch: without a marker nothing is resumed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import maintenance_restart as maint
from obs_agent.queueing import QueuedMessage
from obs_agent.telegram import TelegramBot, TelegramRoute, _ForkTaskRecord

_GAP = 0.05


# --- pure helpers -----------------------------------------------------------


def _marker(entries, *, requested_at=None):
    return maint.ResumeMarker(
        requested_at=time.time() if requested_at is None else requested_at,
        source="test",
        entries=entries,
    )


def test_marker_roundtrip_and_consume_renames_first(tmp_path: Path):
    path = maint.marker_path(tmp_path)
    entry = maint.ResumeEntry(
        chat_id=1,
        thread_id=2,
        session_id="sid",
        task_id="t1",
        is_local=True,
        queued=[maint.QueuedEntry(text="hello", telegram_message_id=5)],
    )
    maint.write_marker(path, _marker([entry]))
    marker, reason = maint.consume_marker(path)
    assert reason == "ok"
    assert not path.exists()
    assert path.with_name(path.name + ".consumed").exists()
    assert marker is not None and marker.entries[0].queued[0].text == "hello"
    assert marker.entries[0].is_local is True
    # A second start (crash loop) finds nothing to replay.
    again, reason2 = maint.consume_marker(path)
    assert again is None and reason2 == "no_marker"


def test_stale_marker_is_consumed_but_ignored(tmp_path: Path):
    path = maint.marker_path(tmp_path)
    maint.write_marker(path, _marker([], requested_at=time.time() - 3600))
    marker, reason = maint.consume_marker(path, max_age_seconds=900)
    assert marker is None and reason.startswith("stale")
    assert not path.exists()


def test_corrupt_marker_never_crashes_and_is_consumed(tmp_path: Path):
    path = maint.marker_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    marker, reason = maint.consume_marker(path)
    assert marker is None and reason.startswith("unreadable")
    assert not path.exists()


def test_local_detection_and_split_order():
    assert maint.is_local_model("local-qwen3.8-27b")
    assert maint.is_local_model("local-qwen3.8-27b[1m]")
    assert not maint.is_local_model("gpt-6-luna[120k]")
    assert not maint.is_local_model(None)
    a = maint.ResumeEntry(chat_id=1, thread_id=1, session_id="a", is_local=True)
    b = maint.ResumeEntry(chat_id=1, thread_id=2, session_id="b")
    c = maint.ResumeEntry(chat_id=1, thread_id=3, session_id="c", is_local=True)
    hosted, local = maint.split_resume_order([a, b, c])
    assert [e.session_id for e in hosted] == ["b"]
    assert [e.session_id for e in local] == ["a", "c"]


def test_resume_prompt_says_verify_before_repeating():
    text = maint.build_resume_prompt(requested_at=0.0, queued_count=2)
    assert "restarted for maintenance" in text
    assert "before repeating" in text
    assert "2 message(s)" in text


def test_cli_sends_sigusr1_to_given_pid():
    with patch.object(maint.os, "kill") as kill:
        assert maint.main(["--pid", "4242"]) == 0
    kill.assert_called_once_with(4242, maint.MAINTENANCE_SIGNAL)


def test_cli_refuses_ambiguous_daemon():
    with patch.object(maint, "find_daemon_pids", return_value=[1, 2]):
        assert maint.main([]) == 2


# --- daemon integration -----------------------------------------------------


def _bot(config) -> TelegramBot:
    return TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)


def _busy_child(bot: TelegramBot, route: TelegramRoute, *, sid: str, model: str | None = None):
    state = bot._get_state(route, topic_title="Worker")
    state.session_manager.set_session_id(sid)
    bot._bind_state_session(state)
    if model:
        state.session_manager.model_override = model
    state.busy = True
    return state


def _record(child_route: TelegramRoute, *, task_id: str, sid: str) -> _ForkTaskRecord:
    return _ForkTaskRecord(
        task_id=task_id,
        parent_route=TelegramRoute(chat_id=67890, thread_id=None),
        parent_session_id_at_launch="sid-parent",
        parent_source_uuid="uuid-parent",
        child_route=child_route,
        child_session_id=sid,
        prompt="original prompt",
        description="worker",
        status="launched",
        is_fork=True,
        launch_tool_name="AgentTask",
        team_name="team-m1",
        agent_name=f"agent-{task_id}",
        idle_ready=False,
        emit_parent_callback=True,
    )


def test_snapshot_includes_only_running_and_not_stopped(config):
    bot = _bot(config)
    busy = TelegramRoute(chat_id=67890, thread_id=11)
    idle = TelegramRoute(chat_id=67890, thread_id=12)
    stopped = TelegramRoute(chat_id=67890, thread_id=13)
    state = _busy_child(bot, busy, sid="sid-busy", model="local-qwen3.8-27b")
    state.pending_messages = [QueuedMessage(text="queued one", telegram_message_id=9)]
    bot._get_state(idle, topic_title="Idle").session_manager.set_session_id("sid-idle")
    _busy_child(bot, stopped, sid="sid-stopped")
    rec = _record(stopped, task_id="t-stop", sid="sid-stopped")
    rec.terminal_request = "stopped"
    bot._fork_tasks_by_id[rec.task_id] = rec
    bot._fork_task_by_child_route[stopped] = rec.task_id

    entries = bot._collect_maintenance_entries()
    assert [(e.thread_id, e.session_id) for e in entries] == [(11, "sid-busy")]
    assert entries[0].is_local is True
    assert entries[0].queued[0].text == "queued one"


@pytest.mark.asyncio
async def test_request_writes_marker_then_exits(config):
    bot = _bot(config)
    _busy_child(bot, TelegramRoute(chat_id=67890, thread_id=21), sid="sid-21")
    exits: list[bool] = []
    bot._maintenance_exit_fn = lambda: exits.append(True)
    count = await bot.request_maintenance_restart(source="test", delay_seconds=0)
    assert count == 1 and exits == [True]
    data = json.loads(bot._maintenance_marker_path().read_text(encoding="utf-8"))
    assert data["entries"][0]["session_id"] == "sid-21"
    # Idempotent: a second request does not rewrite or exit twice.
    assert await bot.request_maintenance_restart(source="again", delay_seconds=0) == 0
    assert exits == [True]


@pytest.mark.asyncio
async def test_plain_restart_without_marker_resumes_nothing(config):
    bot = _bot(config)
    assert bot.start_maintenance_resume() is None


@pytest.mark.asyncio
async def test_resume_rearms_parent_callback_for_fork_record(config):
    first = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=31)
    state = _busy_child(first, route, sid="sid-31")
    state.pending_messages = [QueuedMessage(text="late note")]
    rec = _record(route, task_id="t-31", sid="sid-31")
    first._fork_tasks_by_id[rec.task_id] = rec
    first._fork_task_by_child_route[route] = rec.task_id
    first._register_team_worker_record(rec)
    first._persist_state_for_route(route)
    first.write_maintenance_marker(source="test")
    await first.shutdown()

    restored = _bot(config)
    await restored.initialize_runtime()
    restored._primary_bot = MagicMock()
    restored_record = restored._fork_tasks_by_id["t-31"]
    assert restored_record.emit_parent_callback is False  # restore default
    with patch.object(restored, "_send_system_html_message", AsyncMock(return_value=[])), patch.object(
        restored, "_schedule_fork_task", AsyncMock()
    ) as schedule:
        task = restored.start_maintenance_resume()
        assert task is not None
        await task
    schedule.assert_awaited_once()
    assert restored_record.emit_parent_callback is True
    assert "restarted for maintenance" in restored_record.prompt
    child_state = restored._get_state(route, create=False)
    assert child_state.busy is True  # reserved for the scheduled wake
    assert [m.text for m in child_state.pending_messages] == ["late note"]
    assert not restored._maintenance_marker_path().exists()


@pytest.mark.asyncio
async def test_resume_plain_route_runs_turn_and_releases_busy(config):
    first = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=41)
    _busy_child(first, route, sid="sid-41")
    first._persist_state_for_route(route)
    first.write_maintenance_marker(source="test")
    await first.shutdown()

    restored = _bot(config)
    await restored.initialize_runtime()
    restored._primary_bot = MagicMock()
    run = AsyncMock()
    with patch.object(restored, "_send_system_html_message", AsyncMock(return_value=[])), patch.object(
        restored, "_run_and_send", run
    ):
        await restored.start_maintenance_resume()
        await asyncio.gather(*list(restored._detached_wake_tasks))
    run.assert_awaited_once()
    assert "restarted for maintenance" in run.await_args.kwargs["user_text"]
    assert restored._get_state(route, create=False).busy is False


@pytest.mark.asyncio
async def test_local_routes_resume_strictly_one_at_a_time(config):
    bot = _bot(config)
    order: list[str] = []
    gates = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def fake_resume(entry, *, requested_at, **_kwargs):
        order.append(f"start:{entry.session_id}")

        async def turn():
            await gates[entry.session_id].wait()
            order.append(f"end:{entry.session_id}")

        return asyncio.create_task(turn())

    marker = _marker(
        [
            maint.ResumeEntry(chat_id=1, thread_id=1, session_id="a", is_local=True),
            maint.ResumeEntry(chat_id=1, thread_id=2, session_id="b", is_local=True),
        ]
    )
    with patch.object(bot, "_resume_maintenance_entry", side_effect=fake_resume):
        runner = asyncio.create_task(bot._run_maintenance_resume(marker))
        await asyncio.sleep(0.05)
        assert order == ["start:a"]
        gates["a"].set()
        await asyncio.sleep(0.05)
        assert order == ["start:a", "end:a", "start:b"]
        gates["b"].set()
        await runner
    assert order[-1] == "end:b"


# --- L3 G1: local watchdog (C6) and daemon matching (C7) --------------------


@pytest.mark.asyncio
async def test_local_watchdog_moves_on_without_cancelling_slow_turn(config, monkeypatch):
    monkeypatch.setenv("OBS_MAINTENANCE_RESUME_LOCAL_WAIT_SECONDS", "0.1")
    bot = _bot(config)
    order: list[str] = []
    gates = {"a": asyncio.Event(), "b": asyncio.Event()}
    turns: dict[str, asyncio.Task] = {}

    async def fake_resume(entry, *, requested_at, **_kwargs):
        order.append(f"start:{entry.session_id}")

        async def turn():
            await gates[entry.session_id].wait()
            order.append(f"end:{entry.session_id}")

        turns[entry.session_id] = asyncio.create_task(turn())
        return turns[entry.session_id]

    marker = _marker(
        [
            maint.ResumeEntry(chat_id=1, thread_id=1, session_id="a", is_local=True),
            maint.ResumeEntry(chat_id=1, thread_id=2, session_id="b", is_local=True),
        ]
    )
    with patch.object(bot, "_resume_maintenance_entry", side_effect=fake_resume):
        runner = asyncio.create_task(bot._run_maintenance_resume(marker))
        await asyncio.sleep(0.05)
        assert order == ["start:a"]
        await asyncio.sleep(0.2)  # past the 0.1s watchdog: b starts, a still running
        assert order == ["start:a", "start:b"]
        assert not turns["a"].done() and not turns["a"].cancelled()
        gates["b"].set()
        await runner
        gates["a"].set()
        await turns["a"]
    assert "end:a" in order and "end:b" in order
    assert not turns["a"].cancelled()


def test_local_wait_default_and_unlimited(monkeypatch):
    monkeypatch.delenv("OBS_MAINTENANCE_RESUME_LOCAL_WAIT_SECONDS", raising=False)
    assert maint.local_resume_wait_from_env(600.0) == 600.0
    monkeypatch.setenv("OBS_MAINTENANCE_RESUME_LOCAL_WAIT_SECONDS", "0")
    assert maint.local_resume_wait_from_env(600.0) == 0.0
    monkeypatch.setenv("OBS_MAINTENANCE_RESUME_LOCAL_WAIT_SECONDS", "junk")
    assert maint.local_resume_wait_from_env(600.0) == 600.0


def _fake_proc(root: Path, pid: int, argv: list[str], supervisor: str | None = None) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    env = [b"PATH=/usr/bin"]
    if supervisor:
        env.append(f"SUPERVISOR_PROCESS_NAME={supervisor}".encode())
    (d / "environ").write_bytes(b"\0".join(env) + b"\0")


def test_find_daemon_pids_prefers_supervised_prod_and_skips_shells(tmp_path: Path):
    py = "/workspace/obs/.venv/bin/python"
    _fake_proc(tmp_path, 100, [py, "-m", "obs_agent.telegram_main", "--prod"], "obs-telegram-prod")
    _fake_proc(tmp_path, 200, [py, "-m", "obs_agent.telegram_main"])  # test daemon
    _fake_proc(tmp_path, 300, ["/bin/bash", "-c", "pgrep -f 'python.*-m obs_agent.telegram_main'"])
    _fake_proc(tmp_path, 400, [py, "-m", "obs_agent.telegram_main_helper"])
    (tmp_path / "self").mkdir()
    assert maint.find_daemon_pids(proc_root=tmp_path) == [100]


def test_find_daemon_pids_without_supervisor_marker_returns_all_matches(tmp_path: Path):
    py = "/usr/bin/python3"
    _fake_proc(tmp_path, 10, [py, "-m", "obs_agent.telegram_main"])
    _fake_proc(tmp_path, 20, [py, "-m", "obs_agent.telegram_main"])
    assert maint.find_daemon_pids(proc_root=tmp_path) == [10, 20]


# --- 5-minute resume window (Daniel 2026-09-26 11:02Z) -----------------------


def _entry(task_id):
    return maint.ResumeEntry(chat_id=1, thread_id=2, session_id="sid", task_id=task_id, is_local=False, queued=[])


def test_default_resume_window_is_five_minutes(monkeypatch):
    monkeypatch.delenv("OBS_MAINTENANCE_RESUME_MAX_AGE_SECONDS", raising=False)
    assert maint.DEFAULT_MAX_AGE_SECONDS == 300
    assert maint.max_age_from_env() == 300.0
    monkeypatch.setenv("OBS_MAINTENANCE_RESUME_MAX_AGE_SECONDS", "120")
    assert maint.max_age_from_env() == 120.0


def test_quick_crash_within_five_minutes_resumes(tmp_path: Path):
    path = tmp_path / "crash-resume.json"
    maint.write_marker(path, _marker([_entry("r1")], requested_at=time.time() - 60))
    marker, reason = maint.consume_marker(path)
    assert marker is not None and reason == "ok"


def test_long_outage_over_five_minutes_resumes_nothing(tmp_path: Path):
    for name in ("crash-resume.json", "maintenance-resume.json"):
        path = tmp_path / name
        maint.write_marker(path, _marker([_entry("r1")], requested_at=time.time() - 301))
        marker, reason = maint.consume_marker(path)
        assert marker is None and reason.startswith("stale"), name
        assert not path.exists()


def test_old_file_write_is_stale_even_if_requested_at_is_fresh(tmp_path: Path):
    import os

    path = tmp_path / "crash-resume.json"
    maint.write_marker(path, _marker([_entry("r1")], requested_at=time.time()))
    old = time.time() - 600
    os.utime(path, (old, old))
    assert maint.peek_requested_at(path, max_age_seconds=300) is None
    marker, reason = maint.consume_marker(path)
    assert marker is None and reason.startswith("stale")
