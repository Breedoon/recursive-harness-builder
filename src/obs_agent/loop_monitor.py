"""Event-loop stall monitor (2026-10-21 Telegram freeze).

A heartbeat task stamps a monotonic time every ``interval`` seconds on the
asyncio loop. A daemon thread watches the stamp; when the loop has not
stamped for ``threshold`` seconds it logs one warning with the loop thread's
current stack (the culprit), and a recovery line with the stall length when the
loop resumes. Diagnostics only: it never touches the loop's work.

Disable with ``OBS_LOOP_STALL_DISABLE=1``; tune with
``OBS_LOOP_STALL_THRESHOLD_SECONDS`` (default 2.0).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

_started_loops: set[int] = set()
_lock = threading.Lock()


def _threshold_seconds() -> float:
    raw = (os.environ.get("OBS_LOOP_STALL_THRESHOLD_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else 2.0
    except ValueError:
        value = 2.0
    return max(0.2, value)


def _disabled() -> bool:
    return (os.environ.get("OBS_LOOP_STALL_DISABLE") or "").strip().lower() in ("1", "true", "yes", "on")


class LoopStallMonitor:
    def __init__(self, loop: asyncio.AbstractEventLoop, *, threshold: float, interval: float) -> None:
        self._loop = loop
        self._threshold = threshold
        self._interval = interval
        self._last_beat = time.monotonic()
        self._loop_thread_id: int | None = None
        self._stalled_since: float | None = None
        self._stop = threading.Event()
        self.stall_count = 0

    async def _heartbeat(self) -> None:
        self._loop_thread_id = threading.get_ident()
        while not self._stop.is_set():
            self._last_beat = time.monotonic()
            await asyncio.sleep(self._interval)

    def _stack_of_loop_thread(self) -> str:
        if self._loop_thread_id is None:
            return "<loop thread unknown>"
        frame = sys._current_frames().get(self._loop_thread_id)
        if frame is None:
            return "<loop thread frame unavailable>"
        return "".join(traceback.format_stack(frame, limit=25))

    def check_once(self, now: float | None = None) -> str | None:
        """One watchdog tick; returns 'stall', 'recovered' or None (for tests)."""
        now = time.monotonic() if now is None else now
        lag = now - self._last_beat
        if self._stalled_since is None:
            if lag >= self._threshold:
                self._stalled_since = self._last_beat
                self.stall_count += 1
                logger.warning(
                    "[loop_stall] event loop blocked for %.1fs (threshold %.1fs); loop thread stack:\n%s",
                    lag, self._threshold, self._stack_of_loop_thread(),
                )
                return "stall"
            return None
        if lag < self._threshold:
            total = self._last_beat - self._stalled_since
            self._stalled_since = None
            logger.warning("[loop_stall] event loop recovered after ~%.1fs blocked", total)
            return "recovered"
        return None

    def _watch(self) -> None:
        tick = min(self._interval, self._threshold / 2)
        while not self._stop.wait(tick):
            try:
                self.check_once()
            except Exception:  # never let diagnostics die loudly
                logger.debug("[loop_stall] watchdog tick failed", exc_info=True)

    def start(self) -> None:
        self._loop.create_task(self._heartbeat())
        threading.Thread(target=self._watch, name="obs-loop-stall-monitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def ensure_loop_stall_monitor() -> LoopStallMonitor | None:
    """Start one monitor per running loop; idempotent and never raises."""
    if _disabled():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    with _lock:
        if id(loop) in _started_loops:
            return None
        _started_loops.add(id(loop))
    try:
        monitor = LoopStallMonitor(loop, threshold=_threshold_seconds(), interval=0.5)
        monitor.start()
        return monitor
    except Exception:
        logger.debug("[loop_stall] could not start monitor", exc_info=True)
        return None
