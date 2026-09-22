"""Regression tests for messages sent while an agent is using tools.

Exercise the registered SDK callbacks: tool hooks must leave queued input for
canonical runner queries, never consume it into non-JSONL context.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from obs_agent import hooks
from obs_agent.queueing import QueuedMessage

pytestmark = pytest.mark.asyncio


def _input(event: str, tool_id: str = "tool-1", tool_name: str = "Read") -> dict:
    payload = {
        "hook_event_name": event,
        "session_id": "session-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "tool_name": tool_name,
        "tool_input": {"file_path": "/tmp/input.txt"},
        "tool_use_id": tool_id,
    }
    if event == "PostToolUse":
        payload["tool_response"] = "tool finished"
    if event == "Stop":
        payload["stop_hook_active"] = False
    return payload


def _callbacks(tmp_path, state, monkeypatch, user_hook=None, event="PostToolUse"):
    config = SimpleNamespace(vault_path=tmp_path, is_immutable=lambda path: False)
    user_hooks = None
    if user_hook is not None:
        monkeypatch.setattr(hooks, "load_hook_function", lambda *args: user_hook)
        user_hooks = {event: "delivery_test.py::check"}
    matchers = hooks.create_hook_matchers(config, state, user_hooks=user_hooks)
    return {name: matchers[name][0].hooks[0] for name in matchers}


def _context(result: dict) -> str:
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def _assert_retained(state, result, texts):
    assert "[Queued message from user]:" not in _context(result)
    messages = list(state.message_queue._queue)
    assert [message.text if isinstance(message, QueuedMessage) else message for message in messages] == texts
    assert state.status_queue.empty()


@pytest.mark.parametrize("tool_name", ["Read", "Bash", "mcp__obs__AgentTaskOutput"])
async def test_pre_tracks_tool_without_consuming_message(tmp_path, monkeypatch, tool_name):
    state = hooks.HookState()
    state.message_queue.put_nowait("use the updated requirements")
    callbacks = _callbacks(tmp_path, state, monkeypatch)

    pre = await callbacks["PreToolUse"](_input("PreToolUse", tool_name=tool_name), "tool-1", {})

    assert _context(pre) == ""
    assert state.current_tool_use_id == "tool-1"
    assert state.session_id == "session-1"
    assert state.message_queue.qsize() == 1
    assert state.status_queue.empty()

    post = await callbacks["PostToolUse"](_input("PostToolUse", tool_name=tool_name), "tool-1", {})
    _assert_retained(state, post, ["use the updated requirements"])
    assert not state.message_queue.empty()
    assert state.current_tool_use_id is None
    assert await callbacks["PostToolUse"](_input("PostToolUse"), "tool-1", {}) == {}
    assert state.status_queue.empty()


@pytest.mark.parametrize("flag", ["pause_queue_delivery", "interrupt_requested", "interrupt_flag"])
async def test_post_preserves_messages_while_delivery_is_disabled(tmp_path, monkeypatch, flag):
    state = hooks.HookState(current_tool_use_id="tool-1")
    message = QueuedMessage("do not lose this", telegram_message_id=123)
    state.message_queue.put_nowait(message)
    setattr(state, flag, True)
    callbacks = _callbacks(tmp_path, state, monkeypatch)

    result = await callbacks["PostToolUse"](_input("PostToolUse"), "tool-1", {})

    assert result == {}
    assert state.current_tool_use_id is None
    assert state.message_queue.qsize() == 1
    assert state.status_queue.empty()
    setattr(state, flag, False)
    result = await callbacks["PostToolUse"](_input("PostToolUse", "tool-2"), "tool-2", {})
    _assert_retained(state, result, [message.text])


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
async def test_user_hook_stop_cannot_acknowledge_or_consume_message(tmp_path, monkeypatch, event):
    async def stop(inp, tid, ctx):
        return {"continue_": False, "stopReason": "pause this run"}

    state = hooks.HookState(current_tool_use_id="tool-1")
    state.message_queue.put_nowait("still waiting")
    callbacks = _callbacks(tmp_path, state, monkeypatch, stop, event)

    result = await callbacks[event](_input(event), "tool-1", {})

    assert result["continue_"] is False
    assert "still waiting" not in _context(result)
    assert state.message_queue.qsize() == 1
    assert state.status_queue.empty()
    if event == "PostToolUse":
        assert state.current_tool_use_id is None


async def test_user_pre_hook_denial_leaves_message_for_post(tmp_path, monkeypatch):
    def deny(inp, tid, ctx):
        return {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "blocked"}}

    state = hooks.HookState()
    state.message_queue.put_nowait("keep this instruction")
    callbacks = _callbacks(tmp_path, state, monkeypatch, deny, "PreToolUse")
    result = await callbacks["PreToolUse"](_input("PreToolUse"), "tool-1", {})

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "keep this instruction" not in _context(result)
    assert state.message_queue.qsize() == 1
    assert state.status_queue.empty()


async def test_cancelled_post_hook_keeps_message_for_next_callback(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_hook(inp, tid, ctx):
        entered.set()
        await release.wait()

    state = hooks.HookState(current_tool_use_id="tool-1")
    message = QueuedMessage("important correction", telegram_message_id=456)
    state.message_queue.put_nowait(message)
    callbacks = _callbacks(tmp_path, state, monkeypatch, slow_hook)
    task = asyncio.create_task(callbacks["PostToolUse"](_input("PostToolUse"), "tool-1", {}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert state.message_queue.qsize() == 1
    assert state.status_queue.empty()
    assert state.current_tool_use_id is None
    release.set()
    result = await callbacks["PostToolUse"](_input("PostToolUse", "tool-2"), "tool-2", {})
    _assert_retained(state, result, [message.text])
    assert not state.message_queue.empty()


async def test_message_arriving_during_user_hook_stays_queued(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_hook(inp, tid, ctx):
        entered.set()
        await release.wait()
        return {"hookSpecificOutput": {"additionalContext": "custom hook context"}}

    state = hooks.HookState()
    state.message_queue.put_nowait("first")
    callbacks = _callbacks(tmp_path, state, monkeypatch, slow_hook)
    task = asyncio.create_task(callbacks["PostToolUse"](_input("PostToolUse"), "tool-1", {}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert state.message_queue.qsize() == 1
        assert state.status_queue.empty()
        state.message_queue.put_nowait("arrived while hook was waiting")
    finally:
        release.set()
        result = await task

    _assert_retained(state, result, ["first", "arrived while hook was waiting"])
    assert "custom hook context" in _context(result)
    assert not state.message_queue.empty()


async def test_parallel_post_callbacks_do_not_lose_message_when_one_is_cancelled(tmp_path, monkeypatch):
    entered = {tool_id: asyncio.Event() for tool_id in ("tool-1", "tool-2")}
    release = asyncio.Event()

    async def slow_hook(inp, tid, ctx):
        entered[tid].set()
        await release.wait()

    state = hooks.HookState()
    state.message_queue.put_nowait("exactly once")
    callbacks = _callbacks(tmp_path, state, monkeypatch, slow_hook)
    tasks = []
    try:
        for tool_id in entered:
            tasks.append(asyncio.create_task(callbacks["PostToolUse"](_input("PostToolUse", tool_id), tool_id, {})))
            await asyncio.wait_for(entered[tool_id].wait(), timeout=2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        result = await tasks[1]
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    _assert_retained(state, result, ["exactly once"])
    assert not state.message_queue.empty()


async def test_reply_targets_keep_metadata_and_plain_messages_keep_order(tmp_path, monkeypatch):
    state = hooks.HookState()
    replies = [QueuedMessage("reply one", 11, 1), QueuedMessage("reply two", 12, 2)]
    for message in [replies[0], "first", replies[1], QueuedMessage("second", 13), "second"]:
        state.message_queue.put_nowait(message)
    callbacks = _callbacks(tmp_path, state, monkeypatch)

    result = await callbacks["PostToolUse"](_input("PostToolUse"), "tool-1", {})

    _assert_retained(state, result, ["reply one", "first", "reply two", "second", "second"])
    assert "reply one" not in _context(result)
    assert "reply two" not in _context(result)
    assert state.message_queue.get_nowait() is replies[0]
    assert state.message_queue.get_nowait() == "first"
    assert state.message_queue.get_nowait() is replies[1]
    assert state.message_queue.get_nowait() == QueuedMessage("second", 13)
    assert state.message_queue.get_nowait() == "second"
    assert state.message_queue.empty()


async def test_message_after_last_tool_remains_available_for_runner(tmp_path, monkeypatch):
    state = hooks.HookState()
    callbacks = _callbacks(tmp_path, state, monkeypatch)
    await callbacks["PostToolUse"](_input("PostToolUse"), "tool-1", {})
    message = QueuedMessage("arrived after last tool", telegram_message_id=999)
    state.message_queue.put_nowait(message)

    await callbacks["Stop"](_input("Stop"), None, {})

    # ConversationRunner's continuation loop must still be able to read it.
    assert state.message_queue.get_nowait() is message
    assert state.status_queue.empty()


async def test_no_successful_tool_leaves_message_for_runner(tmp_path, monkeypatch):
    state = hooks.HookState()
    message = QueuedMessage("tool might fail or be cancelled", telegram_message_id=88)
    state.message_queue.put_nowait(message)
    callbacks = _callbacks(tmp_path, state, monkeypatch)

    await callbacks["PreToolUse"](_input("PreToolUse"), "tool-1", {})
    await callbacks["Stop"](_input("Stop"), None, {})

    assert state.message_queue.get_nowait() is message
    assert state.status_queue.empty()


async def test_tool_tracking_survives_out_of_order_post_callbacks(tmp_path, monkeypatch):
    state = hooks.HookState()
    callbacks = _callbacks(tmp_path, state, monkeypatch)
    for tool_id in ("tool-1", "tool-2"):
        await callbacks["PreToolUse"](_input("PreToolUse", tool_id), tool_id, {})
    await callbacks["PostToolUse"](_input("PostToolUse", "tool-1"), "tool-1", {})
    assert state.current_tool_use_id == "tool-2"
    await callbacks["PostToolUse"](_input("PostToolUse", "tool-2"), "tool-2", {})
    assert state.current_tool_use_id is None


async def test_guard_still_runs_before_user_hook(tmp_path, monkeypatch):
    calls = []

    def user_hook(inp, tid, ctx):
        calls.append(tid)

    state = hooks.HookState()
    state.message_queue.put_nowait("not delivered by a blocked tool")
    callbacks = _callbacks(tmp_path, state, monkeypatch, user_hook, "PreToolUse")
    result = await callbacks["PreToolUse"](_input("PreToolUse", tool_name="Task"), "tool-1", {})

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == []
    assert state.message_queue.qsize() == 1
    assert state.status_queue.empty()


async def test_different_agents_cannot_drain_each_others_queue(tmp_path, monkeypatch):
    first, second = hooks.HookState(), hooks.HookState()
    first.message_queue.put_nowait("for first only")
    second.message_queue.put_nowait("for second only")
    callbacks = _callbacks(tmp_path, first, monkeypatch)

    result = await callbacks["PostToolUse"](_input("PostToolUse"), "tool-1", {})

    _assert_retained(first, result, ["for first only"])
    assert second.message_queue.get_nowait() == "for second only"
    assert second.status_queue.empty()
