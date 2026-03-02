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
from dataclasses import dataclass
import logging
import os
import re
from pathlib import Path

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


@dataclass(frozen=True)
class TelegramObservedMessage:
    """Structured Telegram message observed from the bot."""

    message_id: int
    text: str
    reply_to_message_id: int | None = None


@dataclass(frozen=True)
class TelegramResponseTrace:
    """Structured record of one user send plus the resulting bot messages."""

    sent_message_id: int | None
    output: str
    messages: list[TelegramObservedMessage]


def _coerce_observed_message(message: TelegramObservedMessage | str) -> TelegramObservedMessage:
    if isinstance(message, TelegramObservedMessage):
        return message
    return TelegramObservedMessage(message_id=-1, text=message)


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
        self._response_queue: asyncio.Queue[TelegramObservedMessage | str] = asyncio.Queue()
        self._recent_bot_messages: list[TelegramObservedMessage] = []

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
            observed = TelegramObservedMessage(
                message_id=event.message.id,
                text=event.message.text or "",
                reply_to_message_id=getattr(
                    getattr(event.message, "reply_to", None),
                    "reply_to_msg_id",
                    None,
                ),
            )
            self._recent_bot_messages.append(observed)
            self._recent_bot_messages = self._recent_bot_messages[-200:]
            self._response_queue.put_nowait(observed)

        logger.info("TelegramPlatform connected to %s", self._bot_username)

    async def _collect_response_trace(
        self,
        timeout: float,
        *,
        require_done: bool = True,
        first_message_timeout: float | None = None,
        done_timeout: float | None = None,
        idle_quiescence_timeout: float | None = None,
        sent_message_id: int | None = None,
    ) -> TelegramResponseTrace:
        """Collect all response chunks until the completion marker or timeout.

        Timeout model:
        - Wait up to first_message_timeout for first bot message
        - For control commands (require_done=False), collect until short settle idle
        - For normal turns (require_done=True):
          - Wait for the final completion summary up to done_timeout (capped by `timeout`)
          - If idle >= idle_quiescence_timeout before that marker, fail fast
            with a structured timeout marker
        """
        observed_messages: list[TelegramObservedMessage] = []
        first_budget = min(timeout, first_message_timeout or self._first_message_timeout)
        done_budget = min(timeout, done_timeout or self._done_timeout)
        idle_budget = idle_quiescence_timeout or self._idle_quiescence_timeout

        # Wait for the first message
        try:
            first_raw = await asyncio.wait_for(
                self._response_queue.get(), timeout=first_budget
            )
            first = _coerce_observed_message(first_raw)
            observed_messages.append(first)
        except asyncio.TimeoutError:
            return TelegramResponseTrace(
                sent_message_id=sent_message_id,
                output=f"(timeout: no response from bot after {first_budget:.0f}s)",
                messages=[],
            )

        if _is_completion_message(first.text):
            return TelegramResponseTrace(
                sent_message_id=sent_message_id,
                output="\n".join(message.text for message in observed_messages),
                messages=observed_messages,
            )

        if not require_done:
            # Control commands (e.g. /new) do not emit the completion summary.
            while True:
                try:
                    more_raw = await asyncio.wait_for(
                        self._response_queue.get(), timeout=_CONTROL_SETTLE_SECONDS
                    )
                    observed_messages.append(_coerce_observed_message(more_raw))
                except asyncio.TimeoutError:
                    break
            return TelegramResponseTrace(
                sent_message_id=sent_message_id,
                output="\n".join(message.text for message in observed_messages),
                messages=observed_messages,
            )

        # Collect additional chunks until the completion marker, with bounded done + idle budgets.
        done_deadline = asyncio.get_running_loop().time() + done_budget
        while True:
            remaining = done_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return TelegramResponseTrace(
                    sent_message_id=sent_message_id,
                    output=(
                        f"(timeout: missing completion marker after {done_budget:.0f}s; "
                        f"{len(observed_messages)} chunk(s) received)"
                    ),
                    messages=observed_messages,
                )
            try:
                more_raw = await asyncio.wait_for(
                    self._response_queue.get(),
                    timeout=min(idle_budget, remaining),
                )
                more = _coerce_observed_message(more_raw)
                observed_messages.append(more)
                if _is_completion_message(more.text):
                    break
            except asyncio.TimeoutError:
                return TelegramResponseTrace(
                    sent_message_id=sent_message_id,
                    output=(
                        f"(timeout: missing completion marker after {idle_budget:.0f}s idle; "
                        f"{len(observed_messages)} chunk(s) received)"
                    ),
                    messages=observed_messages,
                )

        return TelegramResponseTrace(
            sent_message_id=sent_message_id,
            output="\n".join(message.text for message in observed_messages),
            messages=observed_messages,
        )

    async def _collect_response(
        self,
        timeout: float,
        *,
        require_done: bool = True,
        first_message_timeout: float | None = None,
        done_timeout: float | None = None,
        idle_quiescence_timeout: float | None = None,
    ) -> str:
        trace = await self._collect_response_trace(
            timeout=timeout,
            require_done=require_done,
            first_message_timeout=first_message_timeout,
            done_timeout=done_timeout,
            idle_quiescence_timeout=idle_quiescence_timeout,
        )
        return trace.output

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

        self._drain_queue()

        sent = await self._client.send_message(self._bot_entity, text)
        trace = await self._collect_response_trace(
            timeout=self._timeout,
            require_done=True,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace.output

    async def send_with_trace(self, text: str) -> TelegramResponseTrace:
        """Send a message and return structured Telegram metadata for the reply."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        self._drain_queue()
        sent = await self._client.send_message(self._bot_entity, text)
        trace = await self._collect_response_trace(
            timeout=self._timeout,
            require_done=True,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace

    async def send_control(self, text: str, timeout: float = 20.0) -> str:
        """Send a control command that is not expected to emit the completion summary."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        self._drain_queue()

        sent = await self._client.send_message(self._bot_entity, text)
        trace = await self._collect_response_trace(
            timeout=timeout,
            require_done=False,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace.output

    async def send_control_with_trace(
        self, text: str, timeout: float = 20.0
    ) -> TelegramResponseTrace:
        """Send a control command and return structured Telegram metadata."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        self._drain_queue()
        sent = await self._client.send_message(self._bot_entity, text)
        trace = await self._collect_response_trace(
            timeout=timeout,
            require_done=False,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace

    async def send_file(
        self,
        path: str | Path,
        *,
        caption: str | None = None,
        force_document: bool = False,
        voice_note: bool = False,
        video_note: bool = False,
        timeout: float | None = None,
        first_message_timeout: float | None = None,
        done_timeout: float | None = None,
        idle_quiescence_timeout: float | None = None,
    ) -> str:
        """Send a local file to the bot and wait for the complete reply."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        self._drain_queue()
        sent = await self._client.send_file(
            self._bot_entity,
            str(path),
            caption=caption,
            force_document=force_document,
            voice_note=voice_note,
            video_note=video_note,
        )
        trace = await self._collect_response_trace(
            timeout=timeout or self._timeout,
            require_done=True,
            first_message_timeout=first_message_timeout,
            done_timeout=done_timeout,
            idle_quiescence_timeout=idle_quiescence_timeout,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace.output

    async def send_files(
        self,
        paths: list[str | Path],
        *,
        captions: list[str] | None = None,
        force_document: bool = False,
        timeout: float | None = None,
        first_message_timeout: float | None = None,
        done_timeout: float | None = None,
        idle_quiescence_timeout: float | None = None,
    ) -> str:
        """Send multiple local files, usually as a Telegram album."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        self._drain_queue()
        sent = await self._client.send_file(
            self._bot_entity,
            [str(path) for path in paths],
            caption=captions,
            force_document=force_document,
        )
        trace = await self._collect_response_trace(
            timeout=timeout or self._timeout,
            require_done=True,
            first_message_timeout=first_message_timeout,
            done_timeout=done_timeout,
            idle_quiescence_timeout=idle_quiescence_timeout,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace.output

    async def send_nowait(self, text: str) -> None:
        """Send a message without waiting for response."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")
        await self._client.send_message(self._bot_entity, text)

    async def reply(self, text: str, *, reply_to_message_id: int) -> str:
        """Reply to a specific Telegram message and wait for the bot reply."""
        trace = await self.reply_with_trace(text, reply_to_message_id=reply_to_message_id)
        return trace.output

    async def reply_with_trace(
        self,
        text: str,
        *,
        reply_to_message_id: int,
        timeout: float | None = None,
        require_done: bool = True,
    ) -> TelegramResponseTrace:
        """Reply to a specific Telegram message and return structured reply data."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        self._drain_queue()
        sent = await self._client.send_message(
            self._bot_entity,
            text,
            reply_to=reply_to_message_id,
        )
        trace = await self._collect_response_trace(
            timeout=timeout or self._timeout,
            require_done=require_done,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace

    async def reply_control_with_trace(
        self,
        text: str,
        *,
        reply_to_message_id: int,
        timeout: float = 20.0,
    ) -> TelegramResponseTrace:
        """Reply with a control command that does not emit completion summary."""
        return await self.reply_with_trace(
            text,
            reply_to_message_id=reply_to_message_id,
            timeout=timeout,
            require_done=False,
        )

    async def read(self) -> str:
        """Return the most recent bot output."""
        return self._last_output

    async def wait_for_prompt(self, timeout: int = 120) -> str:
        """Wait for the next bot message(s), collecting multi-message responses."""
        trace = await self._collect_response_trace(timeout=timeout, require_done=True)
        self._last_output = trace.output
        return trace.output

    async def wait_for_prompt_with_trace(self, timeout: int = 120) -> TelegramResponseTrace:
        """Wait for the next bot message(s), preserving Telegram metadata."""
        trace = await self._collect_response_trace(timeout=timeout, require_done=True)
        self._last_output = trace.output
        return trace

    async def get_recent_messages(self, limit: int = 20) -> list[TelegramObservedMessage]:
        """Fetch recent bot-chat messages directly from Telegram."""
        if self._client is None:
            raise RuntimeError("TelegramPlatform not connected")

        messages: list[TelegramObservedMessage] = []
        async for message in self._client.iter_messages(self._bot_entity, limit=limit):
            messages.append(
                TelegramObservedMessage(
                    message_id=message.id,
                    text=message.text or "",
                    reply_to_message_id=getattr(
                        getattr(message, "reply_to", None),
                        "reply_to_msg_id",
                        None,
                    ),
                )
            )
        messages.reverse()
        return messages

    def format_recent_messages(self, limit: int = 20) -> str:
        """Render recent observed bot messages for assertion failures."""
        recent = self._recent_bot_messages[-limit:]
        if not recent:
            return "(no observed bot messages)"
        return "\n".join(
            f"id={message.message_id} reply_to={message.reply_to_message_id} text={message.text!r}"
            for message in recent
        )

    async def close(self) -> None:
        """Disconnect the Telethon client."""
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    def _drain_queue(self) -> None:
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
