"""Tests for obs_agent.fork - Step 3 TDD (RED phase).

These tests define the ForkRunner contract. ForkRunner is the generic mechanism
for forking Claude sessions to perform subtasks (classify, search, extract).
All should FAIL until fork.py is implemented.

See implementation-plan.md Step 3 and decision D018 (forking as core primitive).
SDK reference: fork_session=True reuses KV cache for near-zero marginal cost.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.config import OBSConfig
from obs_agent.fork import ForkRunner


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
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(return_value=iter([]))

        await runner.run("Do something")

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.resume == "sess-abc-123"

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_options_set_fork_flag(self, mock_query, runner):
        """run() sets fork_session=True so original session is preserved."""
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(return_value=iter([]))

        await runner.run("Do something")

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.fork_session is True

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_inherits_system_prompt(self, mock_query, runner):
        """Forked session uses the same system prompt for KV cache reuse."""
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(return_value=iter([]))

        await runner.run("Do something", system_prompt="You are an assistant.")

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.system_prompt == "You are an assistant."

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_max_turns(self, mock_query, runner):
        """run() respects the max_turns parameter for safety."""
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(return_value=iter([]))

        await runner.run("Do something", max_turns=3)

        call_kwargs = mock_query.call_args
        options = call_kwargs.kwargs.get("options") or call_kwargs[1].get("options")
        assert options.max_turns == 3

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_fork_passes_prompt(self, mock_query, runner):
        """run() passes the task prompt to the SDK query function."""
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(return_value=iter([]))

        await runner.run("Analyze this conversation")

        call_kwargs = mock_query.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[0][0]
        assert "Analyze this conversation" in prompt


# --- Classify Fork ---


class TestClassifyFork:
    """classify() determines which skills a user message requires."""

    @pytest.fixture
    def runner(self, config):
        return ForkRunner(config=config, session_id="sess-abc-123")

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_classify_returns_skill_names(self, mock_query, runner):
        """classify() returns a list of skill name strings."""
        # Mock the SDK to return a response listing skill names
        mock_msg = MagicMock()
        mock_msg.content = '[{"skill": "daily-planning"}, {"skill": "update-context"}]'
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(
            return_value=iter([mock_msg])
        )

        result = await runner.classify("help me plan my day")
        assert isinstance(result, list)
        # classify should return string skill names
        for name in result:
            assert isinstance(name, str)

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_classify_returns_empty_for_simple(self, mock_query, runner):
        """classify() returns empty list for simple queries needing no skills."""
        mock_msg = MagicMock()
        mock_msg.content = "[]"
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(
            return_value=iter([mock_msg])
        )

        result = await runner.classify("what time is it")
        assert isinstance(result, list)
        # Simple questions should need few or no skills
        assert len(result) == 0

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_classify_prompt_has_skill_manifest(self, mock_query, runner):
        """The classify prompt includes the list of all available skills."""
        mock_msg = MagicMock()
        mock_msg.content = "[]"
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(
            return_value=iter([mock_msg])
        )

        await runner.classify("help me organize my notes")

        call_kwargs = mock_query.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[0][0]
        # The classify prompt must list available skills for the LLM to choose from
        assert "file-conventions" in prompt, (
            "Classify prompt must include skill names from manifest"
        )
        assert "session-offboard" in prompt or "update-context" in prompt, (
            "Classify prompt must list multiple skills"
        )


# --- Search Fork ---


class TestSearchFork:
    """search() queries the vault and returns structured results."""

    @pytest.fixture
    def runner(self, config):
        return ForkRunner(config=config, session_id="sess-abc-123")

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_search_returns_structured_results(self, mock_query, runner):
        """search() returns a dict with a results list."""
        mock_msg = MagicMock()
        mock_msg.content = '{"results": [{"file": "Agent/context.md", "excerpt": "goals"}]}'
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(
            return_value=iter([mock_msg])
        )

        result = await runner.search("goals")
        assert isinstance(result, dict)
        assert "results" in result
        assert isinstance(result["results"], list)

    @pytest.mark.asyncio
    @patch("obs_agent.fork.query")
    async def test_search_results_have_file_paths(self, mock_query, runner):
        """Each search result includes a file path."""
        mock_msg = MagicMock()
        mock_msg.content = '{"results": [{"file": "Agent/context.md", "excerpt": "goals for Q1", "relevance": "directly relevant"}]}'
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(
            return_value=iter([mock_msg])
        )

        result = await runner.search("goals")
        for item in result["results"]:
            assert "file" in item, "Each search result must include a file path"


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
        mock_msg.content = "Memory extraction complete."
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(
            return_value=iter([mock_msg])
        )

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
        """extract_memory prompt instructs writing to Agent/memory/YYYY-MM-DD.md."""
        mock_msg = MagicMock()
        mock_msg.content = "Memory extraction complete."
        mock_query.return_value = AsyncMock()
        mock_query.return_value.__aiter__ = AsyncMock(
            return_value=iter([mock_msg])
        )

        await runner.extract_memory()

        call_kwargs = mock_query.call_args
        prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[0][0]
        # Should reference the daily memory log pattern
        assert "memory" in prompt.lower(), (
            "Extract prompt must mention daily memory log"
        )
