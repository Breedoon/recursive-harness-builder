"""Tests for obs_agent.hooks - Step 4 TDD (RED phase).

These tests define the Hook contracts for:
- PreToolUse: guards immutable files and .env from writes
- Stop: triggers memory extraction via fork
- PreCompact: triggers extraction then denies compaction
- UserPromptSubmit: classifies skills and injects SKILL.md content

See implementation-plan.md Step 4 and decisions:
- D018: Forks for subtasks
- D019: Skill injection via fork classification
- D022: No compaction - flush and restart
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
    _make_queue_check,
    create_hook_matchers,
)


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

    def test_allows_write_to_agent_files(self, config):
        """Allows Write tool for Agent/ directory files."""
        result = on_pre_tool_use(
            tool_name="Write",
            tool_input={
                "file_path": str(config.vault_path / "Agent" / "context.md"),
                "content": "updated context",
            },
            config=config,
        )
        # Should return None or empty dict (allow)
        assert result is None or result == {}

    def test_allows_edit_to_agent_files(self, config):
        """Allows Edit tool for Agent/ directory files."""
        result = on_pre_tool_use(
            tool_name="Edit",
            tool_input={
                "file_path": str(config.vault_path / "Agent" / "topics" / "goals.md"),
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


# --- UserPromptSubmit Hook ---


class TestUserPromptSubmitHook:
    """UserPromptSubmit hook classifies user message and injects skills."""

    @pytest.mark.asyncio
    async def test_triggers_classify(self, config):
        """Hook calls fork runner's classify method with the user message."""
        # Import here since the function may not exist on the stub
        from obs_agent.hooks import on_user_prompt_submit

        mock_fork_runner = MagicMock()
        mock_fork_runner.classify = AsyncMock(return_value=["file-conventions"])

        await on_user_prompt_submit(
            user_message="help me organize my vault",
            config=config,
            fork_runner=mock_fork_runner,
        )

        mock_fork_runner.classify.assert_called_once_with("help me organize my vault")

    @pytest.mark.asyncio
    async def test_reads_skill_files(self, config, fixture_vault):
        """After classify returns skill names, hook reads SKILL.md files."""
        from obs_agent.hooks import on_user_prompt_submit

        # Create a skill file in the fixture vault
        skill_dir = fixture_vault / "Agent" / "skills" / "file-conventions"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: file-conventions\n---\n# File Conventions\nMaster reference.\n"
        )

        mock_fork_runner = MagicMock()
        mock_fork_runner.classify = AsyncMock(return_value=["file-conventions"])

        result = await on_user_prompt_submit(
            user_message="help me organize my vault",
            config=config,
            fork_runner=mock_fork_runner,
        )

        # Result should contain the skill content to inject
        assert result is not None
        assert isinstance(result, (str, dict))
        # The skill content should be included somehow
        result_str = str(result)
        assert "file-conventions" in result_str.lower() or "File Conventions" in result_str, (
            "Hook must return skill content for injection"
        )

    @pytest.mark.asyncio
    async def test_no_skills_returns_empty(self, config):
        """When classify returns no skills, hook returns None or empty."""
        from obs_agent.hooks import on_user_prompt_submit

        mock_fork_runner = MagicMock()
        mock_fork_runner.classify = AsyncMock(return_value=[])

        result = await on_user_prompt_submit(
            user_message="what time is it",
            config=config,
            fork_runner=mock_fork_runner,
        )

        # No skills needed = no injection
        assert result is None or result == "" or result == {}


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
                    "permissionDecision": "deny",
                    "reason": "blocked",
                }
            }

        async def should_not_run(inp, tid, ctx):
            raise AssertionError("This check should not have been called")

        pipeline = HookPipeline([deny_check, should_not_run])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["reason"] == "blocked"

    @pytest.mark.asyncio
    async def test_accumulates_context(self):
        """Pipeline merges additionalContext from multiple checks."""
        async def check_a(inp, tid, ctx):
            return {"hookSpecificOutput": {"additionalContext": "context A"}}

        async def check_b(inp, tid, ctx):
            return {"hookSpecificOutput": {"additionalContext": "context B"}}

        pipeline = HookPipeline([check_a, check_b])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "context A" in ctx
        assert "context B" in ctx

    @pytest.mark.asyncio
    async def test_none_checks_are_skipped(self):
        """Checks returning None are treated as no-ops."""
        async def noop(inp, tid, ctx):
            return None

        async def provides_context(inp, tid, ctx):
            return {"hookSpecificOutput": {"additionalContext": "hello"}}

        pipeline = HookPipeline([noop, provides_context])
        result = await pipeline(_make_pre_tool_use_input(), "tu-123", _EMPTY_CONTEXT)
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


class TestCreateHookMatchers:
    """create_hook_matchers builds the correct pipeline structure."""

    def test_returns_pre_and_post_tool_use(self, config):
        """Returns dict with PreToolUse and PostToolUse keys."""
        state = HookState()
        matchers = create_hook_matchers(config, state)
        assert "PreToolUse" in matchers
        assert "PostToolUse" in matchers

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
        assert "queued msg" in result.get("hookSpecificOutput", {}).get("additionalContext", "")
