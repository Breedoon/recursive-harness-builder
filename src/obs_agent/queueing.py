"""Shared queue payload helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueuedMessage:
    """A user message waiting to be injected into a later turn."""

    text: str
    telegram_message_id: int | None = None
    reply_to_message_id: int | None = None


def coerce_queued_message(item: QueuedMessage | str) -> QueuedMessage:
    """Normalize legacy string queue items into ``QueuedMessage`` objects."""

    if isinstance(item, QueuedMessage):
        return item
    return QueuedMessage(text=str(item))


def queued_texts(items: list[QueuedMessage | str]) -> list[str]:
    """Extract plain message text for prompts and status payloads."""

    return [coerce_queued_message(item).text for item in items]
