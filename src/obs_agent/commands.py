"""Command registry for OBS Agent.

Maps command names to handler functions. Both CLI (via HTTP endpoints)
and the agent (via hooks/tools) use the same handlers through the registry.

See plan: command registry + daemon endpoints (Step 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from obs_agent.hooks import HookState


@dataclass
class CommandResult:
    """Result of executing a command."""

    success: bool
    message: str


class CommandRegistry:
    """Maps command names to handler functions.

    Both CLI (via HTTP) and agent (via hooks/tools) use the same handlers.
    """

    def __init__(self, hook_state: HookState) -> None:
        self._state = hook_state
        self._commands: dict[str, str] = {
            "stop": "Interrupt the agent at the next tool boundary",
            "quit": "End the session and exit",
            "enqueue": "Queue a message for injection at next hook",
        }

    async def execute(self, name: str, **kwargs: Any) -> CommandResult:
        """Dispatch a command by name."""
        if name == "stop":
            return await self._handle_stop()
        elif name == "quit":
            return await self._handle_quit()
        elif name == "enqueue":
            return await self._handle_enqueue(**kwargs)
        else:
            return CommandResult(success=False, message=f"Unknown command: {name}")

    def list_commands(self) -> list[dict[str, str]]:
        """Return list of available commands with descriptions."""
        return [
            {"name": name, "description": desc}
            for name, desc in self._commands.items()
        ]

    async def _handle_stop(self) -> CommandResult:
        """Set the interrupt flag to stop the agent at next tool boundary."""
        self._state.interrupt_flag = True
        return CommandResult(success=True, message="Interrupt signal sent")

    async def _handle_quit(self) -> CommandResult:
        """Signal a session quit."""
        self._state.interrupt_flag = True
        return CommandResult(success=True, message="Quit signal sent")

    async def _handle_enqueue(self, **kwargs: Any) -> CommandResult:
        """Queue a message for injection at the next hook boundary."""
        message = kwargs.get("message")
        if not message:
            return CommandResult(success=False, message="Message is required")

        self._state.message_queue.put_nowait(message)
        return CommandResult(
            success=True,
            message=f"Message queued (queue size: {self._state.message_queue.qsize()})",
        )
