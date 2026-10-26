"""Content-free local-model request lifecycle logging and attribution bindings.

The proxy writes one immutable row per local upstream transmission. OBS writes
trusted session bindings to a sidecar SQLite database before a request is sent;
the proxy only reads that database and never treats prompt text as authority.
Post-start logging remains fail-open; local transmission requires one durable
content-free pre-send record and never changes request bytes or admission logic.
"""
from __future__ import annotations

import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import queue
import re
import socket
import sqlite3
import threading
import time
from typing import Any
import uuid

ROW_SCHEMA = "obs-local-request-lifecycle/2"
COVERAGE_SCHEMA = "obs-local-request-coverage/1"
BINDING_SCHEMA = "obs-session-attribution-binding/1"
RETENTION_SCHEMA = "obs-local-request-retention-state/1"

DEFAULT_STATE_ROOT = Path(
    os.environ.get("OBS_REQUEST_LIFECYCLE_STATE_ROOT", "/workspace/runtime/state/local-gpu")
)
DEFAULT_LOG_DIR = Path(
    os.environ.get("OBS_REQUEST_LIFECYCLE_DIR", str(DEFAULT_STATE_ROOT / "requests"))
)
DEFAULT_BINDING_DB = Path(
    os.environ.get("OBS_REQUEST_BINDING_DB", str(DEFAULT_STATE_ROOT / "attribution-bindings.sqlite3"))
)
DEFAULT_MAX_BYTES = int(os.environ.get("OBS_REQUEST_LIFECYCLE_MAX_GB", "2") or "2") * 10**9
DEFAULT_QUEUE_SIZE = 64
PRODUCTION_PROXY_PORTS = frozenset({28925, 28931})

SAFE_OUTCOMES = frozenset(
    {
        "success",
        "error",
        "timeout",
        "client_gone",
        "cancel_requested",
        "cancelled",
        "invalid_response",
        "not_transmitted",
        "unknown",
    }
)
SAFE_ERROR_CLASSES = frozenset(
    {
        "upstream_http_error",
        "recovery_timeout",
        "upstream_silence",
        "wedge",
        "proxy_upstream_error",
        "invalid_response",
        "missing_usage",
        "stream_error_event",
        "client_disconnected",
        "malformed_request",
        "logging_loss",
        "unknown",
    }
)
SAFE_WAIT_REASONS = frozenset({"none", "engine_unhealthy", "unknown"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def lifecycle_root_for_proxy(port: int, cache_proxy_log_dir: str | os.PathLike[str]) -> str:
    """Choose an isolated logger root without contaminating production accounting."""
    explicit = os.environ.get("OBS_REQUEST_LIFECYCLE_DIR")
    if explicit:
        return explicit
    if int(port) in PRODUCTION_PROXY_PORTS:
        return str(DEFAULT_LOG_DIR)
    return str(Path(cache_proxy_log_dir) / "request-lifecycle")


def lifecycle_fallback_root(root: str | os.PathLike[str]) -> str:
    """Choose a distinct durable channel for catastrophic pre-send outages."""
    primary = Path(root)
    explicit = os.environ.get("OBS_REQUEST_LIFECYCLE_FALLBACK_DIR")
    fallback = Path(explicit) if explicit else primary.with_name(f"{primary.name}-fallback")
    if fallback.resolve() == primary.resolve():
        fallback = primary.with_name(f"{primary.name}-fallback")
    return str(fallback)


def new_lifecycle_request_id(session_id: str = "") -> str:
    """Generate a process-unique transmission id independent of body capture."""
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f".{int((now % 1) * 1e6):06d}Z"
    sid = (session_id or "nosession")[:8]
    return f"lc-{stamp}-{os.getpid()}-{next(_id_seq):06d}-{sid}"


_id_seq = itertools.count(1)


def _clean_scalar(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text[:limit] or None


def _clean_lineage(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = [_clean_scalar(item, limit=240) for item in value]
    return [item for item in result if item] or None


def _safe_identifier(value: Any) -> str | None:
    text = _clean_scalar(value, limit=200)
    return text if text and _SAFE_ID_RE.fullmatch(text) else None


class SessionBindingStore:
    """Durable time-versioned session -> agent/task attribution bindings."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_BINDING_DB, *, writable: bool = True):
        self.path = Path(path)
        self.writable = writable
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: dict[tuple[str, int], dict[str, Any] | None] = {}
        if writable:
            self._initialize()

    def _connect(self) -> sqlite3.Connection | None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if not self.writable and not self.path.exists():
            return None
        if self.writable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        else:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=200")
        self._local.conn = conn
        return conn

    def _initialize(self) -> None:
        with self._init_lock:
            conn = self._connect()
            assert conn is not None
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_binding (
                    binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema_name TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    bound_at REAL NOT NULL,
                    superseded_at REAL,
                    root_team_key TEXT,
                    agent_name TEXT,
                    lineage_json TEXT,
                    task_id TEXT,
                    attempt_id TEXT,
                    category TEXT,
                    lane TEXT,
                    source TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_session_binding_active
                    ON session_binding(session_id) WHERE superseded_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_session_binding_lookup
                    ON session_binding(session_id, bound_at, superseded_at);
                """
            )

    @staticmethod
    def _normalized(binding: dict[str, Any]) -> dict[str, Any]:
        return {
            "root_team_key": _clean_scalar(binding.get("root_team_key")),
            "agent_name": _clean_scalar(binding.get("agent_name")),
            "lineage": _clean_lineage(binding.get("lineage")),
            "task_id": _clean_scalar(binding.get("task_id")),
            "attempt_id": _clean_scalar(binding.get("attempt_id")),
            "category": _clean_scalar(binding.get("category"), limit=80),
            "lane": _clean_scalar(binding.get("lane"), limit=80),
            "source": _clean_scalar(binding.get("source"), limit=80) or "runtime_state",
        }

    def upsert(self, session_id: str, binding: dict[str, Any], *, bound_at: float | None = None) -> int | None:
        if not self.writable or not session_id:
            return None
        normalized = self._normalized(binding)
        now = float(bound_at if bound_at is not None else time.time())
        conn = self._connect()
        assert conn is not None
        lineage_json = json.dumps(normalized["lineage"], separators=(",", ":")) if normalized["lineage"] else None
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                """SELECT * FROM session_binding
                   WHERE session_id = ? AND superseded_at IS NULL
                   ORDER BY binding_id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            values = (
                normalized["root_team_key"], normalized["agent_name"], lineage_json,
                normalized["task_id"], normalized["attempt_id"], normalized["category"],
                normalized["lane"], normalized["source"],
            )
            if current is not None:
                current_values = (
                    current["root_team_key"], current["agent_name"], current["lineage_json"],
                    current["task_id"], current["attempt_id"], current["category"],
                    current["lane"], current["source"],
                )
                if current_values == values:
                    conn.execute(
                        "UPDATE session_binding SET updated_at = ? WHERE binding_id = ?",
                        (now, int(current["binding_id"])),
                    )
                    conn.execute("COMMIT")
                    with self._cache_lock:
                        self._cache.clear()
                    return int(current["binding_id"])
                conn.execute(
                    "UPDATE session_binding SET superseded_at = ?, updated_at = ? WHERE binding_id = ?",
                    (now, now, int(current["binding_id"])),
                )
            cursor = conn.execute(
                """INSERT INTO session_binding (
                       schema_name, session_id, bound_at, superseded_at,
                       root_team_key, agent_name, lineage_json, task_id, attempt_id,
                       category, lane, source, updated_at
                   ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (BINDING_SCHEMA, session_id, now, *values, now),
            )
            binding_id = int(cursor.lastrowid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        with self._cache_lock:
            self._cache.clear()
        return binding_id

    def get(self, session_id: str, *, at_ts: float | None = None) -> dict[str, Any] | None:
        if not session_id:
            return None
        conn = self._connect()
        if conn is None:
            return None
        point = float(at_ts if at_ts is not None else time.time())
        try:
            row = conn.execute(
                """SELECT * FROM session_binding
                   WHERE session_id = ? AND bound_at <= ?
                     AND (superseded_at IS NULL OR superseded_at > ?)
                   ORDER BY bound_at DESC, binding_id DESC LIMIT 1""",
                (session_id, point, point),
            ).fetchone()
        except sqlite3.Error:
            return None
        result = None
        if row is not None:
            try:
                lineage = json.loads(row["lineage_json"]) if row["lineage_json"] else None
            except (TypeError, ValueError):
                lineage = None
            result = {
                "binding_id": int(row["binding_id"]),
                "binding_schema": row["schema_name"],
                "bound_at": float(row["bound_at"]),
                "root_team_key": row["root_team_key"],
                "agent_name": row["agent_name"],
                "lineage": lineage,
                "task_id": row["task_id"],
                "attempt_id": row["attempt_id"],
                "category": row["category"],
                "lane": row["lane"],
                "source": row["source"],
            }
        return result


class LifecycleRow:
    """Accumulate content-free metadata for one local upstream transmission."""

    def __init__(
        self,
        *,
        request_id: str,
        session_id: str,
        model: str,
        pool: str | None = None,
        engine: str | None = None,
        route: str = "local",
        client_kind: str = "other",
        max_tokens: int | None = None,
        stream: bool = False,
        receive_wall: float | None = None,
        receive_mono: float | None = None,
        proxy_instance_id: str | None = None,
        proxy_source_sha256: str | None = None,
        proxy_port: int | None = None,
        engine_instance: str | None = None,
        engine_epoch: str | None = None,
        config_epoch: str | None = None,
    ) -> None:
        self.request_id = request_id
        self.session_id = session_id
        self.model = _clean_scalar(model) or "local-unknown"
        self.pool = _clean_scalar(pool, limit=80)
        self.engine = _clean_scalar(engine, limit=80)
        self.route = route
        self.client_kind = client_kind if client_kind in {"claude-cli", "other"} else "other"
        self.max_tokens = max_tokens if isinstance(max_tokens, int) and max_tokens >= 0 else None
        self.stream = bool(stream)
        self.t_receive_wall = float(receive_wall if receive_wall is not None else time.time())
        self.t_receive_mono = float(receive_mono if receive_mono is not None else time.monotonic())
        self.t_queue: tuple[float, float] | None = None
        self.t_granted: tuple[float, float] | None = None
        self.t_hold_start: tuple[float, float] | None = None
        self.t_hold_end: tuple[float, float] | None = None
        self.t_upstream_send: tuple[float, float] | None = None
        self.t_headers: tuple[float, float] | None = None
        self.t_first_event: tuple[float, float] | None = None
        self.t_first_content: tuple[float, float] | None = None
        self.t_first_text: tuple[float, float] | None = None
        self.t_cancel_requested: tuple[float, float] | None = None
        self.t_cancel_acknowledged: tuple[float, float] | None = None
        self.t_terminal: tuple[float, float] | None = None
        self.http_status: int | None = None
        self.outcome: str | None = None
        self.error_class: str | None = None
        self.wait_reason = "none"
        self.usage: dict[str, int | None] | None = None
        self.usage_available: dict[str, bool] | None = None
        self.binding: dict[str, Any] | None = None
        self.binding_source = "unattributed"
        self.proxy_instance_id = _clean_scalar(proxy_instance_id, limit=160)
        self.proxy_source_sha256 = _safe_identifier(proxy_source_sha256)
        self.proxy_port = int(proxy_port) if proxy_port is not None else None
        self.engine_instance = _clean_scalar(engine_instance, limit=160)
        self.engine_epoch = _clean_scalar(engine_epoch, limit=160)
        self.config_epoch = _clean_scalar(config_epoch, limit=160)
        self.native_request_id: str | None = None
        self.native_terminal_observed = False
        self.native_cancel_acknowledged = False
        self.transmission_start_persisted: bool | None = None
        self._sse_buffer = b""
        self._stream_terminal_seen = False
        self._stream_error_seen = False

    def _now(self) -> tuple[float, float]:
        return time.time(), time.monotonic()

    def bind(self, binding: dict[str, Any] | None) -> None:
        if not binding:
            self.binding = None
            self.binding_source = "unattributed"
            return
        self.binding = {
            "binding_id": binding.get("binding_id"),
            "binding_schema": binding.get("binding_schema") or BINDING_SCHEMA,
            "bound_at": binding.get("bound_at"),
            "root_team_key": _clean_scalar(binding.get("root_team_key")),
            "agent_name": _clean_scalar(binding.get("agent_name")),
            "lineage": _clean_lineage(binding.get("lineage")),
            "task_id": _clean_scalar(binding.get("task_id")),
            "attempt_id": _clean_scalar(binding.get("attempt_id")),
            "category": _clean_scalar(binding.get("category"), limit=80),
            "lane": _clean_scalar(binding.get("lane"), limit=80),
            "source": _clean_scalar(binding.get("source"), limit=80) or "runtime_state",
        }
        self.binding_source = "trusted_runtime_state"

    def mark_queue(self, wait_reason: str = "unknown") -> None:
        self.t_queue = self.t_queue or self._now()
        self.wait_reason = wait_reason if wait_reason in SAFE_WAIT_REASONS else "unknown"

    def mark_granted(self) -> None:
        self.t_granted = self.t_granted or self._now()

    def mark_hold_start(self, wait_reason: str = "engine_unhealthy") -> None:
        self.t_hold_start = self.t_hold_start or self._now()
        self.wait_reason = wait_reason if wait_reason in SAFE_WAIT_REASONS else "unknown"

    def mark_hold_end(self) -> None:
        self.t_hold_end = self.t_hold_end or self._now()

    def mark_upstream_send(self) -> None:
        self.t_upstream_send = self.t_upstream_send or self._now()

    def mark_headers(self, http_status: int, *, native_request_id: str | None = None) -> None:
        self.t_headers = self.t_headers or self._now()
        self.http_status = int(http_status)
        self.native_request_id = _safe_identifier(native_request_id)

    def mark_cancel_requested(self, error_class: str = "unknown") -> None:
        self.t_cancel_requested = self.t_cancel_requested or self._now()
        self.error_class = error_class if error_class in SAFE_ERROR_CLASSES else "unknown"

    def mark_cancel_acknowledged(self) -> None:
        self.t_cancel_acknowledged = self.t_cancel_acknowledged or self._now()
        self.native_cancel_acknowledged = True

    def _mark_first(self, name: str) -> None:
        if getattr(self, name) is None:
            setattr(self, name, self._now())

    def observe_stream_chunk(self, chunk: bytes) -> None:
        """Observe complete SSE events without persisting response content."""
        if not chunk:
            return
        self._sse_buffer = (self._sse_buffer + chunk)[-262144:]
        parts = re.split(br"\r?\n\r?\n", self._sse_buffer)
        self._sse_buffer = parts.pop() if parts else b""
        for frame in parts:
            data_lines = []
            for line in re.split(br"\r?\n", frame):
                if line.startswith(b"data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            payload = b"\n".join(data_lines)
            if payload == b"[DONE]":
                continue
            try:
                event = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            self._mark_first("t_first_event")
            event_type = event.get("type")
            if event_type in {"content_block_start", "content_block_delta"}:
                self._mark_first("t_first_content")
            block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if (
                (block.get("type") == "text" and isinstance(block.get("text"), str) and block.get("text"))
                or (delta.get("type") == "text_delta" and isinstance(delta.get("text"), str) and delta.get("text"))
            ):
                self._mark_first("t_first_text")
            if event_type == "message_stop":
                self._stream_terminal_seen = True
            elif event_type == "error":
                self._stream_error_seen = True

    def observe_nonstream_payload(self, payload: bytes) -> tuple[bool, dict[str, Any] | None]:
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False, None
        if not isinstance(parsed, dict):
            return False, None
        self._mark_first("t_first_event")
        content = parsed.get("content")
        if isinstance(content, list):
            self._mark_first("t_first_content")
            if any(
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block.get("text")
                for block in content
            ):
                self._mark_first("t_first_text")
        return True, parsed

    @property
    def stream_terminal_seen(self) -> bool:
        return self._stream_terminal_seen

    @property
    def stream_error_seen(self) -> bool:
        return self._stream_error_seen

    @staticmethod
    def _usage_value(usage: dict[str, Any], key: str) -> tuple[int | None, bool]:
        if key not in usage:
            return None, False
        value = usage.get(key)
        if isinstance(value, bool):
            return None, False
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None, False
        return (number, True) if number >= 0 else (None, False)

    def set_usage(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            self.usage = None
            self.usage_available = None
            return
        keys = (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
        values: dict[str, int | None] = {}
        available: dict[str, bool] = {}
        for key in keys:
            values[key], available[key] = self._usage_value(usage, key)
        self.usage = values
        self.usage_available = available

    def mark_terminal(
        self,
        outcome: str,
        *,
        error_class: str | None = None,
        usage: dict[str, Any] | None = None,
        http_status: int | None = None,
        native_terminal: bool = False,
    ) -> None:
        self.t_terminal = self.t_terminal or self._now()
        self.outcome = outcome if outcome in SAFE_OUTCOMES else "unknown"
        self.error_class = (
            error_class if error_class in SAFE_ERROR_CLASSES else ("unknown" if error_class else None)
        )
        if usage is not None:
            self.set_usage(usage)
        if http_status is not None:
            self.http_status = int(http_status)
        self.native_terminal_observed = bool(native_terminal)

    @staticmethod
    def _duration_ms(start: tuple[float, float] | None, end: tuple[float, float] | None) -> int | None:
        if start is None or end is None:
            return None
        return max(0, int((end[1] - start[1]) * 1000))

    @staticmethod
    def _wall(point: tuple[float, float] | None) -> float | None:
        return point[0] if point is not None else None

    def _reservation(self) -> tuple[int | None, str | None]:
        if self.max_tokens is None or not self.usage or not self.usage_available:
            return None, None
        input_keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        if not all(self.usage_available.get(key) for key in input_keys):
            return None, None
        prompt = sum(int(self.usage[key] or 0) for key in input_keys)
        return prompt + self.max_tokens, "upstream_anthropic_split_usage_plus_requested_output"

    def to_record(self) -> dict[str, Any]:
        terminal = self.t_terminal or self._now()
        recv = (self.t_receive_wall, self.t_receive_mono)
        proxy_forwarded_ms = self._duration_ms(self.t_upstream_send, terminal)
        reservation_tokens, reservation_source = self._reservation()
        proxy_stream_hours = proxy_forwarded_ms / 3_600_000 if proxy_forwarded_ms is not None else None
        proxy_reserved_token_hours = (
            reservation_tokens * proxy_stream_hours
            if reservation_tokens is not None and proxy_stream_hours is not None
            else None
        )
        return {
            "schema": ROW_SCHEMA,
            "request_id": self.request_id,
            "session_id": self.session_id or None,
            "model": self.model,
            "route": self.route,
            "pool": self.pool,
            "engine": self.engine,
            "client_kind": self.client_kind,
            "stream": self.stream,
            "max_output_tokens": self.max_tokens,
            "ts": self.t_receive_wall,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.t_receive_wall)) + "Z",
            "lifecycle": {
                "received": self.t_receive_wall,
                "queued": self._wall(self.t_queue),
                "granted": self._wall(self.t_granted),
                "hold_started": self._wall(self.t_hold_start),
                "hold_ended": self._wall(self.t_hold_end),
                "upstream_sent": self._wall(self.t_upstream_send),
                "headers": self._wall(self.t_headers),
                "first_event": self._wall(self.t_first_event),
                "first_content": self._wall(self.t_first_content),
                "first_text": self._wall(self.t_first_text),
                "cancel_requested": self._wall(self.t_cancel_requested),
                "cancel_acknowledged": self._wall(self.t_cancel_acknowledged),
                "terminal": self._wall(terminal),
            },
            "durations": {
                "ingress_to_terminal_ms": self._duration_ms(recv, terminal),
                "proxy_hold_ms": self._duration_ms(self.t_hold_start, self.t_hold_end),
                "upstream_to_headers_ms": self._duration_ms(self.t_upstream_send, self.t_headers),
                "upstream_to_first_event_ms": self._duration_ms(self.t_upstream_send, self.t_first_event),
                "upstream_to_first_content_ms": self._duration_ms(self.t_upstream_send, self.t_first_content),
                "upstream_to_first_text_ms": self._duration_ms(self.t_upstream_send, self.t_first_text),
                "proxy_forwarded_ms": proxy_forwarded_ms,
                "proxy_response_stream_ms": self._duration_ms(self.t_headers, terminal),
                "native_engine_execution_ms": None,
            },
            "accounting": {
                "reservation_tokens": reservation_tokens,
                "reservation_source": reservation_source,
                "proxy_reserved_token_hours": proxy_reserved_token_hours,
                "native_memory_token_hours": None,
                "proxy_forwarded_stream_hours": proxy_stream_hours,
                "native_stream_hours": None,
                "reference_slot_hours": None,
                "hardware_gpu_hours": None,
            },
            "wait_reason": self.wait_reason,
            "outcome": self.outcome or "unknown",
            "http_status": self.http_status,
            "error_class": self.error_class,
            "usage": self.usage,
            "usage_available": self.usage_available,
            "usage_semantics": {
                "input_tokens": "fresh_uncached_input",
                "cache_read_input_tokens": "provider_cache_read_input",
                "cache_creation_input_tokens": "provider_cache_write_input",
                "output_tokens": "generated_output",
                "missing": "null_unavailable_not_zero",
            },
            "binding": self.binding,
            "binding_source": self.binding_source,
            "correlation": {
                "native_request_id": self.native_request_id,
                "native_start_observed": False,
                "native_terminal_observed": self.native_terminal_observed,
                "cancel_requested": self.t_cancel_requested is not None,
                "native_cancel_acknowledged": self.native_cancel_acknowledged,
                "native_event_source": "response_metadata" if self.native_request_id else None,
            },
            "epochs": {
                "proxy_instance": self.proxy_instance_id,
                "proxy_source_sha256": self.proxy_source_sha256,
                "proxy_port": self.proxy_port,
                "engine_instance": self.engine_instance,
                "engine_epoch": self.engine_epoch,
                "config_epoch": self.config_epoch,
                "engine_epoch_available": self.engine_epoch is not None,
                "config_epoch_available": self.config_epoch is not None,
            },
            "coverage": {
                "transmission_start_persisted": self.transmission_start_persisted,
                "native_timing_available": False,
                "hardware_time_available": False,
            },
        }


class RequestLifecycleLogger:
    """Process-safe segmented async writer plus durable coverage journal."""

    def __init__(
        self,
        root: str | os.PathLike[str] = DEFAULT_LOG_DIR,
        max_bytes: int = DEFAULT_MAX_BYTES,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        *,
        proxy_port: int | None = None,
        proxy_source_sha256: str | None = None,
        cache_proxy_log_dir: str | None = None,
    ) -> None:
        self.root = str(root)
        self.max_bytes = max(int(max_bytes), 256)
        self.proxy_port = proxy_port
        self.proxy_source_sha256 = proxy_source_sha256
        self.cache_proxy_log_dir = cache_proxy_log_dir
        self.instance_id = self._instance_id()
        self.proxy_instance_id = self.instance_id
        self._q: queue.Queue = queue.Queue(maxsize=max(int(queue_size), 1))
        self.dropped = 0
        self.errors = 0
        self.written = 0
        self._lock = threading.Lock()
        self._bytes: int | None = None
        self._request_seq = 0
        self._coverage_seq = 0
        self._request_path: Path | None = None
        self._coverage_path: Path | None = None
        self._request_bytes = 0
        self._coverage_bytes = 0
        self._request_segment_limit = max(128, int(self.max_bytes * 0.60))
        self._coverage_segment_limit = max(96, int(self.max_bytes * 0.20))
        Path(self.root).mkdir(parents=True, exist_ok=True)
        self._append_coverage("startup", code="logger_started", sync=True)
        self._thread = threading.Thread(
            target=self._run, name=f"request-lifecycle-{self.instance_id[-8:]}", daemon=True
        )
        self._thread.start()

    def _instance_id(self) -> str:
        raw = f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}:{uuid.uuid4().hex}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _segment_name(self, kind: str, seq: int) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return Path(self.root) / f"{kind}-{stamp}-{self.instance_id}-s{seq:06d}.jsonl"

    def _measure(self) -> int:
        total = 0
        try:
            for path in Path(self.root).iterdir():
                if path.is_file() and path.suffix == ".jsonl":
                    total += path.stat().st_size
        except OSError:
            pass
        return total

    def _account(self, delta: int) -> None:
        # Re-measure the shared directory: production has two proxy processes.
        self._bytes = self._measure()
        if self._bytes > self.max_bytes:
            self._prune_locked()

    @staticmethod
    def _retention_stats(path: Path) -> dict[str, Any]:
        stats = {
            "raw_lines": 0,
            "request_rows": 0,
            "transmission_starts": 0,
            "loss_events": 0,
            "malformed_lines": 0,
            "max_ts": None,
        }
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stats["raw_lines"] += 1
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        stats["malformed_lines"] += 1
                        continue
                    if not isinstance(row, dict):
                        stats["malformed_lines"] += 1
                        continue
                    ts = row.get("ts")
                    if isinstance(ts, (int, float)):
                        stats["max_ts"] = max(float(ts), stats["max_ts"] or float(ts))
                    if row.get("schema") == ROW_SCHEMA:
                        stats["request_rows"] += 1
                    elif row.get("schema") == COVERAGE_SCHEMA:
                        if row.get("kind") == "transmission_start":
                            stats["transmission_starts"] += 1
                        elif row.get("kind") == "loss":
                            stats["loss_events"] += 1
        except OSError:
            pass
        return stats

    def _write_retention_state(self, pruned: list[dict[str, Any]], remaining: list[Path]) -> None:
        state_path = Path(self.root) / "retention-state.json"
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            previous = {}
        earliest_retained = None
        for path in remaining:
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = row.get("ts") if isinstance(row, dict) else None
                        if isinstance(ts, (int, float)):
                            earliest_retained = (
                                float(ts) if earliest_retained is None
                                else min(earliest_retained, float(ts))
                            )
                            break
            except OSError:
                continue
        state = {
            "schema": RETENTION_SCHEMA,
            "updated_at": time.time(),
            "pruned_files": int(previous.get("pruned_files") or 0) + len(pruned),
            "pruned_raw_lines": int(previous.get("pruned_raw_lines") or 0)
                + sum(int(item.get("raw_lines") or 0) for item in pruned),
            "pruned_request_rows": int(previous.get("pruned_request_rows") or 0)
                + sum(int(item.get("request_rows") or 0) for item in pruned),
            "pruned_transmission_starts": int(previous.get("pruned_transmission_starts") or 0)
                + sum(int(item.get("transmission_starts") or 0) for item in pruned),
            "pruned_loss_events": int(previous.get("pruned_loss_events") or 0)
                + sum(int(item.get("loss_events") or 0) for item in pruned),
            "pruned_malformed_lines": int(previous.get("pruned_malformed_lines") or 0)
                + sum(int(item.get("malformed_lines") or 0) for item in pruned),
            "latest_pruned_ts": max(
                [float(previous.get("latest_pruned_ts") or 0.0)]
                + [float(item.get("max_ts") or 0.0) for item in pruned]
            ) or None,
            "earliest_retained_ts": earliest_retained,
        }
        temp = state_path.with_name(f".{state_path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        payload = json.dumps(state, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, state_path)

    def _prune_locked(self) -> None:
        root = Path(self.root)
        lock_path = root / ".rotation.lock"
        try:
            with lock_path.open("a+") as rotation_lock:
                fcntl.flock(rotation_lock.fileno(), fcntl.LOCK_EX)
                files = sorted(
                    (path for path in root.iterdir() if path.is_file() and path.suffix == ".jsonl"),
                    key=lambda path: (path.stat().st_mtime_ns, path.name),
                )
                current = sum(path.stat().st_size for path in files if path.exists())
                now = time.time()
                protected = {
                    path for path in files
                    if path in {self._request_path, self._coverage_path}
                    or (self.instance_id not in path.name and now - path.stat().st_mtime < 60)
                }
                pruned: list[dict[str, Any]] = []
                for path in files:
                    if current <= self.max_bytes:
                        break
                    if path in protected:
                        continue
                    try:
                        size = path.stat().st_size
                        stats = self._retention_stats(path)
                        path.unlink()
                        current -= size
                        pruned.append(stats)
                    except OSError:
                        continue
                remaining = [path for path in files if path.exists()]
                if pruned:
                    self._write_retention_state(pruned, remaining)
                self._bytes = current
                fcntl.flock(rotation_lock.fileno(), fcntl.LOCK_UN)
        except OSError:
            return

    def _append_coverage(
        self,
        kind: str,
        *,
        request_id: str | None = None,
        code: str | None = None,
        sync: bool = False,
        attributes: dict[str, Any] | None = None,
    ) -> bool:
        event = {
            "schema": COVERAGE_SCHEMA,
            "ts": time.time(),
            "kind": kind,
            "request_id": request_id,
            "code": code,
            "proxy_instance": self.proxy_instance_id,
            "proxy_port": self.proxy_port,
        }
        if attributes:
            event.update({
                key: value for key, value in attributes.items()
                if key in {"session_id", "task_id", "attempt_id", "category", "pool"}
            })
        line = json.dumps(event, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        try:
            with self._lock:
                new_segment = False
                if (
                    self._coverage_path is None
                    or (self._coverage_bytes and self._coverage_bytes + len(line) > self._coverage_segment_limit)
                ):
                    self._coverage_seq += 1
                    self._coverage_path = self._segment_name("coverage", self._coverage_seq)
                    self._coverage_bytes = 0
                    new_segment = True
                fd = os.open(self._coverage_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
                try:
                    view = memoryview(line)
                    while view:
                        view = view[os.write(fd, view):]
                    if sync:
                        os.fsync(fd)
                finally:
                    os.close(fd)
                if sync and new_segment:
                    directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                self._coverage_bytes += len(line)
                self._account(len(line))
            return True
        except Exception:
            self.errors += 1
            return False

    def record_transmission_start(self, row: LifecycleRow) -> bool:
        binding = row.binding or {}
        row.transmission_start_persisted = self._append_coverage(
            "transmission_start",
            request_id=row.request_id,
            sync=True,
            attributes={
                "session_id": row.session_id or None,
                "task_id": binding.get("task_id"),
                "attempt_id": binding.get("attempt_id"),
                "category": binding.get("category"),
                "pool": row.pool,
            },
        )
        return bool(row.transmission_start_persisted)

    def record_loss(
        self,
        request_id: str | None,
        code: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self._append_coverage(
            "loss", request_id=request_id, code=code, sync=True, attributes=attributes
        )

    @staticmethod
    def _record_attributes(record: dict[str, Any]) -> dict[str, Any]:
        binding = record.get("binding") or {}
        return {
            "session_id": record.get("session_id"),
            "task_id": binding.get("task_id"),
            "attempt_id": binding.get("attempt_id"),
            "category": binding.get("category"),
            "pool": record.get("pool"),
        }

    def submit(self, record: dict[str, Any]) -> None:
        request_id = _clean_scalar(record.get("request_id"), limit=240)
        attributes = self._record_attributes(record)
        try:
            self._q.put_nowait(("record", record))
        except queue.Full:
            self.dropped += 1
            self.record_loss(request_id, "queue_full", attributes=attributes)
        except Exception:
            self.errors += 1
            self.record_loss(request_id, "submit_error", attributes=attributes)

    def flush(self, timeout: float = 10.0) -> None:
        done = threading.Event()
        try:
            self._q.put_nowait(("barrier", done))
        except queue.Full:
            return
        done.wait(timeout)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item[0] == "barrier":
                    item[1].set()
                else:
                    self._write(item[1])
            except Exception:
                self.errors += 1
                record = item[1] if item[0] == "record" else {}
                self.record_loss(
                    record.get("request_id"),
                    "write_error",
                    attributes=self._record_attributes(record),
                )

    def _write(self, record: dict[str, Any]) -> None:
        request_id = _clean_scalar(record.get("request_id"), limit=240)
        attributes = self._record_attributes(record)
        try:
            line = json.dumps(record, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        except Exception:
            self.record_loss(request_id, "serialization_error", attributes=attributes)
            return
        if len(line) > max(128, self.max_bytes - self._coverage_segment_limit):
            self.record_loss(request_id, "row_oversize", attributes=attributes)
            return
        with self._lock:
            if (
                self._request_path is None
                or (self._request_bytes and self._request_bytes + len(line) > self._request_segment_limit)
            ):
                self._request_seq += 1
                self._request_path = self._segment_name("requests", self._request_seq)
                self._request_bytes = 0
            with self._request_path.open("ab") as handle:
                handle.write(line)
            self._request_bytes += len(line)
            self.written += 1
            self._account(len(line))


def record_startup_failure(
    root: str | os.PathLike[str], *, proxy_port: int | None, code: str = "logger_start_failed"
) -> None:
    """Best-effort durable startup classification when the async logger cannot start."""
    try:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        event = {
            "schema": COVERAGE_SCHEMA,
            "ts": time.time(),
            "kind": "startup_failure",
            "request_id": None,
            "code": code,
            "proxy_instance": f"startup-{os.getpid()}",
            "proxy_port": proxy_port,
        }
        line = json.dumps(event, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        target = path / f"coverage-startup-{int(time.time())}-{os.getpid()}.jsonl"
        fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        pass


def record_direct_loss(
    root: str | os.PathLike[str],
    *,
    request_id: str,
    code: str,
    proxy_port: int | None,
    attributes: dict[str, Any] | None = None,
) -> bool:
    """Synchronously journal a transmission and its classified loss.

    Used only when the normal lifecycle row/logger cannot be constructed. Two
    small content-free lines preserve exact conservation without blocking the
    request on the asynchronous writer.
    """
    try:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        base = {
            "schema": COVERAGE_SCHEMA,
            "request_id": request_id,
            "proxy_instance": f"fallback-{os.getpid()}",
            "proxy_port": proxy_port,
        }
        safe_attributes = {
            key: value for key, value in (attributes or {}).items()
            if key in {"session_id", "task_id", "attempt_id", "category", "pool"}
        }
        now = time.time()
        start = {**base, **safe_attributes, "ts": now, "kind": "transmission_start", "code": None}
        loss = {**base, **safe_attributes, "ts": now, "kind": "loss", "code": code}
        payload = b"".join(
            json.dumps(event, separators=(",", ":"), sort_keys=True).encode() + b"\n"
            for event in (start, loss)
        )
        target = path / f"coverage-fallback-{int(now)}-{os.getpid()}.jsonl"
        fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except Exception:
        return False
