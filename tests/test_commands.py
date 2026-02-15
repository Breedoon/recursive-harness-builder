"""Tests for obs_agent.commands - Step 3 TDD.

Tests the CommandRegistry:
- execute stop sets interrupt flag
- execute enqueue adds message to queue
- execute quit sets interrupt flag
- execute unknown command returns failure
- list_commands returns available commands
"""

import pytest

from obs_agent.commands import CommandRegistry, CommandResult
from obs_agent.hooks import HookState


class TestCommandResult:
    """CommandResult dataclass holds success and message."""

    def test_success_result(self):
        result = CommandResult(success=True, message="ok")
        assert result.success is True
        assert result.message == "ok"

    def test_failure_result(self):
        result = CommandResult(success=False, message="failed")
        assert result.success is False
        assert result.message == "failed"


class TestCommandRegistry:
    """CommandRegistry dispatches commands to handlers."""

    @pytest.mark.asyncio
    async def test_execute_stop(self):
        """Stop command sets interrupt flag."""
        state = HookState()
        registry = CommandRegistry(state)
        result = await registry.execute("stop")
        assert result.success is True
        assert state.interrupt_flag is True

    @pytest.mark.asyncio
    async def test_execute_quit(self):
        """Quit command sets interrupt flag."""
        state = HookState()
        registry = CommandRegistry(state)
        result = await registry.execute("quit")
        assert result.success is True
        assert state.interrupt_flag is True

    @pytest.mark.asyncio
    async def test_execute_enqueue(self):
        """Enqueue command adds message to queue."""
        state = HookState()
        registry = CommandRegistry(state)
        result = await registry.execute("enqueue", message="hello")
        assert result.success is True
        assert state.message_queue.qsize() == 1
        assert state.message_queue.get_nowait() == "hello"

    @pytest.mark.asyncio
    async def test_execute_enqueue_no_message(self):
        """Enqueue without message returns failure."""
        state = HookState()
        registry = CommandRegistry(state)
        result = await registry.execute("enqueue")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_enqueue_empty_message(self):
        """Enqueue with empty message returns failure."""
        state = HookState()
        registry = CommandRegistry(state)
        result = await registry.execute("enqueue", message="")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_unknown_command(self):
        """Unknown command returns failure."""
        state = HookState()
        registry = CommandRegistry(state)
        result = await registry.execute("nonexistent")
        assert result.success is False
        assert "Unknown command" in result.message

    def test_list_commands(self):
        """list_commands returns all registered commands."""
        state = HookState()
        registry = CommandRegistry(state)
        commands = registry.list_commands()
        names = [c["name"] for c in commands]
        assert "stop" in names
        assert "quit" in names
        assert "enqueue" in names

    @pytest.mark.asyncio
    async def test_multiple_enqueues(self):
        """Multiple enqueues accumulate in the queue."""
        state = HookState()
        registry = CommandRegistry(state)
        await registry.execute("enqueue", message="first")
        await registry.execute("enqueue", message="second")
        await registry.execute("enqueue", message="third")
        assert state.message_queue.qsize() == 3
