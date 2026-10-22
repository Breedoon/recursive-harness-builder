"""Events wait layer (obs-22 C3; design-v3 §2.3; ADR-0018).

Tests never sleep. They wait on what OBS reports through its event tap (C7,
``src/obs_agent/event_tap.py``) or on rows the CLI appends to a session JSONL:

- :class:`FileTail` follows an append-only file with an inotify watch on its
  directory (so the file may appear later) and hands complete lines to a callback;
- :class:`EventLog` parses the lane's ``events.jsonl`` and resolves waiters the
  moment a matching line lands;
- :class:`JsonlTail` turns appended session-JSONL rows into ``jsonl_append`` events.

Timeouts are failure-only (defaults: 120 s per cheap-model turn, 900 s for stress
steps). A negative assertion is a check over the log, not a wait: "``turn_end``
seen, and no further ``telegram_send`` for that agent before the next
``turn_start``" (:meth:`EventLog.assert_none`).

Every blocking wait is recorded (event ts, wake ts, latency) in the run's timing
file; the numbers behind the "median ≤ 2 s, p95 ≤ 5 s" acceptance come from there.

INVARIANT(protocol): a wait resolves only on an observed event or line, never on
elapsed time; elapsed time only ever fails a wait. Breaking it brings back the
"tested agent would reply already and the test would keep waiting" fixed sleeps
Daniel asked to remove (R10).
Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Events Wait Layer#Waits
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .inotify import IN_Q_OVERFLOW, Inotify

TURN_TIMEOUT = float(os.environ.get("OBS_E2E_TURN_TIMEOUT") or 120)
STRESS_TIMEOUT = float(os.environ.get("OBS_E2E_STRESS_TIMEOUT") or 900)
EVENT_KINDS = (
    "turn_start", "turn_end", "telegram_send", "schedule_fired", "inbox_delivered",
    "session_build", "stop_cleanup", "recovery", "jsonl_append",
)


class WaitTimeout(AssertionError):
    """A wait failed: the event never arrived within its failure timeout."""


class Timing:
    """Appends wait and test timing records to ``<run>/runner/timing-<lane>.jsonl``."""

    def __init__(self, path: Path | None, lane: str = "") -> None:
        self.path = path
        self.lane = lane
        self.test: str | None = None

    def record(self, kind: str, **fields: Any) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"kind": kind, "lane": self.lane, "test": self.test, "ts": time.time(), **fields}, default=str)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class FileTail:
    """Follow one append-only file; ``on_line`` gets each complete line once."""

    def __init__(self, path: Path, on_line: Callable[[str, float], None]) -> None:
        self.path = Path(path)
        self.on_line = on_line
        self.offset = 0
        self._partial = b""
        self._inotify: Inotify | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll_task: asyncio.Task | None = None
        self.mode = "unstarted"

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._inotify = Inotify()
            self._inotify.add_watch(str(self.path.parent))
            self._loop.add_reader(self._inotify.fileno(), self._on_inotify)
            self.mode = "inotify"
        except OSError:
            # Degraded: no inotify (watch limit). Recorded in timing as mode=poll.
            self._inotify = None
            self.mode = "poll"
            self._poll_task = self._loop.create_task(self._poll())
        self.drain()

    async def _poll(self) -> None:
        while True:
            self.drain()
            await asyncio.sleep(0.2)  # obs-e2e-lint: allow (inotify-unavailable fallback)

    def _on_inotify(self) -> None:
        try:
            events = self._inotify.read() if self._inotify else []
        except OSError:
            events = [(0, IN_Q_OVERFLOW, "")]
        if any(name == self.path.name or mask & IN_Q_OVERFLOW for _wd, mask, name in events):
            self.drain()

    def drain(self) -> None:
        """Read whatever was appended since the last drain."""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
        except FileNotFoundError:
            return
        if not chunk:
            return
        self.offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        now = time.time()
        for raw in lines:
            if raw.strip():
                self.on_line(raw.decode("utf-8", "replace"), now)

    def close(self) -> None:
        if self._inotify is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._inotify.fileno())
            except Exception:
                pass
            self._inotify.close()
            self._inotify = None
        if self._poll_task is not None:
            self._poll_task.cancel()


@dataclass
class _Waiter:
    predicate: Callable[[dict], bool]
    future: asyncio.Future
    started: float
    kind: str
    min_index: int


def _matches(event: dict, kind: str | None, agent: Any, match: Any) -> bool:
    if kind is not None and event.get("kind") != kind:
        return False
    if agent is not None:
        topic_id = getattr(agent, "topic_id", None)
        if topic_id is not None:
            if event.get("topic_id") != topic_id:
                return False
        elif isinstance(agent, int):
            if event.get("topic_id") != agent:
                return False
        elif event.get("agent_name") != agent:
            return False
    if match is None:
        return True
    if callable(match):
        return bool(match(event))
    return all(event.get(key) == value for key, value in dict(match).items())


@dataclass
class EventLog:
    """The lane's event-tap log, parsed as it grows."""

    path: Path
    timing: Timing = field(default_factory=lambda: Timing(None))
    events: list[dict] = field(default_factory=list)
    cursor: int = 0  # index where "this test" starts (Lane.begin_test moves it)

    def __post_init__(self) -> None:
        self._waiters: list[_Waiter] = []
        self.inputs: list[dict] = []
        self._tail = FileTail(self.path, self._on_line)
        self.bad_lines = 0

    @property
    def mode(self) -> str:
        return self._tail.mode

    def start(self) -> None:
        self._tail.start()

    def close(self) -> None:
        self._tail.close()

    def mark(self) -> int:
        """A position to pass as ``after=``: only later events count."""
        self._tail.drain()
        return len(self.events)

    def record_input(self, *, position: int, message_id: int, chat_id: int, topic_id: int, ts: float) -> None:
        """Record the pre-send cursor after Telegram confirms that the input was sent."""
        self.inputs.append({"position": position, "message_id": message_id,
                            "chat_id": chat_id, "topic_id": topic_id, "ts": ts})

    def _on_line(self, line: str, read_at: float) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            self.bad_lines += 1
            return
        if not isinstance(event, dict):
            self.bad_lines += 1
            return
        event["_index"] = len(self.events)
        event["_read_at"] = read_at
        self.events.append(event)
        for waiter in list(self._waiters):
            if waiter.future.done():
                self._waiters.remove(waiter)
                continue
            if event["_index"] >= waiter.min_index and waiter.predicate(event):
                self._waiters.remove(waiter)
                waiter.future.set_result(event)

    def _min_index(self, after: Any) -> int:
        if after is None:
            return self.cursor
        if isinstance(after, dict) and "_index" in after:
            return int(after["_index"]) + 1
        if isinstance(after, bool):
            raise TypeError("after must be an event, an index, a float ts or None")
        if isinstance(after, int):
            return after
        if isinstance(after, float):
            for event in self.events:
                if float(event.get("ts", 0)) > after:
                    return event["_index"]
            return len(self.events)
        raise TypeError("after must be an event, an index, a float ts or None")

    def find(self, kind: str | None = None, *, agent: Any = None, match: Any = None, after: Any = None,
             before: Any = None) -> list[dict]:
        """Events already seen that match (no waiting)."""
        self._tail.drain()
        lo = self._min_index(after)
        hi = len(self.events) if before is None else self._min_index(before) - (1 if isinstance(before, dict) else 0)
        return [e for e in self.events[lo:hi] if _matches(e, kind, agent, match)]

    async def wait(self, kind: str, *, agent: Any = None, match: Any = None, timeout: float | None = None,
                   after: Any = None) -> dict:
        """Return the first matching event at or after ``after``; fail after ``timeout``."""
        timeout = TURN_TIMEOUT if timeout is None else float(timeout)
        self._tail.drain()
        min_index = self._min_index(after)
        started = time.time()
        for event in self.events[min_index:]:
            if _matches(event, kind, agent, match):
                self.timing.record("wait", event_kind=kind, event_ts=event.get("ts"), wake_ts=started,
                                   latency_s=None, blocked=False, waited_s=0.0)
                return event
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        waiter = _Waiter(lambda e: _matches(e, kind, agent, match), future, started, kind, min_index)
        self._waiters.append(waiter)
        try:
            event = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.timing.record("wait_timeout", event_kind=kind, waited_s=time.time() - started, timeout_s=timeout)
            raise WaitTimeout(
                f"no {kind} event (agent={agent!r} match={match!r}) within {timeout:.0f}s; "
                f"last events: {[(e.get('kind'), e.get('agent_name'), e.get('topic_id')) for e in self.events[-5:]]}"
            ) from None
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
        wake = time.time()
        event_ts = float(event.get("ts", wake))
        self.timing.record("wait", event_kind=kind, event_ts=event_ts, wake_ts=wake,
                           latency_s=max(0.0, wake - event_ts), blocked=True, waited_s=wake - started)
        return event

    def assert_none(self, kind: str, *, agent: Any = None, match: Any = None, after: Any = None,
                    before: Any = None) -> None:
        """Assert absence until the next input/turn, preserving explicit old-turn attribution."""
        candidates = self.find(kind, agent=agent, match=match, after=after, before=before)
        lower = self._min_index(after)
        anchor = after if isinstance(after, dict) else {}
        asserted_message = anchor.get("trigger_message_id")
        found = []
        for event in candidates:
            scope = {key: event.get(key) if event.get(key) is not None else anchor.get(key)
                     for key in ("chat_id", "topic_id")}

            def same_route(record):
                return all(record.get(key) == scope[key] for key in scope)

            reply = event.get("reply_to_message_id", event.get("reply_to_msg_id"))
            # INVARIANT(C3): a later receipt is outside the window; an explicit old-turn reply is not.
            # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Runner#Python test API
            if asserted_message is not None and reply == asserted_message and same_route(anchor):
                found.append(event)
                continue
            inputs = [record for record in self.inputs if record["position"] >= lower and same_route(record)]
            later_reply = reply is not None and any(record["message_id"] == reply for record in inputs)
            past_input = any(record["position"] <= event["_index"] for record in inputs)
            past_turn = any(record.get("kind") == "turn_start" and same_route(record) and
                            (asserted_message is None or record.get("trigger_message_id") != asserted_message)
                            for record in self.events[lower:event["_index"] + 1])
            if not (later_reply or past_input or past_turn):
                found.append(event)
        if found:
            raise AssertionError(f"unexpected {kind} event(s) for {agent!r}: {found[:3]}")


class JsonlTail:
    """Rows appended to one session JSONL, as ``jsonl_append`` events."""

    def __init__(self, path: Path, session_id: str, timing: Timing | None = None) -> None:
        self.log = EventLog(Path(path), timing=timing or Timing(None))
        self.session_id = session_id
        self.log._tail.on_line = self._on_row

    def _on_row(self, line: str, read_at: float) -> None:
        try:
            row = json.loads(line)
        except ValueError:
            return
        event = {
            "kind": "jsonl_append",
            "ts": read_at,
            "session_id": self.session_id,
            "type": row.get("type"),
            "uuid": row.get("uuid"),
            "parent_uuid": row.get("parentUuid"),
            "stop_reason": ((row.get("message") or {}).get("stop_reason") if isinstance(row.get("message"), dict) else None),
            "row": row,
        }
        self.log._on_line(json.dumps(event, default=str), read_at)

    def start(self) -> None:
        self.log.start()

    def close(self) -> None:
        self.log.close()
