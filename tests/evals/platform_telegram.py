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
import os
import logging

from telethon import TelegramClient
from telethon.events import NewMessage
from telethon.sessions import StringSession

logger = logging.getLogger("obs_agent.eval.telegram")

# How long to wait for additional message chunks after the last one
_SETTLE_SECONDS = 3.0
_DONE_SENTINEL = "(done)"


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
        timeout: int = 120,
    ) -> None:
        self._api_id = api_id or int(os.environ["TELEGRAM_API_ID"])
        self._api_hash = api_hash or os.environ["TELEGRAM_API_HASH"]
        self._session_string = session_string or os.environ["TELEGRAM_SESSION"]
        self._bot_username = bot_username or os.environ["TELEGRAM_TEST_BOT_USERNAME"]
        self._timeout = timeout
        self._client: TelegramClient | None = None
        self._last_output = ""
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()

    async def connect(self) -> None:
        """Connect the Telethon client and set up the message handler."""
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

        # Listen for messages from the bot
        @self._client.on(NewMessage(from_users=[self._bot_entity.id]))
        async def _on_bot_message(event: NewMessage.Event) -> None:
            text = event.message.text or ""
            self._response_queue.put_nowait(text)

        logger.info("TelegramPlatform connected to %s", self._bot_username)

    async def _collect_response(self, timeout: float) -> str:
        """Collect all response chunks until no new message for _SETTLE_SECONDS.

        Waits up to `timeout` for the first message, then keeps collecting
        until either:
        - a '(done)' sentinel arrives, or
        - no new message for _SETTLE_SECONDS.

        Returns all chunks concatenated with newlines.
        """
        chunks: list[str] = []

        # Wait for the first message with the full timeout
        try:
            first = await asyncio.wait_for(
                self._response_queue.get(), timeout=timeout
            )
            chunks.append(first)
        except asyncio.TimeoutError:
            return "(timeout: no response from bot)"

        if first.strip() == _DONE_SENTINEL:
            return "\n".join(chunks)

        # Collect additional chunks until done sentinel or settling window expires
        while True:
            try:
                more = await asyncio.wait_for(
                    self._response_queue.get(), timeout=_SETTLE_SECONDS
                )
                chunks.append(more)
                if more.strip() == _DONE_SENTINEL:
                    break
            except asyncio.TimeoutError:
                break

        return "\n".join(chunks)

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

        response = await self._collect_response(timeout=self._timeout)
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
        response = await self._collect_response(timeout=timeout)
        self._last_output = response
        return response

    async def close(self) -> None:
        """Disconnect the Telethon client."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
