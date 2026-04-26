from __future__ import annotations

from unittest.mock import patch

from obs_agent.telegram_state_store import TelegramStateStore


def test_state_store_roundtrip_snapshot(tmp_path):
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-1001,
        thread_id=42,
        session_id="sid-1",
        topic_title="General - Worker",
        topic_icon_custom_emoji_id="emoji-1",
        child_fork_count=3,
        child_fork_base_title="General",
        notify_on_completion=False,
        last_inbound_message_id=777,
        agent_lineage=("General", "Worker"),
        pending_obs_bootstrap=(
            "<obs-bootstrap version='1'><obs-lineage>"
            "<obs-node name='General' /><obs-node name='Worker' />"
            "</obs-lineage></obs-bootstrap>"
        ),
    )
    store.upsert_message_binding(
        chat_id=-1001,
        message_id=900,
        session_id="sid-1",
        jsonl_uuid="uuid-1",
        role="assistant",
        route_chat_id=-1001,
        route_thread_id=42,
    )
    store.upsert_system_message(
        chat_id=-1001,
        message_id=901,
        route_chat_id=-1001,
        route_thread_id=42,
    )
    store.upsert_session_head(session_id="sid-1", jsonl_uuid="uuid-1")
    store.upsert_team_worker_state(
        team_name="team-alpha",
        agent_name="worker-a",
        task_id="task-1",
        child_chat_id=-1001,
        child_thread_id=42,
        child_session_id="sid-child",
        description="Team worker",
        status="completed",
        idle_ready=True,
    )
    store.upsert_task_handle_state(
        task_id="task-1",
        parent_chat_id=-1001,
        parent_thread_id=42,
        parent_session_id_at_launch="sid-1",
        parent_source_uuid="uuid-1",
        child_chat_id=-1001,
        child_thread_id=99,
        child_session_id="sid-child",
        description="Team worker",
        status="completed",
        is_fork=False,
        launch_tool_name="AgentTask",
        team_name="team-alpha",
        agent_name="worker-a",
        idle_ready=True,
        terminal_request=None,
        result_text="done",
        error=None,
        timeout_ms=600000,
        max_turns=20,
        launch_parent_message_id=902,
        launch_child_message_id=903,
        child_completion_message_id=904,
        parent_callback_message_id=905,
        created_at=123.0,
        completed_at=456.0,
    )
    store.upsert_topic_schedule(
        schedule_id="sched-1",
        chat_id=-1001,
        thread_id=42,
        description="Nightly",
        schedule_mode="interval",
        cron_expr="0 0 */1 * *",
        trigger_kind="interval",
        interval_seconds=86400,
        prompt="Run maintenance",
        run_mode="continue",
        recurring=True,
        enabled=True,
        run_count=2,
        max_runs=10,
        from_ts=None,
        until_ts=None,
        inherit_mode="none",
        next_run_at=123456.0,
        last_run_at=120000.0,
        last_success_at=120001.0,
        last_error=None,
        max_retry_attempts=2,
        retry_delay_seconds=45,
        retry_attempt_count=1,
    )

    snapshot = store.load_snapshot()
    assert len(snapshot.route_states) == 1
    route_state = snapshot.route_states[0]
    assert route_state.chat_id == -1001
    assert route_state.thread_id == 42
    assert route_state.session_id == "sid-1"
    assert route_state.topic_title == "General - Worker"
    assert route_state.topic_icon_custom_emoji_id == "emoji-1"
    assert route_state.child_fork_count == 3
    assert route_state.child_fork_base_title == "General"
    assert route_state.notify_on_completion is False
    assert route_state.last_inbound_message_id == 777
    assert route_state.agent_lineage == ("General", "Worker")
    assert route_state.pending_obs_bootstrap is not None
    assert "<obs-bootstrap" in route_state.pending_obs_bootstrap

    assert len(snapshot.message_bindings) == 1
    binding = snapshot.message_bindings[0]
    assert binding.chat_id == -1001
    assert binding.message_id == 900
    assert binding.session_id == "sid-1"
    assert binding.jsonl_uuid == "uuid-1"
    assert binding.route_chat_id == -1001
    assert binding.route_thread_id == 42

    assert len(snapshot.system_messages) == 1
    system_message = snapshot.system_messages[0]
    assert system_message.chat_id == -1001
    assert system_message.message_id == 901
    assert system_message.route_chat_id == -1001
    assert system_message.route_thread_id == 42

    assert snapshot.session_heads == {"sid-1": "uuid-1"}
    assert len(snapshot.team_worker_states) == 1
    worker = snapshot.team_worker_states[0]
    assert worker.team_name == "team-alpha"
    assert worker.agent_name == "worker-a"
    assert worker.task_id == "task-1"
    assert worker.child_chat_id == -1001
    assert worker.child_thread_id == 42
    assert worker.child_session_id == "sid-child"
    assert worker.description == "Team worker"
    assert worker.status == "completed"
    assert worker.idle_ready is True
    assert len(snapshot.task_handle_states) == 1
    handle = snapshot.task_handle_states[0]
    assert handle.task_id == "task-1"
    assert handle.parent_chat_id == -1001
    assert handle.parent_thread_id == 42
    assert handle.child_chat_id == -1001
    assert handle.child_thread_id == 99
    assert handle.child_session_id == "sid-child"
    assert handle.status == "completed"
    assert handle.is_fork is False
    assert handle.launch_tool_name == "AgentTask"
    assert handle.team_name == "team-alpha"
    assert handle.agent_name == "worker-a"
    assert handle.idle_ready is True
    assert handle.result_text == "done"
    assert handle.timeout_ms == 600000
    assert handle.max_turns == 20
    assert handle.launch_parent_message_id == 902
    assert handle.launch_child_message_id == 903
    assert handle.child_completion_message_id == 904
    assert handle.parent_callback_message_id == 905
    assert handle.created_at == 123.0
    assert handle.completed_at == 456.0
    assert len(snapshot.topic_schedules) == 1
    schedule = snapshot.topic_schedules[0]
    assert schedule.schedule_id == "sched-1"
    assert schedule.chat_id == -1001
    assert schedule.thread_id == 42
    assert schedule.description == "Nightly"
    assert schedule.schedule_mode == "interval"
    assert schedule.trigger_kind == "interval"
    assert schedule.interval_seconds == 86400
    assert schedule.prompt == "Run maintenance"
    assert schedule.run_mode == "continue"
    assert schedule.recurring is True
    assert schedule.enabled is True
    assert schedule.run_count == 2
    assert schedule.max_runs == 10
    assert schedule.max_retry_attempts == 2
    assert schedule.retry_delay_seconds == 45
    assert schedule.retry_attempt_count == 1
    store.close()


def test_state_store_keeps_fresh_task_rows_without_child_session_id(tmp_path):
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_team_worker_state(
        team_name="team-alpha",
        agent_name="worker-a",
        task_id="task-fresh",
        child_chat_id=-1001,
        child_thread_id=42,
        child_session_id="",
        description="Fresh worker",
        status="launched",
        idle_ready=False,
    )
    store.upsert_task_handle_state(
        task_id="task-fresh",
        parent_chat_id=-1001,
        parent_thread_id=1,
        parent_session_id_at_launch="sid-parent",
        parent_source_uuid="uuid-parent",
        child_chat_id=-1001,
        child_thread_id=42,
        child_session_id="",
        description="Fresh worker",
        status="launched",
        is_fork=False,
        launch_tool_name="AgentTask",
        team_name="team-alpha",
        agent_name="worker-a",
        idle_ready=False,
        terminal_request=None,
        result_text=None,
        error=None,
        timeout_ms=600000,
        max_turns=8,
        launch_parent_message_id=10,
        launch_child_message_id=11,
        child_completion_message_id=None,
        parent_callback_message_id=None,
        created_at=123.0,
        completed_at=None,
    )

    snapshot = store.load_snapshot()
    assert len(snapshot.team_worker_states) == 1
    assert snapshot.team_worker_states[0].child_session_id == ""
    assert len(snapshot.task_handle_states) == 1
    assert snapshot.task_handle_states[0].child_session_id == ""
    store.close()


def test_state_store_prune_removes_expired_rows(tmp_path):
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    with patch("obs_agent.telegram_state_store.time.time", return_value=10.0):
        store.upsert_route_state(
            chat_id=-1002,
            thread_id=None,
            session_id="sid-old",
            topic_title="General",
            topic_icon_custom_emoji_id=None,
            child_fork_count=0,
            child_fork_base_title=None,
            notify_on_completion=True,
            last_inbound_message_id=1,
        )
        store.upsert_message_binding(
            chat_id=-1002,
            message_id=1,
            session_id="sid-old",
            jsonl_uuid="uuid-old",
            role="assistant",
            route_chat_id=-1002,
            route_thread_id=None,
        )
        store.upsert_system_message(
            chat_id=-1002,
            message_id=2,
            route_chat_id=-1002,
            route_thread_id=None,
        )
        store.upsert_session_head(session_id="sid-old", jsonl_uuid="uuid-old")
        store.upsert_team_worker_state(
            team_name="team-old",
            agent_name="worker-old",
            task_id="task-old",
            child_chat_id=-1002,
            child_thread_id=None,
            child_session_id="sid-old-child",
            description=None,
            status="completed",
            idle_ready=True,
        )
        store.upsert_task_handle_state(
            task_id="task-old",
            parent_chat_id=-1002,
            parent_thread_id=None,
            parent_session_id_at_launch="sid-old",
            parent_source_uuid="uuid-old",
            child_chat_id=-1002,
            child_thread_id=None,
            child_session_id="sid-old-child",
            description=None,
            status="completed",
            is_fork=False,
            launch_tool_name="AgentTask",
            team_name="team-old",
            agent_name="worker-old",
            idle_ready=True,
            terminal_request=None,
            result_text=None,
            error=None,
            timeout_ms=600000,
            max_turns=None,
            launch_parent_message_id=None,
            launch_child_message_id=None,
            child_completion_message_id=None,
            parent_callback_message_id=None,
            created_at=10.0,
            completed_at=10.0,
        )
        store.upsert_topic_schedule(
            schedule_id="sched-old",
            chat_id=-1002,
            thread_id=None,
            description=None,
            schedule_mode="interval",
            cron_expr="*/5 * * * *",
            trigger_kind="interval",
            interval_seconds=300,
            prompt="old",
            run_mode="continue",
            recurring=True,
            enabled=True,
            run_count=0,
            max_runs=None,
            from_ts=None,
            until_ts=None,
            inherit_mode="none",
            next_run_at=20.0,
            last_run_at=None,
            last_success_at=None,
            last_error=None,
            max_retry_attempts=0,
            retry_delay_seconds=30,
            retry_attempt_count=0,
        )

    with patch("obs_agent.telegram_state_store.time.time", return_value=(35 * 24 * 60 * 60)):
        store.prune(retention_days=30)

    snapshot = store.load_snapshot()
    assert snapshot.route_states == []
    assert snapshot.message_bindings == []
    assert snapshot.system_messages == []
    assert snapshot.session_heads == {}
    assert snapshot.team_worker_states == []
    assert snapshot.task_handle_states == []
    assert snapshot.topic_schedules == []
    store.close()


def test_state_store_delete_route_and_bindings(tmp_path):
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()
    store.upsert_route_state(
        chat_id=-1003,
        thread_id=123,
        session_id="sid-3",
        topic_title="Topic",
        topic_icon_custom_emoji_id=None,
        child_fork_count=0,
        child_fork_base_title=None,
        notify_on_completion=True,
        last_inbound_message_id=None,
    )
    store.upsert_message_binding(
        chat_id=-1003,
        message_id=31,
        session_id="sid-3",
        jsonl_uuid="uuid-3",
        role="assistant",
        route_chat_id=-1003,
        route_thread_id=123,
    )
    store.upsert_system_message(
        chat_id=-1003,
        message_id=32,
        route_chat_id=-1003,
        route_thread_id=123,
    )
    store.delete_message_bindings_for_route(chat_id=-1003, thread_id=123)
    store.delete_system_messages_for_route(chat_id=-1003, thread_id=123)
    store.delete_route_state(chat_id=-1003, thread_id=123)
    store.delete_session_head(session_id="sid-3")
    store.upsert_team_worker_state(
        team_name="team-gamma",
        agent_name="worker-g",
        task_id="task-3",
        child_chat_id=-1003,
        child_thread_id=123,
        child_session_id="sid-3-child",
        description="Worker",
        status="completed",
        idle_ready=True,
    )
    store.upsert_topic_schedule(
        schedule_id="sched-3",
        chat_id=-1003,
        thread_id=123,
        description=None,
        schedule_mode="interval",
        cron_expr="@hourly",
        trigger_kind="interval",
        interval_seconds=3600,
        prompt="check",
        run_mode="continue",
        recurring=True,
        enabled=True,
        run_count=0,
        max_runs=None,
        from_ts=None,
        until_ts=None,
        inherit_mode="none",
        next_run_at=10.0,
        last_run_at=None,
        last_success_at=None,
        last_error=None,
        max_retry_attempts=0,
        retry_delay_seconds=30,
        retry_attempt_count=0,
    )
    store.delete_team_worker_states_for_route(chat_id=-1003, thread_id=123)
    store.upsert_task_handle_state(
        task_id="task-3",
        parent_chat_id=-1003,
        parent_thread_id=123,
        parent_session_id_at_launch="sid-3",
        parent_source_uuid="uuid-3",
        child_chat_id=-1003,
        child_thread_id=123,
        child_session_id="sid-3-child",
        description="Worker",
        status="completed",
        is_fork=False,
        launch_tool_name="AgentTask",
        team_name="team-gamma",
        agent_name="worker-g",
        idle_ready=True,
        terminal_request=None,
        result_text=None,
        error=None,
        timeout_ms=600000,
        max_turns=10,
        launch_parent_message_id=None,
        launch_child_message_id=None,
        child_completion_message_id=None,
        parent_callback_message_id=None,
        created_at=1.0,
        completed_at=2.0,
    )
    store.delete_task_handle_states_for_route(chat_id=-1003, thread_id=123)
    store.delete_topic_schedules_for_route(chat_id=-1003, thread_id=123)

    snapshot = store.load_snapshot()
    assert snapshot.route_states == []
    assert snapshot.message_bindings == []
    assert snapshot.system_messages == []
    assert snapshot.team_worker_states == []
    assert snapshot.task_handle_states == []
    assert snapshot.topic_schedules == []
    store.close()


def test_model_override_roundtrip(tmp_path):
    """model_override persists through save/restore cycle."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-2001,
        thread_id=100,
        session_id="sid-gpt",
        topic_title="GPT Session",
        topic_icon_custom_emoji_id=None,
        child_fork_count=0,
        child_fork_base_title=None,
        notify_on_completion=True,
        last_inbound_message_id=1,
        model_override="gpt-5-codex-mini",
    )

    snapshot = store.load_snapshot()
    assert len(snapshot.route_states) == 1
    assert snapshot.route_states[0].model_override == "gpt-5-codex-mini"
    store.close()


def test_user_hooks_json_roundtrip(tmp_path):
    """user_hooks_json persists through save/restore cycle."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    hooks_json = '{"PreToolUse": "guards/check.py::validate_tool"}'
    store.upsert_route_state(
        chat_id=-2002,
        thread_id=101,
        session_id="sid-hooks",
        topic_title="Hooks Session",
        topic_icon_custom_emoji_id=None,
        child_fork_count=0,
        child_fork_base_title=None,
        notify_on_completion=True,
        last_inbound_message_id=2,
        user_hooks_json=hooks_json,
    )

    snapshot = store.load_snapshot()
    assert len(snapshot.route_states) == 1
    assert snapshot.route_states[0].user_hooks_json == hooks_json
    import json
    parsed = json.loads(snapshot.route_states[0].user_hooks_json)
    assert parsed == {"PreToolUse": "guards/check.py::validate_tool"}
    store.close()


def test_model_override_and_hooks_null_roundtrip(tmp_path):
    """None values for model_override and user_hooks_json persist correctly."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-2003,
        thread_id=102,
        session_id="sid-claude",
        topic_title="Claude Session",
        topic_icon_custom_emoji_id=None,
        child_fork_count=0,
        child_fork_base_title=None,
        notify_on_completion=True,
        last_inbound_message_id=3,
        # model_override and user_hooks_json default to None
    )

    snapshot = store.load_snapshot()
    assert len(snapshot.route_states) == 1
    assert snapshot.route_states[0].model_override is None
    assert snapshot.route_states[0].user_hooks_json is None
    store.close()


def test_model_override_update_preserves_value(tmp_path):
    """Updating a route state preserves model_override on upsert."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-2004,
        thread_id=103,
        session_id="sid-update",
        topic_title="Update Test",
        topic_icon_custom_emoji_id=None,
        child_fork_count=0,
        child_fork_base_title=None,
        notify_on_completion=True,
        last_inbound_message_id=4,
        model_override="gemini-3.1-flash-lite-preview",
    )
    # Update fork count but keep the same model_override
    store.upsert_route_state(
        chat_id=-2004,
        thread_id=103,
        session_id="sid-update",
        topic_title="Update Test",
        topic_icon_custom_emoji_id=None,
        child_fork_count=2,
        child_fork_base_title="Update Test",
        notify_on_completion=True,
        last_inbound_message_id=5,
        model_override="gemini-3.1-flash-lite-preview",
    )

    snapshot = store.load_snapshot()
    assert len(snapshot.route_states) == 1
    assert snapshot.route_states[0].child_fork_count == 2
    assert snapshot.route_states[0].model_override == "gemini-3.1-flash-lite-preview"
    store.close()


def test_schema_migration_adds_columns(tmp_path):
    """Schema migration adds model_override and user_hooks_json to existing DBs."""
    db_path = tmp_path / "telegram-state.sqlite3"
    # Create a DB with the old schema (no model_override/user_hooks_json columns)
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
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
            agent_lineage_json TEXT,
            pending_obs_bootstrap TEXT,
            updated_at REAL
        )
    """)
    # Insert a row using the old schema
    conn.execute(
        "INSERT INTO route_state (route_key, chat_id, thread_id, session_id, "
        "topic_title, child_fork_count, notify_on_completion, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("-2005:104", -2005, 104, "sid-old-schema", "Old Schema", 0, 1, 1.0),
    )
    conn.commit()
    conn.close()

    # Now open with TelegramStateStore — migration should add the columns
    store = TelegramStateStore(db_path)
    store.initialize()

    snapshot = store.load_snapshot()
    assert len(snapshot.route_states) == 1
    assert snapshot.route_states[0].session_id == "sid-old-schema"
    assert snapshot.route_states[0].model_override is None
    assert snapshot.route_states[0].user_hooks_json is None

    # Now upsert with the new fields — should work on the migrated schema
    store.upsert_route_state(
        chat_id=-2005,
        thread_id=104,
        session_id="sid-old-schema",
        topic_title="Old Schema",
        topic_icon_custom_emoji_id=None,
        child_fork_count=0,
        child_fork_base_title=None,
        notify_on_completion=True,
        last_inbound_message_id=None,
        model_override="gpt-5-codex-mini",
        user_hooks_json='{"PreToolUse": "hook.py::fn"}',
    )

    snapshot = store.load_snapshot()
    assert snapshot.route_states[0].model_override == "gpt-5-codex-mini"
    assert snapshot.route_states[0].user_hooks_json == '{"PreToolUse": "hook.py::fn"}'
    store.close()


def test_existing_roundtrip_includes_new_fields(tmp_path):
    """The existing comprehensive roundtrip test's route_state has None for new fields."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    # Use the same pattern as the existing roundtrip test but verify new fields
    store.upsert_route_state(
        chat_id=-2006,
        thread_id=105,
        session_id="sid-compat",
        topic_title="Compat Test",
        topic_icon_custom_emoji_id=None,
        child_fork_count=0,
        child_fork_base_title=None,
        notify_on_completion=True,
        last_inbound_message_id=None,
    )

    snapshot = store.load_snapshot()
    route = snapshot.route_states[0]
    # New fields default to None when not passed
    assert route.model_override is None
    assert route.user_hooks_json is None
    store.close()


def test_model_override_empty_hooks_dict(tmp_path):
    """Empty hooks dict '{}' persists correctly (not treated as None)."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-3001, thread_id=200, session_id="sid-empty-hooks",
        topic_title="Empty Hooks", topic_icon_custom_emoji_id=None,
        child_fork_count=0, child_fork_base_title=None,
        notify_on_completion=True, last_inbound_message_id=1,
        user_hooks_json="{}",
    )

    snapshot = store.load_snapshot()
    assert snapshot.route_states[0].user_hooks_json == "{}"
    import json
    assert json.loads(snapshot.route_states[0].user_hooks_json) == {}
    store.close()


def test_model_override_with_context_suffix(tmp_path):
    """Model with context suffix like 'gpt-5-codex-mini[1m]' persists correctly."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-3002, thread_id=201, session_id="sid-suffix",
        topic_title="Suffix Model", topic_icon_custom_emoji_id=None,
        child_fork_count=0, child_fork_base_title=None,
        notify_on_completion=True, last_inbound_message_id=1,
        model_override="gpt-5-codex-mini[1m]",
    )

    snapshot = store.load_snapshot()
    assert snapshot.route_states[0].model_override == "gpt-5-codex-mini[1m]"
    store.close()


def test_model_override_inherit_value(tmp_path):
    """'inherit' as model_override value persists correctly."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-3003, thread_id=202, session_id="sid-inherit",
        topic_title="Inherit Model", topic_icon_custom_emoji_id=None,
        child_fork_count=0, child_fork_base_title=None,
        notify_on_completion=True, last_inbound_message_id=1,
        model_override="inherit",
    )

    snapshot = store.load_snapshot()
    assert snapshot.route_states[0].model_override == "inherit"
    store.close()


def test_hooks_multi_event_json_roundtrip(tmp_path):
    """Complex hooks JSON with multiple event types roundtrips correctly."""
    import json
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    hooks = {
        "PreToolUse": "guards/check.py::validate",
        "PostToolUse": "hooks/log.py::log_result",
        "Stop": "hooks/cleanup.py::on_stop",
    }
    hooks_json = json.dumps(hooks)
    store.upsert_route_state(
        chat_id=-3004, thread_id=203, session_id="sid-multi-hooks",
        topic_title="Multi Hooks", topic_icon_custom_emoji_id=None,
        child_fork_count=0, child_fork_base_title=None,
        notify_on_completion=True, last_inbound_message_id=1,
        user_hooks_json=hooks_json,
    )

    snapshot = store.load_snapshot()
    parsed = json.loads(snapshot.route_states[0].user_hooks_json)
    assert parsed == hooks
    store.close()


def test_model_override_overwrite_on_upsert(tmp_path):
    """Upsert with a different model_override replaces the old value."""
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()

    store.upsert_route_state(
        chat_id=-3005, thread_id=204, session_id="sid-overwrite",
        topic_title="Overwrite", topic_icon_custom_emoji_id=None,
        child_fork_count=0, child_fork_base_title=None,
        notify_on_completion=True, last_inbound_message_id=1,
        model_override="gpt-5-codex-mini",
    )
    store.upsert_route_state(
        chat_id=-3005, thread_id=204, session_id="sid-overwrite",
        topic_title="Overwrite", topic_icon_custom_emoji_id=None,
        child_fork_count=0, child_fork_base_title=None,
        notify_on_completion=True, last_inbound_message_id=2,
        model_override="gemini-3.1-flash-lite-preview",
    )

    snapshot = store.load_snapshot()
    assert snapshot.route_states[0].model_override == "gemini-3.1-flash-lite-preview"
    assert snapshot.route_states[0].last_inbound_message_id == 2
    store.close()


def test_state_store_delete_team_worker_by_task(tmp_path):
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()
    store.upsert_team_worker_state(
        team_name="team-alpha",
        agent_name="worker-a",
        task_id="task-a",
        child_chat_id=-1004,
        child_thread_id=None,
        child_session_id="sid-a",
        description=None,
        status="completed",
        idle_ready=True,
    )
    store.upsert_team_worker_state(
        team_name="team-alpha",
        agent_name="worker-b",
        task_id="task-b",
        child_chat_id=-1004,
        child_thread_id=None,
        child_session_id="sid-b",
        description=None,
        status="completed",
        idle_ready=True,
    )
    store.delete_team_worker_state_by_task_id(task_id="task-a")

    snapshot = store.load_snapshot()
    assert len(snapshot.team_worker_states) == 1
    assert snapshot.team_worker_states[0].task_id == "task-b"
    store.close()


def test_state_store_delete_task_handle_by_task(tmp_path):
    db_path = tmp_path / "telegram-state.sqlite3"
    store = TelegramStateStore(db_path)
    store.initialize()
    store.upsert_task_handle_state(
        task_id="task-a",
        parent_chat_id=-1004,
        parent_thread_id=None,
        parent_session_id_at_launch="sid-a",
        parent_source_uuid="uuid-a",
        child_chat_id=-1004,
        child_thread_id=None,
        child_session_id="sid-a-child",
        description=None,
        status="completed",
        is_fork=True,
        launch_tool_name="ForkTask",
        team_name=None,
        agent_name=None,
        idle_ready=False,
        terminal_request=None,
        result_text=None,
        error=None,
        timeout_ms=600000,
        max_turns=20,
        launch_parent_message_id=None,
        launch_child_message_id=None,
        child_completion_message_id=None,
        parent_callback_message_id=None,
        created_at=1.0,
        completed_at=2.0,
    )
    store.upsert_task_handle_state(
        task_id="task-b",
        parent_chat_id=-1004,
        parent_thread_id=None,
        parent_session_id_at_launch="sid-b",
        parent_source_uuid="uuid-b",
        child_chat_id=-1004,
        child_thread_id=44,
        child_session_id="sid-b-child",
        description=None,
        status="completed",
        is_fork=True,
        launch_tool_name="ForkTask",
        team_name=None,
        agent_name=None,
        idle_ready=False,
        terminal_request=None,
        result_text=None,
        error=None,
        timeout_ms=600000,
        max_turns=20,
        launch_parent_message_id=None,
        launch_child_message_id=None,
        child_completion_message_id=None,
        parent_callback_message_id=None,
        created_at=1.0,
        completed_at=2.0,
    )

    store.delete_task_handle_state(task_id="task-a")
    snapshot = store.load_snapshot()
    assert len(snapshot.task_handle_states) == 1
    assert snapshot.task_handle_states[0].task_id == "task-b"
    store.close()
