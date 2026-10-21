"""2026-10-21 Telegram freeze: JSONL analysis memoization + loop-stall monitor."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import obs_agent.jsonl_health as jh
from obs_agent.loop_monitor import LoopStallMonitor


def _write_session(path: Path, n: int) -> None:
    rows = []
    parent = None
    for i in range(n):
        uid = f"u{i}"
        role = "user" if i % 2 == 0 else "assistant"
        obj = {"type": role, "uuid": uid, "parentUuid": parent, "sessionId": "s",
               "message": {"role": role, "content": [{"type": "text", "text": f"t{i}"}],
                           **({"stop_reason": "end_turn"} if role == "assistant" else {})}}
        rows.append(json.dumps(obj))
        parent = uid
    path.write_text("\n".join(rows) + "\n")


def test_analysis_is_memoized_until_file_changes(tmp_path, monkeypatch):
    jh.clear_analysis_cache()
    path = tmp_path / "s.jsonl"
    _write_session(path, 6)
    calls = []
    real = jh._analyze_jsonl_path_uncached
    monkeypatch.setattr(jh, "_analyze_jsonl_path_uncached",
                        lambda **kw: calls.append(1) or real(**kw))
    first = jh.analyze_jsonl_path(path=path, session_id="s")
    second = jh.analyze_jsonl_path(path=path, session_id="s")
    assert second is first and len(calls) == 1
    # an append changes size/mtime -> re-analyzed, and the new row is visible
    time.sleep(0.01)
    with path.open("a") as fh:
        fh.write(json.dumps({"type": "user", "uuid": "u6", "parentUuid": "u5", "sessionId": "s",
                             "message": {"role": "user", "content": "x"}}) + "\n")
    third = jh.analyze_jsonl_path(path=path, session_id="s")
    assert len(calls) == 2 and third.has_uuid("u6") and not first.has_uuid("u6")


def test_branch_uuid_is_part_of_the_key(tmp_path):
    jh.clear_analysis_cache()
    path = tmp_path / "s.jsonl"
    _write_session(path, 6)
    a = jh.analyze_jsonl_path(path=path, session_id="s")
    b = jh.analyze_jsonl_path(path=path, session_id="s", branch_uuid="u3")
    assert a is not b


def test_monitor_reports_a_blocked_loop_with_stack(caplog):
    caplog.set_level(logging.WARNING, logger="obs_agent.loop_monitor")

    async def main():
        mon = LoopStallMonitor(asyncio.get_running_loop(), threshold=0.3, interval=0.05)
        mon.start()
        await asyncio.sleep(0.15)
        time.sleep(0.8)  # block the loop: the bug class
        await asyncio.sleep(0.3)
        mon.stop()
        return mon

    mon = asyncio.run(main())
    assert mon.stall_count == 1
    text = caplog.text
    assert "event loop blocked" in text and "time.sleep" in text  # culprit stack captured
    assert "recovered after" in text


def test_monitor_quiet_when_loop_is_healthy(caplog):
    caplog.set_level(logging.WARNING, logger="obs_agent.loop_monitor")

    async def main():
        mon = LoopStallMonitor(asyncio.get_running_loop(), threshold=0.3, interval=0.05)
        mon.start()
        await asyncio.sleep(0.6)
        mon.stop()
        return mon

    assert asyncio.run(main()).stall_count == 0
    assert "loop_stall" not in caplog.text


def test_heartbeat_ignores_patched_asyncio_sleep():
    # A shared test loop plus a test that patches asyncio.sleep must not turn the
    # heartbeat into a busy loop (that hung tests/test_telegram.py once).
    import obs_agent.loop_monitor as lm
    from unittest.mock import AsyncMock, patch

    real = lm._real_sleep
    with patch("asyncio.sleep", new=AsyncMock()):
        assert lm._real_sleep is real and lm._real_sleep is not asyncio.sleep
