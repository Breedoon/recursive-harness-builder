"""Tests for Telegram daemon entrypoint logging behavior."""

import logging

from obs_agent.telegram_main import _DropGetUpdatesFilter, _configure_logging


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


def test_configure_logging_attaches_runtime_file_handler(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "logs" / "prod" / "telegram-main.log"
    monkeypatch.setenv("OBS_RUNTIME_LOG_FILE", str(log_file))
    monkeypatch.delenv("OBS_TELEGRAM_LOG_FILE", raising=False)
    monkeypatch.delenv("OBS_TELEGRAM_LOG_POLLING", raising=False)

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    root_logger.setLevel(logging.INFO)

    try:
        _configure_logging()
        logger = logging.getLogger("obs_agent.telegram")
        logger.info("runtime log smoke")
        for handler in root_logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.flush()
        assert log_file.exists()
        assert "runtime log smoke" in log_file.read_text(encoding="utf-8")
    finally:
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(original_level)
