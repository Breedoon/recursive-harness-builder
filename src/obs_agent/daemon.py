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
from obs_agent.startup_logging import StartupProfiler

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
    startup = StartupProfiler(logger, "http-daemon")

    with startup.phase("import_config"):
        from obs_agent.config import OBSConfig

    with startup.phase("bootstrap_runtime_env"):
        bootstrap_runtime_env(mutate_argv=False)
    with startup.phase("load_config"):
        config = OBSConfig.from_env()
    with startup.phase("validate_config"):
        config.validate()

    from obs_agent.cache_proxy_lifecycle import should_attempt_proxy_start, start_cache_proxy
    with startup.phase(
        "cache_proxy",
        enabled=config.cache_proxy_enabled,
        port=config.cache_proxy_port,
    ):
        if should_attempt_proxy_start(cache_proxy_enabled=config.cache_proxy_enabled):
            proxy_proc = start_cache_proxy(config.cache_proxy_port)
            if proxy_proc is None:
                logger.warning(
                    "Cache proxy failed to start — sessions will use direct Anthropic API"
                )

    with startup.phase("create_app"):
        app = create_app(config)
    startup.complete(port=config.daemon_port)
    return app


def create_app(config: OBSConfig) -> FastAPI:
    """Create a configured FastAPI application.

    Wires up session manager and all hooks.
    Uses ConversationRunner for the core conversation loop.
    """
    application = FastAPI(title="OBS Agent", version="0.1.0")

    # Shared hook state for message queuing and interrupt
    hook_state = HookState()

    # Store config, session manager, hook state, and command registry in app state
    application.state.config = config
    application.state.hook_state = hook_state
    application.state.session_manager = SessionManager(config=config, hook_state=hook_state)
    application.state.commands = CommandRegistry(hook_state)
    application.state.pending_messages: list[str] = []

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
        cfg: OBSConfig = application.state.config
        pending = getattr(application.state, "pending_messages", [])

        runner = ConversationRunner(
            session_mgr, hook_state, cfg,
            pending_messages=pending,
        )

        result_parts: list[str] = []
        try:
            async for event in runner.run(request.message):
                if isinstance(event, TextEvent):
                    result_parts.append(event.text)
        except Exception as exc:
            logger.exception("Error in /chat")
            return JSONResponse(
                status_code=500,
                content={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )

        application.state.pending_messages = runner.remaining_pending

        return ChatResponse(
            response="\n".join(result_parts),
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
            try:
                async for event in runner.run(request.message):
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
