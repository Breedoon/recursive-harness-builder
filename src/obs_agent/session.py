"""Session lifecycle management.

Manages a ClaudeSDKClient for interactive multi-turn conversations.
Handles connection lifecycle, cache-window-based reconnection, and
builds ClaudeAgentOptions integrating hooks and project-level settings.

See decisions D014 (SDK cache for continuity) and D022 (no compaction).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import logging
import os
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from obs_agent._sdk_patch import ensure_raw_uuid_patch
from obs_agent.hooks import HookState, create_hook_matchers
from obs_agent.prompt import (
    ENTRY_FILE_SENTINEL,
    build_entry_file_context_message,
)
from obs_agent.tools import create_obs_tools

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.session")

ensure_raw_uuid_patch()

_ANTHROPIC_AUTH_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


_DEFAULT_SDK_ENV: dict[str, str] = {
    # Disable background tasks (skill auto-improvement, magic docs, plugin autoupdate).
    # The skill_improvement_apply feature crashes headless SDK sessions.
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    # Suppress git status from the system prompt and git instructions from
    # Bash tool description.  Git status is memoized per-CLI-process; when the
    # worktree changes mid-session (agent edits files), forks recompute it and
    # get a different dynamic system prompt → cache miss on the entire prefix
    # past the ~48K static portion.  Spike-verified: 53% → 92% fork cache hit.
    # Git commands (status, commit, log) via Bash still work normally.
    # See Drafts/2026-04/cache-analysis/ and CC utils/gitSettings.ts:13-18.
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    # NOTE: CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC was here until 2026-04-04.
    # Removed because it disables GrowthBook, which gates 1h prompt cache TTL.
    # See Drafts/2026-04/cache-analysis/query-source-investigation.md
}
def _on_cli_stderr(line: str) -> None:
    """Capture stderr output from the Claude Code CLI subprocess.

    Without this callback, stderr goes directly to the terminal (inherited
    from the daemon process).  Routing it through the logger makes errors
    visible in structured logs and prevents terminal noise.
    """
    logger.warning("CLI stderr: %s", line.rstrip())


@contextmanager
def _scrub_process_env(keys: tuple[str, ...]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_CLIENT_CONNECT_MAX_ATTEMPTS = 3
_CLIENT_CONNECT_RETRY_DELAY_SECONDS = 1.0
_CLIENT_CONNECT_ENV_LOCK = asyncio.Lock()


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
        self._sdk_env_overrides: dict[str, str] = {}
        self._entry_file_context_pending: bool = False
        # Per-session model override.  When set, takes precedence over
        # ``self.config.model`` in ``_build_options``.  Used by AgentTask to
        # give child sessions a different model without mutating the shared
        # OBSConfig instance.
        self.model_override: str | None = None
        # Per-session user hooks.  Mapping of hook event name to
        # ``"file_path::function_name"`` spec.  Threaded to
        # ``create_hook_matchers`` at session creation time.
        self.user_hooks: dict[str, str] | None = None

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

    def set_sdk_env_overrides(self, env: dict[str, str] | None) -> None:
        """Set per-session SDK env overrides for newly created clients.

        Existing connected clients are not reconfigured in-place; callers that
        need immediate effect should reconnect/reset the session.
        """
        self._sdk_env_overrides = {
            str(key): str(value)
            for key, value in (env or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self.hook_state.sdk_env_overrides = dict(self._sdk_env_overrides)
        self.hook_state.vault_path = self.config.vault_path

    @property
    def sdk_env_overrides(self) -> dict[str, str]:
        """Expose the current per-session SDK env override map."""
        return dict(self._sdk_env_overrides)

    @property
    def effective_model(self) -> str:
        """Return the model this session will pass to ClaudeAgentOptions."""
        return self.model_override or self.config.model

    def _jsonl_has_entry_file_context(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        try:
            from obs_agent.context_jsonl import find_session_jsonl

            path = find_session_jsonl(session_id=session_id, cwd=self.config.vault_path)
            if path is None:
                return False
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if ENTRY_FILE_SENTINEL not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "user":
                        continue
                    message = obj.get("message")
                    if isinstance(message, dict) and ENTRY_FILE_SENTINEL in json.dumps(
                        message.get("content", ""),
                        ensure_ascii=False,
                    ):
                        return True
        except Exception:
            logger.debug(
                "Unable to inspect JSONL for entry-file context session_id=%s",
                session_id,
                exc_info=True,
            )
        return False

    def _should_inject_entry_file_context(self, resume_session_id: str | None) -> bool:
        if resume_session_id is None:
            return True
        return not self._jsonl_has_entry_file_context(resume_session_id)

    def latest_jsonl_api_error_text(self) -> str | None:
        """Return the latest synthetic API-error text from the current JSONL tail."""
        if not self._session_id:
            return None
        try:
            from obs_agent.context_jsonl import find_session_jsonl

            path = find_session_jsonl(
                session_id=self._session_id,
                cwd=self.config.vault_path,
            )
            if path is None:
                return None
            with path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
            for line in reversed(lines):
                if "isApiErrorMessage" not in line and '"error"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                if not obj.get("isApiErrorMessage") and not obj.get("error"):
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    texts: list[str] = []
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "text"
                            and isinstance(block.get("text"), str)
                        ):
                            text = block["text"].strip()
                            if text:
                                texts.append(text)
                    if texts:
                        return "\n".join(texts)
                error = obj.get("error") or message.get("error")
                if isinstance(error, str) and error.strip():
                    return error.strip()
        except Exception:
            logger.debug(
                "Unable to inspect JSONL API error tail session_id=%s",
                self._session_id,
                exc_info=True,
            )
        return None

    def prepare_user_message(self, message: str) -> str:
        """Prepend persisted entry-file context once for the current JSONL.

        Claude Code's own project-context reminder is request-only and is
        stripped by the cache proxy.  OBS injects the same project context into
        the first user turn so it is stored in JSONL and inherited verbatim by
        forks.
        """
        if not self._entry_file_context_pending:
            return message
        self._entry_file_context_pending = False
        if ENTRY_FILE_SENTINEL in message:
            return message
        return f"{build_entry_file_context_message(self.config)}\n\n{message}"

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
        from obs_agent.config import (
            auto_compact_window_for_model,
            is_claude_model,
            resolve_model_context,
        )

        hook_matchers = create_hook_matchers(self.config, self.hook_state, user_hooks=self.user_hooks)

        # Create MCP tool server with session_id getter closure and hook_state
        # for background fork result delivery
        tool_server = create_obs_tools(self.config, lambda: self._session_id, hook_state=self.hook_state)

        resolved_model = resolve_model_context(self.effective_model)
        effective_model = resolved_model.model_for_claude_code
        ctx_tokens = resolved_model.context_tokens
        self.hook_state.effective_model = resolved_model.model_with_context

        effective_env = {
            **_DEFAULT_SDK_ENV,
            **self._sdk_env_overrides,
        }
        if resolved_model.model.lower().startswith("local-"):
            local_base_url = os.environ.get("OBS_LOCAL_LLM_BASE_URL", "").strip()
            local_auth_token = os.environ.get("OBS_LOCAL_LLM_AUTH_TOKEN", "").strip()
            local_api_key = os.environ.get("OBS_LOCAL_LLM_API_KEY", "").strip()
            if local_base_url and "ANTHROPIC_BASE_URL" not in effective_env:
                effective_env["ANTHROPIC_BASE_URL"] = local_base_url
            if not any(key in effective_env for key in _ANTHROPIC_AUTH_ENV_KEYS):
                if local_auth_token:
                    effective_env["ANTHROPIC_AUTH_TOKEN"] = local_auth_token
                elif local_api_key:
                    effective_env["ANTHROPIC_API_KEY"] = local_api_key
        self.hook_state.sdk_env_overrides = dict(self._sdk_env_overrides)
        self.hook_state.vault_path = self.config.vault_path

        auto_compact_window = auto_compact_window_for_model(
            effective_model,
            ctx_tokens,
            auto_compact_window_tokens=self.config.auto_compact_window_tokens,
        )
        effective_env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] = str(ctx_tokens)
        effective_env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(auto_compact_window)
        effective_env.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)
        # For non-Claude models, set the API key to the CLI proxy key by
        # default. Explicit per-session credentials and the local-provider
        # process profile take precedence.
        if is_claude_model(effective_model):
            for key in _ANTHROPIC_AUTH_ENV_KEYS:
                effective_env.pop(key, None)
        elif not any(key in effective_env for key in _ANTHROPIC_AUTH_ENV_KEYS):
            effective_env["ANTHROPIC_API_KEY"] = self.config.cli_proxy_api_key

        # Route CC API traffic through the cache-normalizing proxy by default.
        # Explicit per-session and local-provider profile URLs take precedence
        # so one child can select its provider without changing the parent.
        from obs_agent.cache_proxy_lifecycle import should_use_proxy
        if (
            should_use_proxy(cache_proxy_enabled=self.config.cache_proxy_enabled)
            and "ANTHROPIC_BASE_URL" not in effective_env
        ):
            effective_env["ANTHROPIC_BASE_URL"] = (
                f"http://127.0.0.1:{self.config.cache_proxy_port}"
            )

        options = ClaudeAgentOptions(
            model=effective_model,
            hooks=hook_matchers,
            mcp_servers={"obs-agent": tool_server},
            cwd=str(self.config.vault_path),
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            env=effective_env,
            max_buffer_size=self.config.max_buffer_size,
            stderr=_on_cli_stderr,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
            },
        )

        # Resume if within cache window, otherwise fresh
        if self.should_resume():
            options.resume = self._session_id
        else:
            options.resume = None

        return options

    async def _connect_client_with_retry(
        self,
        *,
        options: ClaudeAgentOptions,
    ) -> ClaudeSDKClient:
        """Create and connect a new SDK client with bounded retries."""
        from obs_agent.config import is_claude_model

        scrub_auth_env = is_claude_model(options.model)
        last_error: Exception | None = None
        for attempt in range(1, _CLIENT_CONNECT_MAX_ATTEMPTS + 1):
            client = ClaudeSDKClient(options)
            try:
                async with _CLIENT_CONNECT_ENV_LOCK:
                    if scrub_auth_env:
                        with _scrub_process_env(_ANTHROPIC_AUTH_ENV_KEYS):
                            await asyncio.create_task(client.connect())
                    else:
                        await asyncio.create_task(client.connect())
            except Exception as exc:
                last_error = exc
                try:
                    await client.disconnect()
                except Exception:
                    logger.debug("Error during failed client cleanup", exc_info=True)
                if attempt >= _CLIENT_CONNECT_MAX_ATTEMPTS:
                    break
                logger.warning(
                    "ClaudeSDKClient.connect failed attempt=%s/%s; retrying",
                    attempt,
                    _CLIENT_CONNECT_MAX_ATTEMPTS,
                    exc_info=True,
                )
                await asyncio.sleep(_CLIENT_CONNECT_RETRY_DELAY_SECONDS)
                continue
            self._client = client
            self._connected = True
            self._entry_file_context_pending = self._should_inject_entry_file_context(
                options.resume
            )
            return client

        self._client = None
        self._connected = False
        assert last_error is not None
        raise last_error

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
            return await self._connect_client_with_retry(options=options)

    def has_connected_client(self) -> bool:
        """Return whether this manager currently owns a connected SDK client."""
        return self._client is not None and self._connected

    async def disconnect_idle_client(self, *, direct_kill: bool = False) -> bool:
        """Disconnect an idle client while preserving session resume state.

        Returns True when a connected client reference existed and was cleared.
        """
        async with self._lock:
            had_client = self._client is not None and self._connected
            if direct_kill:
                await self._direct_kill_client_process_unlocked()
            else:
                await self._disconnect_unlocked()
            return had_client

    async def disconnect(self) -> None:
        """Disconnect current client (for daemon shutdown or reconnect)."""
        async with self._lock:
            await self._disconnect_unlocked()

    async def _direct_kill_client_process_unlocked(self) -> None:
        """Best-effort direct teardown of the owned Claude CLI subprocess."""
        client = self._client
        process = getattr(getattr(client, "_transport", None), "_process", None)
        direct_kill_attempted = False
        if process is not None and getattr(process, "returncode", None) is None:
            direct_kill_attempted = True
            try:
                process.kill()
                wait = getattr(process, "wait", None)
                if wait is not None:
                    await asyncio.wait_for(wait(), timeout=2.0)
            except Exception:
                logger.debug("Error during direct Claude process kill", exc_info=True)

        if direct_kill_attempted and client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=2.0)
            except Exception:
                logger.debug("Error during post-kill client disconnect", exc_info=True)
            self._connected = False
            self._client = None
            return

        await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        """Disconnect without acquiring lock (called from within locked context)."""
        if self._client is not None:
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
            return await self._connect_client_with_retry(options=options)

    async def recover_poisoned_session_if_needed(self) -> tuple[str, str, str] | None:
        """Fork away from a synthetic API-error JSONL tail before resuming.

        Returns ``(old_session_id, new_session_id, recovery_uuid)`` when a new
        session was created. The original JSONL is left untouched.
        """
        if not self._session_id:
            return None

        from obs_agent.jsonl_fork import fork_session_jsonl
        from obs_agent.jsonl_health import resolve_safe_jsonl_target

        target = resolve_safe_jsonl_target(
            session_id=self._session_id,
            cwd=self.config.vault_path,
            preferred_uuid=None,
        )
        if target is None or not target.health.needs_recovery or not target.target_uuid:
            return None

        async with self._lock:
            old_session_id = self._session_id
            # The session may have changed while waiting on the lock.
            if not old_session_id:
                return None
            target = resolve_safe_jsonl_target(
                session_id=old_session_id,
                cwd=self.config.vault_path,
                preferred_uuid=None,
            )
            if target is None or not target.health.needs_recovery or not target.target_uuid:
                return None
            await self._disconnect_unlocked()
            import uuid

            new_session_id = fork_session_jsonl(
                session_id=old_session_id,
                target_uuid=target.target_uuid,
                cwd=self.config.vault_path,
                new_session_id=str(uuid.uuid4()),
            )
            logger.warning(
                "Recovered poisoned session JSONL old_session_id=%s new_session_id=%s "
                "recovery_uuid=%s reason=%s first_unsafe=%s first_poison=%s",
                old_session_id,
                new_session_id,
                target.target_uuid,
                target.health.unsafe_tail_reason,
                target.health.first_unsafe_tail_uuid,
                target.health.first_poison_uuid,
            )
            self._session_id = new_session_id
            self.last_activity = time.time()
            return old_session_id, new_session_id, target.target_uuid

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
