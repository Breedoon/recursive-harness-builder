"""Per-route CLI runtime selection (obs-22 rebuild, increment 12, decision A13).

Production runs every Claude session on the Agent SDK's bundled Claude Code
2.1.59. A canary file may name a pinned ``obs-cli`` release binary and the
agents or root teams that should use it::

    {"cli_path": "/abs/path/obs-cli", "agents": ["name"], "teams": ["root-team-key"],
     "new_sessions_only": true}

The file is read at every session build, so adding or removing an agent takes
effect at that agent's next CLI spawn with no OBS restart.  Anything wrong with
the file (missing, unreadable, bad JSON, a ``cli_path`` that is not an absolute
executable file) falls back to the bundled CLI, with one warning per change.
Without the file nothing changes: no record, no log line above debug.

CACHE-PREFIX WARNING: which CLI sends a request decides the request bytes.
Moving a conversation between binaries costs one prefix miss (decision A13).
That is why a resumed session stays on the binary that wrote it while
``new_sessions_only`` is true, and why a deployment needs the live cache
regression first (docs/cache-proxy-safety.md).

Records live in a small SQLite file beside the Telegram state DB
(``obs-cli-runtime.sqlite3``), not in the shared Telegram schema:

- ``session_runtime``: runtime id and binary path per session id;
- ``cli_path_seen``: every canary binary path ever used, which the orphan
  reaper needs to recognise an obs-cli left behind by a previous OBS process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

try:  # the SDK pins the bundled version
    from claude_agent_sdk._cli_version import __cli_version__ as _BUNDLED_VERSION
except Exception:  # pragma: no cover - SDK layout changed
    _BUNDLED_VERSION = "2.1.59"

BUNDLED_RUNTIME = f"bundled-{_BUNDLED_VERSION}"
CANARY_FILENAME = "obs-cli-canary.json"
RUNTIME_DB_FILENAME = "obs-cli-runtime.sqlite3"
CANARY_PATH_ENV = "OBS_CLI_CANARY_PATH"
RUNTIME_DB_ENV = "OBS_CLI_RUNTIME_DB_PATH"
_STATE_DB_ENV = "OBS_TELEGRAM_STATE_DB_PATH"

_lock = threading.Lock()
_seen_paths: set[str] = set()  # canary binaries named since this process started
_warned: dict[str, str] = {}  # canary file -> signature of the last warning
_hash_cache: dict[tuple[str, int, int], str] = {}


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def _state_dir(state_db_path: Path | str | None = None) -> Path:
    if state_db_path is None:
        raw = os.environ.get(_STATE_DB_ENV, "").strip()
        if raw:
            state_db_path = raw
        else:
            from obs_agent.config import _DEFAULT_TELEGRAM_STATE_DB_PATH

            state_db_path = _DEFAULT_TELEGRAM_STATE_DB_PATH
    return Path(state_db_path).parent


def canary_path(state_db_path: Path | str | None = None) -> Path:
    override = os.environ.get(CANARY_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return _state_dir(state_db_path) / CANARY_FILENAME


def runtime_db_path(state_db_path: Path | str | None = None) -> Path:
    override = os.environ.get(RUNTIME_DB_ENV, "").strip()
    if override:
        return Path(override)
    return _state_dir(state_db_path) / RUNTIME_DB_FILENAME


# --------------------------------------------------------------------------
# Canary file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Canary:
    """Result of reading the canary file.

    ``state`` is ``absent`` (no file: behave exactly as production), ``invalid``
    (use the bundled CLI, warn once) or ``valid``.
    """

    state: str
    cli_path: str = ""
    agents: frozenset[str] = frozenset()
    teams: frozenset[str] = frozenset()
    new_sessions_only: bool = True
    detail: str = ""


def _warn_once(path: Path, signature: str, message: str) -> None:
    with _lock:
        if _warned.get(str(path)) == signature:
            return
        _warned[str(path)] = signature
    logger.warning("%s", message)


def _names(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(item.strip() for item in value if isinstance(item, str) and item.strip())


def load_canary(path: Path) -> Canary:
    """Read and validate the canary file; never raises."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        with _lock:
            _warned.pop(str(path), None)
        return Canary("absent")
    except OSError as exc:
        _warn_once(path, f"unreadable:{exc.errno}", f"obs-cli canary file unreadable ({exc.__class__.__name__}); using bundled {BUNDLED_RUNTIME}")
        return Canary("invalid", detail="unreadable")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except ValueError as exc:
        digest = hashlib.sha256(raw).hexdigest()[:12]
        _warn_once(path, f"json:{digest}", f"obs-cli canary file is not valid JSON ({exc.__class__.__name__}); using bundled {BUNDLED_RUNTIME}")
        return Canary("invalid", detail="bad json")
    cli_path = data.get("cli_path")
    if (
        not isinstance(cli_path, str)
        or not cli_path
        or not os.path.isabs(cli_path)
        or not os.path.isfile(cli_path)
        or not os.access(cli_path, os.X_OK)
    ):
        _warn_once(
            path,
            f"cli:{cli_path!r}",
            f"obs-cli canary cli_path {cli_path!r} is not an absolute executable file; using bundled {BUNDLED_RUNTIME}",
        )
        return Canary("invalid", detail="cli_path")
    with _lock:
        _warned.pop(str(path), None)
        _seen_paths.add(cli_path)
    return Canary(
        "valid",
        cli_path=cli_path,
        agents=_names(data.get("agents")),
        teams=_names(data.get("teams")),
        new_sessions_only=data.get("new_sessions_only", True) is not False,
    )


def runtime_id_for_path(cli_path: str) -> str:
    """``obs-cli:<sha256 prefix>`` of a binary; hashed once per path+mtime+size."""
    try:
        st = os.stat(cli_path)
        key = (cli_path, st.st_mtime_ns, st.st_size)
    except OSError:
        return "obs-cli:unreadable"
    cached = _hash_cache.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(cli_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return "obs-cli:unreadable"
    runtime = f"obs-cli:{digest.hexdigest()[:12]}"
    _hash_cache[key] = runtime
    return runtime


# --------------------------------------------------------------------------
# Per-session records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeRecord:
    runtime: str
    cli_path: str | None


def _connect(db_path: Path, *, create: bool) -> sqlite3.Connection | None:
    if not create and not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    if create:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS session_runtime (
                session_id TEXT PRIMARY KEY,
                runtime TEXT NOT NULL,
                cli_path TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cli_path_seen (
                path TEXT PRIMARY KEY,
                updated_at REAL NOT NULL
            );
            """
        )
    return conn


def get_record(db_path: Path, session_id: str | None) -> RuntimeRecord | None:
    if not session_id:
        return None
    try:
        conn = _connect(db_path, create=False)
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT runtime, cli_path FROM session_runtime WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _warn_once(db_path, f"db:{exc.__class__.__name__}", f"obs-cli runtime records unreadable ({exc}); treating as empty")
        return None
    return RuntimeRecord(row[0], row[1]) if row else None


def put_record(db_path: Path, session_id: str, runtime: str, cli_path: str | None) -> None:
    try:
        conn = _connect(db_path, create=True)
        assert conn is not None
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO session_runtime(session_id, runtime, cli_path, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET runtime=excluded.runtime, "
                "cli_path=excluded.cli_path, updated_at=excluded.updated_at",
                (session_id, runtime, cli_path, now),
            )
            if cli_path:
                conn.execute(
                    "INSERT INTO cli_path_seen(path, updated_at) VALUES (?,?) "
                    "ON CONFLICT(path) DO UPDATE SET updated_at=excluded.updated_at",
                    (cli_path, now),
                )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        _warn_once(db_path, f"db:{exc.__class__.__name__}", f"could not record CLI runtime ({exc}); resumed canary sessions will use the bundled CLI")


def inherit_record(source_session_id: str | None, new_session_id: str | None, *, state_db_path: Path | str | None = None) -> None:
    """A fork or recovery copy continues the source's conversation: same binary.

    Best effort and silent when there is nothing to copy (no DB, no record).
    """
    if not source_session_id or not new_session_id or source_session_id == new_session_id:
        return
    try:
        db = runtime_db_path(state_db_path)
        record = get_record(db, source_session_id)
        if record is not None:
            put_record(db, new_session_id, record.runtime, record.cli_path)
    except Exception:
        logger.debug("obs-cli runtime inheritance failed", exc_info=True)


def known_cli_paths(state_db_path: Path | str | None = None) -> frozenset[str]:
    """Every canary binary path this process or a previous one has used.

    The orphan reaper matches argv[0] against these: the in-memory set covers
    this process, the file covers the current selection, and the persisted
    table covers a binary an earlier OBS process started, including after the
    file was edited or removed (rollback).
    """
    paths: set[str] = set()
    with _lock:
        paths.update(_seen_paths)
    try:
        raw = json.loads(canary_path(state_db_path).read_bytes())
        value = raw.get("cli_path") if isinstance(raw, dict) else None
        if isinstance(value, str) and os.path.isabs(value):
            paths.add(value)
    except (OSError, ValueError):
        pass
    try:
        conn = _connect(runtime_db_path(state_db_path), create=False)
        if conn is not None:
            try:
                paths.update(row[0] for row in conn.execute("SELECT path FROM cli_path_seen"))
            finally:
                conn.close()
    except sqlite3.Error as exc:
        _warn_once(runtime_db_path(state_db_path), f"db:{exc.__class__.__name__}", f"obs-cli runtime records unreadable ({exc}); treating as empty")
    return frozenset(paths)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """What a session build decided.

    ``record`` is False when no canary file governs the build (absent, invalid
    or non-Claude route), so production behaviour leaves no trace.
    """

    runtime: str
    cli_path: str | None  # None = leave the SDK's bundled CLI
    record: bool
    reason: str


def select_runtime(
    *,
    canary: Canary,
    agent_name: str | None,
    team_name: str | None,
    eligible: bool,
    resume_session_id: str | None,
    db_path: Path,
) -> Selection:
    if canary.state != "valid" or not eligible:
        reason = canary.state if canary.state != "valid" else "route not eligible"
        return Selection(BUNDLED_RUNTIME, None, False, reason)
    matched = bool(
        (agent_name and agent_name in canary.agents) or (team_name and team_name in canary.teams)
    )
    if not matched:
        return Selection(BUNDLED_RUNTIME, None, True, "not selected")
    if resume_session_id and canary.new_sessions_only:
        record = get_record(db_path, resume_session_id)
        if record is None or record.runtime == BUNDLED_RUNTIME:
            # No record: the session was written by the bundled CLI (OBS records
            # a session only while a canary file governs its builds).
            return Selection(BUNDLED_RUNTIME, None, True, "resume stays on the bundled CLI")
        if record.cli_path and os.path.isfile(record.cli_path) and os.access(record.cli_path, os.X_OK):
            return Selection(runtime_id_for_path(record.cli_path), record.cli_path, True, "resume stays on the recorded obs-cli")
        logger.warning(
            "recorded obs-cli %s for session %s is gone; resuming on the canary binary",
            record.cli_path,
            resume_session_id,
        )
    return Selection(runtime_id_for_path(canary.cli_path), canary.cli_path, True, "selected")
