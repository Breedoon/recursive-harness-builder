"""Telethon-backed forum-group test harness for Telegram topic workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import re
from typing import Any

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession


logger = logging.getLogger("obs_agent.eval.telegram_forum")

_DEFAULT_FIRST_MESSAGE_TIMEOUT = 90.0
_DEFAULT_DONE_TIMEOUT = 120.0
_DEFAULT_IDLE_QUIESCENCE_TIMEOUT = 30.0
_CONTROL_SETTLE_SECONDS = 3.0
_POLL_INTERVAL_SECONDS = 0.5
_COMPLETION_RE = re.compile(
    r"(?ims)^\s*context:\s+\S+\s*/\s*\S+(?:\s*\n\s*@[\w_]+)?\s*$"
)


def _is_completion_message(text: str) -> bool:
    stripped = text.strip()
    normalized = stripped.replace("_", "").replace("*", "")
    return bool(_COMPLETION_RE.search(normalized))


def _forum_thread_id(message: Any) -> int | None:
    reply_to = getattr(message, "reply_to", None)
    if not getattr(reply_to, "forum_topic", False):
        return None
    top_id = getattr(reply_to, "reply_to_top_id", None)
    if isinstance(top_id, int):
        return top_id
    reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)
    if isinstance(reply_to_msg_id, int):
        return reply_to_msg_id
    return None


@dataclass(frozen=True)
class TelegramForumObservedMessage:
    message_id: int
    text: str
    reply_to_message_id: int | None
    thread_id: int | None


@dataclass(frozen=True)
class TelegramForumResponseTrace:
    sent_message_id: int | None
    output: str
    messages: list[TelegramForumObservedMessage]


class TelegramForumPlatform:
    """Drive a Telegram forum supergroup through a real user account."""

    def __init__(
        self,
        *,
        chat_id: int | None = None,
        bot_username: str | None = None,
        bot_token: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
        session_string: str | None = None,
        timeout: float = 180.0,
        first_message_timeout: float = _DEFAULT_FIRST_MESSAGE_TIMEOUT,
        done_timeout: float = _DEFAULT_DONE_TIMEOUT,
        idle_quiescence_timeout: float = _DEFAULT_IDLE_QUIESCENCE_TIMEOUT,
    ) -> None:
        self._chat_id = chat_id or int(os.environ["OBS_TELEGRAM_LIVE_FORUM_CHAT_ID"])
        self._bot_username = bot_username or os.environ["TELEGRAM_TEST_BOT_USERNAME"]
        self._bot_token = bot_token or os.environ["OBS_TELEGRAM_TEST_BOT_TOKEN"]
        self._api_id = api_id or int(os.environ["TELEGRAM_API_ID"])
        self._api_hash = api_hash or os.environ["TELEGRAM_API_HASH"]
        self._session_string = session_string or os.environ["TELEGRAM_SESSION"]
        self._timeout = timeout
        self._first_message_timeout = first_message_timeout
        self._done_timeout = done_timeout
        self._idle_quiescence_timeout = idle_quiescence_timeout
        self._client: TelegramClient | None = None
        self._bot_id: int | None = None
        self._last_output = ""
        self._recent_messages: list[TelegramForumObservedMessage] = []

    async def connect(self) -> None:
        self._client = TelegramClient(
            StringSession(self._session_string),
            self._api_id,
            self._api_hash,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise RuntimeError("Telethon session not authorized")
        bot_entity = await self._client.get_entity(self._bot_username)
        self._bot_id = bot_entity.id

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def create_topic(self, name: str) -> int:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self._bot_token}/createForumTopic",
                data={"chat_id": self._chat_id, "name": name},
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"createForumTopic failed: {payload}")
        thread_id = payload["result"].get("message_thread_id")
        if not isinstance(thread_id, int):
            raise RuntimeError(f"createForumTopic returned no thread id: {payload}")
        return thread_id

    async def delete_topic(self, thread_id: int) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self._bot_token}/deleteForumTopic",
                data={"chat_id": self._chat_id, "message_thread_id": thread_id},
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"deleteForumTopic failed: {payload}")

    async def latest_bot_message_id(self, *, thread_id: int | None = None) -> int:
        messages = await self.get_recent_messages(thread_id=thread_id, limit=1)
        if not messages:
            return 0
        return messages[-1].message_id

    async def get_recent_messages(
        self,
        *,
        thread_id: int | None = None,
        limit: int = 20,
    ) -> list[TelegramForumObservedMessage]:
        if self._client is None or self._bot_id is None:
            raise RuntimeError("TelegramForumPlatform not connected")

        observed: list[TelegramForumObservedMessage] = []
        async for message in self._client.iter_messages(self._chat_id, limit=max(limit * 5, 50)):
            if message.sender_id != self._bot_id:
                continue
            message_thread_id = _forum_thread_id(message)
            if message_thread_id != thread_id:
                continue
            observed.append(
                TelegramForumObservedMessage(
                    message_id=message.id,
                    text=message.text or "",
                    reply_to_message_id=getattr(
                        getattr(message, "reply_to", None),
                        "reply_to_msg_id",
                        None,
                    ),
                    thread_id=message_thread_id,
                )
            )
            if len(observed) >= limit:
                break
        observed.reverse()
        self._remember_messages(observed)
        return observed

    async def send(
        self,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        timeout: float | None = None,
        require_done: bool = True,
    ) -> TelegramForumResponseTrace:
        if self._client is None:
            raise RuntimeError("TelegramForumPlatform not connected")
        baseline = await self.latest_bot_message_id(thread_id=thread_id)
        send_kwargs: dict[str, Any] = {}
        if reply_to_message_id is not None:
            send_kwargs["reply_to"] = reply_to_message_id
        elif thread_id is not None:
            send_kwargs["reply_to"] = thread_id
        sent = await self._client.send_message(self._chat_id, text, **send_kwargs)
        trace = await self._collect_response_trace(
            after_message_id=max(baseline, getattr(sent, "id", 0)),
            thread_id=thread_id,
            timeout=timeout or self._timeout,
            require_done=require_done,
            sent_message_id=getattr(sent, "id", None),
        )
        self._last_output = trace.output
        return trace

    async def send_control(
        self,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        timeout: float = 20.0,
    ) -> TelegramForumResponseTrace:
        return await self.send(
            text,
            thread_id=thread_id,
            reply_to_message_id=reply_to_message_id,
            timeout=timeout,
            require_done=False,
        )

    async def wait_for_prompt(
        self,
        *,
        thread_id: int | None = None,
        timeout: float | None = None,
        require_done: bool = True,
    ) -> TelegramForumResponseTrace:
        baseline = await self.latest_bot_message_id(thread_id=thread_id)
        return await self.wait_for_prompt_after(
            after_message_id=baseline,
            thread_id=thread_id,
            timeout=timeout,
            require_done=require_done,
        )

    async def wait_for_prompt_after(
        self,
        *,
        after_message_id: int,
        thread_id: int | None = None,
        timeout: float | None = None,
        require_done: bool = True,
    ) -> TelegramForumResponseTrace:
        trace = await self._collect_response_trace(
            after_message_id=after_message_id,
            thread_id=thread_id,
            timeout=timeout or self._timeout,
            require_done=require_done,
            sent_message_id=None,
        )
        self._last_output = trace.output
        return trace

    async def send_nowait(
        self,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        if self._client is None:
            raise RuntimeError("TelegramForumPlatform not connected")
        send_kwargs: dict[str, Any] = {}
        if reply_to_message_id is not None:
            send_kwargs["reply_to"] = reply_to_message_id
        elif thread_id is not None:
            send_kwargs["reply_to"] = thread_id
        sent = await self._client.send_message(self._chat_id, text, **send_kwargs)
        return getattr(sent, "id", None)

    async def wait_for_silence(
        self,
        *,
        thread_id: int | None = None,
        seconds: float,
    ) -> None:
        before = await self.latest_bot_message_id(thread_id=thread_id)
        await asyncio.sleep(seconds)
        after = await self.latest_bot_message_id(thread_id=thread_id)
        if after != before:
            recent = await self.get_recent_messages(thread_id=thread_id, limit=10)
            raise AssertionError(
                f"Expected silence in thread {thread_id}, but new bot messages arrived:\n"
                + "\n".join(
                    f"id={message.message_id} reply_to={message.reply_to_message_id} text={message.text!r}"
                    for message in recent
                )
            )

    def format_recent_messages(self, *, limit: int = 20) -> str:
        recent = self._recent_messages[-limit:]
        if not recent:
            return "(no observed forum messages)"
        return "\n".join(
            f"id={message.message_id} thread={message.thread_id} reply_to={message.reply_to_message_id} text={message.text!r}"
            for message in recent
        )

    async def _collect_response_trace(
        self,
        *,
        after_message_id: int,
        thread_id: int | None,
        timeout: float,
        require_done: bool,
        sent_message_id: int | None,
    ) -> TelegramForumResponseTrace:
        observed_messages: list[TelegramForumObservedMessage] = []
        first_budget = min(timeout, self._first_message_timeout)
        done_budget = min(timeout, self._done_timeout)
        idle_budget = self._idle_quiescence_timeout
        first_deadline = asyncio.get_running_loop().time() + first_budget
        done_deadline = asyncio.get_running_loop().time() + done_budget
        seen_ids: set[int] = set()

        while True:
            new_messages = await self._fetch_new_messages(
                after_message_id=after_message_id,
                thread_id=thread_id,
                seen_ids=seen_ids,
            )
            if new_messages:
                observed_messages.extend(new_messages)
                break
            if asyncio.get_running_loop().time() >= first_deadline:
                return TelegramForumResponseTrace(
                    sent_message_id=sent_message_id,
                    output=f"(timeout: no response from bot after {first_budget:.0f}s)",
                    messages=[],
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        if _is_completion_message(observed_messages[-1].text):
            return TelegramForumResponseTrace(
                sent_message_id=sent_message_id,
                output="\n".join(message.text for message in observed_messages),
                messages=observed_messages,
            )

        if not require_done:
            quiet_deadline = asyncio.get_running_loop().time() + _CONTROL_SETTLE_SECONDS
            while True:
                new_messages = await self._fetch_new_messages(
                    after_message_id=after_message_id,
                    thread_id=thread_id,
                    seen_ids=seen_ids,
                )
                if new_messages:
                    observed_messages.extend(new_messages)
                    quiet_deadline = asyncio.get_running_loop().time() + _CONTROL_SETTLE_SECONDS
                elif asyncio.get_running_loop().time() >= quiet_deadline:
                    break
                else:
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            return TelegramForumResponseTrace(
                sent_message_id=sent_message_id,
                output="\n".join(message.text for message in observed_messages),
                messages=observed_messages,
            )

        idle_deadline = asyncio.get_running_loop().time() + idle_budget
        while True:
            if _is_completion_message(observed_messages[-1].text):
                break
            if asyncio.get_running_loop().time() >= done_deadline:
                return TelegramForumResponseTrace(
                    sent_message_id=sent_message_id,
                    output=(
                        f"(timeout: missing completion marker after {done_budget:.0f}s; "
                        f"{len(observed_messages)} chunk(s) received)"
                    ),
                    messages=observed_messages,
                )
            new_messages = await self._fetch_new_messages(
                after_message_id=after_message_id,
                thread_id=thread_id,
                seen_ids=seen_ids,
            )
            if new_messages:
                observed_messages.extend(new_messages)
                idle_deadline = asyncio.get_running_loop().time() + idle_budget
            elif asyncio.get_running_loop().time() >= idle_deadline:
                return TelegramForumResponseTrace(
                    sent_message_id=sent_message_id,
                    output=(
                        f"(timeout: missing completion marker after {idle_budget:.0f}s idle; "
                        f"{len(observed_messages)} chunk(s) received)"
                    ),
                    messages=observed_messages,
                )
            else:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        return TelegramForumResponseTrace(
            sent_message_id=sent_message_id,
            output="\n".join(message.text for message in observed_messages),
            messages=observed_messages,
        )

    async def _fetch_new_messages(
        self,
        *,
        after_message_id: int,
        thread_id: int | None,
        seen_ids: set[int],
    ) -> list[TelegramForumObservedMessage]:
        recent = await self.get_recent_messages(thread_id=thread_id, limit=60)
        new_messages = [
            message
            for message in recent
            if message.message_id > after_message_id and message.message_id not in seen_ids
        ]
        for message in new_messages:
            seen_ids.add(message.message_id)
        return new_messages

    def _remember_messages(self, messages: list[TelegramForumObservedMessage]) -> None:
        self._recent_messages.extend(messages)
        self._recent_messages = self._recent_messages[-400:]
