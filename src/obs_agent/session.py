"""Session lifecycle management.

Manages a ClaudeSDKClient for interactive multi-turn conversations.
Handles connection lifecycle, cache-window-based reconnection, and
builds ClaudeAgentOptions integrating hooks and project-level settings.

See decisions D014 (SDK cache for continuity) and D022 (no compaction).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from obs_agent._sdk_patch import ensure_raw_uuid_patch
from obs_agent.hooks import HookState, create_hook_matchers
from obs_agent.tools import create_obs_tools

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.session")

ensure_raw_uuid_patch()


class SessionManager:
    """Manages agent session lifecycle via ClaudeSDKClient.

    Owns a single ClaudeSDKClient instance that is reused across turns
    within the cache window. When the cache window expires, the client
    is disconnected and a fresh one is created.
    """

    def __init__(self, *, config: OBSConfig, hook_state: HookState | None = None) -> None:
        self.config = config
        self.hook_state = hook_state if hook_state is not None else HookState()
        self._session_id: str | None = None
        self.last_activity: float | None = None
        self._client: ClaudeSDKClient | None = None
        self._connected: bool = False
        self._lock = asyncio.Lock()

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

    def _build_options(self) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with hooks, MCP tools, and resume."""

        hook_matchers = create_hook_matchers(self.config, self.hook_state)

        # Create MCP tool server with session_id getter closure and hook_state
        # for background fork result delivery
        tool_server = create_obs_tools(self.config, lambda: self._session_id, hook_state=self.hook_state)

        options = ClaudeAgentOptions(
            hooks=hook_matchers,
            mcp_servers={"obs-agent": tool_server},
            cwd=str(self.config.vault_path),
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            max_buffer_size=self.config.max_buffer_size,
        )

        # Resume if within cache window, otherwise fresh
        if self.should_resume():
            options.resume = self._session_id
        else:
            options.resume = None

        return options

    # Keep public alias for backward compatibility (used by tests)
    def create_options(self) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with hooks, project settings, and resume."""
        return self._build_options()

    async def get_client(self) -> ClaudeSDKClient:
        """Get or create a connected ClaudeSDKClient.

        Handles reconnect when cache window expires. Serializes access
        via asyncio.Lock to prevent concurrent client creation.

        IMPORTANT: connect() is run in a detached asyncio.Task so that the
        SDK's internal anyio task group (which runs the background message
        reader) is NOT nested inside the HTTP request handler's task scope.
        Without this, Starlette cancels the reader when the request completes,
        breaking multi-turn conversations.
        """
        async with self._lock:
            # If client exists and connected and within cache window, reuse
            if self._client is not None and self._connected:
                if self.should_resume() or self._session_id is None:
                    return self._client
                # Cache expired — disconnect and create fresh
                logger.info("Cache window expired, reconnecting")
                await self._disconnect_unlocked()

            # Create fresh client
            options = self._build_options()
            self._client = ClaudeSDKClient(options)
            # Run connect() in a detached task so the SDK's internal task
            # group outlives the HTTP request that triggered the connection.
            await asyncio.create_task(self._client.connect())
            self._connected = True
            return self._client

    async def disconnect(self) -> None:
        """Disconnect current client (for daemon shutdown or reconnect)."""
        async with self._lock:
            await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        """Disconnect without acquiring lock (called from within locked context)."""
        if self._client is not None and self._connected:
            try:
                await self._client.disconnect()
            except Exception:
                logger.debug("Error during client disconnect", exc_info=True)
            self._connected = False
        self._client = None

    def reset(self) -> None:
        """Reset for a fresh start after memory flush."""
        self._session_id = None
        self.last_activity = None
        # Mark client as stale — next get_client() will create fresh
        self._connected = False
        self._client = None

    async def reconnect(self) -> ClaudeSDKClient:
        """Reconnect to an existing session after a mid-stream error.

        Preserves session_id so the CLI subprocess's conversation history
        is retained, but creates a fresh Python-side client with
        ``resume=session_id``.

        Raises ``RuntimeError`` if no session_id exists to reconnect to.
        """
        if self._session_id is None:
            raise RuntimeError("Cannot reconnect: no session_id")

        async with self._lock:
            await self._disconnect_unlocked()
            # Ensure should_resume() returns True for the new options build.
            self.last_activity = time.time()
            options = self._build_options()
            self._client = ClaudeSDKClient(options)
            await asyncio.create_task(self._client.connect())
            self._connected = True
            return self._client

    async def soft_reset(self) -> None:
        """Disconnect client but preserve session_id for future reconnect.

        Used after recoverable errors where the next user message should
        silently reconnect to the same conversation.
        """
        await self.disconnect()
        # NOTE: session_id and last_activity are intentionally NOT cleared.

    async def async_reset(self) -> None:
        """Async reset that also disconnects the client cleanly."""
        await self.disconnect()
        self._session_id = None
        self.last_activity = None
