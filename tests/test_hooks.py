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

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from obs_agent.config import OBSConfig
from obs_agent.hooks import (
    on_pre_tool_use,
    on_stop,
    on_pre_compact,
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
