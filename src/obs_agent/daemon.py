"""FastAPI server for OBS Agent daemon.

HTTP API with SSE streaming, using ConversationRunner for the core loop.
Integrates session manager and hooks.
Listens on localhost:7832 by default.

See implementation-plan.md Steps 9-10 and decisions D014, D018, D022.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from obs_agent.commands import CommandRegistry
from obs_agent.events import StatusEvent
from obs_agent.hooks import HookState
from obs_agent.runtime_env import bootstrap_runtime_env
from obs_agent.runner import ConversationRunner, DoneEvent, TextEvent
from obs_agent.session import SessionManager
from obs_agent import tracing

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig

logger = logging.getLogger("obs_agent.daemon")


class ChatRequest(BaseModel):
    """Request body for POST /chat."""
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    """Response body for POST /chat (non-streaming)."""
    response: str
    session_id: str | None = None


def create_default_app() -> FastAPI:
    """Factory for uvicorn: create app from environment config.

    Usage: uvicorn obs_agent.daemon:create_default_app --factory
    """
    from obs_agent.config import OBSConfig

    bootstrap_runtime_env(mutate_argv=False)
    config = OBSConfig.from_env()
    config.validate()

    # Start cache-normalizing proxy before any CC sessions
    from obs_agent.cache_proxy_lifecycle import should_attempt_proxy_start, start_cache_proxy
    if should_attempt_proxy_start(cache_proxy_enabled=config.cache_proxy_enabled):
        proxy_proc = start_cache_proxy(config.cache_proxy_port)
        if proxy_proc is None:
            logger.warning(
                "Cache proxy failed to start — sessions will use direct Anthropic API"
            )

    return create_app(config)


def create_app(config: OBSConfig) -> FastAPI:
    """Create a configured FastAPI application.

    Wires up session manager and all hooks.
    Uses ConversationRunner for the core conversation loop.
    """
    tracing.init_weave()

    application = FastAPI(title="OBS Agent", version="0.1.0")

    # Shared hook state for message queuing and interrupt
    hook_state = HookState()

    # Store config, session manager, hook state, and command registry in app state
    application.state.config = config
    application.state.hook_state = hook_state
    application.state.session_manager = SessionManager(config=config, hook_state=hook_state)
    application.state.commands = CommandRegistry(hook_state)
    application.state.pending_messages: list[str] = []

    # Traced turn driver: captures turn inputs/outputs as a Weave span.
    # ForkRunner calls during the turn nest automatically via asyncio contextvars.
    # When tracing is disabled, traced_op returns the function unchanged.
    async def _drive_turn(message: str, session_id: str | None, model: str) -> dict:
        session_mgr: SessionManager = application.state.session_manager
        pending = list(getattr(application.state, "pending_messages", []))
        runner = ConversationRunner(session_mgr, hook_state, config, pending_messages=pending)
        text_parts: list[str] = []
        tool_uses: list[str] = []
        async for event in runner.run(message):
            if isinstance(event, TextEvent):
                text_parts.append(event.text)
            elif isinstance(event, StatusEvent) and event.type == "tool_use":
                tool_uses.append(event.summary)
        application.state.pending_messages = runner.remaining_pending
        last = hook_state.last_result_data or {}
        usage = last.get("usage") or {}
        return {
            "response": "".join(text_parts),
            "tool_uses": tool_uses,
            "cost_usd": last.get("total_cost_usd"),
            "duration_ms": last.get("duration_ms"),
            "num_turns": last.get("num_turns"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
        }

    _drive_turn_traced = tracing.traced_op(_drive_turn)

    @application.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @application.post("/chat/enqueue")
    async def chat_enqueue(request: ChatRequest):
        """Queue a message for injection at the next hook boundary."""
        registry: CommandRegistry = application.state.commands
        result = await registry.execute("enqueue", message=request.message)
        if not result.success:
            return JSONResponse(status_code=422, content={"detail": result.message})
        return {
            "queued": True,
            "queue_size": application.state.hook_state.message_queue.qsize(),
        }

    @application.post("/chat/interrupt")
    async def chat_interrupt():
        """Set the interrupt flag and send SDK-level interrupt."""
        registry: CommandRegistry = application.state.commands
        result = await registry.execute("stop")

        # Also send SDK-level interrupt for immediate stop
        session_mgr: SessionManager = application.state.session_manager
        if session_mgr._client is not None and session_mgr._connected:
            try:
                await session_mgr._client.interrupt()
            except Exception:
                pass

        return {"interrupted": True}

    @application.get("/commands")
    async def list_commands():
        """List available commands for discoverability."""
        registry: CommandRegistry = application.state.commands
        return {"commands": registry.list_commands()}

    @application.post("/chat")
    async def chat(request: ChatRequest):
        """Process a chat message through the agent via ConversationRunner.

        Collects all text events into a single response string.
        """
        session_mgr: SessionManager = application.state.session_manager
        identity = tracing.resolve_identity(session_mgr.session_id, hook_state, config)
        try:
            async with tracing.weave_attributes(identity):
                result = await _drive_turn_traced(
                    request.message,
                    session_mgr.session_id,
                    config.model,
                )
        except Exception as exc:
            logger.exception("Error in /chat")
            return JSONResponse(
                status_code=500,
                content={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )

        return ChatResponse(
            response=result["response"],
            session_id=session_mgr.session_id,
        )

    @application.post("/chat/stream")
    async def chat_stream(request: ChatRequest):
        """SSE streaming variant of /chat using ConversationRunner.

        Converts RunnerEvents to SSE wire format.
        """
        session_mgr: SessionManager = application.state.session_manager
        cfg: OBSConfig = application.state.config
        pending = getattr(application.state, "pending_messages", [])

        runner = ConversationRunner(
            session_mgr, hook_state, cfg,
            pending_messages=pending,
        )

        async def event_generator():
            async with tracing.TurnTracer(request.message, session_mgr, hook_state, config) as tracer:
                try:
                    async for event in runner.run(request.message):
                        tracer.record_event(event)
                        if isinstance(event, TextEvent):
                            for text_line in event.text.split("\n"):
                                yield f"data: {text_line}\n"
                            yield "\n"
                        elif isinstance(event, StatusEvent):
                            yield event.to_sse()
                        elif isinstance(event, DoneEvent):
                            application.state.pending_messages = runner.remaining_pending
                            yield "data: [DONE]\n\n"
                except Exception as exc:
                    logger.exception("Error in SSE stream")
                    error_msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                    yield f"event: error\ndata: {json.dumps({'error': error_msg})}\n\n"
                    yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
        )

    return application
