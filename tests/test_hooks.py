"""Tests for obs_agent.hooks - Hook contracts.

- PreToolUse: guards immutable files and .env from writes
- Stop: triggers memory extraction via fork
- PreCompact: triggers extraction then denies compaction
- HookPipeline: extensible middleware that chains check functions
- HookState: shared state for message queuing and interrupt

See implementation-plan.md Step 4 and decisions D018, D022.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from obs_agent.config import OBSConfig
from obs_agent.hooks import (
    on_pre_tool_use,
    on_stop,
    on_pre_compact,
    HookState,
    HookPipeline,
    _make_interrupt_check,
    _make_immutable_check,
    _make_notification_check,
    _make_stop_check,
    _make_queue_check,
    create_hook_matchers,
    load_hook_function,
    _make_user_hook_check,
)
from obs_agent.queueing import QueuedMessage


# --- PreToolUse Guard: Immutable Files ---


class TestPreToolUseImmutableGuard:
    """PreToolUse hook blocks writes to immutable files (Meeting Notes, .env)."""

    def test_blocks_write_to_meeting_notes(self, config):
        """Blocks Write tool targeting Misc/Meeting Notes/ files."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": str(config.vault_path / "Misc" / "Meeting Notes" / "2025-01-15 standup.md"),
                "content": "modified content",
            },
            config=config,
        )
        # Hook should return a deny signal
        assert result is not None
        assert "deny" in str(result).lower() or result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_blocks_edit_to_meeting_notes(self, config):
        """Blocks Edit tool targeting Misc/Meeting Notes/ files."""
        result = on_pre_tool_use(
            tool_name="Edit",
            tool_input={
                "file_path": str(config.vault_path / "Misc" / "Meeting Notes" / "2025-02-04 call.md"),
                "old_string": "original",
                "new_string": "modified",
            },
            config=config,
        )
        assert result is not None
        assert "deny" in str(result).lower() or result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_blocks_write_to_env_files(self, config):
        """Blocks Write/Edit to .env files anywhere."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": "/Users/breedoon/Documents/obs/.env",
                "content": "SECRET=123",
            },
            config=config,
        )
        assert result is not None
        assert "deny" in str(result).lower() or result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    def test_blocks_edit_to_env_files(self, config):
        """Blocks Edit targeting .env files."""
        result = on_pre_tool_use(
            tool_name="Edit",
            tool_input={
                "file_path": "/some/path/.env.local",
                "old_string": "KEY=old",
                "new_string": "KEY=new",
            },
            config=config,
        )
        assert result is not None
        assert "deny" in str(result).lower() or result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# --- PreToolUse Guard: Native Tool Denylist ---


class TestPreToolUseNativeToolGuard:
    """PreToolUse hook blocks native tools superseded by OBS tools."""

    def test_blocks_native_task_tool(self, config):
        result = on_pre_tool_use(
            tool_name="Task",
            tool_input={"prompt": "run in background"},
            config=config,
        )
        assert result is not None
        assert result["decision"] == "block"
        assert "systemMessage" in result
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "AgentTask" in reason

    def test_allows_native_task_output_tool(self, config):
        """TaskOutput is allowed (read-only, useful for background bash)."""
        result = on_pre_tool_use(
            tool_name="mcp__native__TaskOutput",
            tool_input={"task_id": "abc"},
            config=config,
        )
        assert result is None  # None means allowed

    def test_blocks_native_inbox_send_tool(self, config):
        result = on_pre_tool_use(
            tool_name="SendMessage",
            tool_input={"recipient": "worker-a", "content": "hello"},
            config=config,
        )
        assert result is not None
        assert result["decision"] == "block"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "SendInboxMessage" in reason
        assert "ReadInbox" in reason

    def test_blocks_native_inbox_read_tool(self, config):
        result = on_pre_tool_use(
            tool_name="ReadMessages",
            tool_input={"limit": 10},
            config=config,
        )
        assert result is not None
        assert result["decision"] == "block"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "SendInboxMessage" in reason
        assert "ReadInbox" in reason

    def test_blocks_enter_plan_mode(self, config):
        result = on_pre_tool_use(
            tool_name="EnterPlanMode",
            tool_input={},
            config=config,
        )
        assert result is not None
        assert result["decision"] == "block"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "EnterPlanMode" in reason

    def test_allows_obs_inbox_tools(self, config):
        send_result = on_pre_tool_use(
            tool_name="SendInboxMessage",
            tool_input={"team_name": "t", "recipient": "r", "content": "x"},
            config=config,
        )
        read_result = on_pre_tool_use(
            tool_name="ReadInbox",
            tool_input={"team_name": "t", "agent": "a"},
            config=config,
        )
        assert send_result is None or send_result == {}
        assert read_result is None or read_result == {}


# --- PreToolUse Guard: Allowed Operations ---


class TestPreToolUseAllowed:
    """PreToolUse hook allows legitimate operations."""

    def test_allows_write_to_vault_files(self, config):
        """Allows Write tool for vault files."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": str(config.vault_path / "CLAUDE.md"),
                "content": "updated context",
            },
            config=config,
        )
        # Should return None or empty dict (allow)
        assert result is None or result == {}

    def test_allows_edit_to_claude_files(self, config):
        """Allows Edit tool for .claude/ directory files."""
        result = on_pre_tool_use(
            tool_name="Edit",
            tool_input={
                "file_path": str(config.vault_path / ".claude" / "topics" / "goals.md"),
                "old_string": "old goal",
                "new_string": "new goal",
            },
            config=config,
        )
        assert result is None or result == {}

    def test_allows_read_anywhere(self, config):
        """Allows Read tool for any file, including immutable ones."""
        result = on_pre_tool_use(
            tool_name="Read",
            tool_input={
                "file_path": str(config.vault_path / "Misc" / "Meeting Notes" / "2025-01-15.md"),
            },
            config=config,
        )
        assert result is None or result == {}

    def test_allows_glob_anywhere(self, config):
        """Allows Glob tool everywhere (read-only operation)."""
        result = on_pre_tool_use(
            tool_name="Glob",
            tool_input={
                "pattern": "**/*.md",
                "path": str(config.vault_path / "Misc" / "Meeting Notes"),
            },
            config=config,
        )
        assert result is None or result == {}

    def test_allows_grep_anywhere(self, config):
        """Allows Grep tool everywhere (read-only operation)."""
        result = on_pre_tool_use(
            tool_name="Grep",
            tool_input={
                "pattern": "meeting",
                "path": str(config.vault_path),
            },
            config=config,
        )
        assert result is None or result == {}

    def test_allows_write_to_vault_topics(self, config):
        """Allows writing to Vault/ knowledge files."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": str(config.vault_path / "Vault" / "CS" / "algorithms.md"),
                "content": "new content",
            },
            config=config,
        )
        assert result is None or result == {}


# --- Stop Hook: Memory Extraction ---


class TestStopHook:
    """Stop hook triggers memory extraction via fork runner."""

    @pytest.mark.asyncio
    async def test_stop_triggers_extraction(self, config):
        """Stop hook calls fork runner's extract_memory method."""
        mock_fork_runner = MagicMock()
        mock_fork_runner.extract_memory = AsyncMock()

        await on_stop(
            config=config,
            fork_runner=mock_fork_runner,
        )

        mock_fork_runner.extract_memory.assert_called_once()


# --- PreCompact Hook: Extraction + Deny ---


class TestPreCompactHook:
    """PreCompact hook extracts memories then prevents compaction."""

    @pytest.mark.asyncio
    async def test_pre_compact_triggers_extraction(self, config):
        """PreCompact hook calls extract_memory before denying."""
        mock_fork_runner = MagicMock()
        mock_fork_runner.extract_memory = AsyncMock()

        result = await on_pre_compact(
            config=config,
            fork_runner=mock_fork_runner,
        )

        mock_fork_runner.extract_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_pre_compact_prevents_compaction(self, config):
        """PreCompact returns a deny signal to prevent lossy compaction."""
        mock_fork_runner = MagicMock()
        mock_fork_runner.extract_memory = AsyncMock()

        result = await on_pre_compact(
            config=config,
            fork_runner=mock_fork_runner,
        )

        # Must return deny to prevent SDK compaction (D022)
        assert result is not None
        assert "deny" in str(result).lower() or result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# ---------------------------------------------------------------------------
# Hook Pipeline Tests
# ---------------------------------------------------------------------------


def _make_pre_tool_use_input(**overrides) -> dict:
    """Helper to build a minimal PreToolUseHookInput dict for testing."""
    base = {
        "hook_event_name": "PreToolUse",
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript",
        "cwd": "/tmp",
        "tool_name": "Read",
        "tool_input": {"file_path": "/some/file.md"},
        "tool_use_id": "tu-123",
    }
    base.update(overrides)
    return base


def _make_post_tool_use_input(**overrides) -> dict:
    """Helper to build a minimal PostToolUseHookInput dict for testing."""
    base = {
        "hook_event_name": "PostToolUse",
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript",
        "cwd": "/tmp",
        "tool_name": "Read",
        "tool_input": {"file_path": "/some/file.md"},
        "tool_response": "file contents",
        "tool_use_id": "tu-123",
    }
    base.update(overrides)
    return base


def _make_notification_input(**overrides) -> dict:
    base = {
        "hook_event_name": "Notification",
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript",
        "cwd": "/tmp",
        "notification_type": "TaskCompleted",
        "title": "Task done",
        "message": "worker-a completed task 1",
    }
    base.update(overrides)
    return base


def _make_subagent_start_input(**overrides) -> dict:
    base = {
        "hook_event_name": "SubagentStart",
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript",
        "cwd": "/tmp",
        "agent_id": "agent-123",
        "agent_type": "general-purpose",
    }
    base.update(overrides)
    return base


def _make_subagent_stop_input(**overrides) -> dict:
    base = {
        "hook_event_name": "SubagentStop",
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript",
        "cwd": "/tmp",
        "stop_hook_active": False,
        "agent_id": "agent-123",
        "agent_type": "general-purpose",
        "agent_transcript_path": "/tmp/agent-123.jsonl",
    }
    base.update(overrides)
    return base


def _make_stop_input(**overrides) -> dict:
    base = {
        "hook_event_name": "Stop",
        "session_id": "test-session",
        "transcript_path": "/tmp/transcript",
        "cwd": "/tmp",
        "stop_hook_active": False,
    }
    base.update(overrides)
    return base


_EMPTY_CONTEXT = {"signal": None}


class TestHookPipeline:
    """HookPipeline chains check functions and merges results."""

    @pytest.mark.asyncio
    async def test_empty_pipeline_returns_empty(self):
        """An empty pipeline returns an empty dict (allow everything)."""
        pipeline = HookPipeline([])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result == {}

    @pytest.mark.asyncio
    async def test_short_circuits_on_interrupt(self):
        """Pipeline stops at first check returning continue_: False."""
        async def interrupt_check(inp, tid, ctx):
            return {"continue_": False, "stopReason": "stopped"}

        async def should_not_run(inp, tid, ctx):
            raise AssertionError("This check should not have been called")

        pipeline = HookPipeline([interrupt_check, should_not_run])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result["continue_"] is False
        assert result["stopReason"] == "stopped"

    @pytest.mark.asyncio
    async def test_short_circuits_on_deny(self):
        """Pipeline stops at first check returning permissionDecision: deny."""
        async def deny_check(inp, tid, ctx):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "blocked",
                }
            }

        async def should_not_run(inp, tid, ctx):
            raise AssertionError("This check should not have been called")

        pipeline = HookPipeline([deny_check, should_not_run])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "blocked"

    @pytest.mark.asyncio
    async def test_accumulates_context(self):
        """Pipeline merges additionalContext from multiple checks."""
        async def check_a(inp, tid, ctx):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "context A"}}

        async def check_b(inp, tid, ctx):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "context B"}}

        pipeline = HookPipeline([check_a, check_b])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "context A" in ctx
        assert "context B" in ctx

    @pytest.mark.asyncio
    async def test_none_checks_are_skipped(self):
        """Checks returning None are treated as no-ops."""
        async def noop(inp, tid, ctx):
            return None

        async def provides_context(inp, tid, ctx):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "hello"}}

        pipeline = HookPipeline([noop, provides_context])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert result["hookSpecificOutput"]["additionalContext"] == "hello"


class TestCheckInterrupt:
    """_make_interrupt_check returns stop when flag is set, clears it."""

    @pytest.mark.asyncio
    async def test_returns_none_when_not_set(self):
        """No interrupt flag -> None (no opinion)."""
        state = HookState()
        check = _make_interrupt_check(state)
        result = await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_stop_when_set(self):
        """Interrupt flag set -> continue_: False."""
        state = HookState(interrupt_flag=True)
        check = _make_interrupt_check(state)
        result = await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result is not None
        assert result["continue_"] is False
        assert result["stopReason"] == "Interrupted by user"

    @pytest.mark.asyncio
    async def test_clears_flag_after_firing(self):
        """Interrupt flag is cleared after the check fires."""
        state = HookState(interrupt_flag=True)
        check = _make_interrupt_check(state)
        await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert state.interrupt_flag is False


class TestCheckImmutableGuard:
    """_make_immutable_check denies writes to immutable files via pipeline."""

    @pytest.mark.asyncio
    async def test_deny_write_to_meeting_notes(self, config):
        """Denies Write to Meeting Notes through the pipeline check."""
        check = _make_immutable_check(config)
        inp = _make_pre_tool_use_input(
            tool_name="Write",
            tool_input={
                "file_path": str(config.vault_path / "Misc" / "Meeting Notes" / "test.md"),
                "content": "bad",
            },
        )
        result = await check(inp, "tu-123", _EMPTY_CONTEXT)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.mark.asyncio
    async def test_allow_read(self, config):
        """Allows Read tool (not a write-mutating tool)."""
        check = _make_immutable_check(config)
        inp = _make_pre_tool_use_input(
            tool_name="Read",
            tool_input={"file_path": str(config.vault_path / "Misc" / "Meeting Notes" / "test.md")},
        )
        result = await check(inp, "tu-123", _EMPTY_CONTEXT)
        assert result is None

    @pytest.mark.asyncio
    async def test_deny_env(self, config):
        """Denies Write to .env files."""
        check = _make_immutable_check(config)
        inp = _make_pre_tool_use_input(
            tool_name="Write",
            tool_input={"file_path": "/project/.env", "content": "SECRET=x"},
        )
        result = await check(inp, "tu-123", _EMPTY_CONTEXT)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.mark.asyncio
    async def test_deny_native_task_tools(self, config):
        """Denies native Task tools so AgentTask tooling is always used."""
        check = _make_immutable_check(config)
        inp = _make_pre_tool_use_input(
            tool_name="TaskStop",
            tool_input={"task_id": "native-task-1"},
        )
        result = await check(inp, "tu-123", _EMPTY_CONTEXT)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "AgentTaskStop" in reason

    @pytest.mark.asyncio
    async def test_ignores_non_pretooluse_events(self, config):
        """Returns None for non-PreToolUse events."""
        check = _make_immutable_check(config)
        inp = _make_post_tool_use_input()  # PostToolUse event
        result = await check(inp, "tu-123", _EMPTY_CONTEXT)
        assert result is None


class TestCheckMessageQueue:
    """_make_queue_check drains messages from the queue."""

    @pytest.mark.asyncio
    async def test_empty_queue_returns_none(self):
        """Empty queue -> None (no opinion)."""
        state = HookState()
        check = _make_queue_check(state)
        result = await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result is None

    @pytest.mark.asyncio
    async def test_single_message(self):
        """Single queued message appears in additionalContext."""
        state = HookState()
        state.message_queue.put_nowait("hello from user")
        check = _make_queue_check(state)
        result = await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result is not None
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "hello from user" in ctx
        assert "[Queued message from user]" in ctx

    @pytest.mark.asyncio
    async def test_multiple_messages(self):
        """Multiple queued messages all appear in additionalContext."""
        state = HookState()
        state.message_queue.put_nowait("first message")
        state.message_queue.put_nowait("second message")
        check = _make_queue_check(state)
        result = await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result is not None
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "first message" in ctx
        assert "second message" in ctx

    @pytest.mark.asyncio
    async def test_drains_all_messages(self):
        """Queue is empty after the check runs."""
        state = HookState()
        state.message_queue.put_nowait("msg1")
        state.message_queue.put_nowait("msg2")
        check = _make_queue_check(state)
        await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert state.message_queue.empty()

    @pytest.mark.asyncio
    async def test_pushes_status_event_on_drain(self):
        """Draining messages pushes a queue_delivered StatusEvent to status_queue."""
        from obs_agent.events import StatusEvent

        state = HookState()
        state.message_queue.put_nowait("hello")
        check = _make_queue_check(state)
        await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)

        assert not state.status_queue.empty()
        event = state.status_queue.get_nowait()
        assert isinstance(event, StatusEvent)
        assert event.type == "queue_delivered"
        assert event.summary == "queued message delivered"
        assert event.count == 1
        assert event.messages == ["hello"]

    @pytest.mark.asyncio
    async def test_status_event_count_matches_messages(self):
        """StatusEvent count matches the number of drained messages."""
        state = HookState()
        state.message_queue.put_nowait("msg1")
        state.message_queue.put_nowait("msg2")
        state.message_queue.put_nowait("msg3")
        check = _make_queue_check(state)
        await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)

        event = state.status_queue.get_nowait()
        assert event.count == 3
        assert event.messages == ["msg1", "msg2", "msg3"]

    @pytest.mark.asyncio
    async def test_no_status_event_on_empty_queue(self):
        """No StatusEvent pushed when message queue is empty."""
        state = HookState()
        check = _make_queue_check(state)
        await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert state.status_queue.empty()

    @pytest.mark.asyncio
    async def test_reply_target_messages_remain_queued(self):
        """Reply-target queued messages must not be injected as additionalContext."""
        state = HookState()
        deferred = QueuedMessage(
            text="fork me later",
            telegram_message_id=42,
            reply_to_message_id=7,
        )
        state.message_queue.put_nowait(deferred)
        check = _make_queue_check(state)

        result = await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)

        assert result is None
        remaining = state.message_queue.get_nowait()
        assert remaining == deferred
        assert state.status_queue.empty()

    @pytest.mark.asyncio
    async def test_plain_messages_drain_while_reply_targets_stay_queued(self):
        """Plain queued messages inject immediately, reply-targets stay for later routing."""
        state = HookState()
        deferred = QueuedMessage(
            text="fork me later",
            telegram_message_id=42,
            reply_to_message_id=7,
        )
        state.message_queue.put_nowait("plain message")
        state.message_queue.put_nowait(deferred)
        check = _make_queue_check(state)

        result = await check(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)

        assert result is not None
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "plain message" in ctx
        assert "fork me later" not in ctx
        remaining = state.message_queue.get_nowait()
        assert remaining == deferred


class TestHookStateStatusQueue:
    """HookState has a status_queue for SSE status events."""

    def test_hook_state_has_status_queue(self):
        """HookState has a status_queue field."""
        state = HookState()
        assert hasattr(state, "status_queue")
        assert state.status_queue.empty()

    def test_status_queue_accepts_events(self):
        """status_queue can accept StatusEvent objects."""
        from obs_agent.events import StatusEvent

        state = HookState()
        event = StatusEvent(type="test", summary="test event")
        state.status_queue.put_nowait(event)
        assert not state.status_queue.empty()
        got = state.status_queue.get_nowait()
        assert got is event


class TestCheckNotification:
    """_make_notification_check mirrors hook notifications into status queue."""

    @pytest.mark.asyncio
    async def test_notification_event_pushes_status_event(self):
        state = HookState()
        check = _make_notification_check(state)

        result = await check(_make_notification_input(), None, _EMPTY_CONTEXT)

        assert result is None
        event = state.status_queue.get_nowait()
        assert event.type == "notification"
        assert event.summary == "notification: TaskCompleted"
        assert event.messages == ["title: Task done", "worker-a completed task 1"]

    @pytest.mark.asyncio
    async def test_subagent_start_pushes_status_event(self):
        state = HookState()
        check = _make_notification_check(state)

        result = await check(_make_subagent_start_input(), None, _EMPTY_CONTEXT)

        assert result is None
        event = state.status_queue.get_nowait()
        assert event.type == "notification"
        assert event.summary == "notification: SubagentStart"
        assert "agent_id: agent-123" in (event.messages or [])
        assert "agent_type: general-purpose" in (event.messages or [])

    @pytest.mark.asyncio
    async def test_subagent_stop_pushes_status_event(self):
        state = HookState()
        check = _make_notification_check(state)

        result = await check(_make_subagent_stop_input(), None, _EMPTY_CONTEXT)

        assert result is None
        event = state.status_queue.get_nowait()
        assert event.type == "notification"
        assert event.summary == "notification: SubagentStop"
        assert "transcript: /tmp/agent-123.jsonl" in (event.messages or [])


class TestCheckStop:
    @pytest.mark.asyncio
    async def test_stop_event_notifies_transport(self):
        state = HookState()
        state.stop_event_notifier = AsyncMock()
        check = _make_stop_check(state)

        result = await check(_make_stop_input(), None, _EMPTY_CONTEXT)

        assert result is None
        state.stop_event_notifier.assert_awaited_once()
        payload = state.stop_event_notifier.await_args.args[0]
        assert payload["session_id"] == "test-session"
        assert payload["schedule_run_active"] is False
        assert payload["execution_active"] is False


class TestCreateHookMatchers:
    """create_hook_matchers builds the correct pipeline structure."""

    def test_returns_pre_post_and_notification_hooks(self, config):
        """Returns dict with task/tool and notification lifecycle hook keys."""
        state = HookState()
        matchers = create_hook_matchers(config, state)
        assert "PreToolUse" in matchers
        assert "PostToolUse" in matchers
        assert "Notification" in matchers
        assert "SubagentStart" in matchers
        assert "SubagentStop" in matchers
        assert "Stop" in matchers

    def test_pre_tool_use_has_one_matcher(self, config):
        """PreToolUse has exactly one HookMatcher with one pipeline."""
        state = HookState()
        matchers = create_hook_matchers(config, state)
        pre = matchers["PreToolUse"]
        assert len(pre) == 1
        assert pre[0].matcher is None  # matches all tools
        assert len(pre[0].hooks) == 1  # one pipeline callback
        assert isinstance(pre[0].hooks[0], HookPipeline)

    def test_post_tool_use_has_one_matcher(self, config):
        """PostToolUse has exactly one HookMatcher with one pipeline."""
        state = HookState()
        matchers = create_hook_matchers(config, state)
        post = matchers["PostToolUse"]
        assert len(post) == 1
        assert post[0].matcher is None
        assert len(post[0].hooks) == 1
        assert isinstance(post[0].hooks[0], HookPipeline)

    @pytest.mark.asyncio
    async def test_pre_tool_use_pipeline_blocks_immutable(self, config):
        """The PreToolUse pipeline blocks writes to Meeting Notes."""
        state = HookState()
        matchers = create_hook_matchers(config, state)
        pipeline = matchers["PreToolUse"][0].hooks[0]

        inp = _make_pre_tool_use_input(
            tool_name="Write",
            tool_input={
                "file_path": str(config.vault_path / "Misc" / "Meeting Notes" / "test.md"),
                "content": "bad",
            },
        )
        result = await pipeline(inp, "tu-123", _EMPTY_CONTEXT)
        assert result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    @pytest.mark.asyncio
    async def test_pre_tool_use_pipeline_interrupts(self, config):
        """The PreToolUse pipeline short-circuits on interrupt flag."""
        state = HookState(interrupt_flag=True)
        matchers = create_hook_matchers(config, state)
        pipeline = matchers["PreToolUse"][0].hooks[0]

        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result.get("continue_") is False
        assert state.interrupt_flag is False

    @pytest.mark.asyncio
    async def test_post_tool_use_pipeline_drains_queue(self, config):
        """The PostToolUse pipeline drains queued messages."""
        state = HookState()
        state.message_queue.put_nowait("queued msg")
        matchers = create_hook_matchers(config, state)
        pipeline = matchers["PostToolUse"][0].hooks[0]

        result = await pipeline(_make_post_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result.get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse"
        assert "queued msg" in result.get("hookSpecificOutput", {}).get("additionalContext", "")

    @pytest.mark.asyncio
    async def test_notification_pipeline_pushes_status_event(self, config):
        state = HookState()
        matchers = create_hook_matchers(config, state)
        pipeline = matchers["Notification"][0].hooks[0]

        result = await pipeline(_make_notification_input(), None, _EMPTY_CONTEXT)

        assert result == {}
        event = state.status_queue.get_nowait()
        assert event.type == "notification"
        assert event.summary == "notification: TaskCompleted"


# ---------------------------------------------------------------------------
# Dynamic User Hook Loading Tests
# ---------------------------------------------------------------------------


class TestLoadHookFunction:
    """Tests for load_hook_function — dynamic Python function loading."""

    def test_loads_valid_function(self, tmp_path):
        """Loads a simple function from a temp .py file."""
        hook_file = tmp_path / "my_hook.py"
        hook_file.write_text(
            "def my_check(hook_input, tool_use_id, context):\n"
            "    return {'decision': 'allow'}\n"
        )
        func = load_hook_function(str(hook_file), "my_check")
        assert callable(func)
        assert func.__name__ == "my_check"
        # Verify it actually works
        result = func({}, None, {})
        assert result == {"decision": "allow"}

    def test_file_not_found(self, tmp_path):
        """Raises FileNotFoundError for nonexistent file."""
        with pytest.raises(FileNotFoundError, match="Hook file not found"):
            load_hook_function(str(tmp_path / "nonexistent.py"), "fn")

    def test_function_not_found_lists_available(self, tmp_path):
        """Raises AttributeError listing available functions when target not found."""
        hook_file = tmp_path / "hook.py"
        hook_file.write_text(
            "def alpha():\n    pass\n"
            "def beta():\n    pass\n"
        )
        with pytest.raises(AttributeError, match="Available:.*alpha.*beta"):
            load_hook_function(str(hook_file), "nonexistent")

    def test_non_py_file_rejected(self, tmp_path):
        """Raises ValueError for non-.py files."""
        txt_file = tmp_path / "hook.txt"
        txt_file.write_text("def fn(): pass")
        with pytest.raises(ValueError, match="must be a .py file"):
            load_hook_function(str(txt_file), "fn")

    def test_syntax_error_in_file(self, tmp_path):
        """Raises SyntaxError for files with syntax errors."""
        bad_file = tmp_path / "bad.py"
        bad_file.write_text("def broken(\n")
        with pytest.raises(SyntaxError, match="Syntax error"):
            load_hook_function(str(bad_file), "broken")

    def test_not_callable_rejected(self, tmp_path):
        """Raises TypeError if the name resolves to a non-callable."""
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("NOT_A_FUNC = 42\n")
        with pytest.raises(TypeError, match="is not callable"):
            load_hook_function(str(hook_file), "NOT_A_FUNC")

    def test_lenient_signature_one_param_loads(self, tmp_path):
        """Loads function with only 1 parameter (warns but doesn't fail)."""
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("def one_param(x):\n    return None\n")
        func = load_hook_function(str(hook_file), "one_param")
        assert callable(func)

    def test_kwargs_function_accepted(self, tmp_path):
        """Function with **kwargs doesn't trigger signature warning."""
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("def flex(**kwargs):\n    return None\n")
        func = load_hook_function(str(hook_file), "flex")
        assert callable(func)

    def test_import_error_in_file(self, tmp_path):
        """Raises ImportError for files that fail during import."""
        bad_file = tmp_path / "bad_import.py"
        bad_file.write_text("import nonexistent_module_xyz_12345\n")
        with pytest.raises(ImportError, match="Error loading hook file"):
            load_hook_function(str(bad_file), "fn")

    def test_path_resolution_works(self, tmp_path):
        """Path resolution (expanduser, resolve) works for valid paths."""
        hook_file = tmp_path / "hook.py"
        hook_file.write_text("def fn(a, b, c):\n    return None\n")
        func = load_hook_function(str(hook_file), "fn")
        assert callable(func)


class TestMakeUserHookCheck:
    """Tests for _make_user_hook_check — async wrapper with enriched context."""

    @pytest.mark.asyncio
    async def test_sync_hook_returns_correctly(self):
        """Synchronous user hook returns its dict result."""
        def sync_hook(hook_input, tool_use_id, context):
            return {"decision": "allow"}

        state = HookState()
        check = _make_user_hook_check(sync_hook, state)
        result = await check({"tool_name": "Read"}, "tu-1", {})
        assert result == {"decision": "allow"}

    @pytest.mark.asyncio
    async def test_async_hook_returns_correctly(self):
        """Async user hook returns its dict result."""
        async def async_hook(hook_input, tool_use_id, context):
            return {"decision": "deny"}

        state = HookState()
        check = _make_user_hook_check(async_hook, state)
        result = await check({"tool_name": "Write"}, "tu-2", {})
        assert result == {"decision": "deny"}

    @pytest.mark.asyncio
    async def test_exception_swallowed_returns_none(self):
        """Exception in user hook is caught, returns None (never crashes)."""
        def exploding_hook(hook_input, tool_use_id, context):
            raise RuntimeError("kaboom!")

        state = HookState()
        check = _make_user_hook_check(exploding_hook, state)
        result = await check({"tool_name": "Bash"}, "tu-3", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_bad_return_type_treated_as_none(self):
        """Non-dict, non-None return treated as None with warning."""
        def bad_return_hook(hook_input, tool_use_id, context):
            return "this is not a dict"

        state = HookState()
        check = _make_user_hook_check(bad_return_hook, state)
        result = await check({"tool_name": "Read"}, "tu-4", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_none_return_passes_through(self):
        """Hook returning None passes through correctly (no opinion)."""
        def noop_hook(hook_input, tool_use_id, context):
            return None

        state = HookState()
        check = _make_user_hook_check(noop_hook, state)
        result = await check({"tool_name": "Read"}, "tu-5", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_context_enriched_with_obs_capabilities(self):
        """Context dict is enriched with obs.launch_agent, agent_output, agent_stop, session_id."""
        received_context = {}

        def capture_hook(hook_input, tool_use_id, context):
            received_context.update(context)
            return None

        launcher = AsyncMock(return_value={"agentId": "test"})
        outputter = AsyncMock(return_value={"output": "test"})
        stopper = AsyncMock(return_value={"stopped": True})

        state = HookState(
            session_id="sess-123",
            fork_task_launcher=launcher,
            fork_task_outputter=outputter,
            fork_task_stopper=stopper,
        )
        check = _make_user_hook_check(capture_hook, state)
        await check({"tool_name": "Read"}, "tu-6", {"signal": None})

        assert "obs" in received_context
        obs = received_context["obs"]
        assert obs["launch_agent"] is launcher
        assert obs["agent_output"] is outputter
        assert obs["agent_stop"] is stopper
        assert obs["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_context_obs_none_when_state_fields_none(self):
        """When HookState fields are None, context['obs'] values are None (not crash)."""
        received_context = {}

        def capture_hook(hook_input, tool_use_id, context):
            received_context.update(context)
            return None

        state = HookState()  # All launchers default to None
        check = _make_user_hook_check(capture_hook, state)
        await check({"tool_name": "Read"}, "tu-7", {})

        assert "obs" in received_context
        obs = received_context["obs"]
        assert obs["launch_agent"] is None
        assert obs["agent_output"] is None
        assert obs["agent_stop"] is None
        assert obs["session_id"] is None

    @pytest.mark.asyncio
    async def test_original_context_not_mutated(self):
        """The wrapper creates a copy of context — original is not mutated."""
        original_context = {"signal": None}

        def noop_hook(hook_input, tool_use_id, context):
            return None

        state = HookState(session_id="sess-x")
        check = _make_user_hook_check(noop_hook, state)
        await check({"tool_name": "Read"}, "tu-8", original_context)

        assert "obs" not in original_context  # Original untouched

    @pytest.mark.asyncio
    async def test_async_exception_swallowed(self):
        """Async hook that raises is also caught and swallowed."""
        async def async_explode(hook_input, tool_use_id, context):
            raise ValueError("async kaboom!")

        state = HookState()
        check = _make_user_hook_check(async_explode, state)
        result = await check({"tool_name": "Bash"}, "tu-9", {})
        assert result is None
