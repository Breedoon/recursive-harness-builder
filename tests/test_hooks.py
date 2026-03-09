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
    _make_queue_check,
    create_hook_matchers,
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
