"""Tests for Telegram daemon entrypoint logging behavior."""

import logging

from obs_agent.telegram_main import _DropGetUpdatesFilter


def _log_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_polling_filter_drops_get_updates_lines() -> None:
    drop_filter = _DropGetUpdatesFilter()
    record = _log_record(
        'HTTP Request: POST https://api.telegram.org/bot123/getUpdates "HTTP/1.1 200 OK"'
    )
    assert drop_filter.filter(record) is False


def test_polling_filter_keeps_non_polling_lines() -> None:
    drop_filter = _DropGetUpdatesFilter()
    record = _log_record(
        'HTTP Request: POST https://api.telegram.org/bot123/sendMessage "HTTP/1.1 200 OK"'
    )
    assert drop_filter.filter(record) is True
