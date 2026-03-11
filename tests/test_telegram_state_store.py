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
    store.delete_topic_schedules_for_route(chat_id=-1003, thread_id=123)

    snapshot = store.load_snapshot()
    assert snapshot.route_states == []
    assert snapshot.message_bindings == []
    assert snapshot.system_messages == []
    assert snapshot.team_worker_states == []
    assert snapshot.topic_schedules == []
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
