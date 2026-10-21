"""Power-cut resume defects from the 2026-10-21 outages (vault-9tij, vault-qjit).

* vault-9tij: a 12.6-min cut lost all 12 mid-turn routes because the crash
  snapshot's age was measured after Telegram finally connected (boot + Wi-Fi
  wait counted). Age is now measured to this process's start.
* vault-qjit: a transcript ending in a torn record (NUL run) made replay raise
  JSONDecodeError on every resume, and auto-delivery retried every ~3 s
  (about 900 errors). Torn final records are treated as end of file, and
  failing auto-delivery backs off with one visible error per streak.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import maintenance_restart as maint
from obs_agent.queueing import QueuedMessage
from obs_agent.telegram import TelegramBot, TelegramRoute, _RunOutcome


# --- vault-9tij -------------------------------------------------------------

def _write_snapshot(path: Path, requested_at: float) -> None:
    path.write_text(json.dumps({
        "version": maint.MARKER_VERSION, "requested_at": requested_at,
        "source": "crash", "entries": [], "inflight_task_ids": [],
    }), encoding="utf-8")
    os.utime(path, (requested_at, requested_at))


def test_outage_age_is_measured_to_process_start_not_telegram_connect(tmp_path, monkeypatch):
    # Real timeline: last snapshot 14:03:29, OBS process start 14:16:05,
    # Telegram up and resume step 14:18:55 -> 925 s by the old measure.
    last_write = 1_000_000.0
    process_start = last_write + 756.0
    resume_step = last_write + 925.0
    snapshot = tmp_path / "crash-resume.json"
    _write_snapshot(snapshot, last_write)
    monkeypatch.setattr(maint.time, "time", lambda: resume_step)
    monkeypatch.setattr(maint, "_PROCESS_STARTED_AT", process_start)
    marker, reason = maint.consume_marker(snapshot, max_age_seconds=15 * 60)
    assert reason == "ok" and marker is not None


def test_outage_longer_than_window_still_resumes_nothing(tmp_path, monkeypatch):
    last_write = 1_000_000.0
    snapshot = tmp_path / "crash-resume.json"
    _write_snapshot(snapshot, last_write)
    monkeypatch.setattr(maint.time, "time", lambda: last_write + 1300.0)
    monkeypatch.setattr(maint, "_PROCESS_STARTED_AT", last_write + 1000.0)
    marker, reason = maint.consume_marker(snapshot, max_age_seconds=15 * 60)
    assert marker is None and reason.startswith("stale:")


def test_files_written_by_this_process_use_real_now(tmp_path, monkeypatch):
    start = 1_000_000.0
    snapshot = tmp_path / "crash-resume.json"
    _write_snapshot(snapshot, start + 10.0)  # written after this process started
    monkeypatch.setattr(maint.time, "time", lambda: start + 2000.0)
    monkeypatch.setattr(maint, "_PROCESS_STARTED_AT", start)
    marker, reason = maint.consume_marker(snapshot, max_age_seconds=15 * 60)
    assert marker is None and reason.startswith("stale:")


# --- vault-qjit: torn final record ------------------------------------------

def _healthy_session(path: Path) -> list[str]:
    rows = [
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "s",
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "s",
         "message": {"id": "m1", "role": "assistant", "model": "claude",
                     "content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn"}},
    ]
    raw = [json.dumps(row) for row in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(raw) + "\n", encoding="utf-8")
    return raw


@pytest.mark.parametrize("tail", [b"\x00" * 1102, b'{"type":"assistant","uuid":"a2","mess'])
def test_replay_treats_torn_final_record_as_eof(tmp_path, monkeypatch, tail):
    from obs_agent.jsonl_replay import prepare_session_replay

    monkeypatch.setenv("OBS_SESSION_REPLAY_PROVENANCE_DIR", str(tmp_path / "prov"))
    monkeypatch.setenv("OBS_JSONL_TORN_BACKUP_DIR", str(tmp_path / "torn"))
    source = tmp_path / "proj" / "s.jsonl"
    raw = _healthy_session(source)
    clean = source.read_bytes()
    with source.open("ab") as handle:
        handle.write(tail)
    result = prepare_session_replay(session_id="s", cwd=tmp_path, source_path=source)
    assert result is not None
    assert source.read_bytes() == clean  # torn tail cut, healthy rows untouched
    backups = list((tmp_path / "torn").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == tail
    assert source.read_text(encoding="utf-8").splitlines() == raw


def test_replay_still_raises_on_mid_file_damage(tmp_path, monkeypatch):
    from obs_agent.jsonl_replay import prepare_session_replay

    monkeypatch.setenv("OBS_SESSION_REPLAY_PROVENANCE_DIR", str(tmp_path / "prov"))
    monkeypatch.setenv("OBS_JSONL_TORN_BACKUP_DIR", str(tmp_path / "torn"))
    source = tmp_path / "proj" / "s.jsonl"
    raw = _healthy_session(source)
    source.write_text(raw[0] + "\n{broken\n" + raw[1] + "\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        prepare_session_replay(session_id="s", cwd=tmp_path, source_path=source)


# --- vault-qjit: bounded auto-delivery retries -------------------------------

@pytest.mark.asyncio
async def test_failing_auto_delivery_backs_off_and_posts_one_error(config):
    bot = TelegramBot(config, fragment_gap=0.05, enable_background_poller=False)
    route = TelegramRoute(chat_id=-1009876543, thread_id=107104)
    state = bot._get_state(route, topic_title="General - writer")
    fake = MagicMock()
    fake.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    state.last_bot = fake
    calls: list[bool] = []

    async def _failing_run(*, state, quiet_error=False, **kwargs):
        calls.append(quiet_error)
        return _RunOutcome(assistant_text="", failed=True, error="JSONDecodeError")

    state.pending_messages = [MagicMock(spec=QueuedMessage)]
    with patch.object(bot, "_run_and_send", _failing_run):
        for _ in range(20):  # 20 poll ticks in quick succession
            await bot._poll_background_queues_once()
            deadline = time.monotonic() + 1
            while bot._detached_wake_tasks and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
    assert len(calls) == 1, "backoff must stop tight retry loops"
    assert calls == [False]  # first failure shows its error
    failures, _next = bot._auto_delivery_backoff[route]
    assert failures == 1
    # A later retry after the backoff is quiet, and success clears the streak.
    bot._auto_delivery_backoff[route] = (1, 0.0)
    with patch.object(bot, "_run_and_send", _failing_run):
        await bot._poll_background_queues_once()
        while bot._detached_wake_tasks:
            await asyncio.sleep(0.01)
    assert calls == [False, True]
    bot._record_auto_delivery_outcome(route, failed=False)
    assert route not in bot._auto_delivery_backoff
    await bot.shutdown()
