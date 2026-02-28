"""TelegramPlatform for eval testing.

Uses Telethon (userbot client) to interact with the OBS Agent Telegram bot,
implementing the same Platform protocol as CLIPlatform.

Key behavior: send() collects ALL response chunks (the bot may split long
responses into multiple Telegram messages). It waits for messages to stop
arriving (no new message within a settling window) before returning the
concatenated result.

Requires environment variables:
- TELEGRAM_API_ID: Telegram API ID (from https://my.telegram.org)
- TELEGRAM_API_HASH: Telegram API hash
- TELEGRAM_SESSION: Telethon StringSession (pre-authenticated)
- TELEGRAM_TEST_BOT_USERNAME: bot username (without @)

The Telethon session must be pre-authenticated (run spikes/generate_session.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from telethon import TelegramClient
from telethon.events import NewMessage
from telethon.sessions import StringSession

logger = logging.getLogger("obs_agent.eval.telegram")

# Default timeout tuning:
# - First bot response can legitimately take some time
# - After first content, long idle without the final completion summary usually
#   means transport desync or a missing end marker, so fail fast instead of
#   idling for minutes
_DEFAULT_FIRST_MESSAGE_TIMEOUT = 90.0
_DEFAULT_DONE_TIMEOUT = 120.0
_DEFAULT_IDLE_QUIESCENCE_TIMEOUT = 30.0
_CONTROL_SETTLE_SECONDS = 3.0
_COMPLETION_RE = re.compile(
    r"(?ims)^\s*context:\s+\S+\s*/\s*\S+(?:\s*\n\s*@[\w_]+)?\s*$"
)


def _is_completion_message(text: str) -> bool:
    stripped = text.strip()
    if stripped == "(done)":
        return True
    normalized = stripped.replace("_", "").replace("*", "")
    return bool(_COMPLETION_RE.search(normalized))


class TelegramPlatform:
    """Telethon-based platform for driving the OBS Agent Telegram bot.

    Sends messages as a real Telegram user, waits for bot replies,
    and returns the text content. Handles multi-message responses by
    collecting all chunks within a settling window.
    """

    def __init__(
        self,
        api_id: int | None = None,
        api_hash: str | None = None,
        session_string: str | None = None,
        bot_username: str | None = None,
        timeout: int = 180,
        first_message_timeout: float = _DEFAULT_FIRST_MESSAGE_TIMEOUT,
        done_timeout: float = _DEFAULT_DONE_TIMEOUT,
        idle_quiescence_timeout: float = _DEFAULT_IDLE_QUIESCENCE_TIMEOUT,
    ) -> None:
        self._api_id = api_id or int(os.environ["TELEGRAM_API_ID"])
        self._api_hash = api_hash or os.environ["TELEGRAM_API_HASH"]
        self._session_string = session_string or os.environ["TELEGRAM_SESSION"]
        self._bot_username = bot_username or os.environ["TELEGRAM_TEST_BOT_USERNAME"]
        self._timeout = timeout
        self._first_message_timeout = first_message_timeout
        self._done_timeout = done_timeout
        self._idle_quiescence_timeout = idle_quiescence_timeout
        self._client: TelegramClient | None = None
        self._last_output = ""
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()

    async def connect(self) -> None:
        """Connect the Telethon client and set up the message handler.

        After connecting, reads the latest message ID from the bot chat to
        establish a baseline. Only messages with ID > baseline are enqueued,
        preventing stale messages from previous test runs from contaminating
        the current scenario.
        """
        self._client = TelegramClient(
            StringSession(self._session_string),
            self._api_id,
            self._api_hash,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError(
                "Telethon session not authorized. Run spikes/generate_session.py first."
            )

        # Resolve bot entity
        self._bot_entity = await self._client.get_entity(self._bot_username)

        # Establish baseline: ignore any messages already in the chat.
        # This prevents cross-run contamination from previous test runs.
        self._baseline_msg_id = 0
        async for msg in self._client.iter_messages(self._bot_entity, limit=1):
            self._baseline_msg_id = msg.id
        logger.info(
            "TelegramPlatform baseline message ID: %d", self._baseline_msg_id
        )

        # Listen for messages from the bot — only enqueue messages AFTER baseline
        @self._client.on(NewMessage(from_users=[self._bot_entity.id]))
        async def _on_bot_message(event: NewMessage.Event) -> None:
            if event.message.id <= self._baseline_msg_id:
                logger.debug(
                    "Ignoring stale message id=%d (baseline=%d)",
                    event.message.id, self._baseline_msg_id,
                )
                return
            text = event.message.text or ""
            self._response_queue.put_nowait(text)

        logger.info("TelegramPlatform connected to %s", self._bot_username)

    async def _collect_response(
        self,
        timeout: float,
        *,
        require_done: bool = True,
        first_message_timeout: float | None = None,
        done_timeout: float | None = None,
        idle_quiescence_timeout: float | None = None,
    ) -> str:
        """Collect all response chunks until the completion marker or timeout.

        Timeout model:
        - Wait up to first_message_timeout for first bot message
        - For control commands (require_done=False), collect until short settle idle
        - For normal turns (require_done=True):
          - Wait for the final completion summary up to done_timeout (capped by `timeout`)
          - If idle >= idle_quiescence_timeout before that marker, fail fast
            with a structured timeout marker
        """
        chunks: list[str] = []
        first_budget = min(timeout, first_message_timeout or self._first_message_timeout)
        done_budget = min(timeout, done_timeout or self._done_timeout)
        idle_budget = idle_quiescence_timeout or self._idle_quiescence_timeout

        # Wait for the first message
        try:
            first = await asyncio.wait_for(
                self._response_queue.get(), timeout=first_budget
            )
            chunks.append(first)
        except asyncio.TimeoutError:
            return f"(timeout: no response from bot after {first_budget:.0f}s)"

        if _is_completion_message(first):
            return "\n".join(chunks)

        if not require_done:
            # Control commands (e.g. /new) do not emit the completion summary.
            while True:
                try:
                    more = await asyncio.wait_for(
                        self._response_queue.get(), timeout=_CONTROL_SETTLE_SECONDS
                    )
                    chunks.append(more)
                except asyncio.TimeoutError:
                    break
            return "\n".join(chunks)

        # Collect additional chunks until the completion marker, with bounded done + idle budgets.
        done_deadline = asyncio.get_running_loop().time() + done_budget
        while True:
            remaining = done_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return (
                    f"(timeout: missing completion marker after {done_budget:.0f}s; "
                    f"{len(chunks)} chunk(s) received)"
                )
            try:
                more = await asyncio.wait_for(
                    self._response_queue.get(),
                    timeout=min(idle_budget, remaining),
                )
                chunks.append(more)
                if _is_completion_message(more):
                    break
            except asyncio.TimeoutError:
                return (
                    f"(timeout: missing completion marker after {idle_budget:.0f}s idle; "
                    f"{len(chunks)} chunk(s) received)"
                )

        return "\n".join(chunks)

    async def rebaseline(self) -> None:
        """Update baseline to the latest message in the chat.

        Call after /new succeeds to ignore any stale chunks still being
        delivered from a previous scenario's response.
        """
        if self._client is None:
            return
        async for msg in self._client.iter_messages(self._bot_entity, limit=1):
            old = self._baseline_msg_id
            self._baseline_msg_id = msg.id
            logger.info(
                "Rebaselined message ID: %d -> %d", old, self._baseline_msg_id
            )
        # Drain the queue of any chunks that arrived before rebaseline
        drained = 0
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            logger.info("Rebaseline drained %d stale chunk(s)", drained)

    async def send(self, text: str) -> str:
        """Send a message to the bot and wait for the complete reply.

        Collects all response chunks (handles message splitting).
        """
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        # Drain any stale messages
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        await self._client.send_message(self._bot_entity, text)

        response = await self._collect_response(timeout=self._timeout, require_done=True)
        self._last_output = response
        return response

    async def send_control(self, text: str, timeout: float = 20.0) -> str:
        """Send a control command that is not expected to emit the completion summary."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        await self._client.send_message(self._bot_entity, text)
        response = await self._collect_response(timeout=timeout, require_done=False)
        self._last_output = response
        return response

    async def send_nowait(self, text: str) -> None:
        """Send a message without waiting for response."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")
        await self._client.send_message(self._bot_entity, text)

    async def read(self) -> str:
        """Return the most recent bot output."""
        return self._last_output

    async def wait_for_prompt(self, timeout: int = 120) -> str:
        """Wait for the next bot message(s), collecting multi-message responses."""
        response = await self._collect_response(timeout=timeout, require_done=True)
        self._last_output = response
        return response

    async def close(self) -> None:
        """Disconnect the Telethon client."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
