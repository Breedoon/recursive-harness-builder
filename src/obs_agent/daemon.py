"""FastAPI server for OBS Agent daemon.

HTTP API with SSE streaming, using ClaudeSDKClient for interactive multi-turn
conversations. Integrates session manager, fork runner, and hooks.
Listens on localhost:7832 by default.

See implementation-plan.md Steps 9-10 and decisions D014, D018, D019, D022.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from claude_agent_sdk import CLIConnectionError, TextBlock, ThinkingBlock, ToolUseBlock
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from obs_agent.commands import CommandRegistry
from obs_agent.events import StatusEvent, summarize_tool_use
from obs_agent.fork import ForkRunner, classify_without_fork
from obs_agent.hooks import HookState, on_user_prompt_submit
from obs_agent.metrics import log_result
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


def _drain_queue(queue: asyncio.Queue) -> list[str]:
    """Drain all messages from an asyncio.Queue, returning them as a list."""
    messages: list[str] = []
    while not queue.empty():
        try:
            messages.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return messages


def create_default_app() -> FastAPI:
    """Factory for uvicorn: create app from environment config.

    Usage: uvicorn obs_agent.daemon:create_default_app --factory
    """
    from obs_agent.config import OBSConfig

    config = OBSConfig.from_env()
    config.validate()
    return create_app(config)


def create_app(config: OBSConfig) -> FastAPI:
    """Create a configured FastAPI application.

    Wires up session manager, fork runner, and all hooks.
    Uses ClaudeSDKClient for interactive multi-turn conversations.
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
            from fastapi.responses import JSONResponse
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
        """Process a chat message through the agent via ClaudeSDKClient.

        Flow:
        1. Inject pending messages from previous turn's undrained queue
        2. Run UserPromptSubmit hook (skill classification + injection)
        3. Get or create SDK client from session manager
        4. Send query and stream response
        5. Drain remaining queued messages for next turn
        6. Touch session activity timer
        7. Return response
        """
        session_mgr: SessionManager = application.state.session_manager
        cfg: OBSConfig = application.state.config

        # Inject pending messages from previous turn's queue
        pending = getattr(application.state, "pending_messages", [])
        user_message = request.message
        if pending:
            prefix = "\n".join(
                f"[Queued message from user]: {m}" for m in pending
            )
            user_message = f"{prefix}\n\n{request.message}"
            application.state.pending_messages = []

        # UserPromptSubmit hook: classify skills and inject if available
        injected_content = None
        should_classify = len(user_message) > cfg.classification_threshold
        if should_classify:
            if session_mgr.session_id is not None:
                fork_runner = ForkRunner(
                    config=cfg, session_id=session_mgr.session_id
                )
                try:
                    injected_content = await on_user_prompt_submit(
                        user_message, config=cfg, fork_runner=fork_runner
                    )
                except Exception:
                    logger.exception("UserPromptSubmit hook failed")
            else:
                # First message: no session to fork from, use standalone classify
                try:
                    skill_names = await classify_without_fork(
                        user_message, cfg
                    )
                    if skill_names:
                        from obs_agent.prompt import _read_file

                        parts: list[str] = []
                        for name in skill_names:
                            skill_path = cfg.skill_path(name)
                            content = _read_file(skill_path)
                            if content:
                                parts.append(f"## Skill: {name}\n\n{content}")
                        if parts:
                            injected_content = "\n\n---\n\n".join(parts)
                except Exception:
                    logger.exception("First-message skill classification failed")

        # Prepare the prompt (with skill injection if any)
        prompt = user_message
        if injected_content:
            prompt = f"{user_message}\n\n---\n\n{injected_content}"

        # Query via ClaudeSDKClient
        result_parts: list[str] = []
        last_message = None

        client = await session_mgr.get_client()
        try:
            await client.query(prompt)
        except CLIConnectionError:
            logger.warning("Client connection lost, reconnecting")
            await session_mgr.disconnect()
            client = await session_mgr.get_client()
            await client.query(prompt)

        async for message in client.receive_response():
            last_message = message
            if hasattr(message, "session_id") and message.session_id:
                session_mgr.set_session_id(message.session_id)
            if hasattr(message, "content") and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_parts.append(block.text)

        if last_message is not None:
            log_result(last_message, label="chat")

        # Continuation loop for non-streaming
        continuation_count = 0
        while continuation_count < cfg.max_queue_continuations:
            remaining_cont = _drain_queue(hook_state.message_queue)
            if not remaining_cont:
                break
            continuation_count += 1

            prefix = "\n".join(
                f"[Queued message from user]: {m}" for m in remaining_cont
            )
            continuation_prompt = (
                f"{prefix}\n\n"
                "(User sent these while you were responding. Address them briefly.)"
            )

            await client.query(continuation_prompt)
            async for cont_message in client.receive_response():
                if hasattr(cont_message, "session_id") and cont_message.session_id:
                    session_mgr.set_session_id(cont_message.session_id)
                if hasattr(cont_message, "content") and isinstance(
                    cont_message.content, list
                ):
                    for block in cont_message.content:
                        if isinstance(block, TextBlock):
                            result_parts.append(block.text)

        application.state.pending_messages = _drain_queue(hook_state.message_queue)

        response_text = "\n".join(result_parts)
        session_mgr.touch()

        return ChatResponse(
            response=response_text,
            session_id=session_mgr.session_id,
        )

    @application.post("/chat/stream")
    async def chat_stream(request: ChatRequest):
        """SSE streaming variant of /chat using ClaudeSDKClient.

        Returns a text/event-stream response with assistant text chunks
        and status events for tool use, thinking, skill classification,
        and queue delivery.
        """
        session_mgr: SessionManager = application.state.session_manager
        cfg: OBSConfig = application.state.config

        # Inject pending messages from previous turn's queue
        pending = getattr(application.state, "pending_messages", [])
        user_message = request.message
        had_pending = bool(pending)
        pending_count = len(pending)
        if pending:
            prefix = "\n".join(
                f"[Queued message from user]: {m}" for m in pending
            )
            user_message = f"{prefix}\n\n{request.message}"
            application.state.pending_messages = []

        async def event_generator():
            # Emit queue_delivered if pending messages were prepended
            if had_pending:
                yield StatusEvent(
                    type="queue_delivered",
                    summary="queued message delivered",
                    count=pending_count,
                    messages=pending,
                ).to_sse()

            # Skill classification inside the generator so status events
            # can be yielded during this phase
            should_classify = len(user_message) > cfg.classification_threshold
            injected_content = None
            if should_classify:
                yield StatusEvent(
                    type="skill_classify",
                    summary="classifying skills...",
                ).to_sse()

                if session_mgr.session_id is not None:
                    fork_runner = ForkRunner(
                        config=cfg, session_id=session_mgr.session_id
                    )
                    try:
                        injected_content = await on_user_prompt_submit(
                            user_message, config=cfg, fork_runner=fork_runner
                        )
                    except Exception:
                        logger.exception("UserPromptSubmit hook failed (stream)")
                else:
                    try:
                        skill_names = await classify_without_fork(
                            user_message, cfg
                        )
                        if skill_names:
                            from obs_agent.prompt import _read_file

                            parts: list[str] = []
                            for name in skill_names:
                                skill_path = cfg.skill_path(name)
                                content = _read_file(skill_path)
                                if content:
                                    parts.append(f"## Skill: {name}\n\n{content}")
                            if parts:
                                injected_content = "\n\n---\n\n".join(parts)
                    except Exception:
                        logger.exception("First-message skill classification failed (stream)")

            prompt = user_message
            if injected_content:
                prompt = f"{user_message}\n\n---\n\n{injected_content}"

            # Get client and query
            client = await session_mgr.get_client()
            try:
                await client.query(prompt)
            except CLIConnectionError:
                logger.warning("Client connection lost, reconnecting")
                await session_mgr.disconnect()
                client = await session_mgr.get_client()
                await client.query(prompt)

            last_message = None
            async for message in client.receive_response():
                last_message = message
                if hasattr(message, "session_id") and message.session_id:
                    session_mgr.set_session_id(message.session_id)
                if hasattr(message, "content") and isinstance(
                    message.content, list
                ):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_name = getattr(block, "name", "")
                            tool_input = getattr(block, "input", {}) or {}
                            yield StatusEvent(
                                type="tool_use",
                                summary=summarize_tool_use(tool_name, tool_input),
                            ).to_sse()
                        elif isinstance(block, ThinkingBlock):
                            yield StatusEvent(
                                type="thinking",
                                summary="thinking...",
                            ).to_sse()
                        elif isinstance(block, TextBlock):
                            # SSE spec: multi-line data needs separate data: per line
                            for text_line in block.text.split("\n"):
                                yield f"data: {text_line}\n"
                            yield "\n"  # event boundary

                # Drain status_queue after each message (queue_delivered from hooks)
                while not hook_state.status_queue.empty():
                    try:
                        status_event = hook_state.status_queue.get_nowait()
                        yield status_event.to_sse()
                    except asyncio.QueueEmpty:
                        break

            if last_message is not None:
                log_result(last_message, label="chat/stream")

            # Continuation loop: process queued messages inline
            continuation_count = 0
            while continuation_count < cfg.max_queue_continuations:
                remaining = _drain_queue(hook_state.message_queue)
                if not remaining:
                    break
                continuation_count += 1

                prefix = "\n".join(
                    f"[Queued message from user]: {m}" for m in remaining
                )
                continuation_prompt = (
                    f"{prefix}\n\n"
                    "(User sent these while you were responding. Address them briefly.)"
                )

                yield StatusEvent(
                    type="queue_delivered",
                    summary="queued message delivered",
                    count=len(remaining),
                    messages=remaining,
                ).to_sse()

                await client.query(continuation_prompt)
                async for cont_message in client.receive_response():
                    if hasattr(cont_message, "session_id") and cont_message.session_id:
                        session_mgr.set_session_id(cont_message.session_id)
                    if hasattr(cont_message, "content") and isinstance(
                        cont_message.content, list
                    ):
                        for block in cont_message.content:
                            if isinstance(block, ToolUseBlock):
                                tool_name = getattr(block, "name", "")
                                tool_input = getattr(block, "input", {}) or {}
                                yield StatusEvent(
                                    type="tool_use",
                                    summary=summarize_tool_use(tool_name, tool_input),
                                ).to_sse()
                            elif isinstance(block, ThinkingBlock):
                                yield StatusEvent(
                                    type="thinking",
                                    summary="thinking...",
                                ).to_sse()
                            elif isinstance(block, TextBlock):
                                for text_line in block.text.split("\n"):
                                    yield f"data: {text_line}\n"
                                yield "\n"

                    while not hook_state.status_queue.empty():
                        try:
                            status_event = hook_state.status_queue.get_nowait()
                            yield status_event.to_sse()
                        except asyncio.QueueEmpty:
                            break

            # Fallback: save any remaining messages after max continuations
            application.state.pending_messages = _drain_queue(hook_state.message_queue)

            session_mgr.touch()
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
        )

    return application
