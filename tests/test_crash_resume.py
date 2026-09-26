"""Crash resume, default maintenance restart, stop escalation and orphan reaping.

Covers vault-u3b.70 (B-B): periodic crash-resume snapshot consumed at startup,
kill-switch SIGTERM / wrapper sentinel discarding it, `/restart [plain]`,
`--plain` CLI, stop that really stops (vault-u3b.65), orphaned-CLI reaper
(vault-u3b.66), outage inbox wake floor (vault-u3b.35), and compatibility
with the maintenance marker written by the running 19ddec9 daemon.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ProcessError, TextBlock

from obs_agent import maintenance_restart as maint
from obs_agent import telegram as tg
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, TextEvent
from obs_agent.telegram import TelegramBot, TelegramRoute, _ForkTaskRecord

_GAP = 0.05
FIXTURE_19DDEC9 = Path(__file__).parent / "fixtures" / "maintenance-resume-19ddec9.json"


def _bot(config) -> TelegramBot:
    return TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)


def _busy(bot: TelegramBot, route: TelegramRoute, *, sid: str, model: str | None = None):
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
        team_name="team-cr",
        agent_name=f"agent-{task_id}",
        idle_ready=False,
        emit_parent_callback=True,
    )


# --- compatibility with the running 19ddec9 daemon's marker ------------------


def test_19ddec9_marker_fixture_is_consumed_unchanged(tmp_path: Path):
    path = maint.marker_path(tmp_path)
    shutil.copy(FIXTURE_19DDEC9, path)
    requested_at = json.loads(path.read_text())["requested_at"]
    marker, reason = maint.consume_marker(path, now=requested_at + 30)
    assert reason == "ok"
    assert marker is not None and marker.source == "signal:SIGUSR1"
    fork, local = marker.entries
    assert fork.task_id == "task-fork-1" and fork.is_local is False
    assert fork.queued[0].text == "queued before restart" and fork.queued[0].telegram_message_id == 77
    assert local.is_local is True and local.model.startswith("local-")
    assert fork.crash_resume_count == 0 and local.crash_resume_count == 0
    assert not path.exists() and path.with_name(path.name + ".consumed").exists()


@pytest.mark.asyncio
async def test_19ddec9_marker_resumes_as_maintenance_at_daemon_start(config):
    bot = _bot(config)
    raw = json.loads(FIXTURE_19DDEC9.read_text())
    raw["requested_at"] = time.time() - 5
    bot._maintenance_marker_path().parent.mkdir(parents=True, exist_ok=True)
    bot._maintenance_marker_path().write_text(json.dumps(raw))
    seen: list[tuple[int, str]] = []

    async def fake_run(marker, *, kind):
        seen.append((len(marker.entries), kind))

    with patch.object(bot, "_run_maintenance_resume", side_effect=fake_run):
        task = bot.start_maintenance_resume()
        await task
    assert seen == [(2, "maintenance")]


def test_new_marker_is_readable_by_19ddec9_reader(tmp_path: Path):
    """Rollback safety: the 19ddec9 reader ignores the new crash_resume_count field."""
    import importlib.util
    import sys

    source = subprocess.run(
        ["git", "show", "19ddec9:src/obs_agent/maintenance_restart.py"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    if source.returncode != 0:
        pytest.skip("19ddec9 not reachable from this checkout")
    old_file = tmp_path / "maint_19ddec9.py"
    old_file.write_text(source.stdout)
    spec = importlib.util.spec_from_file_location("maint_19ddec9", old_file)
    old = importlib.util.module_from_spec(spec)
    sys.modules["maint_19ddec9"] = old
    try:
        spec.loader.exec_module(old)
        path = maint.marker_path(tmp_path)
        maint.write_marker(
            path,
            maint.ResumeMarker(
                requested_at=time.time(),
                source="test",
                entries=[maint.ResumeEntry(chat_id=1, thread_id=2, session_id="s", crash_resume_count=1)],
            ),
        )
        marker, reason = old.consume_marker(path)
        assert reason == "ok" and marker.entries[0].session_id == "s"
    finally:
        sys.modules.pop("maint_19ddec9", None)


# --- crash snapshot ------------------------------------------------------------


def test_crash_snapshot_lists_running_routes_and_skips_stopped(config):
    bot = _bot(config)
    _busy(bot, TelegramRoute(chat_id=67890, thread_id=11), sid="sid-busy")
    stopped = _busy(bot, TelegramRoute(chat_id=67890, thread_id=12), sid="sid-trunk-stopped")
    stopped.hook_state.stop_requested_at = time.time()  # trunk /stop, no fork record
    bot._get_state(TelegramRoute(chat_id=67890, thread_id=13), topic_title="Idle")
    path = bot.write_crash_snapshot()
    data = json.loads(path.read_text())
    assert path.name == maint.CRASH_SNAPSHOT_FILENAME
    assert data["source"] == "periodic" and data["version"] == maint.MARKER_VERSION
    assert [e["session_id"] for e in data["entries"]] == ["sid-busy"]


@pytest.mark.asyncio
async def test_crash_snapshot_writer_refreshes_periodically(config, monkeypatch):
    monkeypatch.setenv("OBS_CRASH_RESUME_SNAPSHOT_SECONDS", "0.05")
    bot = _bot(config)
    task = bot.start_crash_snapshot_writer()
    await asyncio.sleep(0.02)
    first = json.loads(bot._crash_snapshot_path().read_text())["requested_at"]
    await asyncio.sleep(0.12)
    second = json.loads(bot._crash_snapshot_path().read_text())["requested_at"]
    assert second > first
    await bot.shutdown()
    assert task.done()


def test_crash_snapshot_writer_disabled_by_zero(config, monkeypatch):
    monkeypatch.setenv("OBS_CRASH_RESUME_SNAPSHOT_SECONDS", "0")
    assert _bot(config).start_crash_snapshot_writer() is None


@pytest.mark.asyncio
async def test_crash_snapshot_resumes_as_crash_with_crash_prompt(config):
    first = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=41)
    _busy(first, route, sid="sid-41")
    first._persist_state_for_route(route)
    first.write_crash_snapshot()
    await first.shutdown()  # daemon "dies" without SIGTERM: snapshot survives

    restored = _bot(config)
    await restored.initialize_runtime()
    restored._primary_bot = MagicMock()
    run = AsyncMock()
    sent = AsyncMock(return_value=[])
    counts_during_turn: list[int] = []

    async def fake_run_and_send(**kwargs):
        counts_during_turn.append(restored._crash_resume_counts.get(route, 0))
        entries = restored._collect_maintenance_entries()
        counts_during_turn.append(entries[0].crash_resume_count if entries else -1)

    run.side_effect = fake_run_and_send
    with patch.object(restored, "_send_system_html_message", sent), patch.object(restored, "_run_and_send", run):
        await restored.start_maintenance_resume()
        await asyncio.gather(*list(restored._detached_wake_tasks))
        await asyncio.sleep(0)  # let done callbacks run
    run.assert_awaited_once()
    assert "restarted unexpectedly" in run.await_args.kwargs["user_text"]
    assert "crash recovery" in sent.await_args.kwargs["html_text"]
    # While the resumed turn runs, a new snapshot carries count 1 (crash-loop guard) ...
    assert counts_during_turn == [1, 1]
    # ... and the count is cleared once the turn ends.
    assert route not in restored._crash_resume_counts
    assert not restored._crash_snapshot_path().exists()


@pytest.mark.asyncio
async def test_crash_loop_guard_skips_routes_resumed_too_often(config):
    bot = _bot(config)
    entries = [
        maint.ResumeEntry(chat_id=1, thread_id=1, session_id="fresh", crash_resume_count=0),
        maint.ResumeEntry(chat_id=1, thread_id=2, session_id="looping", crash_resume_count=2),
    ]
    maint.write_marker(
        bot._crash_snapshot_path(),
        maint.ResumeMarker(requested_at=time.time(), source="periodic", entries=entries),
    )
    seen: list[list[str]] = []

    async def fake_run(marker, *, kind):
        seen.append([e.session_id for e in marker.entries])

    with patch.object(bot, "_run_maintenance_resume", side_effect=fake_run):
        await bot.start_maintenance_resume()
    assert seen == [["fresh"]]


@pytest.mark.asyncio
async def test_maintenance_marker_wins_over_crash_snapshot(config):
    bot = _bot(config)
    now = time.time()
    maint.write_marker(
        bot._maintenance_marker_path(),
        maint.ResumeMarker(requested_at=now, source="maint", entries=[maint.ResumeEntry(chat_id=1, thread_id=1, session_id="m")]),
    )
    maint.write_marker(
        bot._crash_snapshot_path(),
        maint.ResumeMarker(requested_at=now, source="periodic", entries=[maint.ResumeEntry(chat_id=1, thread_id=2, session_id="c")]),
    )
    seen: list[tuple[str, list[str]]] = []

    async def fake_run(marker, *, kind):
        seen.append((kind, [e.session_id for e in marker.entries]))

    with patch.object(bot, "_run_maintenance_resume", side_effect=fake_run):
        await bot.start_maintenance_resume()
    assert seen == [("maintenance", ["m"])]
    # Both files were consumed before acting; neither can replay.
    assert not bot._maintenance_marker_path().exists() and not bot._crash_snapshot_path().exists()


@pytest.mark.asyncio
async def test_in_process_runtime_restart_does_not_replay_crash_snapshot(config):
    bot = _bot(config)
    maint.write_marker(
        bot._crash_snapshot_path(),
        maint.ResumeMarker(requested_at=time.time(), source="periodic", entries=[maint.ResumeEntry(chat_id=1, thread_id=2, session_id="c")]),
    )
    assert bot.start_maintenance_resume(include_crash=False) is None
    assert bot._crash_snapshot_path().exists()


# --- kill switch -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapper_killswitch_sentinel_discards_crash_snapshot(config, tmp_path, monkeypatch):
    bot = _bot(config)
    maint.write_marker(
        bot._crash_snapshot_path(),
        maint.ResumeMarker(requested_at=time.time(), source="periodic", entries=[maint.ResumeEntry(chat_id=1, thread_id=2, session_id="c")]),
    )
    sentinel = tmp_path / "obs-telegram-prod.killswitch"
    sentinel.write_text("")
    monkeypatch.setenv(maint.KILLSWITCH_SENTINEL_ENV, str(sentinel))
    assert bot.start_maintenance_resume() is None
    assert not sentinel.exists()
    assert bot._crash_snapshot_path().with_name("crash-resume.json.killswitch").exists()


def test_sigterm_handler_discards_snapshot_then_dies_with_default_term(config, monkeypatch):
    bot = _bot(config)
    bot.write_crash_snapshot()
    installed: dict[int, object] = {}
    kills: list[tuple[int, int]] = []

    def fake_signal(signum, handler):
        installed[signum] = handler

    monkeypatch.setattr(tg.signal, "signal", fake_signal)
    monkeypatch.setattr(tg.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    tg._install_kill_switch_sigterm_handler(bot)
    handler = installed[signal.SIGTERM]
    handler(signal.SIGTERM, None)
    assert not bot._crash_snapshot_path().exists()
    assert installed[signal.SIGTERM] is signal.SIG_DFL
    assert kills and kills[-1][1] == signal.SIGTERM
    # The writer does not recreate the snapshot after the kill switch.
    assert bot.write_crash_snapshot() is None


@pytest.mark.asyncio
async def test_plain_restart_discards_snapshot_and_terminates(config):
    bot = _bot(config)
    bot.write_crash_snapshot()
    exits: list[bool] = []
    bot._maintenance_exit_fn = lambda: exits.append(True)
    with patch.object(bot, "_send_system_message", AsyncMock()):
        await bot.request_plain_restart(route=TelegramRoute(chat_id=1, thread_id=None), bot=MagicMock(), source="t", delay_seconds=0)
    assert exits == [True]
    assert not bot._crash_snapshot_path().exists()
    assert not bot._maintenance_marker_path().exists()


def _update(args):
    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_message.chat_id = 67890
    update.effective_message.message_thread_id = None
    context = MagicMock()
    context.args = args
    return update, context


@pytest.mark.asyncio
async def test_restart_command_defaults_to_maintenance_and_plain_flag(config):
    bot = _bot(config)
    with patch.object(bot, "_is_authorized", return_value=True), patch.object(
        bot, "handle_maintenance_restart", AsyncMock()
    ) as maintenance, patch.object(bot, "request_plain_restart", AsyncMock()) as plain, patch.object(
        bot, "_send_system_message", AsyncMock()
    ) as usage:
        await bot.handle_restart(*_update([]))
        maintenance.assert_awaited_once()
        plain.assert_not_awaited()
        await bot.handle_restart(*_update(["plain"]))
        plain.assert_awaited_once()
        await bot.handle_restart(*_update(["now"]))
        assert "usage" in usage.await_args.kwargs["text"]
        assert maintenance.await_count == 1 and plain.await_count == 1


def test_cli_plain_runs_supervisorctl_in_new_session():
    popen = MagicMock()
    assert maint.plain_restart(popen=popen) == 0
    args, kwargs = popen.call_args
    assert args[0] == ["supervisorctl", "restart", "obs-telegram-prod"]
    assert kwargs["start_new_session"] is True
    with patch.object(maint, "plain_restart", return_value=0) as plain:
        assert maint.main(["--plain"]) == 0
    plain.assert_called_once()


def test_cli_status_shows_crash_snapshot(tmp_path: Path, capsys):
    maint.write_marker(
        maint.crash_snapshot_path(tmp_path),
        maint.ResumeMarker(requested_at=1.0, source="periodic", entries=[]),
    )
    assert maint.main(["--status", str(tmp_path)]) == 0
    assert "crash-resume.json" in capsys.readouterr().out


# --- stop that really stops (vault-u3b.65) -------------------------------------------


@pytest.mark.asyncio
async def test_stop_escalation_kills_cli_that_ignores_interrupt(config, monkeypatch):
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0.05")
    bot = _bot(config)
    state = _busy(bot, TelegramRoute(chat_id=67890, thread_id=51), sid="sid-51")
    kill = AsyncMock(return_value=True)
    monkeypatch.setattr(state.session_manager, "disconnect_idle_client", kill)
    bot._mark_stop_requested(state)
    assert state.hook_state.stop_requested_at is not None
    await asyncio.sleep(0.15)
    kill.assert_awaited_once_with(direct_kill=True)


@pytest.mark.asyncio
async def test_stop_escalation_spares_turn_that_already_ended(config, monkeypatch):
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0.05")
    bot = _bot(config)
    state = _busy(bot, TelegramRoute(chat_id=67890, thread_id=52), sid="sid-52")
    kill = AsyncMock(return_value=True)
    monkeypatch.setattr(state.session_manager, "disconnect_idle_client", kill)
    bot._mark_stop_requested(state)
    state.busy = False  # the interrupt worked
    await asyncio.sleep(0.15)
    kill.assert_not_awaited()


@pytest.mark.asyncio
async def test_agenttaskstop_arms_escalation_and_excludes_from_snapshot(config, monkeypatch):
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0")
    bot = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=53)
    state = _busy(bot, route, sid="sid-53")
    rec = _record(route, task_id="t-53", sid="sid-53")
    bot._fork_tasks_by_id[rec.task_id] = rec
    bot._fork_task_by_child_route[route] = rec.task_id
    bot._fork_task_tasks[rec.task_id] = asyncio.get_running_loop().create_future()
    state.session_manager.get_client = AsyncMock(side_effect=RuntimeError("no client"))
    monkeypatch.setattr(bot, "_resolve_task_lookup", lambda **_kw: rec)
    result = await bot._fork_task_stop(route=rec.parent_route, args={"task_id": "t-53"})
    assert "stopped" in json.dumps(result), result
    assert state.hook_state.stop_requested_at is not None
    assert bot._collect_maintenance_entries() == []


def test_stop_escalation_grace_env(monkeypatch):
    monkeypatch.delenv("OBS_STOP_KILL_GRACE_SECONDS", raising=False)
    assert tg._stop_kill_grace_seconds() == 30.0
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0")
    assert tg._stop_kill_grace_seconds() == 0.0


@pytest.mark.asyncio
@patch("obs_agent.session.SessionManager.reconnect")
@patch("obs_agent.session.SessionManager.get_client")
async def test_runner_does_not_reconnect_after_stop(mock_get_client, mock_reconnect, config):
    hook_state = HookState()
    failing_client = AsyncMock()
    failing_client.query = AsyncMock()

    async def failing_receive():
        hook_state.stop_requested_at = time.time()  # /stop arrives mid-turn
        raise ProcessError("CLI killed", exit_code=-9)
        yield  # noqa: unreachable

    failing_client.receive_response = failing_receive
    mock_get_client.return_value = failing_client
    recovery = MagicMock(content=[TextBlock(text="should not happen")], session_id="x")
    mock_reconnect.return_value = recovery
    from obs_agent.session import SessionManager

    session_mgr = SessionManager(config=config, hook_state=hook_state)
    session_mgr.recover_poisoned_session_if_needed = AsyncMock(return_value=None)
    runner = ConversationRunner(session_mgr, hook_state, config)
    events = [event async for event in runner.run("hello")]
    mock_reconnect.assert_not_called()
    assert not any(isinstance(e, TextEvent) and "should not happen" in e.text for e in events)


def test_hook_state_reset_clears_stop():
    state = HookState()
    state.stop_requested_at = 1.0
    state.reset()
    assert state.stop_requested_at is None


def test_cli_oom_score_raised_on_child_process(monkeypatch):
    from obs_agent.session import _raise_cli_oom_score

    child = subprocess.Popen(["sleep", "5"])
    try:
        client = MagicMock()
        client._transport._process.pid = child.pid
        monkeypatch.delenv("OBS_CLI_OOM_SCORE_ADJ", raising=False)
        _raise_cli_oom_score(client)
        assert Path(f"/proc/{child.pid}/oom_score_adj").read_text().strip() == "300"
    finally:
        child.kill()
        child.wait()


# --- orphaned CLIs (vault-u3b.66) ----------------------------------------------------------


CLI = "/workspace/obs/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"


def _proc(root: Path, pid: int, argv: list[str], *, ppid: int, pgid: int) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (claude spin) R {ppid} {pgid} {pgid} 0 -1\n")


def test_orphan_finder_matches_only_dead_group_sdk_clis(tmp_path: Path):
    _proc(tmp_path, 500, ["/bin/bash", "wrapper.sh"], ppid=1, pgid=500)  # live wrapper (group leader)
    _proc(tmp_path, 501, [CLI, "--output-format"], ppid=1, pgid=400)  # orphan: leader 400 gone
    _proc(tmp_path, 502, [CLI], ppid=600, pgid=500)  # live CLI of the current daemon
    _proc(tmp_path, 503, [CLI], ppid=1, pgid=500)  # ppid 1 but own (live) group
    _proc(tmp_path, 504, ["/usr/bin/claude"], ppid=1, pgid=400)  # not the SDK binary
    _proc(tmp_path, 505, [CLI], ppid=1, pgid=700)  # other live group leader
    _proc(tmp_path, 700, ["/bin/bash", "other.sh"], ppid=1, pgid=700)
    assert maint.find_orphaned_agent_clis(own_pgid=500, proc_root=tmp_path) == [501]


def test_kill_process_tree_kills_cli_and_its_tool_children(tmp_path: Path):
    _proc(tmp_path, 600, ["python"], ppid=1, pgid=500)  # daemon
    _proc(tmp_path, 601, [CLI], ppid=600, pgid=500)  # stuck CLI
    _proc(tmp_path, 602, ["/bin/bash", "-c", "sleep 999"], ppid=601, pgid=500)  # its tool shell
    _proc(tmp_path, 603, ["sleep", "999"], ppid=602, pgid=500)
    _proc(tmp_path, 604, [CLI], ppid=600, pgid=500)  # another agent's CLI
    sent: list[tuple[int, int]] = []
    killed = maint.kill_process_tree(601, proc_root=tmp_path, kill=lambda pid, sig: sent.append((pid, sig)))
    assert sorted(killed) == [601, 602, 603]
    assert all(sig == signal.SIGKILL for _, sig in sent)
    assert 600 not in killed and 604 not in killed


@pytest.mark.asyncio
async def test_stop_escalation_kills_process_tree_when_pid_known(config, monkeypatch):
    monkeypatch.setenv("OBS_STOP_KILL_GRACE_SECONDS", "0.05")
    bot = _bot(config)
    state = _busy(bot, TelegramRoute(chat_id=67890, thread_id=54), sid="sid-54")
    client = MagicMock()
    client._transport._process.pid = 424242
    state.session_manager._client = client
    monkeypatch.setattr(state.session_manager, "disconnect_idle_client", AsyncMock(return_value=True))
    trees: list[int] = []
    monkeypatch.setattr(maint, "kill_process_tree", lambda pid: trees.append(pid) or [pid])
    bot._mark_stop_requested(state)
    await asyncio.sleep(0.15)
    assert trees == [424242]


def test_orphan_reaper_terms_then_kills_survivors(tmp_path: Path):
    _proc(tmp_path, 501, [CLI], ppid=1, pgid=400)
    _proc(tmp_path, 502, [CLI], ppid=1, pgid=400)
    sent: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        if sig == signal.SIGTERM and pid == 502:
            shutil.rmtree(tmp_path / "502")  # 502 exits on TERM; 501 spins on

    pids = maint.reap_orphaned_agent_clis(own_pgid=1, proc_root=tmp_path, kill=fake_kill, sleep=lambda s: None)
    assert pids == [501, 502]
    assert (501, signal.SIGKILL) in sent and (502, signal.SIGKILL) not in sent


# --- outage inbox messages (vault-u3b.35) ----------------------------------------------


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.mark.asyncio
async def test_outage_inbox_messages_stay_unread_after_crash(config):
    first = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=61)
    state = _busy(first, route, sid="sid-61")
    rec = _record(route, task_id="t-61", sid="sid-61")
    rec.idle_ready = True
    rec.status = "completed"
    first._fork_tasks_by_id[rec.task_id] = rec
    first._fork_task_by_child_route[route] = rec.task_id
    first._register_team_worker_record(rec)
    state.busy = False
    first._persist_state_for_route(route)
    first.write_crash_snapshot()
    await first.shutdown()

    inbox = first._team_inbox_path(rec.team_name, rec.agent_name)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    inbox.write_text(
        json.dumps(
            [
                {"from": "old", "text": "long ago", "timestamp": _iso(now - 3600), "read": False},
                {"from": "peer", "text": "sent during outage", "timestamp": _iso(now - 1), "read": False},
            ]
        )
    )
    restored = _bot(config)
    await restored.initialize_runtime()
    items = json.loads(inbox.read_text())
    assert [i["read"] for i in items] == [True, False]
    assert restored._inbox_wake_floor < restored._daemon_started_at
    latest = restored._latest_unread_team_inbox_message(team_name=rec.team_name, agent_name=rec.agent_name)
    assert latest is not None and latest[2] == "sent during outage"


@pytest.mark.asyncio
async def test_kill_switch_keeps_marking_outage_messages_read(config, tmp_path, monkeypatch):
    first = _bot(config)
    route = TelegramRoute(chat_id=67890, thread_id=62)
    rec = _record(route, task_id="t-62", sid="sid-62")
    rec.idle_ready = True
    rec.status = "completed"
    first._get_state(route, topic_title="W").session_manager.set_session_id("sid-62")
    first._fork_tasks_by_id[rec.task_id] = rec
    first._fork_task_by_child_route[route] = rec.task_id
    first._register_team_worker_record(rec)
    first._persist_state_for_route(route)
    first.write_crash_snapshot()
    await first.shutdown()
    sentinel = tmp_path / "ks"
    sentinel.write_text("")
    monkeypatch.setenv(maint.KILLSWITCH_SENTINEL_ENV, str(sentinel))

    inbox = first._team_inbox_path(rec.team_name, rec.agent_name)
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(json.dumps([{"from": "peer", "text": "x", "timestamp": _iso(time.time() - 1), "read": False}]))
    restored = _bot(config)
    await restored.initialize_runtime()
    assert json.loads(inbox.read_text())[0]["read"] is True
    assert restored._inbox_wake_floor == restored._daemon_started_at
