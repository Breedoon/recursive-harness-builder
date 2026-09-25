"""Tests for obs_agent.hooks - Hook contracts.

- PreToolUse: guards immutable files and .env from writes
- Stop: triggers memory extraction via fork
- PreCompact: opt-in handoff policy interrupts automatic compaction
- HookPipeline: extensible middleware that chains check functions
- HookState: shared state for message queuing and interrupt

See implementation-plan.md Step 4 and decisions D018, D022.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from obs_agent.config import OBSConfig
from obs_agent.lineage import build_obs_bootstrap_xml
from obs_agent.hooks import (
    on_pre_tool_use,
    on_stop,
    _make_pre_compact_callback,
    DEFAULT_COMPACTION_HANDOFF_PROMPT,
    HookState,
    HookPipeline,
    _make_interrupt_check,
    _make_immutable_check,
    _make_notification_check,
    _make_stop_check,
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

    def test_allows_write_to_env_files(self, config):
        """.env writes are allowed (Daniel 2026-09-25: guard never wanted)."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": "/workspace/recursive-harness/.env",
                "content": "SECRET=123",
            },
            config=config,
        )
        assert result is None

    def test_allows_edit_to_env_files(self, config):
        """.env edits are allowed (Daniel 2026-09-25: guard never wanted)."""
        result = on_pre_tool_use(
            tool_name="Edit",
            tool_input={
                "file_path": "/some/path/.env.local",
                "old_string": "KEY=old",
                "new_string": "KEY=new",
            },
            config=config,
        )
        assert result is None


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


# --- PreCompact Hook: opt-in handoff policy ---


def _pre_compact_input(trigger: str = "auto") -> dict:
    return {
        "hook_event_name": "PreCompact",
        "session_id": "sess-1",
        "transcript_path": "/tmp/sess-1.jsonl",
        "trigger": trigger,
        "custom_instructions": None,
    }


class TestPreCompactHandoffPolicy:
    """OBS_COMPACT_POLICY=handoff interrupts automatic compaction (E3 design)."""

    @pytest.mark.asyncio
    async def test_default_policy_leaves_compaction_alone(self):
        state = HookState()
        interrupter = AsyncMock()
        state.client_interrupter = interrupter
        callback = _make_pre_compact_callback(state, None)

        result = await callback(_pre_compact_input(), None, {})

        assert result == {}
        interrupter.assert_not_called()
        assert state.compaction_intercepted is False

    @pytest.mark.asyncio
    async def test_default_policy_passes_user_result_through(self):
        state = HookState()
        state.client_interrupter = AsyncMock()

        async def user_check(hook_input, tool_use_id, context):
            return {"systemMessage": "summarize carefully"}

        callback = _make_pre_compact_callback(state, user_check)
        assert await callback(_pre_compact_input(), None, {}) == {"systemMessage": "summarize carefully"}

    @pytest.mark.asyncio
    async def test_handoff_policy_awaits_interrupt_and_records_state(self):
        state = HookState()
        state.sdk_env_overrides = {"OBS_COMPACT_POLICY": "handoff"}
        order: list[str] = []

        async def interrupter():
            order.append("interrupt")

        state.client_interrupter = interrupter
        callback = _make_pre_compact_callback(state, None)

        result = await callback(_pre_compact_input(), None, {})

        assert result == {}
        assert order == ["interrupt"]
        assert state.compaction_intercepted is True
        assert state.compaction_handoff_prompt is None  # runner uses the default
        assert state.compaction_intercept_info["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_handoff_prompt_comes_from_user_hook_additional_context(self):
        state = HookState()
        state.sdk_env_overrides = {"OBS_COMPACT_POLICY": " Handoff "}
        state.client_interrupter = AsyncMock()
        calls: list[str] = []

        async def user_check(hook_input, tool_use_id, context):
            calls.append("user")
            return {"hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": "WRITE THE HANDBACK"}}

        callback = _make_pre_compact_callback(state, user_check)
        result = await callback(_pre_compact_input(), None, {})

        assert result == {}
        assert calls == ["user"]
        assert state.compaction_handoff_prompt == "WRITE THE HANDBACK"
        state.client_interrupter.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_manual_compact_is_not_intercepted(self):
        state = HookState()
        state.sdk_env_overrides = {"OBS_COMPACT_POLICY": "handoff"}
        state.client_interrupter = AsyncMock()
        callback = _make_pre_compact_callback(state, None)

        assert await callback(_pre_compact_input("manual"), None, {}) == {}
        state.client_interrupter.assert_not_called()
        assert state.compaction_intercepted is False

    @pytest.mark.asyncio
    async def test_second_firing_does_not_interrupt_twice(self):
        state = HookState()
        state.sdk_env_overrides = {"OBS_COMPACT_POLICY": "handoff"}
        state.client_interrupter = AsyncMock()
        callback = _make_pre_compact_callback(state, None)

        await callback(_pre_compact_input(), None, {})
        await callback(_pre_compact_input(), None, {})
        state.client_interrupter.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_or_failing_interrupter_lets_native_compaction_proceed(self):
        state = HookState()
        state.sdk_env_overrides = {"OBS_COMPACT_POLICY": "handoff"}
        callback = _make_pre_compact_callback(state, None)
        assert await callback(_pre_compact_input(), None, {}) == {}
        assert state.compaction_intercepted is False

        state.client_interrupter = AsyncMock(side_effect=RuntimeError("gone"))
        assert await callback(_pre_compact_input(), None, {}) == {}
        assert state.compaction_intercepted is False

    def test_default_prompt_demands_verbose_cited_handback(self):
        text = DEFAULT_COMPACTION_HANDOFF_PROMPT
        assert "Do NOT continue the task" in text
        assert "VERBOSELY" in text and "Source-cite" in text and "related-reports" in text

    @pytest.mark.asyncio
    async def test_create_hook_matchers_registers_pre_compact_with_user_hook(self, config, tmp_path):
        hook_file = tmp_path / "pc.py"
        hook_file.write_text(
            "def pc(hook_input, tool_use_id, context):\n"
            "    return {'hookSpecificOutput': {'hookEventName': 'PreCompact', 'additionalContext': 'VAULT PROMPT'}}\n"
        )
        state = HookState()
        state.sdk_env_overrides = {"OBS_COMPACT_POLICY": "handoff"}
        state.client_interrupter = AsyncMock()
        matchers = create_hook_matchers(config, state, user_hooks={"PreCompact": f"{hook_file}::pc"})
        assert "PreCompact" in matchers
        callback = matchers["PreCompact"][0].hooks[0]
        await callback(_pre_compact_input(), None, {})
        assert state.compaction_handoff_prompt == "VAULT PROMPT"
        state.client_interrupter.assert_awaited_once()


class TestStopDecisionPassThrough:
    """User Stop hooks can block ending the turn (CLI 2.1.59 honours it, E3 probe b)."""

    @pytest.mark.asyncio
    async def test_stop_block_is_passed_through(self):
        async def blocker(hook_input, tool_use_id, context):
            return {"decision": "block", "reason": "write handback.md first"}

        pipeline = HookPipeline([AsyncMock(return_value=None), blocker])
        result = await pipeline({"hook_event_name": "Stop", "stop_hook_active": False}, None, {})
        assert result["decision"] == "block"
        assert result["reason"] == "write handback.md first"

    @pytest.mark.asyncio
    async def test_stop_without_block_is_unchanged(self):
        async def ctx(hook_input, tool_use_id, context):
            return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "note"}}

        result = await HookPipeline([ctx])({"hook_event_name": "Stop"}, None, {})
        assert "decision" not in result
        assert result["hookSpecificOutput"]["additionalContext"] == "note"

    @pytest.mark.asyncio
    async def test_decision_block_on_post_tool_use_is_not_passed_through(self):
        async def blocker(hook_input, tool_use_id, context):
            return {"decision": "block", "reason": "x"}

        result = await HookPipeline([blocker])({"hook_event_name": "PostToolUse"}, "t1", {})
        assert "decision" not in result

    @pytest.mark.asyncio
    async def test_stop_block_through_create_hook_matchers(self, config, tmp_path):
        hook_file = tmp_path / "st.py"
        hook_file.write_text(
            "def st(hook_input, tool_use_id, context):\n"
            "    return {'decision': 'block', 'reason': 'not yet'}\n"
        )
        state = HookState()
        matchers = create_hook_matchers(config, state, user_hooks={"Stop": f"{hook_file}::st"})
        result = await matchers["Stop"][0].hooks[0]({"hook_event_name": "Stop", "session_id": "s"}, None, {})
        assert result["decision"] == "block" and result["reason"] == "not yet"


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
    async def test_allows_env(self, config):
        """Allows Write to .env files (Daniel 2026-09-25: guard never wanted)."""
        check = _make_immutable_check(config)
        inp = _make_pre_tool_use_input(
            tool_name="Write",
            tool_input={"file_path": "/project/.env", "content": "SECRET=x"},
        )
        result = await check(inp, "tu-123", _EMPTY_CONTEXT)
        assert not result or result.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

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
    """Native hook context must never own non-replayable queued input."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
    async def test_messages_remain_for_canonical_runner_query(self, config, event):
        state = HookState()
        messages = ["same", "same", QueuedMessage("reply", 42, 7)]
        for message in messages:
            state.message_queue.put_nowait(message)
        callback = create_hook_matchers(config, state)[event][0].hooks[0]
        inp = _make_pre_tool_use_input() if event == "PreToolUse" else _make_post_tool_use_input()

        result = await callback(inp, "tu-123", _EMPTY_CONTEXT)

        assert result.get("hookSpecificOutput", {}).get("additionalContext", "") == ""
        assert [state.message_queue.get_nowait() for _ in messages] == messages
        assert state.status_queue.empty()


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

    def test_reset_clears_runtime_snapshot_fields(self):
        state = HookState(
            sdk_env_overrides={"A": "B"},
            vault_path=Path("/tmp"),
            effective_model="claude-opus-4-6[1m]",
            current_tool_use_id="tu",
            schedule_run_active=True,
            execution_active=True,
            triggered_schedule_id="sched",
            active_schedule={"id": "sched"},
            pending_obs_bootstrap_xml="<obs-bootstrap/>",
        )

        state.reset()

        assert state.sdk_env_overrides == {}
        assert state.vault_path is None
        assert state.effective_model is None
        assert state.current_tool_use_id is None
        assert state.schedule_run_active is False
        assert state.execution_active is False
        assert state.triggered_schedule_id is None
        assert state.active_schedule is None


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
        state.current_tool_use_id = "tu-stop"
        state.triggered_schedule_id = "sched-stop"
        state.active_schedule = {"id": "sched-stop"}
        check = _make_stop_check(state)

        result = await check(_make_stop_input(), None, _EMPTY_CONTEXT)

        assert result is None
        state.stop_event_notifier.assert_awaited_once()
        payload = state.stop_event_notifier.await_args.args[0]
        assert payload["session_id"] == "test-session"
        assert payload["schedule_run_active"] is False
        assert payload["execution_active"] is False
        assert payload["current_tool_use_id"] == "tu-stop"
        assert payload["triggered_schedule_id"] == "sched-stop"
        assert payload["active_schedule"] == {"id": "sched-stop"}


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
    async def test_post_tool_use_pipeline_keeps_queue(self, config):
        """Tool completion cannot move queued input into transient context."""
        state = HookState()
        state.message_queue.put_nowait("queued msg")
        matchers = create_hook_matchers(config, state)
        pipeline = matchers["PostToolUse"][0].hooks[0]

        result = await pipeline(_make_post_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result == {}
        assert state.message_queue.get_nowait() == "queued msg"
        assert state.status_queue.empty()

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

    @pytest.mark.asyncio
    async def test_user_hook_loads_from_vault_relative_agenttask_spec(self, config):
        hook_file = config.vault_path / "Projects" / "Hooks" / "relative_hook.py"
        hook_file.parent.mkdir(parents=True)
        hook_file.write_text(
            "def check(hook_input, tool_use_id, context):\n"
            "    return {'hookSpecificOutput': {'additionalContext': 'relative hook ran'}}\n",
            encoding="utf-8",
        )
        state = HookState()
        matchers = create_hook_matchers(
            config,
            state,
            user_hooks={"PreToolUse": "Projects/Hooks/relative_hook.py::check"},
        )
        pipeline = matchers["PreToolUse"][0].hooks[0]

        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)

        assert "relative hook ran" in result.get("hookSpecificOutput", {}).get("additionalContext", "")


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
            current_tool_use_id="tu-existing",
            schedule_run_active=True,
            execution_active=True,
            triggered_schedule_id="sched-123",
            active_schedule={"id": "sched-123", "description": "test schedule"},
            effective_model="claude-opus-4-6[1m]",
        )
        check = _make_user_hook_check(capture_hook, state)
        await check({"tool_name": "Read"}, "tu-6", {"signal": None})

        assert "obs" in received_context
        obs = received_context["obs"]
        assert obs["launch_agent"] is launcher
        assert obs["agent_output"] is outputter
        assert obs["agent_stop"] is stopper
        assert obs["session_id"] == "sess-123"
        assert obs["runtime"] == {
            "schedule_run_active": True,
            "execution_active": True,
            "current_tool_use_id": "tu-existing",
        }
        assert obs["schedule"] == {
            "triggered_schedule_id": "sched-123",
            "active_schedule": {"id": "sched-123", "description": "test schedule"},
        }
        assert obs["triggered_schedule_id"] == "sched-123"
        assert obs["active_schedule"] == {"id": "sched-123", "description": "test schedule"}
        assert obs["effective_model"] == "claude-opus-4-6[1m]"
        assert obs["sdk_env_overrides"] == {}
        assert obs["bootstrap"] is None

    @pytest.mark.asyncio
    async def test_context_enriched_with_sdk_env_overrides_and_bootstrap(self, tmp_path):
        """Hooks can read SDK env overrides and bootstrap identity from context['obs']."""
        received_context = {}

        def capture_hook(hook_input, tool_use_id, context):
            received_context.update(context)
            return None

        bootstrap_xml = build_obs_bootstrap_xml(
            lineage=("Root", "Worker"),
            origin="agent_task_fresh",
            is_fork=False,
            session_id=None,
            root_team_key="2026-03-31-10-00-root",
            agent_name="aaaaaaaaaa-worker",
        )
        state = HookState(
            pending_obs_bootstrap_xml=bootstrap_xml,
            sdk_env_overrides={
                "CLAUDE_CODE_TEAM_NAME": "2026-03-31-10-00-root",
                "CLAUDE_CODE_AGENT_NAME": "aaaaaaaaaa-worker",
            },
            vault_path=tmp_path,
        )
        check = _make_user_hook_check(capture_hook, state)
        await check({"tool_name": "Read", "session_id": "sid-live"}, "tu-7", {})

        obs = received_context["obs"]
        assert obs["session_id"] == "sid-live"
        assert obs["sdk_env_overrides"] == {
            "CLAUDE_CODE_TEAM_NAME": "2026-03-31-10-00-root",
            "CLAUDE_CODE_AGENT_NAME": "aaaaaaaaaa-worker",
        }
        assert obs["bootstrap"]["lineage"] == ["Root", "Worker"]
        assert obs["bootstrap"]["session_id"] == "sid-live"
        assert obs["bootstrap"]["root_team_key"] == "2026-03-31-10-00-root"
        assert obs["bootstrap"]["agent_name"] == "aaaaaaaaaa-worker"
        assert obs["bootstrap"]["xml"] == bootstrap_xml

    @pytest.mark.asyncio
    async def test_context_exposes_pre_tool_use_id_immediately(self):
        received_context = {}

        def capture_hook(hook_input, tool_use_id, context):
            received_context.update(context)
            return None

        state = HookState()
        check = _make_user_hook_check(capture_hook, state)
        await check(_make_pre_tool_use_input(), "tu-live", {})

        assert received_context["obs"]["runtime"]["current_tool_use_id"] == "tu-live"

    @pytest.mark.asyncio
    async def test_context_uses_safe_snapshot_provider(self):
        received_context = {}

        def capture_hook(hook_input, tool_use_id, context):
            received_context.update(context)
            return None

        def snapshot_provider(**kwargs):
            assert kwargs["session_id"] == "sid-live"
            assert kwargs["tool_use_id"] == "tu-snapshot"
            return {
                "route": {"chat_id": 123, "thread_id": 456},
                "team": {"team_name": "root", "agent_name": "worker", "lineage": ["Root", "Worker"]},
                "session": {"session_id": "sid-live", "head_uuid": "uuid-1"},
                "topic": {"title": "Worker"},
                "effective_model": "gpt-5.4-mini[200k]",
                "unsafe": {"secret": "not exposed"},
            }

        state = HookState(context_snapshot_provider=snapshot_provider)
        check = _make_user_hook_check(capture_hook, state)
        await check({"tool_name": "Read", "session_id": "sid-live"}, "tu-snapshot", {})

        obs = received_context["obs"]
        assert obs["snapshot"] == {
            "route": {"chat_id": 123, "thread_id": 456},
            "team": {"team_name": "root", "agent_name": "worker", "lineage": ["Root", "Worker"]},
            "session": {"session_id": "sid-live", "head_uuid": "uuid-1"},
            "topic": {"title": "Worker"},
            "effective_model": "gpt-5.4-mini[200k]",
        }
        assert obs["route"] == {"chat_id": 123, "thread_id": 456}
        assert obs["team"]["agent_name"] == "worker"
        assert obs["effective_model"] == "gpt-5.4-mini[200k]"
        assert "unsafe" not in obs["snapshot"]

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
        assert obs["runtime"] == {
            "schedule_run_active": False,
            "execution_active": False,
            "current_tool_use_id": None,
        }
        assert obs["schedule"] == {
            "triggered_schedule_id": None,
            "active_schedule": None,
        }
        assert obs["snapshot"] is None
        assert obs["effective_model"] is None
        assert obs["sdk_env_overrides"] == {}
        assert obs["bootstrap"] is None

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
