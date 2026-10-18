"""Scoped /unschedule and global /stop all (bead vault-maw0)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent import schedule_scope
from obs_agent.telegram import TelegramBot, TelegramRoute, _TopicScheduleRecord

from tests.test_telegram import _TEST_GAP, _make_context, _make_update, _state

TEAM = "2026-08-12-08-05-root"
OTHER_TEAM = "2026-08-13-09-00-other"
_REAL_ORPHAN_CHECK = schedule_scope._ensure_orphan_check


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["corrupt-map", "backend-failure", "backend-timeout"])
async def test_owner_failure_never_blocks_creation_or_restore(config, ownership_files, monkeypatch, failure):
    import threading
    import subprocess
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._state_store.initialize()
    state = _state(bot, thread_id=119)
    calls = []
    main_thread = threading.get_ident()
    original_run = schedule_scope.subprocess.run
    def failed_backend(command, **kwargs):
        if not str(command[0]).endswith("/bd-safe"):
            return original_run(command, **kwargs)
        calls.append(threading.get_ident())
        if failure == "backend-timeout":
            raise subprocess.TimeoutExpired(command, 60)
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(schedule_scope.subprocess, "run", failed_backend)
    monkeypatch.setattr(schedule_scope, "_ensure_orphan_check", _REAL_ORPHAN_CHECK)
    if failure == "corrupt-map":
        ownership_files[1].write_text("{not json")
    result = await bot._cron_create(route=state.route, args={
        "schedule_mode": "interval", "interval_seconds": 3600,
        "description": "degraded creation", "prompt": "continue",
    })
    sid = result["tool_use_result"]["schedule"]["id"]
    assert sid in bot._topic_schedules_by_id and sid in bot._pending_schedule_owners
    assert result["tool_use_result"]["owner_assignment"]["pending"]
    assert calls == [], "CronCreate must not wait for Beads, even in a worker thread"
    restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    restored._state_store.initialize()
    restored._restore_state_from_store()
    assert sid in restored._topic_schedules_by_id and sid in restored._pending_schedule_owners
    assert calls == [], "startup restore must not run a blocking Beads command"
    await restored._repair_pending_schedule_owners()
    assert sid in restored._topic_schedules_by_id and sid in restored._pending_schedule_owners
    assert all(thread != main_thread for thread in calls)
    if failure == "corrupt-map":
        ownership_files[1].write_text('{"schedules": {}}')
        restored._topic_schedules_by_id[sid].prompt = "Task bead: ops-1"
        await restored._repair_pending_schedule_owners()
        assert sid not in restored._pending_schedule_owners
        assert schedule_scope.ScheduleOwnership.load(config.vault_path).owners[sid]["bead"] == "ops-1"
    else:
        assert calls, "the deferred backend path must actually be exercised"


@pytest.mark.parametrize("failure", ["corrupt-map", "backend-failure", "backend-timeout"])
def test_inheritance_continues_when_ownership_is_unavailable(config, ownership_files, monkeypatch, failure):
    import subprocess
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    parent, child = _state(bot, thread_id=123), _state(bot, thread_id=124)
    parent.inflight_user_text = "Task bead: ops-1"
    _sched(bot, "inherit-errors", parent.route)
    bot._topic_schedules_by_id["inherit-errors"].inherit_mode = "all"
    if failure == "corrupt-map":
        ownership_files[1].write_text("{not json")
    else:
        ownership_files[1].write_text('{"schedules": {}}')
        parent.inflight_user_text = "continue"
        error = subprocess.TimeoutExpired("bd-safe", 60) if failure == "backend-timeout" else subprocess.CalledProcessError(1, "bd-safe")
        monkeypatch.setattr(schedule_scope, "_ensure_orphan_check", MagicMock(side_effect=error))
    bot._inherit_topic_schedules(parent_route=parent.route, child_route=child.route, is_fork=True)
    sid = next(iter(bot._schedule_ids_by_route[child.route]))
    assert sid in bot._topic_schedules_by_id and sid in bot._pending_schedule_owners


@pytest.mark.asyncio
async def test_pending_backend_and_busy_map_do_not_block_event_loop(config, ownership_files, monkeypatch):
    import asyncio
    import threading
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    state = _state(bot, thread_id=122)
    _sched(bot, "slow-owner", state.route)
    entered, release = threading.Event(), threading.Event()
    def backend(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        raise RuntimeError("Beads unavailable")
    monkeypatch.setattr(schedule_scope, "_ensure_orphan_check", backend)
    repair = asyncio.create_task(bot._repair_pending_schedule_owners())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        result = await asyncio.wait_for(bot._cron_create(route=state.route, args={
            "schedule_mode": "interval", "interval_seconds": 3600, "prompt": "continue",
        }), timeout=0.5)
        assert result["tool_use_result"]["schedule"]["id"] in bot._topic_schedules_by_id
        assert not repair.done()
    finally:
        release.set()
        await repair
    assert "slow-owner" in bot._topic_schedules_by_id
    assert "slow-owner" in bot._pending_schedule_owners


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["[ops-missing] work", "[old-1] work"])
async def test_invalid_prefix_cron_resolves_open_task(config, ownership_files, description):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    state = _state(bot, thread_id=120)
    state.inflight_user_text = "Task bead: ops-1"
    result = await bot._cron_create(route=state.route, args={
        "schedule_mode": "interval", "interval_seconds": 3600,
        "description": description, "prompt": "continue",
    })
    assert result["tool_use_result"]["owner_assignment"]["bead"] == "ops-1"
    assert not result["tool_use_result"]["warnings"]


def test_delete_and_route_delete_retire_automatic_owners(config, ownership_files):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    state = _state(bot, thread_id=121)
    state.inflight_user_text = "Task bead: ops-1"
    for sid in ("retire-one", "retire-two"):
        _sched(bot, sid, state.route)
    original = json.loads(ownership_files[1].read_text())["schedules"]["retire-one"]
    bot._delete_topic_schedule("retire-one")
    bot._delete_topic_schedules_for_route(state.route)
    data = json.loads(ownership_files[1].read_text())
    for sid in ("retire-one", "retire-two"):
        assert sid not in data["schedules"]
        assert data["_retired"][sid]["bead"] == "ops-1"
        assert data["_retired"][sid]["retired_at"]
    assert data["_retired"]["retire-one"]["note"] == original["note"]


def test_restore_retires_previously_deleted_proof_mapping(config, ownership_files):
    data = json.loads(ownership_files[1].read_text())
    data["schedules"]["9bd7f535"] = {"bead": "old-1", "note": "stale prior proof"}
    ownership_files[1].write_text(json.dumps(data))
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._state_store.initialize()
    bot._restore_state_from_store()
    final = json.loads(ownership_files[1].read_text())
    assert "9bd7f535" not in final["schedules"]
    assert final["_retired"]["9bd7f535"]["note"] == "stale prior proof"


@pytest.mark.asyncio
async def test_deferred_assignment_cannot_resurrect_deleted_mapping(config, ownership_files, monkeypatch):
    import asyncio
    import threading
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    state = _state(bot, thread_id=125)
    _sched(bot, "slow-owner", state.route)
    entered, release = threading.Event(), threading.Event()
    def backend(vault, owners, **kwargs):
        entered.set()
        assert release.wait(3)
        rows = [json.loads(line) for line in ownership_files[0].read_text().splitlines()]
        rows.append({"id": "ops-slow", "status": "open", "metadata": {}})
        ownership_files[0].write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return "ops-slow"
    monkeypatch.setattr(schedule_scope, "_ensure_orphan_check", backend)
    repair = asyncio.create_task(bot._repair_pending_schedule_owners())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        bot._delete_topic_schedule("slow-owner")
        assert "slow-owner" not in bot._topic_schedules_by_id
    finally:
        release.set()
        await repair
    data = json.loads(ownership_files[1].read_text())
    assert "slow-owner" not in data["schedules"]
    assert data["_retired"]["slow-owner"]["bead"] == "ops-slow"


@pytest.mark.asyncio
async def test_pending_inheritance_keeps_owner_after_parent_delete(config, ownership_files, monkeypatch):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    parent, child = _state(bot, thread_id=126), _state(bot, thread_id=127)
    parent.inflight_user_text = "Task bead: ops-1"
    child.inflight_user_text = "Task bead: reg-1"
    _sched(bot, "retired-parent", parent.route)
    bot._topic_schedules_by_id["retired-parent"].inherit_mode = "all"
    with monkeypatch.context() as scoped:
        scoped.setattr(schedule_scope.os, "replace", MagicMock(side_effect=OSError("map write failed")))
        bot._inherit_topic_schedules(parent_route=parent.route, child_route=child.route, is_fork=True)
    sid = next(iter(bot._schedule_ids_by_route[child.route]))
    assert bot._pending_schedule_owners[sid][1].schedule_id == "retired-parent"
    bot._delete_topic_schedule("retired-parent")
    await bot._repair_pending_schedule_owners()
    owners = schedule_scope.ScheduleOwnership.load(config.vault_path)
    assert owners.owners[sid]["bead"] == "ops-1"
    assert "retired-parent" in owners.retired


@pytest.mark.asyncio
async def test_retirement_failure_keeps_delete_available_and_retries(config, ownership_files, monkeypatch):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    state = _state(bot, thread_id=128)
    state.inflight_user_text = "Task bead: ops-1"
    _sched(bot, "retire-error", state.route)
    with monkeypatch.context() as scoped:
        scoped.setattr(schedule_scope.os, "replace", MagicMock(side_effect=OSError("write unavailable")))
        bot._delete_topic_schedule("retire-error")
    assert "retire-error" not in bot._topic_schedules_by_id
    assert "retire-error" in bot._pending_schedule_owner_retirements
    await bot._repair_pending_schedule_owners()
    data = json.loads(ownership_files[1].read_text())
    assert "retire-error" not in data["schedules"]
    assert data["_retired"]["retire-error"]["bead"] == "ops-1"


@pytest.mark.asyncio
async def test_restore_sweep_survives_corrupt_map_then_retires_after_recovery(config, ownership_files):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._state_store.initialize()
    ownership_files[1].write_text("{not json")
    restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    restored._state_store.initialize()
    restored._restore_state_from_store()
    assert restored._schedule_owner_sweep_pending
    ownership_files[1].write_text('{"schedules": {"stale": {"bead": "ops-1"}}}')
    await restored._repair_pending_schedule_owners()
    data = json.loads(ownership_files[1].read_text())
    assert "stale" not in data["schedules"] and "stale" in data["_retired"]
    assert not restored._schedule_owner_sweep_pending


def test_creation_ownership_is_warning_only_prefix_or_mapping():
    owners = schedule_scope.ScheduleOwnership(
        beads={"ops-1": {"status": "open"}, "ops-2": {"status": "closed"}},
        owners={"mapped": {"bead": "ops-1"}},
    )
    assert "schedule ownership rule" in owners.creation_warnings(schedule_id="new", description="unnamed")[0]
    assert not owners.creation_warnings(schedule_id="new", description="[ops-1] work")
    assert not owners.creation_warnings(schedule_id="mapped", description="legacy")
    assert "orphaned" in owners.creation_warnings(schedule_id="new", description="[ops-2] closed")[0]
    assert "orphaned" in owners.creation_warnings(schedule_id="new", description="[ops-missing] missing")[0]


def test_adopted_nonbead_route_tag_matches_inventory_and_stays_protected():
    owners = schedule_scope.ScheduleOwnership(
        beads={"ops-1": {"status": "open", "labels": ["responsibility"]},
               "ops-2": {"status": "closed"}},
        owners={"mapped": {"bead": "ops-1"}},
    )
    assert owners.owner_of(schedule_id="mapped", description="[deckpress-phase2] legacy")[0] == "ops-1"
    assert not owners.creation_warnings(schedule_id="mapped", description="[deckpress-phase2] legacy")
    assert owners.protection(schedule_id="mapped", description="[deckpress-phase2] legacy", tree=TEAM).protected
    assert owners.owner_of(schedule_id="mapped", description="[ops-2] closed")[0] == "ops-1"
    assert not owners.creation_warnings(schedule_id="mapped", description="[ops-2] closed")


@pytest.mark.asyncio
async def test_prefixless_cron_is_owned_before_registration(config, ownership_files, monkeypatch):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    register = bot._register_topic_schedule
    def owned_before_register(record):
        owners = schedule_scope.ScheduleOwnership.load(config.vault_path)
        assert owners.owner_of(schedule_id=record.schedule_id, description=record.description)[0] == "ops-1"
        register(record)
    monkeypatch.setattr(bot, "_register_topic_schedule", owned_before_register)
    state = _state(bot, thread_id=104)
    state.inflight_user_text = "Task bead: ops-1. Continue the registered work."
    result = await bot._cron_create(route=state.route, args={
        "schedule_mode": "interval", "interval_seconds": 3600,
        "prompt": "continue", "description": "prefixless owner proof",
    })
    payload = result["tool_use_result"]
    assert payload["warnings"] == []
    assert payload["owner_assignment"]["bead"] == "ops-1"
    owners = schedule_scope.ScheduleOwnership.load(config.vault_path)
    assert owners.owner_of(schedule_id=payload["schedule"]["id"], description=None)[0] == "ops-1"


@pytest.mark.asyncio
async def test_cron_inherits_nearest_ancestor_task(config, ownership_files):
    from obs_agent.lineage import agent_name_for_lineage
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    states = [_state(bot, thread_id=n) for n in (105, 106, 107)]
    for index, state in enumerate(states):
        lineage = ("Root", "Parent", "Worker")[:index + 1]
        name = agent_name_for_lineage(lineage, team_key=TEAM)
        state.agent_lineage = lineage
        state.session_manager.set_sdk_env_overrides({"CLAUDE_CODE_TEAM_NAME": TEAM, "CLAUDE_CODE_AGENT_NAME": name})
        bot._route_inbox_targets[(TEAM, name)] = state.route
        bot._route_inbox_target_keys_by_route[state.route] = (TEAM, name)
    states[0].inflight_user_text = "Task bead: ops-2"
    states[1].inflight_user_text = "Task bead: ops-1"
    result = await bot._cron_create(route=states[2].route, args={
        "schedule_mode": "interval", "interval_seconds": 3600, "prompt": "continue",
    })
    assigned = result["tool_use_result"]["owner_assignment"]
    assert assigned["bead"] == "ops-1" and assigned["source"] == "ancestor-task"


def test_default_schedule_creation_uses_same_owner_path(config, ownership_files):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._default_schedule_template = bot._parse_default_schedule_template(
        {"enabled": True, "schedule_mode": "interval", "interval_seconds": 3600,
         "prompt": "Task bead: ops-1", "description": "default without prefix"}, source_label="test")
    route = TelegramRoute(chat_id=67890, thread_id=108)
    bot._get_state(route)
    sid = next(iter(bot._schedule_ids_by_route[route]))
    owners = schedule_scope.ScheduleOwnership.load(config.vault_path)
    assert owners.owner_of(schedule_id=sid, description=None)[0] == "ops-1"
    assert not owners.creation_warnings(schedule_id=sid, description=None)


def test_inherited_prefixless_schedule_keeps_parent_owner(config, ownership_files):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    parent = _state(bot, thread_id=109)
    parent.inflight_user_text = "Task bead: ops-1"
    child = _state(bot, thread_id=110)
    child.inflight_user_text = "Task bead: reg-1"
    _sched(bot, "parent", parent.route)
    bot._topic_schedules_by_id["parent"].inherit_mode = "all"
    bot._inherit_topic_schedules(parent_route=parent.route, child_route=child.route, is_fork=True)
    sid = next(iter(bot._schedule_ids_by_route[child.route]))
    owners = schedule_scope.ScheduleOwnership.load(config.vault_path)
    assert owners.owner_of(schedule_id=sid, description=None)[0] == "ops-1"
    assert "inherited-owner" in owners.owners[sid]["note"]


@pytest.mark.parametrize("from_file", [False, True])
def test_restored_ownerless_schedule_is_owned_from_card_or_prompt_file(config, ownership_files, tmp_path, from_file):
    bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    bot._state_store.initialize()
    state = _state(bot, thread_id=111)
    memory = tmp_path / "restore-card.md"
    memory.write_text("Task bead: ops-1")
    record = _TopicScheduleRecord(schedule_id="restore-card", route=state.route, description=None,
        schedule_mode="interval", cron_expr=None, trigger_kind="interval", interval_seconds=3600,
        prompt="continue" if from_file else "Task bead: ops-1", reset_session=False,
        prompt_file=str(memory) if from_file else None, next_run_at=5000.0)
    bot._register_topic_schedule(record)
    data = json.loads(ownership_files[1].read_text())
    del data["schedules"][record.schedule_id]  # model an ownerless pre-upgrade persisted row
    ownership_files[1].write_text(json.dumps(data))
    restored = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
    restored._state_store.initialize()
    restored._restore_state_from_store()
    owners = schedule_scope.ScheduleOwnership.load(config.vault_path)
    assert owners.owner_of(schedule_id=record.schedule_id, description=None)[0] == "ops-1"
    assert "schedule-prompt" in owners.owners[record.schedule_id]["note"]
    assert restored._topic_schedules_by_id[record.schedule_id].prompt_file == record.prompt_file


@pytest.mark.parametrize("description", ["[ops-1] explicit", "[old-1] closed", "[ops-missing] explicit"])
def test_auto_owner_explicit_prefix_wins(ownership_files, tmp_path, description):
    before = ownership_files[1].read_bytes()
    result = schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id="new", description=description,
        team=TEAM, agent="worker", session_id=None, contexts=[{"agent": "worker", "texts": ["Task bead: reg-1"]}])
    if description.startswith("[ops-1]"):
        assert result is None
        assert ownership_files[1].read_bytes() == before
    else:
        assert result["bead"] == "reg-1"
        owners = schedule_scope.ScheduleOwnership.load(tmp_path)
        assert owners.owner_of(schedule_id="new", description=description)[0] == "reg-1"
        assert not owners.creation_warnings(schedule_id="new", description=description)


def test_auto_owner_prefers_lane_and_understands_brief(ownership_files):
    owners = schedule_scope.ScheduleOwnership.load(None)
    owners.beads = {"ops-1": {"status": "open"}, "ops-2": {"status": "open"}}
    assert schedule_scope._owner_from_text("Task bead: ops-2. Lane bead: ops-1.", owners.beads) == "ops-1"
    assert schedule_scope._owner_from_text("Use /artifacts/briefs/2026-10/ops-2.md; issue ops-1 is unrelated.", owners.beads) == "ops-2"
    assert schedule_scope._owner_from_text("Ambiguous ops-1 or ops-2", owners.beads) is None


def test_auto_owner_uses_registered_ancestor_when_no_session_is_available(ownership_files, tmp_path):
    rows = [json.loads(line) for line in ownership_files[0].read_text().splitlines()]
    next(row for row in rows if row["id"] == "ops-1")["metadata"]["agents"] = {"root_agent": "parent"}
    ownership_files[0].write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id="new", description=None,
        team=TEAM, agent="worker", session_id=None, contexts=[{"agent": "worker"}, {"agent": "parent"}])
    assert result["bead"] == "ops-1" and result["source"] == "ancestor-task"


def test_auto_owner_does_not_guess_between_conflicting_remembered_tasks(ownership_files, tmp_path):
    data = json.loads(ownership_files[1].read_text())
    data["schedules"].update({"old-a": {"bead": "ops-1", "owner_agent": "worker"},
                              "old-b": {"bead": "ops-2", "owner_agent": "worker"}})
    ownership_files[1].write_text(json.dumps(data))
    result = schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id="new", description=None,
        team=TEAM, agent="worker", session_id=None,
        contexts=[{"agent": "worker"}, {"agent": "parent", "texts": ["Task bead: reg-1"]}])
    assert result["bead"] == "reg-1" and result["source"] == "ancestor-task"


def test_auto_owner_ignores_injected_context_and_closed_tasks(ownership_files, tmp_path):
    result = schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id="new", description=None,
        team=TEAM, agent="worker", session_id=None, contexts=[
            {"agent": "worker", "texts": ["<obs-agent-entry-file-context>Task bead: ops-2</obs-agent-entry-file-context>", "Task bead: old-1"]},
            {"agent": "parent", "texts": ["Task bead: ops-1"]},
        ])
    assert result["bead"] == "ops-1" and result["source"] == "ancestor-task"


def test_auto_owner_session_creation_result_not_arbitrary_read(ownership_files, tmp_path):
    path = tmp_path / "session.jsonl"
    rows = [
        {"type": "user", "sessionId": "ancestor", "message": {"content": "Task bead: ops-2"}},
        {"type": "user", "sessionId": "current", "message": {"content": [{"type": "tool_result", "tool_use_id": "read", "content": "Task bead: ops-2"}]}},
        {"type": "assistant", "sessionId": "current", "message": {"content": [{"type": "tool_use", "id": "create", "input": {"command": ".beads/bd-safe create something"}}]}},
        {"type": "user", "sessionId": "current", "message": {"content": [{"type": "tool_result", "tool_use_id": "create", "content": "Created ops-1"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    result = schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id="new", description=None,
        team=TEAM, agent="worker", session_id="current", contexts=[{"agent": "worker", "jsonl": path, "session_id": "current"}])
    assert result["bead"] == "ops-1" and result["source"] == "session-task"


def test_auto_owner_fallback_is_published_lineage_scoped_and_reused(ownership_files, tmp_path, monkeypatch):
    calls = []
    def backend(command, **kwargs):
        calls.append(command)
        rows = [json.loads(line) for line in ownership_files[0].read_text().splitlines()]
        assert command[1] == "create", "Closed checks must never be reopened or overwritten"
        bid = command[command.index("--id") + 1]
        metadata = json.loads(command[command.index("--metadata") + 1])
        assert metadata["check"]["of"] is None and metadata["check"]["source_session"] == "session"
        assert "orphan-check" in command[command.index("--labels") + 1]
        assert "--priority" not in command and "--force" in command
        rows.append({"id": bid, "status": "open", "metadata": metadata})
        ownership_files[0].write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    monkeypatch.setattr(schedule_scope.subprocess, "run", backend)
    def assign(sid, agent="worker"):
        return schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id=sid, description=None,
            team=TEAM, agent=agent, session_id="session", contexts=[{"agent": agent}])
    first = assign("a")
    second = assign("b")
    assert first["source"] == "orphan-check" and first["bead"] == second["bead"] and len(calls) == 1
    rows = [json.loads(line) for line in ownership_files[0].read_text().splitlines()]
    closed = next(row for row in rows if row["id"] == first["bead"])
    closed.update({"status": "closed", "closed_at": "2026-10-17T21:38:14Z", "close_reason": "prior work done"})
    ownership_files[0].write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    fresh = assign("c")
    assert fresh["bead"] != first["bead"] and calls[-1][1] == "create"
    assert assign("c-again")["bead"] == fresh["bead"] and len(calls) == 2
    assert assign("d", "other")["bead"] not in (first["bead"], fresh["bead"])
    owners = schedule_scope.ScheduleOwnership.load(tmp_path)
    assert owners.beads[first["bead"]] == closed
    assert owners.beads[fresh["bead"]]["metadata"]["prior_schedule_owner_checks"] == [first["bead"]]
    assert not owners.creation_warnings(schedule_id="c", description=None)
    assert owners.owners["s-adopted"]["bead"] == "reg-1"


def test_auto_owner_atomic_failure_never_registers_false_mapping(ownership_files, tmp_path, monkeypatch):
    before = ownership_files[1].read_bytes()
    def fail(*args):
        raise OSError("injected replace failure")
    monkeypatch.setattr(schedule_scope.os, "replace", fail)
    with pytest.raises(OSError, match="injected"):
        schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id="new", description=None,
            team=TEAM, agent="worker", session_id=None,
            contexts=[{"agent": "worker", "texts": ["Task bead: ops-1"]}])
    assert ownership_files[1].read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(p.name for p in ownership_files)


def test_concurrent_auto_owner_assignments_preserve_both_and_existing_map(ownership_files, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    data = json.loads(ownership_files[1].read_text())
    data["_retired"] = {"old": {"note": "preserve"}}
    ownership_files[1].write_text(json.dumps(data))
    def assign(sid):
        return schedule_scope.assign_creation_owner(vault=tmp_path, schedule_id=sid, description=None,
            team=TEAM, agent="worker", session_id=None, contexts=[{"agent": "worker", "texts": ["Task bead: ops-1"]}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(result["bead"] == "ops-1" for result in pool.map(assign, ["a", "b"]))
    owners = schedule_scope.ScheduleOwnership.load(tmp_path)
    assert set(owners.owners) == {"a", "b", "s-adopted"}
    assert json.loads(ownership_files[1].read_text())["_retired"] == {"old": {"note": "preserve"}}


def _sched(bot, schedule_id, route, description=""):
    bot._register_topic_schedule(
        _TopicScheduleRecord(
            schedule_id=schedule_id,
            route=route,
            description=description,
            schedule_mode="interval",
            cron_expr=None,
            trigger_kind="interval",
            interval_seconds=300,
            prompt="run",
            reset_session=False,
            next_run_at=5000.0,
        )
    )


def _tree(bot):
    """Root, branch, leaf (under branch), sibling in TEAM; one agent in OTHER_TEAM.

    The leaf and the other-team agent have no client (idle/cold) to prove schedules are found
    from the schedule store, not only from active routes.
    """
    root = _state(bot, thread_id=None)
    branch = _state(bot, thread_id=101)
    leaf = _state(bot, thread_id=102)
    sibling = _state(bot, thread_id=103)
    other = _state(bot, chat_id=11111, thread_id=7)
    root.agent_lineage = ("Root",)
    branch.agent_lineage = ("Root", "Branch")
    leaf.agent_lineage = ("Root", "Branch", "Leaf")
    sibling.agent_lineage = ("Root", "Sibling")
    other.agent_lineage = ("Other",)
    ids = {
        root.route: (TEAM, TEAM),
        branch.route: (TEAM, "branch-agent"),
        leaf.route: (TEAM, "leaf-agent"),
        sibling.route: (TEAM, "sibling-agent"),
        other.route: (OTHER_TEAM, OTHER_TEAM),
    }
    for st in (root, branch, leaf, sibling, other):
        team, agent = ids[st.route]
        st.session_manager.set_sdk_env_overrides(
            {"CLAUDE_CODE_TEAM_NAME": team, "CLAUDE_CODE_AGENT_NAME": agent}
        )
    bot._route_inbox_target_keys_by_route.update(ids)
    bot._route_inbox_targets.update({v: k for k, v in ids.items()})
    return root, branch, leaf, sibling, other


@pytest.fixture
def ownership_files(tmp_path, monkeypatch):
    beads = tmp_path / "issues.jsonl"
    owners = tmp_path / "owners.json"
    rows = [
        {"id": "ops-2", "status": "open", "labels": ["responsibility"], "metadata": {"owner_team": TEAM}},
        {"id": "ops-1", "status": "open", "labels": [], "metadata": {"owner_team": TEAM}},
        {"id": "reg-1", "status": "open", "labels": [], "metadata": {"owner_team": OTHER_TEAM}},
        {"id": "old-1", "status": "closed", "labels": ["responsibility"], "metadata": {}},
    ]
    beads.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    owners.write_text(json.dumps({"schedules": {"s-adopted": {"bead": "reg-1", "owner_team": OTHER_TEAM}}}))
    monkeypatch.setenv("OBS_BEADS_ISSUES_FILE", str(beads))
    monkeypatch.setenv("OBS_SCHEDULE_OWNERS_FILE", str(owners))
    return beads, owners


def _members():
    return {
        TEAM: {"agent_name": TEAM, "lineage": ["Root"]},
        "branch-agent": {"agent_name": "branch-agent", "lineage": ["Root", "Branch"]},
        "leaf-agent": {"agent_name": "leaf-agent", "lineage": ["Root", "Branch", "Leaf"]},
        "sibling-agent": {"agent_name": "sibling-agent", "lineage": ["Root", "Sibling"]},
    }


def _remaining(bot):
    return set(bot._topic_schedules_by_id)


class TestParseScopeArgs:
    def test_scopes_flags_and_mentions(self):
        assert schedule_scope.parse_scope_args([]).scope is None
        assert schedule_scope.parse_scope_args(["tree"]).scope == "tree"
        assert schedule_scope.parse_scope_args(["ALL@obs_bot"]).scope == "all"
        parsed = schedule_scope.parse_scope_args(["branch", "--include-protected"])
        assert parsed.scope == "branch" and parsed.include_protected
        assert schedule_scope.parse_scope_args(["tree", "\u2014include-protected"]).include_protected
        assert schedule_scope.parse_scope_args(["abc123"]).rest == ("abc123",)
        assert schedule_scope.parse_scope_args(["abc123"]).scope is None

    def test_branch_rule(self):
        assert schedule_scope.lineage_in_branch(("R", "B", "L"), ("R", "B"))
        assert not schedule_scope.lineage_in_branch(("R", "S"), ("R", "B"))
        assert not schedule_scope.lineage_in_branch(("R",), ())


class TestScopedUnschedule:
    async def test_branch_removes_self_and_idle_descendants_only(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        _sched(bot, "s-root", root.route)
        _sched(bot, "s-branch", branch.route)
        _sched(bot, "s-leaf", leaf.route, "monthly strategic tier")
        _sched(bot, "s-sibling", sibling.route)
        _sched(bot, "s-other", other.route)
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["branch"]
            await bot.handle_unschedule(_make_update("/unschedule branch", thread_id=101), ctx)
        assert _remaining(bot) == {"s-root", "s-sibling", "s-other"}
        text = " ".join(c.kwargs["text"] for c in ctx.bot.send_message.call_args_list)
        assert "removed 2 schedule(s)" in text and "s-leaf" in text

    async def test_tree_from_leaf_covers_whole_tree_not_other_trees(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        for sid, st in (("s-root", root), ("s-branch", branch), ("s-leaf", leaf), ("s-sibling", sibling), ("s-other", other)):
            _sched(bot, sid, st.route)
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["tree"]
            await bot.handle_unschedule(_make_update("/unschedule tree", thread_id=102), ctx)
        assert _remaining(bot) == {"s-other"}

    async def test_tree_spares_protected_unless_included(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        _sched(bot, "s-resp", leaf.route, "[ops-2] Reflection watcher")  # responsibility -> protected
        _sched(bot, "s-mission", root.route, "[ops-1] mission heartbeat")  # this tree's own work
        _sched(bot, "s-adopted", sibling.route, "no prefix, adopted by other tree")  # other-team owner
        _sched(bot, "s-closed", branch.route, "[old-1] closed responsibility")  # orphan -> removed
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["tree"]
            await bot.handle_unschedule(_make_update("/unschedule tree", thread_id=101), ctx)
            assert _remaining(bot) == {"s-resp", "s-adopted"}
            text = ctx.bot.send_message.call_args.kwargs["text"]
            assert "kept 2 protected" in text and "--include-protected" in text
            ctx = _make_context()
            ctx.args = ["tree", "--include-protected"]
            await bot.handle_unschedule(_make_update("/unschedule tree --include-protected", thread_id=101), ctx)
        assert _remaining(bot) == set()

    async def test_all_spares_only_responsibilities(self, config, ownership_files):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, branch, leaf, sibling, other = _tree(bot)
        _sched(bot, "s-resp", leaf.route, "[ops-2] watcher")
        _sched(bot, "s-adopted", sibling.route)
        _sched(bot, "s-other", other.route)
        _sched(bot, "s-root", root.route)
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_unschedule(_make_update("/unschedule all", thread_id=1), ctx)
        assert _remaining(bot) == {"s-resp"}
        text = ctx.bot.send_message.call_args.kwargs["text"]
        assert f"- {OTHER_TEAM}: 1" in text and f"- {TEAM}: 2" in text

    async def test_unreadable_owner_data_degrades_to_report(self, config, monkeypatch, tmp_path):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        root, *_ = _tree(bot)
        bad = tmp_path / "owners.json"
        bad.write_text("{not json")
        monkeypatch.setenv("OBS_SCHEDULE_OWNERS_FILE", str(bad))
        monkeypatch.setenv("OBS_BEADS_ISSUES_FILE", str(tmp_path / "missing.jsonl"))
        _sched(bot, "s-root", root.route)
        with patch.object(bot, "_load_tree_members", return_value=_members()):
            ctx = _make_context()
            ctx.args = ["tree"]
            await bot.handle_unschedule(_make_update("/unschedule tree"), ctx)
        assert _remaining(bot) == set()
        assert "owner data incomplete" in ctx.bot.send_message.call_args.kwargs["text"]

    async def test_single_id_with_scope_words_still_works(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        route = TelegramRoute(chat_id=67890, thread_id=1)
        assert bot._get_state(route) is not None
        _sched(bot, "abc12345", route)
        ctx = _make_context()
        ctx.args = ["abc12345"]
        await bot.handle_unschedule(_make_update("/unschedule abc12345", thread_id=1), ctx)
        assert _remaining(bot) == set()


class TestStopAllEveryTree:
    async def test_stop_all_interrupts_active_routes_in_every_chat(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        here = _state(bot, thread_id=1)
        elsewhere = _state(bot, chat_id=22222, thread_id=5)
        idle = _state(bot, chat_id=33333, thread_id=9)
        for st in (here, elsewhere):
            client = MagicMock()
            client.interrupt = AsyncMock()
            st.session_manager._client = client
            st.session_manager._connected = True
        elsewhere.busy = True
        ctx = _make_context()
        ctx.args = ["all"]
        await bot.handle_stop(_make_update("/stop all", thread_id=1), ctx)
        here.session_manager._client.interrupt.assert_awaited_once()
        elsewhere.session_manager._client.interrupt.assert_awaited_once()
        assert elsewhere.hook_state.interrupt_flag is True
        assert idle.hook_state.interrupt_flag is False  # idle disconnected routes untouched
        text = ctx.bot.send_message.call_args.kwargs["text"]
        assert "every tree" in text and "interrupted 2" in text and "/unschedule all" in text

    async def test_stop_all_marks_child_tasks_stopped_not_resumed(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        child = _state(bot, chat_id=22222, thread_id=5)
        child.busy = True
        client = MagicMock()
        client.interrupt = AsyncMock()
        child.session_manager._client = client
        child.session_manager._connected = True
        with patch.object(bot, "_mark_child_task_terminal_request") as mark:
            ctx = _make_context()
            ctx.args = ["all"]
            await bot.handle_stop(_make_update("/stop all", thread_id=1), ctx)
        assert any(call.args == (child.route, "stopped") for call in mark.call_args_list)
        assert child.hook_state.stop_requested_at is not None
