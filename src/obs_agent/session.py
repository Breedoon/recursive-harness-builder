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
from typing import TYPE_CHECKING, Any

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
    # Disable tool search discovery. Uses eager loading of available tools instead.
    # Avoids the nested tool_reference representation bug and reduces startup overhead.
    "ENABLE_TOOL_SEARCH": "false",
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


def _raise_cli_oom_score(client: Any) -> None:
    """Make a Claude CLI child the kernel's preferred OOM victim (best effort).

    On 2026-09-24/25 three container OOMs each killed the cache proxy (the
    largest process), which restarted the whole daemon (vault-u3b.73). Raising
    the CLIs' ``oom_score_adj`` (allowed without privileges) makes the kernel
    kill one CLI instead; its turn then reconnects and resumes on its own.
    ``OBS_CLI_OOM_SCORE_ADJ=0`` turns this off.
    """
    raw = (os.environ.get("OBS_CLI_OOM_SCORE_ADJ") or "").strip()
    try:
        value = int(raw) if raw else 300
    except ValueError:
        value = 300
    if value <= 0:
        return
    process = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(process, "pid", None)
    if not pid:
        return
    try:
        with open(f"/proc/{int(pid)}/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write(str(min(value, 1000)))
    except OSError:
        logger.debug("Could not raise oom_score_adj for Claude CLI pid=%s", pid, exc_info=True)


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


def _is_cache_proxy_url(url: str, port: int) -> bool:
    """Return whether *url* points at the local cache-normalizing proxy."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url).strip())
        return parts.hostname in {"127.0.0.1", "localhost", "::1"} and parts.port == int(port)
    except (TypeError, ValueError):
        return False


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
        self.effort_override: str | None = None
        # Per-session user hooks.  Mapping of hook event name to
        # ``"file_path::function_name"`` spec.  Threaded to
        # ``create_hook_matchers`` at session creation time.
        self.user_hooks: dict[str, str] | None = None
        # Explicit per-session env overrides supplied at launch (AgentTask
        # ``env`` plus launch-derived keys such as temperature), kept apart from
        # the team-identity keys so daemon restore and task resume can re-apply
        # them after rebuilding the team env (vault-u3b.20).
        self.explicit_env_overrides: dict[str, str] | None = None
        # Set when an OBS_COMPACT_POLICY=handoff PreCompact interception fired
        # for the current session id: every later CLI process for this session
        # runs with auto-compaction disabled so the handoff turn (and any later
        # turn) cannot be compacted. Cleared when the session id changes.
        self._compaction_handoff_session_id: str | None = None

    @property
    def compaction_handoff_active(self) -> bool:
        return (
            self._compaction_handoff_session_id is not None
            and self._compaction_handoff_session_id == self._session_id
        )

    def activate_compaction_handoff(self) -> None:
        """Disable auto-compaction for every later CLI process of this session."""
        self._compaction_handoff_session_id = self._session_id

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
        return self._sdk_env_overrides

    @property
    def effective_model(self) -> str:
        """Return the requested OBS model/budget, before CLI capacity translation."""
        return self.model_override or self.config.model

    @property
    def effective_effort(self) -> str:
        from obs_agent.effort import resolve_effort

        return resolve_effort(
            self.effective_model,
            override=self.effort_override,
            configured=self.config.effort_level,
            model_defaults=self.config.model_effort_levels,
            environ=os.environ,
            session_env=self._sdk_env_overrides,
        )

    async def set_effort(self, effort: str) -> str:
        """Reconfigure an idle session without clearing its conversation ID.

        The transport must serialize this with turn admission and reject active
        or queued work. Reconnection is lazy, on the next message.
        """
        from obs_agent.effort import build_effort_env, normalize_effort, resolve_effort

        selection = normalize_effort(effort)
        effective = resolve_effort(
            self.effective_model, override=selection,
            model_defaults=self.config.model_effort_levels,
        )
        build_effort_env(
            self.effective_model, effective, {**os.environ, **self._sdk_env_overrides}
        )  # Validate the merged body before disconnecting or changing state.
        await self.disconnect()
        self.effort_override = selection
        return effective

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
        from obs_agent.cache_proxy_lifecycle import should_use_proxy
        from obs_agent.claude_context import (
            COMPACTION_DISABLE_KEYS,
            CONTEXT_PLAN_ENV_KEYS,
            apply_explicit_context_overrides,
            build_claude_context_plan,
            explicit_compaction_disabled,
            validation_environment,
        )
        from obs_agent.config import is_claude_model, resolve_model_context
        from obs_agent.hooks import COMPACT_POLICY_HANDOFF, effective_compact_policy

        resolved_model = resolve_model_context(self.effective_model)
        clean_model = resolved_model.model
        context_tokens = resolved_model.context_tokens
        requested_model = resolved_model.model_with_context
        is_local_provider = clean_model.lower().startswith("local-")
        explicit_env = self._sdk_env_overrides
        effective_env = {
            **_DEFAULT_SDK_ENV,
            **explicit_env,
        }
        proxy_in_use = should_use_proxy(cache_proxy_enabled=self.config.cache_proxy_enabled)

        # Local models whose requests reach the upstream gate directly (proxy
        # disabled, or an explicit per-session ANTHROPIC_BASE_URL): the gate
        # routes on the model id literally. Claude Code 2.1.59 itself strips a
        # trailing [1m] before sending (wire model = bare id, plus the
        # context-1m beta header, which the gate accepts), but sends [200k]
        # verbatim. So the window-derived [1m] plan is safe on the direct path;
        # only a [200k] selector is replaced by the bare id, whose CLI capacity
        # is the same 200K, so the plan's percentage still holds.
        explicit_base_url = explicit_env.get("ANTHROPIC_BASE_URL")
        if explicit_base_url is not None:
            local_direct = is_local_provider and not _is_cache_proxy_url(
                explicit_base_url, self.config.cache_proxy_port
            )
        else:
            local_direct = is_local_provider and not proxy_in_use

        if is_local_provider:
            local_base_url = os.environ.get("OBS_LOCAL_LLM_BASE_URL", "").strip()
            local_auth_token = os.environ.get("OBS_LOCAL_LLM_AUTH_TOKEN", "").strip()
            local_api_key = os.environ.get("OBS_LOCAL_LLM_API_KEY", "").strip()
            # Local traffic goes through the cache proxy like everything else;
            # the proxy routes local-* on to this same gate. Pointing the session
            # straight at the gate here would pre-empt the proxy branch below and
            # cost local sessions every cache normalization. Keep the direct path
            # as the fallback for when the proxy is disabled or unhealthy.
            if (
                local_base_url
                and "ANTHROPIC_BASE_URL" not in effective_env
                and not proxy_in_use
            ):
                effective_env["ANTHROPIC_BASE_URL"] = local_base_url
            if not any(key in effective_env for key in _ANTHROPIC_AUTH_ENV_KEYS):
                if local_auth_token:
                    effective_env["ANTHROPIC_AUTH_TOKEN"] = local_auth_token
                elif local_api_key:
                    effective_env["ANTHROPIC_API_KEY"] = local_api_key

        # One window-derived policy for every model, local included (see
        # docs/context-compaction.md "Local models" and "Provider input ceiling").
        # target = min(budget - 33K, provider window - CLI max_output - 13K):
        # the provider window comes from MODEL_CONTEXT_WINDOWS/default for the
        # clean model, the budget from the suffix; no per-model constants.
        #
        # vault-u3b.64 root cause B: an explicit DISABLE_AUTO_COMPACT together
        # with OBS_COMPACT_POLICY=handoff means "never summarize, hand off
        # instead". Disabling native compaction outright left sessions without
        # a context guard (tiers, assessment forks) running into the provider's
        # hard limit. So in that combination OBS keeps native compaction armed
        # at the end of the usable window ("wall") and lets the PreCompact
        # handoff intercept it: still no summary, but a handoff instead of an
        # HTTP 500. An explicit disable without the handoff policy is honoured.
        # Local models default to the handoff policy unless the explicit env
        # sets OBS_COMPACT_POLICY (which always wins, incl. an opt-out value).
        handoff_policy = (
            effective_compact_policy(explicit_env, clean_model) == COMPACT_POLICY_HANDOFF
        )
        explicit_disable = explicit_compaction_disabled(explicit_env)
        wall = explicit_disable and handoff_policy and not self.compaction_handoff_active
        auto_compact_disabled = (
            (explicit_disable and not wall) or self.compaction_handoff_active
        )
        provider_window = resolve_model_context(clean_model).context_tokens
        context_plan = build_claude_context_plan(
            model=clean_model,
            context_tokens=context_tokens,
            auto_compact_window_tokens=self.config.auto_compact_window_tokens,
            environ=validation_environment(os.environ, effective_env, explicit_env),
            provider_window_tokens=provider_window,
            wall=wall,
        )
        context_env = dict(context_plan.environment)
        # OBS metadata always carries the real requested budget.
        context_env["OBS_CONTEXT_WINDOW_ESTIMATE_TOKENS"] = str(context_tokens)
        context_env = apply_explicit_context_overrides(
            context_env,
            explicit_env,
            auto_compact_disabled=auto_compact_disabled,
        )
        if wall:
            # Explicit env reaches the CLI through effective_env; neutralize the
            # disable keys at both layers so native compaction stays armed.
            for name in COMPACTION_DISABLE_KEYS:
                if name in explicit_env:
                    context_env[name] = "0"
        effective_env.update(context_env)
        cli_model = context_plan.cli_model
        if local_direct and not cli_model.lower().endswith("[1m]"):
            cli_model = clean_model
        logger.info(
            "Claude context policy model=%s cli_model=%s context=%s "
            "compact_window=%s target=%s output_reserve=%s auto_compact_disabled=%s "
            "explicit_keys=%s provider_window=%s max_output=%s handoff_wall=%s",
            requested_model,
            cli_model,
            context_tokens,
            context_plan.cli_compact_window_tokens,
            context_plan.threshold_tokens,
            context_plan.output_reserve_tokens,
            auto_compact_disabled,
            sorted(
                k for k in explicit_env
                if k in CONTEXT_PLAN_ENV_KEYS or k in COMPACTION_DISABLE_KEYS
            ),
            context_plan.provider_window_tokens,
            context_plan.max_output_tokens,
            wall,
        )

        from obs_agent.effort import build_effort_env

        effort_env = build_effort_env(
            clean_model, self.effective_effort, {**os.environ, **effective_env}
        )
        effective_env.update(effort_env)

        hook_matchers = create_hook_matchers(
            self.config,
            self.hook_state,
            user_hooks=self.user_hooks,
        )

        # Create MCP tool server with session_id getter closure and hook_state
        # for background fork result delivery
        tool_server = create_obs_tools(
            self.config,
            lambda: self._session_id,
            hook_state=self.hook_state,
        )

        # Persist/report the requested OBS budget, NOT the CLI capacity selector.
        # Otherwise [400k] would become [1m] when this session spawns children.
        self.hook_state.effective_model = requested_model
        self.hook_state.sdk_env_overrides = dict(self._sdk_env_overrides)
        self.hook_state.vault_path = self.config.vault_path

        # For non-Claude hosted models, set the API key to the CLI proxy key by
        # default. Explicit per-session and local-provider credentials take precedence.
        if is_claude_model(clean_model):
            for key in _ANTHROPIC_AUTH_ENV_KEYS:
                effective_env.pop(key, None)
        elif (
            not is_local_provider
            and not any(key in effective_env for key in _ANTHROPIC_AUTH_ENV_KEYS)
        ):
            effective_env["ANTHROPIC_API_KEY"] = self.config.cli_proxy_api_key

        # Route ALL CC API traffic through the cache-normalizing proxy by
        # default, including local-* models; the proxy then forwards each
        # request to the upstream its model selects. An explicit per-session
        # ANTHROPIC_BASE_URL still takes precedence, so one child can select
        # its provider without changing the parent.
        from obs_agent.cache_proxy_lifecycle import should_use_proxy
        if (
            should_use_proxy(cache_proxy_enabled=self.config.cache_proxy_enabled)
            and "ANTHROPIC_BASE_URL" not in effective_env
        ):
            effective_env["ANTHROPIC_BASE_URL"] = (
                f"http://127.0.0.1:{self.config.cache_proxy_port}"
            )

        options = ClaudeAgentOptions(
            model=cli_model,
            hooks=hook_matchers,
            mcp_servers={"obs-agent": tool_server},
            cwd=str(self.config.vault_path),
            permission_mode="bypassPermissions",
            setting_sources=["project"],
            # Project settings can also contain env overrides. Supply the budget
            # controls at the CLI-settings boundary without replacing unrelated
            # project settings or changing the persisted OBS model identity.
            settings=json.dumps({"env": {**context_env, **effort_env}}),
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
            _raise_cli_oom_score(client)
            # The PreCompact handoff policy (hooks._make_pre_compact_callback)
            # interrupts the CLI process that is about to compact.
            self.hook_state.client_interrupter = client.interrupt
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
        self.hook_state.client_interrupter = None
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
        self.hook_state.client_interrupter = None
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
        self.hook_state.client_interrupter = None

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
