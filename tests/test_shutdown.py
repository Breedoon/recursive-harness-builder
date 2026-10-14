"""OBS-only shutdown, durable one-shot resume, and host recovery suppression.

No production process, Telegram poller, host stop, or real Supervisor command is
invoked. Daemon turns and process control are mocked; host recovery uses a fake
Docker executable and private temporary paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import maintenance_restart as maint
from obs_agent.queueing import QueuedMessage
from obs_agent.telegram import TelegramRoute
from tests.test_maintenance_restart import _bot, _busy_child, _record


@pytest.fixture(autouse=True)
def isolated_shutdown_sentinel(monkeypatch, tmp_path):
    monkeypatch.setenv(maint.SHUTDOWN_SENTINEL_ENV, str(tmp_path / "shutdown-latch"))
    monkeypatch.setenv(maint.KILLSWITCH_SENTINEL_ENV, str(tmp_path / "kill-switch"))


def test_shutdown_marker_keeps_original_timestamp_and_consumes_once(tmp_path):
    stamp = time.time() - 30 * 86400
    path = maint.shutdown_marker_path(tmp_path)
    marker = maint.ResumeMarker(stamp, "telegram:/shutdown_autoresume", [maint.ResumeEntry(1, 2, "sid")])
    with patch.object(maint.os, "fsync", wraps=maint.os.fsync) as fsync:
        maint.write_marker(path, marker)
    assert fsync.call_count == 2
    assert maint.peek_requested_at(path, max_age_seconds=None) == stamp
    assert maint.peek_marker(path, max_age_seconds=None).requested_at == stamp
    resumed, reason = maint.consume_marker(path, max_age_seconds=None)
    assert reason == "ok" and resumed.requested_at == stamp
    assert maint.consume_marker(path, max_age_seconds=None) == (None, "no_marker")
    # No global relaxation: the normal maintenance and crash reader still expires it.
    maint.write_marker(path, marker)
    assert maint.consume_marker(path)[0] is None


@pytest.mark.parametrize("patch_data", [
    {"version": "broken"}, {"version": 99}, {"requested_at": "NaN"},
    {"requested_at": "Infinity"}, {"requested_at": time.time() + 86400},
])
def test_durable_marker_still_rejects_invalid_or_future_data(tmp_path, patch_data):
    path = maint.shutdown_marker_path(tmp_path)
    raw = {"version": 1, "requested_at": time.time(), "source": "test", "entries": []}
    raw.update(patch_data)
    path.write_text(json.dumps(raw))
    assert maint.peek_marker(path, max_age_seconds=None) is None
    assert maint.consume_marker(path, max_age_seconds=None)[0] is None
    assert not path.exists()


@pytest.mark.asyncio
async def test_autoresume_shutdown_stages_before_supervisor_stop(config):
    bot = _bot(config)
    route = TelegramRoute(67890, 41)
    state = _busy_child(bot, route, sid="sid-41")
    state.pending_messages = [QueuedMessage("queued", telegram_message_id=7)]
    stopped_route = TelegramRoute(67890, 42)
    stopped = _busy_child(bot, stopped_route, sid="sid-stopped")
    stopped.hook_state.stop_requested_at = time.time()
    idle = bot._get_state(TelegramRoute(67890, 43))
    idle.session_manager.set_session_id("idle")
    bot.write_crash_snapshot()
    maint.write_marker(bot._maintenance_marker_path(), maint.ResumeMarker(time.time(), "old", []))
    ordering = []

    def stop():
        ordering.append("stop")
        assert maint.shutdown_sentinel_path(bot._state_dir()).exists()
        assert bot._shutdown_marker_path().exists()
        assert not bot._maintenance_marker_path().exists()
        assert not bot._crash_snapshot_path().exists()

    with patch.object(maint, "stop_daemon", side_effect=stop), patch.object(
        bot, "_send_system_message", AsyncMock(side_effect=lambda **kw: ordering.append("notice")),
    ):
        count = await bot.request_shutdown(route=route, bot=MagicMock(), autoresume=True, source="test", delay_seconds=0)
        assert await bot.request_shutdown(route=route, bot=MagicMock(), autoresume=True, source="again", delay_seconds=0) == 0
    assert count == 1 and ordering == ["notice", "stop"]
    marker = maint.peek_marker(bot._shutdown_marker_path(), max_age_seconds=None)
    assert [(entry.session_id, entry.queued[0].text) for entry in marker.entries] == [("sid-41", "queued")]
    assert bot.write_crash_snapshot() is None
    await bot.shutdown()


@pytest.mark.asyncio
async def test_plain_shutdown_discards_every_resume_file(config):
    bot = _bot(config)
    route = TelegramRoute(67890, 51)
    _busy_child(bot, route, sid="sid")
    marker = maint.ResumeMarker(time.time(), "old", [maint.ResumeEntry(67890, 51, "sid")])
    for path in (bot._maintenance_marker_path(), bot._shutdown_marker_path(), bot._crash_snapshot_path()):
        maint.write_marker(path, marker)
    with patch.object(maint, "stop_daemon") as stop, patch.object(bot, "_send_system_message", AsyncMock()) as notice:
        assert await bot.request_shutdown(route=route, bot=MagicMock(), autoresume=False, source="test", delay_seconds=0) == 0
    stop.assert_called_once()
    assert "No running agents will auto-resume" in notice.await_args.kwargs["text"]
    assert not bot._maintenance_marker_path().exists()
    assert not bot._shutdown_marker_path().exists()
    assert not bot._crash_snapshot_path().exists()
    assert maint.shutdown_sentinel_path(bot._state_dir()).exists()
    assert bot.start_maintenance_resume() is None
    await bot.shutdown()


@pytest.mark.asyncio
async def test_preparation_failure_does_not_stop_and_leaves_no_checkpoint(config):
    bot = _bot(config)
    route = TelegramRoute(67890, 61)
    _busy_child(bot, route, sid="sid")
    with patch.object(maint, "write_marker", side_effect=OSError("disk full")), patch.object(
        maint, "stop_daemon",
    ) as stop, patch.object(bot, "_send_system_message", AsyncMock()) as notice:
        await bot.request_shutdown(route=route, bot=MagicMock(), autoresume=True, source="test", delay_seconds=0)
    stop.assert_not_called()
    assert "still running" in notice.await_args.kwargs["text"]
    assert not maint.shutdown_sentinel_path(bot._state_dir()).exists()
    assert not bot._shutdown_marker_path().exists()
    assert not bot._maintenance_restart_requested
    assert not bot._crash_snapshot_disabled
    await bot.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    FileNotFoundError("supervisorctl"),
    subprocess.CalledProcessError(4, ["supervisorctl", "stop", "obs-telegram-prod"]),
    subprocess.TimeoutExpired(["supervisorctl", "stop", "obs-telegram-prod"], 15),
])
async def test_stop_failure_unstages_resume(config, error):
    bot = _bot(config)
    route = TelegramRoute(67890, 62)
    _busy_child(bot, route, sid="sid")
    with patch.object(maint, "stop_daemon", side_effect=error), patch.object(
        bot, "_send_system_message", AsyncMock(),
    ) as notice:
        await bot.request_shutdown(route=route, bot=MagicMock(), autoresume=True, source="test", delay_seconds=0)
    assert "shutdown failed" in notice.await_args.kwargs["text"]
    assert "no auto-resume is staged" in notice.await_args.kwargs["text"]
    assert not bot._crash_snapshot_disabled
    assert bot.write_crash_snapshot() is not None
    assert not bot._shutdown_marker_path().exists()
    assert not maint.shutdown_sentinel_path(bot._state_dir()).exists()
    assert not bot._maintenance_restart_requested
    await bot.shutdown()


@pytest.mark.asyncio
async def test_long_shutdown_restores_queued_child_and_callback_despite_kill_switch(config):
    first = _bot(config)
    parent = TelegramRoute(67890, None)
    first._get_state(parent)
    child = TelegramRoute(67890, 71)
    state = _busy_child(first, child, sid="sid-71")
    state.pending_messages = [QueuedMessage("queued request")]
    rec = _record(child, task_id="t-71", sid="sid-71")
    first._fork_tasks_by_id[rec.task_id] = rec
    first._fork_task_by_child_route[child] = rec.task_id
    first._register_team_worker_record(rec)
    first._persist_state_for_route(parent)
    first._persist_state_for_route(child)
    stamp = time.time() - 7 * 86400
    maint.write_marker(first._shutdown_marker_path(), maint.ResumeMarker(
        stamp, "telegram:/shutdown_autoresume", first._collect_maintenance_entries(), inflight_task_ids=[rec.task_id],
    ))
    await first.shutdown()
    Path(os.environ[maint.KILLSWITCH_SENTINEL_ENV]).touch()
    restored = _bot(config)
    await restored.initialize_runtime()
    restored._primary_bot = MagicMock()
    assert restored._outage_inbox_floor == stamp - 60
    assert restored._last_outage_inflight_scope() == ({"t-71"}, stamp)
    record = restored._fork_tasks_by_id["t-71"]
    assert record.callback_owed
    with patch.object(restored, "_send_system_html_message", AsyncMock(return_value=[])), patch.object(
        restored, "_schedule_fork_task", AsyncMock(),
    ) as schedule:
        await restored.start_maintenance_resume()
    schedule.assert_awaited_once()
    assert record.emit_parent_callback and not record.callback_owed
    assert "shut down with auto-resume staged" in record.prompt
    assert "before repeating" in record.prompt
    assert [item.text for item in restored._get_state(child).pending_messages] == ["queued request"]
    assert not restored._shutdown_marker_path().exists()
    assert restored.start_maintenance_resume() is None
    await restored.shutdown()


@pytest.mark.asyncio
async def test_shutdown_without_session_carries_original_request(config):
    bot = _bot(config)
    state = bot._get_state(TelegramRoute(67890, 81))
    state.busy = True
    state.inflight_user_text = "original request before first SDK response"
    with patch.object(maint, "stop_daemon"), patch.object(bot, "_send_system_message", AsyncMock()):
        await bot.request_shutdown(route=state.route, bot=MagicMock(), autoresume=True, source="test", delay_seconds=0)
    marker = maint.peek_marker(bot._shutdown_marker_path(), max_age_seconds=None)
    entry = marker.entries[0]
    assert entry.session_id is None and entry.inflight_prompt == state.inflight_user_text
    prompt = maint.build_resume_prompt(requested_at=marker.requested_at, kind="shutdown", original_prompt=entry.inflight_prompt)
    assert "fresh session" in prompt and state.inflight_user_text in prompt
    await bot.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("autoresume", [False, True])
async def test_shutdown_handler_authorization_and_args(config, autoresume):
    bot = _bot(config)
    handler = bot.handle_shutdown_autoresume if autoresume else bot.handle_shutdown
    update = SimpleNamespace(effective_user=SimpleNamespace(id=99999), effective_message=MagicMock())
    context = SimpleNamespace(args=[], bot=MagicMock())
    with patch.object(bot, "request_shutdown", AsyncMock()) as request, patch.object(bot, "_send_system_message", AsyncMock()) as notice:
        await handler(update, context)
        request.assert_not_awaited()
        notice.assert_not_awaited()
        update.effective_user.id = 12345
        context.args = ["host"]
        with patch.object(bot, "_route_for_message", return_value=TelegramRoute(67890, 91)):
            await handler(update, context)
            request.assert_not_awaited()
            assert "usage:" in notice.await_args.kwargs["text"]
            context.args = []
            await handler(update, context)
        assert request.await_args.kwargs["autoresume"] is autoresume
    await bot.shutdown()


def test_supervisor_stop_client_is_detached_and_checked():
    run = MagicMock()
    maint.stop_daemon(run=run)
    assert run.call_args.args[0] == ["supervisorctl", "stop", "obs-telegram-prod"]
    assert run.call_args.kwargs["start_new_session"] is True
    assert run.call_args.kwargs["check"] is True
    assert run.call_args.kwargs["timeout"] == 15


def test_supervisor_stop_nonzero_is_not_treated_as_success(tmp_path):
    executable = tmp_path / "supervisorctl"
    executable.write_text("#!/bin/sh\nexit 4\n")
    executable.chmod(0o755)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    def run(*args, **kwargs):
        return subprocess.run(*args, **kwargs, env=env)
    with pytest.raises(subprocess.CalledProcessError) as error:
        maint.stop_daemon(run=run)
    assert error.value.returncode == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["finished", "stopped", "new_turn", "new_queue"])
async def test_final_checkpoint_refreshed_after_notice_without_admission_gap(config, change):
    bot = _bot(config)
    route = TelegramRoute(67890, 92)
    state = _busy_child(bot, route, sid="sid-92")
    after_yield = []
    initial_stamp = []

    async def notice(**kwargs):
        if not initial_stamp:
            initial_stamp.append(maint.peek_marker(bot._shutdown_marker_path(), max_age_seconds=None).requested_at)
        if change == "finished":
            state.busy = False
            state.hook_state.execution_active = False
        elif change == "stopped":
            state.hook_state.stop_requested_at = time.time()
        elif change == "new_turn":
            _busy_child(bot, TelegramRoute(67890, 93), sid="sid-93")
        elif change == "new_queue":
            state.pending_messages.append(QueuedMessage("arrived during notice"))
        # Any accidental await between the final checkpoint and stop would
        # admit this pending callback before stop's synchronous inspection.
        asyncio.get_running_loop().call_soon(lambda: after_yield.append(True))

    def stop():
        assert after_yield == []
        marker = maint.peek_marker(bot._shutdown_marker_path(), max_age_seconds=None)
        assert marker.requested_at == initial_stamp[0]
        if change in {"finished", "stopped"}:
            assert marker.entries == []
        elif change == "new_turn":
            assert {entry.session_id for entry in marker.entries} == {"sid-92", "sid-93"}
        else:
            assert marker.entries[0].queued[0].text == "arrived during notice"

    with patch.object(bot, "_send_system_message", notice), patch.object(maint, "stop_daemon", side_effect=stop) as stop_mock:
        count = await bot.request_shutdown(route=route, bot=MagicMock(), autoresume=True, source="test", delay_seconds=0)
    stop_mock.assert_called_once()
    assert count == (0 if change in {"finished", "stopped"} else 2 if change == "new_turn" else 1)
    await bot.shutdown()


def _recovery_run(tmp_path, *, latch, stopped=True, arm_during_status=False):
    source = Path(__file__).resolve().parents[1] / "deploy" / "ensure-obs-test-running.sh"
    script = tmp_path / "recovery.sh"
    script.write_text(source.read_text().replace("/run/obs-test-container-recovery.disabled", str(tmp_path / "disabled")))
    docker = tmp_path / "docker"
    docker.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$CALL_LOG"
if [ "$1" = inspect ]; then
  echo 'running|2026-10-14T00:00:00Z|obs-test|app'
elif [ "$3" = supervisorctl ] && [ "$4" = status ]; then
  if [ "$ARM_DURING_STATUS" = 1 ]; then touch "$OBS_TELEGRAM_PROD_SHUTDOWN_FILE"; fi
  echo "obs-telegram-prod $SUPERVISOR_STATE"
fi
""")
    docker.chmod(0o755)
    shutdown = tmp_path / "shutdown"
    if latch:
        shutdown.touch()
    log = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CALL_LOG=str(log),
               OBS_TELEGRAM_PROD_SHUTDOWN_FILE=str(shutdown),
               OBS_TELEGRAM_PROD_RECOVERY_DISABLE_FILE=str(tmp_path / "poller-disabled"),
               SUPERVISOR_STATE="STOPPED" if stopped else "RUNNING", ARM_DURING_STATUS="1" if arm_during_status else "0")
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True, text=True)
    return log.read_text()


@pytest.mark.parametrize("latch", [False, True])
def test_host_timer_respects_shutdown_latch(tmp_path, latch):
    calls = _recovery_run(tmp_path, latch=latch)
    assert ("supervisorctl start obs-telegram-prod" in calls) is not latch


def test_host_timer_rechecks_latch_after_observing_stopped(tmp_path):
    calls = _recovery_run(tmp_path, latch=False, arm_during_status=True)
    assert "supervisorctl start" not in calls


def test_host_timer_does_not_restart_running_obs(tmp_path):
    calls = _recovery_run(tmp_path, latch=False, stopped=False)
    assert "supervisorctl start" not in calls
