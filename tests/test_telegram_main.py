"""Tests for Telegram daemon entrypoint logging behavior."""

import logging
import subprocess
import sys
from unittest.mock import patch

import pytest

from obs_agent.telegram_main import (
    _DEFAULT_TEARDOWN_GRACE_SECONDS,
    _TEARDOWN_EXIT_CODE,
    _DropGetUpdatesFilter,
    _configure_logging,
    _teardown_grace_seconds,
    main,
)


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


@pytest.mark.parametrize(
    "argv",
    [
        ["telegram-main", "--test"],
        ["telegram-main", "--test-instance"],
        ["telegram-main", "--profile", "test"],
    ],
)
def test_telegram_entrypoint_redirects_before_logging_config_proxy_or_poller(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    with patch(
        "obs_agent.telegram_main._configure_logging",
        side_effect=AssertionError("logging side effect must not run"),
    ), patch(
        "obs_agent.telegram_main.run_telegram_bot",
        side_effect=AssertionError("poller factory must not run"),
    ):
        with pytest.raises(SystemExit, match="obs-live-test"):
            main()


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


_TEARDOWN_CHILD = """
import asyncio, sys
from obs_agent.telegram_main import _run_with_bounded_teardown

async def stubborn():
    # Swallows every cancellation, like a send retrying a closed transport.
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            continue

async def runtime(hang):
    if hang:
        asyncio.get_running_loop().create_task(stubborn())
        await asyncio.sleep(0)
    raise RuntimeError("fatal polling conflict")

asyncio.run(_run_with_bounded_teardown(runtime(sys.argv[1] == "hang"), grace_seconds=0.5))
"""


def _run_teardown_child(mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _TEARDOWN_CHILD, mode],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_teardown_that_never_finishes_hard_exits_so_supervisor_restarts() -> None:
    # obs-22.61: a fatal Conflict left asyncio.run gathering a task that never
    # finished cancelling, so the daemon stayed silent but RUNNING for hours.
    result = _run_teardown_child("hang")
    assert result.returncode == _TEARDOWN_EXIT_CODE
    assert "[teardown_deadline]" in result.stderr


def test_normal_teardown_exits_with_the_runtime_error_before_the_deadline() -> None:
    result = _run_teardown_child("clean")
    assert result.returncode == 1
    assert "fatal polling conflict" in result.stderr
    assert "[teardown_deadline]" not in result.stderr


def test_teardown_grace_reads_env_and_falls_back_on_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBS_TELEGRAM_TEARDOWN_GRACE_SECONDS", "5")
    assert _teardown_grace_seconds() == 5.0
    for bad in ("", "nope", "0", "-3"):
        monkeypatch.setenv("OBS_TELEGRAM_TEARDOWN_GRACE_SECONDS", bad)
        assert _teardown_grace_seconds() == _DEFAULT_TEARDOWN_GRACE_SECONDS


_FAKE_TOKEN = "1234567890:AAFakeFakeFakeFakeFakeFakeFakeFake12"


def test_redact_bot_tokens_masks_url_and_bare_forms() -> None:
    from obs_agent.telegram_main import redact_bot_tokens

    url = f"POST https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage"
    assert redact_bot_tokens(url) == "POST https://api.telegram.org/bot<redacted>/sendMessage"
    assert _FAKE_TOKEN not in redact_bot_tokens(f"token={_FAKE_TOKEN} end")
    plain = "route=TelegramRoute(chat_id=-1003780712622, thread_id=743440) 2026-11-24 10:48:03"
    assert redact_bot_tokens(plain) == plain


def test_configure_logging_quiets_http_clients_and_redacts_tokens(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "telegram-prod.log"
    monkeypatch.setenv("OBS_RUNTIME_LOG_FILE", str(log_file))
    monkeypatch.delenv("OBS_TELEGRAM_LOG_FILE", raising=False)
    monkeypatch.delenv("OBS_TELEGRAM_LOG_POLLING", raising=False)

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    original_formatters = {h: h.formatter for h in original_handlers}
    original_level = root_logger.level
    client_levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore", "telegram.request")}
    for name in client_levels:
        logging.getLogger(name).setLevel(logging.NOTSET)
    root_logger.setLevel(logging.INFO)

    try:
        _configure_logging()
        for name in client_levels:
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot%s/sendMessage", _FAKE_TOKEN)
        logging.getLogger("telegram.ext").warning("NetworkError at https://api.telegram.org/bot%s/getMe", _FAKE_TOKEN)
        try:
            raise RuntimeError(f"boom https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage")
        except RuntimeError:
            logging.getLogger("obs_agent.telegram").exception("send failed")
        _configure_logging()  # idempotent: no double wrapping
        for handler in root_logger.handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert _FAKE_TOKEN not in text
        assert "HTTP Request" not in text
        assert "NetworkError at https://api.telegram.org/bot<redacted>/getMe" in text
        assert "boom https://api.telegram.org/bot<redacted>/sendMessage" in text
        assert "send failed" in text
    finally:
        for handler in list(root_logger.handlers):
            if handler not in original_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        for handler, formatter in original_formatters.items():
            handler.setFormatter(formatter)
        root_logger.setLevel(original_level)
        for name, level in client_levels.items():
            logging.getLogger(name).setLevel(level)
