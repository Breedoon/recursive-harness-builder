"""Session lifecycle management.

Tracks session_id, decides resume vs fresh start based on cache window,
and builds ClaudeAgentOptions integrating hooks and system prompt.

See decisions D014 (SDK cache for continuity) and D022 (no compaction).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions

from obs_agent.prompt import build_system_prompt

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


class SessionManager:
    """Manages agent session lifecycle - start, resume, offboard."""

    def __init__(self, *, config: OBSConfig) -> None:
        self.config = config
        self._session_id: str | None = None
        self.last_activity: float | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def set_session_id(self, session_id: str) -> None:
        """Store the session ID from SDK init and record activity."""
        self._session_id = session_id
        self.last_activity = time.time()

    def touch(self) -> None:
        """Update last_activity to current time."""
        self.last_activity = time.time()

    def should_resume(self) -> bool:
        """Decide whether to resume the existing session.

        Returns True only if we have a session_id AND last activity
        is strictly within the cache window. Conservative: exactly at
        the boundary returns False.
        """
        if self._session_id is None or self.last_activity is None:
            return False

        elapsed = time.time() - self.last_activity
        return elapsed < self.config.cache_window_seconds

    def create_options(self) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with system prompt, hooks, and resume."""
        system_prompt = build_system_prompt(self.config)

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            hooks={
                "PreToolUse": [],
                "Stop": [],
                "PreCompact": [],
            },
        )

        # Resume if within cache window, otherwise fresh
        if self.should_resume():
            options.resume = self._session_id
        else:
            options.resume = None

        return options

    def reset(self) -> None:
        """Reset for a fresh start after memory flush."""
        self._session_id = None
        self.last_activity = None
