"""Prior committed rc1 store API projection, executed only by the M-SQL lane.

_connect/get_record/put_record and RuntimeRecord are AST-equal to rc1
5adcd336c84a21d3e6dd31dbbdb9bc900ff74b27:src/obs_agent/cli_runtime.py.
The three shared helpers are unchanged in the candidate. This is NOT current
production 890ad270, which has no cli_runtime module or runtime-store consumer.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from obs_agent.cli_runtime import _warn_once, _binary_key, provisional_runtime_id


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
            CREATE TABLE IF NOT EXISTS binary_hash (
                path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                runtime TEXT NOT NULL,
                PRIMARY KEY (path, mtime_ns, size)
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
    except (sqlite3.Error, OSError) as exc:
        _warn_once(db_path, f"db:{exc.__class__.__name__}", f"obs-cli runtime records unreadable ({exc}); treating as empty")
        return None
    return RuntimeRecord(row[0], row[1]) if row else None


def put_record(db_path: Path, session_id: str, runtime: str, cli_path: str | None) -> None:
    try:
        conn = _connect(db_path, create=True)
        assert conn is not None
        try:
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            if cli_path and runtime == provisional_runtime_id(cli_path):
                key = _binary_key(cli_path)
                row = conn.execute(
                    "SELECT runtime FROM binary_hash WHERE path=? AND mtime_ns=? AND size=?", key
                ).fetchone() if key else None
                if row:
                    runtime = row[0]
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
