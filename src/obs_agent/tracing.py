"""Optional W&B Weave tracing for obs_agent.

All weave imports are isolated in this module.
Set WEAVE_PROJECT=entity/project-name to enable.
"""
from __future__ import annotations

import functools
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

logger = logging.getLogger("obs_agent.tracing")

_enabled: bool = False
_initialized: bool = False
_weave: Any = None
_identity_cache: dict[str, dict] = {}
_streaming_turn_op: Any = None


def init_weave(project: str | None = None) -> None:
    """Initialize Weave tracing. Idempotent. No-op if WEAVE_PROJECT is unset."""
    global _enabled, _initialized, _weave
    if _initialized:
        return
    _initialized = True
    project = project or os.environ.get("WEAVE_PROJECT", "").strip()
    if not project:
        return
    try:
        import weave as _w
        _weave = _w
        _w.init(project)
        _enabled = True
        logger.info("Weave tracing enabled: %s", project)
    except ImportError:
        logger.debug("weave not installed; tracing disabled")
    except Exception:
        logger.warning("weave.init failed; tracing disabled", exc_info=True)


def is_enabled() -> bool:
    """Return whether Weave tracing is active."""
    return _enabled


def traced_op(fn: Callable) -> Callable:
    """Wrap fn with @weave.op() on first call when enabled; no-op otherwise.

    Safe to apply at import time — the actual weave wrapping is deferred until
    init_weave() has run, so decoration order vs init order doesn't matter.
    """
    _wrapped: list[Any] = []  # one-element list used as a mutable cell

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _enabled or _weave is None:
            return await fn(*args, **kwargs)
        if not _wrapped:
            _wrapped.append(_weave.op()(fn))
        return await _wrapped[0](*args, **kwargs)

    return wrapper


@asynccontextmanager
async def weave_attributes(attrs: dict) -> AsyncIterator[None]:
    """Async context manager: attach attrs to the current Weave call scope."""
    if not _enabled or _weave is None or not attrs:
        yield
        return
    try:
        with _weave.attributes(attrs):
            yield
    except Exception:
        logger.debug("weave_attributes failed", exc_info=True)
        yield


def resolve_identity(session_id: str | None, hook_state: Any, config: Any) -> dict:
    """Build obs.* attribute dict from ObsBootstrap identity. Cached per session_id."""
    attrs: dict = {}
    model = getattr(config, "model", None)
    if model:
        attrs["obs.model"] = model
    if not session_id:
        return attrs

    attrs["obs.session_id"] = session_id

    if session_id in _identity_cache:
        return {**_identity_cache[session_id], **attrs}

    try:
        xml = getattr(hook_state, "pending_obs_bootstrap_xml", None)
        bootstrap = None

        if xml:
            from obs_agent.lineage import parse_obs_bootstrap_xml
            bootstrap = parse_obs_bootstrap_xml(xml)

        if bootstrap is None:
            from obs_agent.lineage import find_latest_obs_bootstrap_for_session
            cwd = getattr(config, "vault_path", None)
            if cwd:
                bootstrap = find_latest_obs_bootstrap_for_session(session_id, cwd)

        if bootstrap is not None:
            identity = {
                "obs.agent_name": bootstrap.agent_name,
                "obs.parent_session_id": bootstrap.parent_session_id,
                "obs.lineage": list(bootstrap.lineage),
                "obs.root_team_key": bootstrap.root_team_key,
                "obs.is_fork": bootstrap.is_fork,
            }
            _identity_cache[session_id] = identity
            attrs.update(identity)
    except Exception:
        logger.debug("resolve_identity failed", exc_info=True)

    return attrs


def _get_streaming_turn_op() -> Any:
    """Return the @weave.op() decorated function for logging streaming turns. Lazy init."""
    global _streaming_turn_op
    if _streaming_turn_op is not None:
        return _streaming_turn_op
    if _weave is None:
        return None
    try:
        @_weave.op(name="obs_agent/conversation_turn")
        async def _fn(
            message: str,
            session_id: str | None,
            model: str | None,
            response: str,
            tool_uses: list,
            cost_usd: float | None,
            duration_ms: float | None,
            num_turns: int | None,
            input_tokens: int | None,
            output_tokens: int | None,
            cache_creation_tokens: int | None,
            cache_read_tokens: int | None,
        ) -> dict:
            return {
                "response": response,
                "tool_uses": tool_uses,
                "cost_usd": cost_usd,
                "duration_ms": duration_ms,
                "num_turns": num_turns,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "cache_read_tokens": cache_read_tokens,
            }
        _streaming_turn_op = _fn
    except Exception:
        logger.debug("_get_streaming_turn_op failed", exc_info=True)
    return _streaming_turn_op


class TurnTracer:
    """Accumulates events during a /chat/stream turn and logs to Weave on exit.

    Fork spans from ForkRunner.run() won't be nested here (turn span opens
    post-hoc). Use the _drive_turn closure pattern in /chat for nested spans.
    """

    def __init__(self, message: str, session_mgr: Any, hook_state: Any, config: Any) -> None:
        self._message = message
        self._session_mgr = session_mgr
        self._hook_state = hook_state
        self._config = config
        self._text_parts: list[str] = []
        self._tool_uses: list[str] = []

    async def __aenter__(self) -> "TurnTracer":
        return self

    def record_event(self, event: Any) -> None:
        """Call for each RunnerEvent during the turn."""
        try:
            from obs_agent.runner import TextEvent
            from obs_agent.events import StatusEvent
            if isinstance(event, TextEvent):
                self._text_parts.append(event.text)
            elif isinstance(event, StatusEvent) and event.type == "tool_use":
                self._tool_uses.append(event.summary)
        except Exception:
            pass

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if _enabled:
            try:
                await self._log()
            except Exception:
                logger.debug("TurnTracer._log failed", exc_info=True)
        return False

    async def _log(self) -> None:
        op = _get_streaming_turn_op()
        if op is None:
            return
        last = self._hook_state.last_result_data or {}
        usage = last.get("usage") or {}
        session_id = self._session_mgr.session_id
        identity = resolve_identity(session_id, self._hook_state, self._config)
        with _weave.attributes(identity):
            await op(
                message=self._message,
                session_id=session_id,
                model=getattr(self._config, "model", None),
                response="".join(self._text_parts),
                tool_uses=list(self._tool_uses),
                cost_usd=last.get("total_cost_usd"),
                duration_ms=last.get("duration_ms"),
                num_turns=last.get("num_turns"),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_creation_tokens=usage.get("cache_creation_input_tokens"),
                cache_read_tokens=usage.get("cache_read_input_tokens"),
            )
