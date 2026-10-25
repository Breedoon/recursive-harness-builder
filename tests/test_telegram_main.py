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
