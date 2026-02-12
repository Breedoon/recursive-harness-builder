"""FastAPI server for OBS Agent daemon.

HTTP API with SSE streaming, integrating session manager, fork runner, and hooks.
Listens on localhost:7832 by default.

See implementation-plan.md Steps 9-10 and decisions D014, D018, D019, D022.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, query
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from obs_agent.fork import ForkRunner
from obs_agent.hooks import on_pre_tool_use, on_stop, on_user_prompt_submit
from obs_agent.session import SessionManager

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


# Default app instance (for backward compat with existing tests)
app = FastAPI(title="OBS Agent", version="0.1.0")


def create_app(config: OBSConfig) -> FastAPI:
    """Create a configured FastAPI application.

    Wires up session manager, fork runner, and all hooks.
    """
    application = FastAPI(title="OBS Agent", version="0.1.0")

    # Store config and session manager in app state
    application.state.config = config
    application.state.session_manager = SessionManager(config=config)

    @application.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @application.post("/chat")
    async def chat(request: ChatRequest):
        """Process a chat message through the agent.

        Flow:
        1. Run UserPromptSubmit hook (skill classification + injection)
        2. Build SDK options from session manager
        3. Stream SDK response
        4. Touch session activity timer
        5. Return response
        """
        session_mgr: SessionManager = application.state.session_manager
        cfg: OBSConfig = application.state.config

        # Build options from session manager (handles resume vs fresh)
        options = session_mgr.create_options()

        # UserPromptSubmit hook: classify skills and inject if available
        injected_content = None
        if session_mgr.session_id is not None:
            fork_runner = ForkRunner(
                config=cfg, session_id=session_mgr.session_id
            )
            try:
                injected_content = await on_user_prompt_submit(
                    request.message, config=cfg, fork_runner=fork_runner
                )
            except Exception:
                logger.exception("UserPromptSubmit hook failed")

        # Prepare the prompt (with skill injection if any)
        prompt = request.message
        if injected_content:
            prompt = f"{request.message}\n\n---\n\n{injected_content}"

        # Query the SDK
        result_parts: list[str] = []
        session_id = None

        stream = query(prompt=prompt, options=options)
        aiter = stream.__aiter__()
        if hasattr(aiter, "__await__"):
            aiter = await aiter
        while True:
            try:
                anext = aiter.__anext__()
                if hasattr(anext, "__await__"):
                    message = await anext
                else:
                    message = anext
            except StopAsyncIteration:
                break
            except StopIteration:
                break

            # Capture session_id from init messages
            if hasattr(message, "session_id") and message.session_id:
                session_id = message.session_id
                session_mgr.set_session_id(session_id)

            # Collect text content
            if hasattr(message, "content") and isinstance(message.content, str):
                result_parts.append(message.content)

        response_text = "\n".join(result_parts)
        session_mgr.touch()

        return ChatResponse(
            response=response_text,
            session_id=session_mgr.session_id,
        )

    @application.post("/chat/stream")
    async def chat_stream(request: ChatRequest):
        """SSE streaming variant of /chat.

        Returns a text/event-stream response with assistant text chunks.
        """
        session_mgr: SessionManager = application.state.session_manager
        cfg: OBSConfig = application.state.config
        options = session_mgr.create_options()

        prompt = request.message

        async def event_generator():
            stream = query(prompt=prompt, options=options)
            aiter = stream.__aiter__()
            if hasattr(aiter, "__await__"):
                aiter = await aiter
            while True:
                try:
                    anext = aiter.__anext__()
                    if hasattr(anext, "__await__"):
                        message = await anext
                    else:
                        message = anext
                except StopAsyncIteration:
                    break
                except StopIteration:
                    break

                if hasattr(message, "session_id") and message.session_id:
                    session_mgr.set_session_id(message.session_id)

                if hasattr(message, "content") and isinstance(
                    message.content, str
                ):
                    yield f"data: {message.content}\n\n"

            session_mgr.touch()
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
        )

    return application


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
