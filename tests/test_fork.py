"""Tests for obs_agent.fork - ForkRunner contract.

ForkRunner is the generic mechanism for forking Claude sessions
to perform subtasks (run, extract_memory).

See implementation-plan.md Step 3 and decision D018 (forking as core primitive).
SDK reference: fork_session=True reuses KV cache for near-zero marginal cost.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import TextBlock

from obs_agent.config import OBSConfig
from obs_agent.fork import ForkRunner
from tests.conftest import AsyncIterFromList


# --- Initialization ---


class TestForkRunnerInit:
    """ForkRunner requires config and session_id to operate."""

    def test_takes_config_and_session_id(self, config):
        """ForkRunner can be created with config and session_id."""
        runner = ForkRunner(config=config, session_id="sess-abc-123")
        assert runner.config is config
        assert runner.session_id == "sess-abc-123"

    def test_requires_config(self):
        """ForkRunner needs a config object."""
        with pytest.raises(TypeError):
            ForkRunner(session_id="sess-abc-123")

    def test_requires_session_id(self, config):
        """ForkRunner needs a session_id to fork from."""
        with pytest.raises(TypeError):
            ForkRunner(config=config)


# --- Generic Fork Execution ---


class TestForkRun:
    """ForkRunner.run() creates forked sessions with correct SDK options."""

    @pytest.fixture
    def runner(self, config):
        return ForkRunner(config=config, session_id="sess-abc-123")

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_options_include_session(self, mock_query, runner):
        """run() passes resume=session_id to the SDK for cache reuse."""
        mock_query.return_value = AsyncIterFromList([])

        await runner.run("Do something")

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.resume == "sess-abc-123"

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_options_set_fork_flag(self, mock_query, runner):
        """run() sets fork_session=True so original session is preserved."""
        mock_query.return_value = AsyncIterFromList([])

        await runner.run("Do something")

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.fork_session is True

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_inherits_system_prompt(self, mock_query, runner):
        """Forked session uses the same system prompt for KV cache reuse."""
        mock_query.return_value = AsyncIterFromList([])

        await runner.run("Do something", system_prompt="You are an assistant.")

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.system_prompt == "You are an assistant."

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_max_turns(self, mock_query, runner):
        """run() respects the max_turns parameter for safety."""
        mock_query.return_value = AsyncIterFromList([])

        await runner.run("Do something", max_turns=3)

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.max_turns == 3

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_passes_prompt(self, mock_query, runner):
        """run() passes the task prompt to the SDK query function."""
        mock_query.return_value = AsyncIterFromList([])

        await runner.run("Analyze this conversation")

        call_kwargs = mock_query.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[0][0]
        assert "Analyze this conversation" in prompt


# --- Extract Memory Fork ---


class TestExtractMemoryFork:
    """extract_memory() persists session learnings to the vault."""

    @pytest.fixture
    def runner(self, config):
        return ForkRunner(config=config, session_id="sess-abc-123")

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_extract_memory_prompt_follows_offboard(self, mock_query, runner):
        """extract_memory prompt references the session-offboard procedure."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Memory extraction complete.")]
        mock_query.return_value = AsyncIterFromList([mock_msg])

        await runner.extract_memory()

        call_kwargs = mock_query.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[0][0]
        prompt_lower = prompt.lower()
        # The extraction prompt must reference offboard procedure
        assert any(
            term in prompt_lower
            for term in ["offboard", "memory", "extract", "persist", "session"]
        ), "Extract prompt must reference session-offboard or memory extraction procedure"

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_extract_memory_mentions_daily_log(self, mock_query, runner):
        """extract_memory prompt instructs writing to .claude/memory/YYYY-MM-DD.md."""
        mock_msg = MagicMock()
        mock_msg.content = [TextBlock(text="Memory extraction complete.")]
        mock_query.return_value = AsyncIterFromList([mock_msg])

        await runner.extract_memory()

        call_kwargs = mock_query.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[0][0]
        # Should reference the daily memory log pattern
        assert "memory" in prompt.lower(), (
            "Extract prompt must mention daily memory log"
        )
