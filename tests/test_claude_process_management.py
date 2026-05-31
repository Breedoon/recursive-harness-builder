"""Targeted tests for Claude subprocess idle pruning."""

import logging
import time
from unittest.mock import AsyncMock

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute, TelegramSessionState, _ForkTaskRecord


class FakeSessionManager:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.disconnect_calls: list[bool] = []
        self.session_id = None

    def has_connected_client(self) -> bool:
        return self.connected

    async def disconnect_idle_client(self, *, direct_kill: bool = False) -> bool:
        self.disconnect_calls.append(direct_kill)
        was_connected = self.connected
        self.connected = False
        return was_connected


class FakeHookState:
    def __init__(self, *, execution_active: bool = False) -> None:
        self.execution_active = execution_active


class FakeRunningTask:
    def done(self) -> bool:
        return False


def _route(index: int) -> TelegramRoute:
    return TelegramRoute(chat_id=-1000, thread_id=index)


def _state(route: TelegramRoute, manager: FakeSessionManager, *, busy: bool = False, execution_active: bool = False):
    return TelegramSessionState(
        route=route,
        hook_state=FakeHookState(execution_active=execution_active),
        session_manager=manager,  # type: ignore[arg-type]
        busy=busy,
    )


def _record(task_id: str, child_route: TelegramRoute, *, completed_at: float, idle_ready: bool = True):
    return _ForkTaskRecord(
        task_id=task_id,
        parent_route=_route(0),
        parent_session_id_at_launch="parent-session",
        parent_source_uuid="parent-uuid",
        child_route=child_route,
        child_session_id=f"session-{task_id}",
        prompt="prompt",
        team_name="team",
        agent_name=f"agent-{task_id}",
        status="completed",
        idle_ready=idle_ready,
        created_at=completed_at - 100,
        completed_at=completed_at,
    )


@pytest.fixture
def bot(config):
    return TelegramBot(config, enable_background_poller=False)


def test_idle_candidates_exclude_running_busy_execution_active_and_disconnected(bot):
    now = time.time()
    included_route = _route(1)
    running_route = _route(2)
    busy_route = _route(3)
    active_hook_route = _route(4)
    disconnected_route = _route(5)
    not_idle_route = _route(6)

    included_manager = FakeSessionManager()
    bot._states_by_route[included_route] = _state(included_route, included_manager)
    bot._states_by_route[running_route] = _state(running_route, FakeSessionManager())
    bot._states_by_route[busy_route] = _state(busy_route, FakeSessionManager(), busy=True)
    bot._states_by_route[active_hook_route] = _state(active_hook_route, FakeSessionManager(), execution_active=True)
    bot._states_by_route[disconnected_route] = _state(disconnected_route, FakeSessionManager(connected=False))
    bot._states_by_route[not_idle_route] = _state(not_idle_route, FakeSessionManager())

    records = [
        _record("included", included_route, completed_at=now),
        _record("running", running_route, completed_at=now + 1),
        _record("busy", busy_route, completed_at=now + 2),
        _record("active-hook", active_hook_route, completed_at=now + 3),
        _record("disconnected", disconnected_route, completed_at=now + 4),
        _record("not-idle", not_idle_route, completed_at=now + 5, idle_ready=False),
    ]
    bot._fork_tasks_by_id = {record.task_id: record for record in records}

    bot._fork_task_tasks["running"] = FakeRunningTask()

    candidates = bot._idle_claude_process_candidates()

    assert [candidate.record.task_id for candidate in candidates] == ["included"]


def test_idle_candidates_include_logically_completed_current_task(bot):
    now = time.time()
    route = _route(1)
    bot._states_by_route[route] = _state(route, FakeSessionManager())
    record = _record("completing", route, completed_at=now)
    bot._fork_tasks_by_id = {record.task_id: record}
    bot._fork_task_tasks[record.task_id] = FakeRunningTask()

    assert bot._idle_claude_process_candidates() == []

    candidates = bot._idle_claude_process_candidates(completing_task_id=record.task_id)

    assert [candidate.record.task_id for candidate in candidates] == ["completing"]


def test_idle_candidates_sort_oldest_completed_first(bot):
    routes = [_route(index) for index in range(1, 4)]
    for route in routes:
        bot._states_by_route[route] = _state(route, FakeSessionManager())
    bot._fork_tasks_by_id = {
        "new": _record("new", routes[0], completed_at=30),
        "old": _record("old", routes[1], completed_at=10),
        "middle": _record("middle", routes[2], completed_at=20),
    }

    candidates = bot._idle_claude_process_candidates()

    assert [candidate.record.task_id for candidate in candidates] == ["old", "middle", "new"]


@pytest.mark.asyncio
async def test_cap_prunes_oldest_idle_overage_and_preserves_identity_maps(bot):
    bot._config.claude_idle_process_cap = 2
    routes = [_route(index) for index in range(1, 5)]
    managers = [FakeSessionManager() for _ in routes]
    for route, manager in zip(routes, managers):
        bot._states_by_route[route] = _state(route, manager)
    records = [
        _record("oldest", routes[0], completed_at=10),
        _record("second", routes[1], completed_at=20),
        _record("third", routes[2], completed_at=30),
        _record("newest", routes[3], completed_at=40),
    ]
    bot._fork_tasks_by_id = {record.task_id: record for record in records}
    bot._fork_task_by_child_route = {record.child_route: record.task_id for record in records}
    bot._team_worker_records = {("team", f"agent-{record.task_id}"): record.task_id for record in records}

    pruned = await bot._prune_idle_claude_processes()

    assert pruned == 2
    assert managers[0].disconnect_calls == [True]
    assert managers[1].disconnect_calls == [True]
    assert managers[2].disconnect_calls == []
    assert managers[3].disconnect_calls == []
    assert bot._fork_task_by_child_route == {record.child_route: record.task_id for record in records}
    assert bot._team_worker_records == {("team", f"agent-{record.task_id}"): record.task_id for record in records}


@pytest.mark.asyncio
async def test_active_records_are_exempt_even_when_active_count_exceeds_cap(bot):
    bot._config.claude_idle_process_cap = 1
    routes = [_route(index) for index in range(1, 5)]
    managers = [FakeSessionManager() for _ in routes]
    for route, manager in zip(routes, managers):
        bot._states_by_route[route] = _state(route, manager)
    records = [
        _record("active-1", routes[0], completed_at=10),
        _record("active-2", routes[1], completed_at=20),
        _record("idle-old", routes[2], completed_at=30),
        _record("idle-new", routes[3], completed_at=40),
    ]
    bot._fork_tasks_by_id = {record.task_id: record for record in records}
    bot._fork_task_tasks["active-1"] = FakeRunningTask()
    bot._fork_task_tasks["active-2"] = FakeRunningTask()

    pruned = await bot._prune_idle_claude_processes()

    assert pruned == 1
    assert managers[0].disconnect_calls == []
    assert managers[1].disconnect_calls == []
    assert managers[2].disconnect_calls == [True]
    assert managers[3].disconnect_calls == []


@pytest.mark.asyncio
async def test_kill_on_idle_prunes_currently_completing_worker(bot):
    bot._config.claude_kill_on_idle = True
    route = _route(1)
    manager = FakeSessionManager()
    bot._states_by_route[route] = _state(route, manager)
    record = _record("completing", route, completed_at=10)
    bot._fork_tasks_by_id = {record.task_id: record}
    bot._fork_task_tasks[record.task_id] = FakeRunningTask()

    assert await bot._prune_idle_claude_processes() == 0

    pruned = await bot._prune_idle_claude_processes(completing_task_id=record.task_id)

    assert pruned == 1
    assert manager.disconnect_calls == [True]


@pytest.mark.asyncio
async def test_pruning_logs_observability_counters(bot, caplog):
    bot._config.claude_idle_process_cap = 1
    routes = [_route(1), _route(2)]
    managers = [FakeSessionManager(), FakeSessionManager()]
    for route, manager in zip(routes, managers):
        bot._states_by_route[route] = _state(route, manager)
    bot._fork_tasks_by_id = {
        "old": _record("old", routes[0], completed_at=10),
        "new": _record("new", routes[1], completed_at=20),
    }

    caplog.set_level(logging.INFO, logger="obs_agent.telegram")
    pruned = await bot._prune_idle_claude_processes()

    assert pruned == 1
    assert "Claude idle process pruning mode=cap" in caplog.text
    assert "candidates=2" in caplog.text
    assert "selected=1" in caplog.text
    assert "pruned=1" in caplog.text


@pytest.mark.asyncio
async def test_cap_prunes_current_overage_when_completion_triggers_prune(bot):
    bot._config.claude_idle_process_cap = 2
    routes = [_route(index) for index in range(1, 5)]
    managers = [FakeSessionManager() for _ in routes]
    for route, manager in zip(routes, managers):
        bot._states_by_route[route] = _state(route, manager)
    records = [
        _record("oldest", routes[0], completed_at=10),
        _record("second", routes[1], completed_at=20),
        _record("third", routes[2], completed_at=30),
        _record("completing", routes[3], completed_at=40),
    ]
    bot._fork_tasks_by_id = {record.task_id: record for record in records}
    bot._fork_task_tasks["completing"] = FakeRunningTask()

    pruned = await bot._prune_idle_claude_processes(completing_task_id="completing")

    assert pruned == 2
    assert managers[0].disconnect_calls == [True]
    assert managers[1].disconnect_calls == [True]
    assert managers[2].disconnect_calls == []
    assert managers[3].disconnect_calls == []


@pytest.mark.asyncio
async def test_direct_kill_on_idle_prunes_all_idle_with_cap_disabled(bot):
    bot._config.claude_kill_on_idle = True
    bot._config.claude_idle_process_cap = None
    routes = [_route(index) for index in range(1, 4)]
    managers = [FakeSessionManager() for _ in routes]
    for route, manager in zip(routes, managers):
        bot._states_by_route[route] = _state(route, manager)
    bot._fork_tasks_by_id = {
        f"task-{index}": _record(f"task-{index}", route, completed_at=index)
        for index, route in enumerate(routes)
    }

    pruned = await bot._prune_idle_claude_processes()

    assert pruned == 3
    assert [manager.disconnect_calls for manager in managers] == [[True], [True], [True]]


@pytest.mark.asyncio
async def test_cap_zero_prunes_idle_without_being_required_for_direct_kill(bot):
    bot._config.claude_idle_process_cap = 0
    route = _route(1)
    manager = FakeSessionManager()
    bot._states_by_route[route] = _state(route, manager)
    bot._fork_tasks_by_id = {"idle": _record("idle", route, completed_at=10)}

    pruned = await bot._prune_idle_claude_processes()

    assert pruned == 1
    assert manager.disconnect_calls == [True]
