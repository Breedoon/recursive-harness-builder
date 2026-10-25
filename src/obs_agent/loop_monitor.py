"""Event-loop stall monitor (2026-10-21 Telegram freeze).

A heartbeat task stamps a monotonic time every ``interval`` seconds on the
asyncio loop. A daemon thread watches the stamp; when the loop has not
stamped for ``threshold`` seconds it logs one warning with the loop thread's
current stack (the culprit), and a recovery line with the stall length when the
loop resumes. Diagnostics only: it never touches the loop's work.

obs-54: it also keeps totals and logs one ``[loop_stall_summary]`` line per
``summary`` interval (default 600 s), stalls or not, so the stall rate and the
share of time the loop was blocked can be read from the log directly.

Disable with ``OBS_LOOP_STALL_DISABLE=1``; tune with
``OBS_LOOP_STALL_THRESHOLD_SECONDS`` (default 2.0) and
``OBS_LOOP_STALL_SUMMARY_SECONDS`` (default 600; 0 turns the summary off).
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

# Bound at import so a test that patches asyncio.sleep cannot turn the heartbeat
# into a busy loop on a shared event loop.
_real_sleep = asyncio.sleep
_started_loops: set[int] = set()
_lock = threading.Lock()


def _threshold_seconds() -> float:
    raw = (os.environ.get("OBS_LOOP_STALL_THRESHOLD_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else 2.0
    except ValueError:
        value = 2.0
    return max(0.2, value)


def _summary_seconds() -> float:
    raw = (os.environ.get("OBS_LOOP_STALL_SUMMARY_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else 600.0
    except ValueError:
        value = 600.0
    return max(0.0, value)


def _disabled() -> bool:
    return (os.environ.get("OBS_LOOP_STALL_DISABLE") or "").strip().lower() in ("1", "true", "yes", "on")


class LoopStallMonitor:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold: float,
        interval: float,
        summary: float = 0.0,
    ) -> None:
        self._loop = loop
        self._threshold = threshold
        self._interval = interval
        self._summary = summary
        self._last_beat = time.monotonic()
        self._loop_thread_id: int | None = None
        self._stalled_since: float | None = None
        self._stop = threading.Event()
        self.stall_count = 0
        self.blocked_seconds = 0.0
        self.max_blocked_seconds = 0.0
        self._started_at = time.monotonic()
        self._window_started_at = self._started_at
        self._window_stalls = 0
        self._window_blocked = 0.0
        self._window_max = 0.0

    async def _heartbeat(self) -> None:
        self._loop_thread_id = threading.get_ident()
        while not self._stop.is_set():
            self._last_beat = time.monotonic()
            await _real_sleep(self._interval)

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
                self._window_stalls += 1
                logger.warning(
                    "[loop_stall] event loop blocked for %.1fs (threshold %.1fs); loop thread stack:\n%s",
                    lag, self._threshold, self._stack_of_loop_thread(),
                )
                return "stall"
            return None
        if lag < self._threshold:
            total = self._last_beat - self._stalled_since
            self._stalled_since = None
            self.blocked_seconds += total
            self.max_blocked_seconds = max(self.max_blocked_seconds, total)
            self._window_blocked += total
            self._window_max = max(self._window_max, total)
            logger.warning("[loop_stall] event loop recovered after ~%.1fs blocked", total)
            return "recovered"
        return None

    def summary_once(self, now: float | None = None) -> str | None:
        """Log and reset the window's totals once it is ``summary`` long (for tests)."""
        if self._summary <= 0:
            return None
        now = time.monotonic() if now is None else now
        window = now - self._window_started_at
        if window < self._summary:
            return None
        share = 100.0 * self._window_blocked / window if window > 0 else 0.0
        line = (
            f"[loop_stall_summary] last {window:.0f}s: stalls={self._window_stalls} "
            f"blocked={self._window_blocked:.1f}s ({share:.1f}%) max={self._window_max:.1f}s; "
            f"since monitor start {now - self._started_at:.0f}s ago: stalls={self.stall_count} "
            f"blocked={self.blocked_seconds:.1f}s max={self.max_blocked_seconds:.1f}s"
            + (" (a stall is in progress)" if self._stalled_since is not None else "")
        )
        logger.log(logging.WARNING if share >= 5.0 else logging.INFO, line)
        self._window_started_at = now
        self._window_stalls = 0
        self._window_blocked = 0.0
        self._window_max = 0.0
        return line

    def _watch(self) -> None:
        tick = min(self._interval, self._threshold / 2)
        while not self._stop.wait(tick):
            try:
                self.check_once()
                self.summary_once()
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
        monitor = LoopStallMonitor(
            loop, threshold=_threshold_seconds(), interval=0.5, summary=_summary_seconds(),
        )
        monitor.start()
        return monitor
    except Exception:
        logger.debug("[loop_stall] could not start monitor", exc_info=True)
        return None
