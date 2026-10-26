"""Content-free local-model request lifecycle logging.

One immutable row per transmission attempt (retries included). Records
lifecycle timing, identity, usage and outcome — never prompt/response
bodies, raw content or credentials. Fail-open: logging failures never
block, delay or alter the proxied request.

Design contract: Local GPU Budgeting Plan §C (vault, bead vault-gsku).
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import re
import threading
import time
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap identity extraction (regex, no full XML parse on the hot path)
# ---------------------------------------------------------------------------
_AGENT_NAME_RE = re.compile(
    r"<team_context>\s*(?:.*?)"
    r"<agent_name>([^<]+)</agent_name>",
    re.DOTALL,
)
_ROOT_TEAM_RE = re.compile(
    r"<root_team_key>([^<]+)</root_team_key>"
)
# Lineage: all agent_name attrs on obs-node elements, in document order.
_LINEAGE_NODE_RE = re.compile(
    r'<obs-node\b[^>]*\bagent_name="([^"]*)"'
)
_OBS_BOOTSTRAP_RE = re.compile(
    r"(?s)(<obs-bootstrap\b[^>]*>.*?</obs-bootstrap>)"
)


def extract_identity_from_bootstrap(text: str) -> dict[str, Any] | None:
    """Extract safe identity fields from an obs-bootstrap XML block.

    Returns ``{agent_name, root_team_key, lineage}`` or None.
    Never logs bodies; only reads the bootstrap XML structure already
    present in user messages. Does not infer a whole trunk from a child.
    """
    match = _OBS_BOOTSTRAP_RE.search(text)
    if match is None:
        return None
    xml = match.group(1)
    agent = _AGENT_NAME_RE.search(xml)
    root = _ROOT_TEAM_RE.search(xml)
    lineage = _LINEAGE_NODE_RE.findall(xml)
    if not agent and not root:
        return None
    return {
        "agent_name": agent.group(1).strip() if agent else None,
        "root_team_key": root.group(1).strip() if root else None,
        "lineage": lineage or None,
    }


def extract_identity_from_body(body: dict) -> dict[str, Any] | None:
    """Scan user messages for obs-bootstrap identity, last one wins.

    Only the lightweight regex extraction is used — no full XML parse,
    no body logging.
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return None
    result = None
    for msg in reversed(msgs):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        texts.append(t)
        for t in texts:
            if "<obs-bootstrap" not in t:
                continue
            identity = extract_identity_from_bootstrap(t)
            if identity is not None:
                return identity
    return result


# ---------------------------------------------------------------------------
# Session identity cache (thread-safe, bounded, no disk)
# ---------------------------------------------------------------------------
_MAX_SESSION_CACHE = 2000
_SESSION_CACHE_TTL = 86400  # 24h


class _SessionIdentityCache:
    """Map session_id → identity dict, with LRU eviction."""

    def __init__(self, max_size: int = _MAX_SESSION_CACHE,
                 ttl: float = _SESSION_CACHE_TTL):
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, dict]] = {}
        self._max = max_size
        self._ttl = ttl

    def get(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        with self._lock:
            entry = self._store.get(session_id)
            if entry is None:
                return None
            ts, identity = entry
            if time.time() - ts > self._ttl:
                del self._store[session_id]
                return None
            # refresh
            self._store[session_id] = (time.time(), identity)
            return identity

    def put(self, session_id: str, identity: dict[str, Any]) -> None:
        if not session_id or not identity:
            return
        with self._lock:
            self._store[session_id] = (time.time(), identity)
            if len(self._store) > self._max:
                oldest = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest]

    def size(self) -> int:
        with self._lock:
            return len(self._store)


_identity_cache = _SessionIdentityCache()


# ---------------------------------------------------------------------------
# Request lifecycle row builder
# ---------------------------------------------------------------------------


def new_lifecycle_request_id(session_id: str = "") -> str:
    """Generate a unique request id, even when the body logger is off."""
    now = time.time()
    stamp = (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
             + f".{int((now % 1) * 1e6):06d}Z")
    sid = (session_id or "nosession")[:8]
    return f"lc-{stamp}-{os.getpid()}-{next(_id_seq):06d}-{sid}"


_id_seq = itertools.count(1)


class LifecycleRow:
    """Accumulate timing and metadata for one local-route request.

    All timing uses both wall (``time.time()``) and monotonic
    (``time.monotonic()``) clocks. Wall provides sortable timestamps;
    monotonic provides reliable durations even across NTP adjustments.

    Usage::

        row = LifecycleRow(session_id, model, ...)
        row.mark_hold_start()
        row.mark_hold_end()
        row.mark_upstream_send()
        row.mark_headers(status)
        row.mark_first_event()
        row.mark_first_delta()
        row.mark_terminal(outcome, usage=...)
        record = row.to_record()
    """

    def __init__(
        self,
        *,
        request_id: str,
        session_id: str,
        model: str,
        route: str = "local",
        client_kind: str = "other",
        max_tokens: int | None = None,
        stream: bool = False,
    ):
        self.request_id = request_id
        self.session_id = session_id
        self.model = model
        self.route = route
        self.client_kind = client_kind
        self.max_tokens = max_tokens
        self.stream = stream

        # Wall + monotonic at receive
        self.t_receive_wall = time.time()
        self.t_receive_mono = time.monotonic()

        # Optional lifecycle points (wall, mono)
        self.t_hold_start: tuple[float, float] | None = None
        self.t_hold_end: tuple[float, float] | None = None
        self.t_upstream_send: tuple[float, float] | None = None
        self.t_headers: tuple[float, float] | None = None
        self.t_first_event: tuple[float, float] | None = None
        self.t_first_delta: tuple[float, float] | None = None
        self.t_terminal: tuple[float, float] | None = None

        self.http_status: int | None = None
        self.outcome: str | None = None  # success, error, cancelled, client_gone, timeout
        self.error_class: str | None = None
        self.error_message: str | None = None

        self.usage: dict[str, int] | None = None

        # Identity (filled from bootstrap or session cache)
        self.identity: dict[str, Any] | None = None
        self.identity_source: str = "unknown"  # bootstrap, session_cache, unknown

    def _now(self) -> tuple[float, float]:
        return (time.time(), time.monotonic())

    def mark_hold_start(self) -> None:
        self.t_hold_start = self._now()

    def mark_hold_end(self) -> None:
        self.t_hold_end = self._now()

    def mark_upstream_send(self) -> None:
        self.t_upstream_send = self._now()

    def mark_headers(self, http_status: int) -> None:
        self.t_headers = self._now()
        self.http_status = http_status

    def mark_first_event(self) -> None:
        if self.t_first_event is None:
            self.t_first_event = self._now()

    def mark_first_delta(self) -> None:
        if self.t_first_delta is None:
            self.t_first_delta = self._now()

    def mark_terminal(
        self,
        outcome: str,
        *,
        error_class: str | None = None,
        error_message: str | None = None,
        usage: dict | None = None,
        http_status: int | None = None,
    ) -> None:
        self.t_terminal = self._now()
        self.outcome = outcome
        self.error_class = error_class
        if error_message:
            self.error_message = error_message[:500]
        if usage:
            self.usage = {
                "input_tokens": usage.get("input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
        if http_status is not None:
            self.http_status = http_status

    def resolve_identity(self, body: dict | None = None) -> None:
        """Try bootstrap extraction, then session cache, then mark unknown."""
        if body is not None:
            identity = extract_identity_from_body(body)
            if identity is not None:
                self.identity = identity
                self.identity_source = "bootstrap"
                _identity_cache.put(self.session_id, identity)
                return

        cached = _identity_cache.get(self.session_id)
        if cached is not None:
            self.identity = cached
            self.identity_source = "session_cache"
            return

        self.identity_source = "unknown"

    def _duration_ms(self, start: tuple[float, float] | None,
                     end: tuple[float, float] | None) -> int | None:
        """Monotonic duration in ms, or None if either endpoint is missing."""
        if start is None or end is None:
            return None
        return max(0, int((end[1] - start[1]) * 1000))

    def _wall(self, point: tuple[float, float] | None) -> float | None:
        return point[0] if point is not None else None

    def _mono(self, point: tuple[float, float] | None) -> float | None:
        return point[1] if point is not None else None

    def to_record(self) -> dict[str, Any]:
        """Serialize to one immutable JSONL record. Never contains bodies."""
        recv = (self.t_receive_wall, self.t_receive_mono)
        terminal = self.t_terminal or self._now()

        record: dict[str, Any] = {
            "request_id": self.request_id,
            "session_id": self.session_id or None,
            "model": self.model,
            "route": self.route,
            "client_kind": self.client_kind,
            "stream": self.stream,
            "max_tokens": self.max_tokens,
            # Lifecycle wall times (sortable)
            "ts": self.t_receive_wall,
            "ts_iso": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.gmtime(self.t_receive_wall)) + "Z"),
            "t_receive": self.t_receive_wall,
            "t_hold_start": self._wall(self.t_hold_start),
            "t_hold_end": self._wall(self.t_hold_end),
            "t_upstream_send": self._wall(self.t_upstream_send),
            "t_headers": self._wall(self.t_headers),
            "t_first_event": self._wall(self.t_first_event),
            "t_first_delta": self._wall(self.t_first_delta),
            "t_terminal": self._wall(terminal),
            # Outcome
            "outcome": self.outcome or "unknown",
            "http_status": self.http_status,
            "error_class": self.error_class,
            "error_message": self.error_message,
            # Usage
            "usage": self.usage,
            # Duration components (monotonic, ms)
            "durations": {
                "total_ms": self._duration_ms(recv, terminal),
                "hold_ms": self._duration_ms(self.t_hold_start, self.t_hold_end),
                "upstream_wait_ms": self._duration_ms(
                    self.t_upstream_send, self.t_headers),
                "ttfb_ms": self._duration_ms(
                    self.t_upstream_send,
                    self.t_first_event or self.t_headers),
                "ttfd_ms": self._duration_ms(
                    self.t_upstream_send, self.t_first_delta),
                "generation_ms": self._duration_ms(
                    self.t_first_event or self.t_headers, terminal),
            },
            # Identity
            "identity": self.identity,
            "identity_source": self.identity_source,
        }
        return record


# ---------------------------------------------------------------------------
# Async JSONL writer (bounded queue, fail-open, rotation)
# ---------------------------------------------------------------------------

_DEFAULT_LOG_DIR = os.environ.get(
    "OBS_REQUEST_LIFECYCLE_DIR",
    "/workspace/runtime/state/local-gpu/requests",
)
_DEFAULT_MAX_BYTES = int(os.environ.get(
    "OBS_REQUEST_LIFECYCLE_MAX_GB", "2"
) or "2") * 10**9
_DEFAULT_QUEUE_SIZE = 64


class RequestLifecycleLogger:
    """Async JSONL writer for local-request lifecycle rows.

    - One file per day: ``YYYY-MM-DD.jsonl``
    - Bounded queue; drops are counted, never block the caller
    - Ring-buffer rotation: oldest files evicted past max_bytes
    - Thread-safe, fail-open
    """

    def __init__(self, root: str = _DEFAULT_LOG_DIR,
                 max_bytes: int = _DEFAULT_MAX_BYTES,
                 queue_size: int = _DEFAULT_QUEUE_SIZE):
        self.root = root
        self.max_bytes = max_bytes
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self.dropped = 0
        self.errors = 0
        self.written = 0
        self._bytes: int | None = None
        os.makedirs(root, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name="request-lifecycle-log", daemon=True)
        self._thread.start()

    def submit(self, record: dict) -> None:
        """Enqueue one lifecycle record. Never blocks or raises."""
        try:
            self._q.put_nowait(("record", record))
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.errors += 1

    def flush(self, timeout: float = 10.0) -> None:
        """Block until queued items are written (tests/shutdown)."""
        done = threading.Event()
        try:
            self._q.put_nowait(("barrier", done))
        except queue.Full:
            return
        done.wait(timeout)

    # -- writer thread --

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                kind = item[0]
                if kind == "barrier":
                    item[1].set()
                elif kind == "record":
                    self._write(item[1])
            except Exception:
                self.errors += 1

    def _write(self, record: dict) -> None:
        ts = record.get("ts", time.time())
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        path = os.path.join(self.root, f"{day}.jsonl")
        line = json.dumps(record, separators=(",", ":")).encode() + b"\n"
        os.makedirs(self.root, exist_ok=True)
        with open(path, "ab") as f:
            f.write(line)
        self.written += 1
        self._account(len(line))

    def _measure(self) -> int:
        total = 0
        try:
            for name in os.listdir(self.root):
                if name.endswith(".jsonl"):
                    try:
                        total += os.path.getsize(os.path.join(self.root, name))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    def _account(self, added: int) -> None:
        if self._bytes is None:
            self._bytes = self._measure()
        else:
            self._bytes += added
        if self._bytes > self.max_bytes:
            self._prune()

    def _prune(self) -> None:
        """Remove oldest JSONL files until under max_bytes."""
        try:
            files = sorted(
                (f for f in os.listdir(self.root) if f.endswith(".jsonl")),
            )
        except OSError:
            return
        for name in files:
            if self._bytes is not None and self._bytes <= self.max_bytes:
                break
            path = os.path.join(self.root, name)
            try:
                size = os.path.getsize(path)
                os.unlink(path)
                if self._bytes is not None:
                    self._bytes -= size
            except OSError:
                pass
