"""SQLite-backed persistence for Telegram runtime route/session/message state."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path


def _route_key(chat_id: int, thread_id: int | None) -> str:
    thread_token = "none" if thread_id is None else str(thread_id)
    return f"{chat_id}:{thread_token}"


@dataclass(frozen=True)
class PersistedRouteState:
    chat_id: int
    thread_id: int | None
    session_id: str | None
    topic_title: str | None
    topic_icon_custom_emoji_id: str | None
    child_fork_count: int
    child_fork_base_title: str | None
    notify_on_completion: bool
    last_inbound_message_id: int | None


@dataclass(frozen=True)
class PersistedMessageBinding:
    chat_id: int
    message_id: int
    session_id: str
    jsonl_uuid: str
    role: str
    route_chat_id: int
    route_thread_id: int | None


@dataclass(frozen=True)
class PersistedSystemMessage:
    chat_id: int
    message_id: int
    route_chat_id: int
    route_thread_id: int | None


@dataclass(frozen=True)
class PersistedTeamWorkerState:
    team_name: str
    agent_name: str
    task_id: str
    child_chat_id: int
    child_thread_id: int | None
    child_session_id: str
    description: str | None
    status: str
    idle_ready: bool


@dataclass(frozen=True)
class PersistedTopicSchedule:
    schedule_id: str
    chat_id: int
    thread_id: int | None
    description: str | None
    schedule_mode: str
    cron_expr: str | None
    trigger_kind: str
    interval_seconds: int | None
    prompt: str
    run_mode: str
    recurring: bool
    enabled: bool
    run_count: int
    max_runs: int | None
    from_ts: float | None
    until_ts: float | None
    inherit_mode: str
    next_run_at: float | None
    last_run_at: float | None
    last_success_at: float | None
    last_error: str | None
    max_retry_attempts: int
    retry_delay_seconds: int
    retry_attempt_count: int


@dataclass(frozen=True)
class TelegramStateSnapshot:
    route_states: list[PersistedRouteState] = field(default_factory=list)
    message_bindings: list[PersistedMessageBinding] = field(default_factory=list)
    system_messages: list[PersistedSystemMessage] = field(default_factory=list)
    session_heads: dict[str, str] = field(default_factory=dict)
    team_worker_states: list[PersistedTeamWorkerState] = field(default_factory=list)
    topic_schedules: list[PersistedTopicSchedule] = field(default_factory=list)


class TelegramStateStore:
    """Minimal write-through persistence for Telegram runtime mapping state."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        self._create_schema()

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TelegramStateStore is not initialized")
        return self._conn

    def _create_schema(self) -> None:
        conn = self._require_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS route_state (
                route_key TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                session_id TEXT,
                topic_title TEXT,
                topic_icon_custom_emoji_id TEXT,
                child_fork_count INTEGER NOT NULL DEFAULT 0,
                child_fork_base_title TEXT,
                notify_on_completion INTEGER NOT NULL DEFAULT 1,
                last_inbound_message_id INTEGER,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_binding (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                jsonl_uuid TEXT NOT NULL,
                role TEXT NOT NULL,
                route_key TEXT NOT NULL,
                route_chat_id INTEGER NOT NULL,
                route_thread_id INTEGER,
                updated_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_binding_route_key
                ON message_binding(route_key);
            CREATE INDEX IF NOT EXISTS idx_message_binding_session_uuid
                ON message_binding(session_id, jsonl_uuid);

            CREATE TABLE IF NOT EXISTS session_head (
                session_id TEXT PRIMARY KEY,
                jsonl_uuid TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_message (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                route_key TEXT NOT NULL,
                route_chat_id INTEGER NOT NULL,
                route_thread_id INTEGER,
                updated_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_system_message_route_key
                ON system_message(route_key);

            CREATE TABLE IF NOT EXISTS team_worker_state (
                team_name TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                task_id TEXT NOT NULL,
                child_chat_id INTEGER NOT NULL,
                child_thread_id INTEGER,
                child_session_id TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                idle_ready INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY (team_name, agent_name)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_team_worker_state_task_id
                ON team_worker_state(task_id);
            CREATE INDEX IF NOT EXISTS idx_team_worker_state_route
                ON team_worker_state(child_chat_id, child_thread_id);

            CREATE TABLE IF NOT EXISTS topic_schedule (
                schedule_id TEXT PRIMARY KEY,
                route_key TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,
                description TEXT,
                schedule_mode TEXT NOT NULL DEFAULT 'interval',
                cron_expr TEXT,
                trigger_kind TEXT NOT NULL,
                interval_seconds INTEGER,
                prompt TEXT NOT NULL,
                run_mode TEXT NOT NULL,
                recurring INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                run_count INTEGER NOT NULL DEFAULT 0,
                max_runs INTEGER,
                from_ts REAL,
                until_ts REAL,
                inherit_mode TEXT NOT NULL DEFAULT 'none',
                next_run_at REAL,
                last_run_at REAL,
                last_success_at REAL,
                last_error TEXT,
                max_retry_attempts INTEGER NOT NULL DEFAULT 0,
                retry_delay_seconds INTEGER NOT NULL DEFAULT 30,
                retry_attempt_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_topic_schedule_route_key
                ON topic_schedule(route_key);
            CREATE INDEX IF NOT EXISTS idx_topic_schedule_due
                ON topic_schedule(enabled, next_run_at);
            """
        )
        self._ensure_column_exists(
            table="topic_schedule",
            column="schedule_mode",
            declaration="TEXT NOT NULL DEFAULT 'interval'",
        )
        self._ensure_column_exists(
            table="topic_schedule",
            column="from_ts",
            declaration="REAL",
        )
        self._ensure_column_exists(
            table="topic_schedule",
            column="max_retry_attempts",
            declaration="INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column_exists(
            table="topic_schedule",
            column="retry_delay_seconds",
            declaration="INTEGER NOT NULL DEFAULT 30",
        )
        self._ensure_column_exists(
            table="topic_schedule",
            column="retry_attempt_count",
            declaration="INTEGER NOT NULL DEFAULT 0",
        )

    def _ensure_column_exists(self, *, table: str, column: str, declaration: str) -> None:
        conn = self._require_conn()
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(str(row["name"]) == column for row in rows):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def prune(self, *, retention_days: int) -> None:
        conn = self._require_conn()
        days = max(int(retention_days), 0)
        if days <= 0:
            return
        cutoff = time.time() - (days * 24 * 60 * 60)
        for table in (
            "route_state",
            "message_binding",
            "session_head",
            "system_message",
            "team_worker_state",
            "topic_schedule",
        ):
            conn.execute(f"DELETE FROM {table} WHERE updated_at < ?", (cutoff,))

    def load_snapshot(self) -> TelegramStateSnapshot:
        conn = self._require_conn()
        route_rows = conn.execute(
            """
            SELECT
                chat_id,
                thread_id,
                session_id,
                topic_title,
                topic_icon_custom_emoji_id,
                child_fork_count,
                child_fork_base_title,
                notify_on_completion,
                last_inbound_message_id
            FROM route_state
            ORDER BY updated_at ASC
            """
        ).fetchall()
        binding_rows = conn.execute(
            """
            SELECT
                chat_id,
                message_id,
                session_id,
                jsonl_uuid,
                role,
                route_chat_id,
                route_thread_id
            FROM message_binding
            ORDER BY updated_at ASC
            """
        ).fetchall()
        system_rows = conn.execute(
            """
            SELECT
                chat_id,
                message_id,
                route_chat_id,
                route_thread_id
            FROM system_message
            ORDER BY updated_at ASC
            """
        ).fetchall()
        head_rows = conn.execute(
            "SELECT session_id, jsonl_uuid FROM session_head"
        ).fetchall()
        team_worker_rows = conn.execute(
            """
            SELECT
                team_name,
                agent_name,
                task_id,
                child_chat_id,
                child_thread_id,
                child_session_id,
                description,
                status,
                idle_ready
            FROM team_worker_state
            ORDER BY updated_at ASC
            """
        ).fetchall()
        schedule_rows = conn.execute(
            """
            SELECT
                schedule_id,
                chat_id,
                thread_id,
                description,
                schedule_mode,
                cron_expr,
                trigger_kind,
                interval_seconds,
                prompt,
                run_mode,
                recurring,
                enabled,
                run_count,
                max_runs,
                from_ts,
                until_ts,
                inherit_mode,
                next_run_at,
                last_run_at,
                last_success_at,
                last_error,
                max_retry_attempts,
                retry_delay_seconds,
                retry_attempt_count
            FROM topic_schedule
            ORDER BY updated_at ASC
            """
        ).fetchall()

        route_states = [
            PersistedRouteState(
                chat_id=int(row["chat_id"]),
                thread_id=int(row["thread_id"]) if row["thread_id"] is not None else None,
                session_id=str(row["session_id"]) if row["session_id"] else None,
                topic_title=str(row["topic_title"]) if row["topic_title"] else None,
                topic_icon_custom_emoji_id=(
                    str(row["topic_icon_custom_emoji_id"])
                    if row["topic_icon_custom_emoji_id"]
                    else None
                ),
                child_fork_count=max(int(row["child_fork_count"] or 0), 0),
                child_fork_base_title=(
                    str(row["child_fork_base_title"]) if row["child_fork_base_title"] else None
                ),
                notify_on_completion=bool(int(row["notify_on_completion"] or 0)),
                last_inbound_message_id=(
                    int(row["last_inbound_message_id"])
                    if row["last_inbound_message_id"] is not None
                    else None
                ),
            )
            for row in route_rows
        ]
        message_bindings = [
            PersistedMessageBinding(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                session_id=str(row["session_id"]),
                jsonl_uuid=str(row["jsonl_uuid"]),
                role=str(row["role"]),
                route_chat_id=int(row["route_chat_id"]),
                route_thread_id=(
                    int(row["route_thread_id"]) if row["route_thread_id"] is not None else None
                ),
            )
            for row in binding_rows
        ]
        system_messages = [
            PersistedSystemMessage(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                route_chat_id=int(row["route_chat_id"]),
                route_thread_id=(
                    int(row["route_thread_id"]) if row["route_thread_id"] is not None else None
                ),
            )
            for row in system_rows
        ]
        session_heads = {
            str(row["session_id"]): str(row["jsonl_uuid"])
            for row in head_rows
            if row["session_id"] and row["jsonl_uuid"]
        }
        team_worker_states = [
            PersistedTeamWorkerState(
                team_name=str(row["team_name"]),
                agent_name=str(row["agent_name"]),
                task_id=str(row["task_id"]),
                child_chat_id=int(row["child_chat_id"]),
                child_thread_id=(
                    int(row["child_thread_id"]) if row["child_thread_id"] is not None else None
                ),
                child_session_id=str(row["child_session_id"]),
                description=str(row["description"]) if row["description"] else None,
                status=str(row["status"]),
                idle_ready=bool(int(row["idle_ready"] or 0)),
            )
            for row in team_worker_rows
            if row["team_name"] and row["agent_name"] and row["task_id"] and row["child_session_id"]
        ]
        topic_schedules = [
            PersistedTopicSchedule(
                schedule_id=str(row["schedule_id"]),
                chat_id=int(row["chat_id"]),
                thread_id=(
                    int(row["thread_id"]) if row["thread_id"] is not None else None
                ),
                description=str(row["description"]) if row["description"] else None,
                schedule_mode=str(row["schedule_mode"] or "interval"),
                cron_expr=str(row["cron_expr"]) if row["cron_expr"] else None,
                trigger_kind=str(row["trigger_kind"] or "interval"),
                interval_seconds=(
                    int(row["interval_seconds"])
                    if row["interval_seconds"] is not None
                    else None
                ),
                prompt=str(row["prompt"]),
                run_mode=str(row["run_mode"] or "continue"),
                recurring=bool(int(row["recurring"] or 0)),
                enabled=bool(int(row["enabled"] or 0)),
                run_count=max(int(row["run_count"] or 0), 0),
                max_runs=(
                    int(row["max_runs"])
                    if row["max_runs"] is not None
                    else None
                ),
                from_ts=(
                    float(row["from_ts"])
                    if row["from_ts"] is not None
                    else None
                ),
                until_ts=(
                    float(row["until_ts"])
                    if row["until_ts"] is not None
                    else None
                ),
                inherit_mode=str(row["inherit_mode"] or "none"),
                next_run_at=(
                    float(row["next_run_at"])
                    if row["next_run_at"] is not None
                    else None
                ),
                last_run_at=(
                    float(row["last_run_at"])
                    if row["last_run_at"] is not None
                    else None
                ),
                last_success_at=(
                    float(row["last_success_at"])
                    if row["last_success_at"] is not None
                    else None
                ),
                last_error=str(row["last_error"]) if row["last_error"] else None,
                max_retry_attempts=max(int(row["max_retry_attempts"] or 0), 0),
                retry_delay_seconds=max(int(row["retry_delay_seconds"] or 30), 1),
                retry_attempt_count=max(int(row["retry_attempt_count"] or 0), 0),
            )
            for row in schedule_rows
            if row["schedule_id"] and row["prompt"]
        ]
        return TelegramStateSnapshot(
            route_states=route_states,
            message_bindings=message_bindings,
            system_messages=system_messages,
            session_heads=session_heads,
            team_worker_states=team_worker_states,
            topic_schedules=topic_schedules,
        )

    def upsert_route_state(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        session_id: str | None,
        topic_title: str | None,
        topic_icon_custom_emoji_id: str | None,
        child_fork_count: int,
        child_fork_base_title: str | None,
        notify_on_completion: bool,
        last_inbound_message_id: int | None,
    ) -> None:
        conn = self._require_conn()
        now = time.time()
        route_key = _route_key(chat_id, thread_id)
        conn.execute(
            """
            INSERT INTO route_state (
                route_key,
                chat_id,
                thread_id,
                session_id,
                topic_title,
                topic_icon_custom_emoji_id,
                child_fork_count,
                child_fork_base_title,
                notify_on_completion,
                last_inbound_message_id,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(route_key) DO UPDATE SET
                session_id=excluded.session_id,
                topic_title=excluded.topic_title,
                topic_icon_custom_emoji_id=excluded.topic_icon_custom_emoji_id,
                child_fork_count=excluded.child_fork_count,
                child_fork_base_title=excluded.child_fork_base_title,
                notify_on_completion=excluded.notify_on_completion,
                last_inbound_message_id=excluded.last_inbound_message_id,
                updated_at=excluded.updated_at
            """,
            (
                route_key,
                chat_id,
                thread_id,
                session_id,
                topic_title,
                topic_icon_custom_emoji_id,
                max(int(child_fork_count), 0),
                child_fork_base_title,
                1 if notify_on_completion else 0,
                last_inbound_message_id,
                now,
            ),
        )

    def delete_route_state(self, *, chat_id: int, thread_id: int | None) -> None:
        conn = self._require_conn()
        conn.execute(
            "DELETE FROM route_state WHERE route_key = ?",
            (_route_key(chat_id, thread_id),),
        )

    def upsert_message_binding(
        self,
        *,
        chat_id: int,
        message_id: int,
        session_id: str,
        jsonl_uuid: str,
        role: str,
        route_chat_id: int,
        route_thread_id: int | None,
    ) -> None:
        conn = self._require_conn()
        now = time.time()
        conn.execute(
            """
            INSERT INTO message_binding (
                chat_id,
                message_id,
                session_id,
                jsonl_uuid,
                role,
                route_key,
                route_chat_id,
                route_thread_id,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                session_id=excluded.session_id,
                jsonl_uuid=excluded.jsonl_uuid,
                role=excluded.role,
                route_key=excluded.route_key,
                route_chat_id=excluded.route_chat_id,
                route_thread_id=excluded.route_thread_id,
                updated_at=excluded.updated_at
            """,
            (
                chat_id,
                message_id,
                session_id,
                jsonl_uuid,
                role,
                _route_key(route_chat_id, route_thread_id),
                route_chat_id,
                route_thread_id,
                now,
            ),
        )

    def delete_message_bindings_for_route(self, *, chat_id: int, thread_id: int | None) -> None:
        conn = self._require_conn()
        conn.execute(
            "DELETE FROM message_binding WHERE route_key = ?",
            (_route_key(chat_id, thread_id),),
        )

    def upsert_system_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        route_chat_id: int,
        route_thread_id: int | None,
    ) -> None:
        conn = self._require_conn()
        now = time.time()
        conn.execute(
            """
            INSERT INTO system_message (
                chat_id,
                message_id,
                route_key,
                route_chat_id,
                route_thread_id,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                route_key=excluded.route_key,
                route_chat_id=excluded.route_chat_id,
                route_thread_id=excluded.route_thread_id,
                updated_at=excluded.updated_at
            """,
            (
                chat_id,
                message_id,
                _route_key(route_chat_id, route_thread_id),
                route_chat_id,
                route_thread_id,
                now,
            ),
        )

    def delete_system_messages_for_route(self, *, chat_id: int, thread_id: int | None) -> None:
        conn = self._require_conn()
        conn.execute(
            "DELETE FROM system_message WHERE route_key = ?",
            (_route_key(chat_id, thread_id),),
        )

    def upsert_session_head(self, *, session_id: str, jsonl_uuid: str) -> None:
        conn = self._require_conn()
        now = time.time()
        conn.execute(
            """
            INSERT INTO session_head (session_id, jsonl_uuid, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                jsonl_uuid=excluded.jsonl_uuid,
                updated_at=excluded.updated_at
            """,
            (session_id, jsonl_uuid, now),
        )

    def delete_session_head(self, *, session_id: str) -> None:
        conn = self._require_conn()
        conn.execute("DELETE FROM session_head WHERE session_id = ?", (session_id,))

    def upsert_team_worker_state(
        self,
        *,
        team_name: str,
        agent_name: str,
        task_id: str,
        child_chat_id: int,
        child_thread_id: int | None,
        child_session_id: str,
        description: str | None,
        status: str,
        idle_ready: bool,
    ) -> None:
        conn = self._require_conn()
        now = time.time()
        conn.execute(
            """
            INSERT INTO team_worker_state (
                team_name,
                agent_name,
                task_id,
                child_chat_id,
                child_thread_id,
                child_session_id,
                description,
                status,
                idle_ready,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_name, agent_name) DO UPDATE SET
                task_id=excluded.task_id,
                child_chat_id=excluded.child_chat_id,
                child_thread_id=excluded.child_thread_id,
                child_session_id=excluded.child_session_id,
                description=excluded.description,
                status=excluded.status,
                idle_ready=excluded.idle_ready,
                updated_at=excluded.updated_at
            """,
            (
                team_name,
                agent_name,
                task_id,
                child_chat_id,
                child_thread_id,
                child_session_id,
                description,
                status,
                1 if idle_ready else 0,
                now,
            ),
        )

    def delete_team_worker_state(self, *, team_name: str, agent_name: str) -> None:
        conn = self._require_conn()
        conn.execute(
            "DELETE FROM team_worker_state WHERE team_name = ? AND agent_name = ?",
            (team_name, agent_name),
        )

    def delete_team_worker_state_by_task_id(self, *, task_id: str) -> None:
        conn = self._require_conn()
        conn.execute(
            "DELETE FROM team_worker_state WHERE task_id = ?",
            (task_id,),
        )

    def delete_team_worker_states_for_route(self, *, chat_id: int, thread_id: int | None) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            DELETE FROM team_worker_state
            WHERE child_chat_id = ?
              AND (
                child_thread_id = ?
                OR (child_thread_id IS NULL AND ? IS NULL)
              )
            """,
            (chat_id, thread_id, thread_id),
        )

    def upsert_topic_schedule(
        self,
        *,
        schedule_id: str,
        chat_id: int,
        thread_id: int | None,
        description: str | None,
        schedule_mode: str,
        cron_expr: str | None,
        trigger_kind: str,
        interval_seconds: int | None,
        prompt: str,
        run_mode: str,
        recurring: bool,
        enabled: bool,
        run_count: int,
        max_runs: int | None,
        from_ts: float | None,
        until_ts: float | None,
        inherit_mode: str,
        next_run_at: float | None,
        last_run_at: float | None,
        last_success_at: float | None,
        last_error: str | None,
        max_retry_attempts: int,
        retry_delay_seconds: int,
        retry_attempt_count: int,
    ) -> None:
        conn = self._require_conn()
        now = time.time()
        conn.execute(
            """
            INSERT INTO topic_schedule (
                schedule_id,
                route_key,
                chat_id,
                thread_id,
                description,
                schedule_mode,
                cron_expr,
                trigger_kind,
                interval_seconds,
                prompt,
                run_mode,
                recurring,
                enabled,
                run_count,
                max_runs,
                from_ts,
                until_ts,
                inherit_mode,
                next_run_at,
                last_run_at,
                last_success_at,
                last_error,
                max_retry_attempts,
                retry_delay_seconds,
                retry_attempt_count,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(schedule_id) DO UPDATE SET
                route_key=excluded.route_key,
                chat_id=excluded.chat_id,
                thread_id=excluded.thread_id,
                description=excluded.description,
                schedule_mode=excluded.schedule_mode,
                cron_expr=excluded.cron_expr,
                trigger_kind=excluded.trigger_kind,
                interval_seconds=excluded.interval_seconds,
                prompt=excluded.prompt,
                run_mode=excluded.run_mode,
                recurring=excluded.recurring,
                enabled=excluded.enabled,
                run_count=excluded.run_count,
                max_runs=excluded.max_runs,
                from_ts=excluded.from_ts,
                until_ts=excluded.until_ts,
                inherit_mode=excluded.inherit_mode,
                next_run_at=excluded.next_run_at,
                last_run_at=excluded.last_run_at,
                last_success_at=excluded.last_success_at,
                last_error=excluded.last_error,
                max_retry_attempts=excluded.max_retry_attempts,
                retry_delay_seconds=excluded.retry_delay_seconds,
                retry_attempt_count=excluded.retry_attempt_count,
                updated_at=excluded.updated_at
            """,
            (
                schedule_id,
                _route_key(chat_id, thread_id),
                chat_id,
                thread_id,
                description,
                schedule_mode,
                cron_expr,
                trigger_kind,
                interval_seconds,
                prompt,
                run_mode,
                1 if recurring else 0,
                1 if enabled else 0,
                max(int(run_count), 0),
                max_runs,
                from_ts,
                until_ts,
                inherit_mode,
                next_run_at,
                last_run_at,
                last_success_at,
                last_error,
                max(int(max_retry_attempts), 0),
                max(int(retry_delay_seconds), 1),
                max(int(retry_attempt_count), 0),
                now,
            ),
        )

    def delete_topic_schedule(self, *, schedule_id: str) -> None:
        conn = self._require_conn()
        conn.execute("DELETE FROM topic_schedule WHERE schedule_id = ?", (schedule_id,))

    def delete_topic_schedules_for_route(self, *, chat_id: int, thread_id: int | None) -> None:
        conn = self._require_conn()
        conn.execute(
            "DELETE FROM topic_schedule WHERE route_key = ?",
            (_route_key(chat_id, thread_id),),
        )
