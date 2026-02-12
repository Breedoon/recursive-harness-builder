"""FastAPI server for OBS Agent daemon.

HTTP API with SSE streaming, integrating session manager, fork runner, and hooks.
Listens on localhost:7832 by default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, query
from fastapi import FastAPI
from pydantic import BaseModel, Field

from obs_agent.session import SessionManager

if TYPE_CHECKING:
    from obs_agent.config import OBSConfig


class ChatRequest(BaseModel):
    """Request body for POST /chat."""
    message: str = Field(..., min_length=1)


# Default app instance (for backward compat with existing tests)
app = FastAPI(title="OBS Agent", version="0.1.0")


def create_app(config: OBSConfig) -> FastAPI:
    """Create a configured FastAPI application."""
    application = FastAPI(title="OBS Agent", version="0.1.0")

    # Store config and session manager in app state
    application.state.config = config
    application.state.session_manager = SessionManager(config=config)

    @application.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @application.post("/chat")
    async def chat(request: ChatRequest):
        session_mgr = application.state.session_manager
        cfg = application.state.config
        options = session_mgr.create_options()

        result_parts: list[str] = []
        async for message in query(prompt=request.message, options=options):
            if hasattr(message, "content") and isinstance(message.content, str):
                result_parts.append(message.content)

        response_text = "\n".join(result_parts)
        session_mgr.touch()

        return {"response": response_text}

    return application


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}
