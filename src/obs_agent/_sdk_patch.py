"""Local SDK compatibility patches."""

from __future__ import annotations

from claude_agent_sdk._internal import message_parser

_PATCHED = False


def ensure_raw_uuid_patch() -> None:
    """Preserve raw JSONL UUIDs on streamed SDK message objects.

    The SDK parser currently drops ``uuid`` for most typed message variants even
    though it is present in the raw JSON payload. We attach it as ``_raw_uuid``
    so transport adapters can correlate rendered output back to JSONL entries.
    """

    global _PATCHED
    if _PATCHED:
        return

    original_parse = message_parser.parse_message

    def _parse_with_uuid(data):
        message = original_parse(data)
        if message is not None and isinstance(data, dict):
            raw_uuid = data.get("uuid")
            if isinstance(raw_uuid, str) and raw_uuid:
                setattr(message, "_raw_uuid", raw_uuid)
        return message

    message_parser.parse_message = _parse_with_uuid
    _PATCHED = True
