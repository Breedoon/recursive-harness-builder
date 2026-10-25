"""Entry point for running the Telegram bot standalone.

Usage:
    python -m obs_agent.telegram_main
"""

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable
from pathlib import Path

from obs_agent.config import OBSConfig
from obs_agent.runtime_env import (
    LegacyFormalTestRedirect,
    assert_live_entrypoint_allowed,
    bootstrap_runtime_env,
)
from obs_agent.telegram import run_telegram_bot

# asyncio.run() cancels every task when the Telegram runtime ends and waits for
# all of them. One that never finishes (a send retrying the closed HTTPX
# transport after a fatal getUpdates Conflict) kept the daemon silent but
# RUNNING for hours, so Supervisor never restarted it (obs-22.61). Past the
# grace period the process hard-exits instead; os._exit skips the SIGTERM kill
# switch, so the crash snapshot survives and the wrapper's restart resumes it.
_DEFAULT_TEARDOWN_GRACE_SECONDS = 60.0
_TEARDOWN_EXIT_CODE = 70


def _teardown_grace_seconds() -> float:
    raw = (os.environ.get("OBS_TELEGRAM_TEARDOWN_GRACE_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TEARDOWN_GRACE_SECONDS
    return value if value > 0 else _DEFAULT_TEARDOWN_GRACE_SECONDS


def _arm_teardown_deadline(grace_seconds: float) -> None:
    def _expire() -> None:
        os.write(
            2,
            f"[teardown_deadline] runtime teardown still running after {grace_seconds:g}s; "
            f"hard exit {_TEARDOWN_EXIT_CODE} so Supervisor restarts OBS (obs-22.61)\n".encode(),
        )
        os._exit(_TEARDOWN_EXIT_CODE)

    timer = threading.Timer(grace_seconds, _expire)
    timer.daemon = True
    timer.start()


async def _run_with_bounded_teardown(runtime: Awaitable[None], grace_seconds: float) -> None:
    try:
        await runtime
    finally:
        _arm_teardown_deadline(grace_seconds)


class _DropGetUpdatesFilter(logging.Filter):
    """Suppress high-volume polling lines while keeping other runtime logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "getUpdates" not in record.getMessage()


def _attach_runtime_file_handler() -> None:
    """Attach a file handler when OBS runtime log path is configured."""
    raw_path = (
        (os.environ.get("OBS_RUNTIME_LOG_FILE") or "").strip()
        or (os.environ.get("OBS_TELEGRAM_LOG_FILE") or "").strip()
    )
    if not raw_path:
        return

    log_path = Path(raw_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    normalized_target = log_path.resolve(strict=False)
    for handler in root_logger.handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        existing_path = Path(getattr(handler, "baseFilename", "")).resolve(strict=False)
        if existing_path == normalized_target:
            return

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    root_logger.addHandler(file_handler)


def _configure_logging() -> None:
    """Apply pragmatic Telegram logging defaults for daemon usage."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _attach_runtime_file_handler()

    keep_poll_logs = (os.environ.get("OBS_TELEGRAM_LOG_POLLING") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        return
    if keep_poll_logs:
        return
    for handler in root_logger.handlers:
        if any(isinstance(active_filter, _DropGetUpdatesFilter) for active_filter in handler.filters):
            continue
        handler.addFilter(_DropGetUpdatesFilter())


def main() -> None:
    try:
        assert_live_entrypoint_allowed()
    except LegacyFormalTestRedirect as exc:
        raise SystemExit(str(exc)) from exc
    bootstrap_runtime_env()
    try:
        assert_live_entrypoint_allowed()
    except LegacyFormalTestRedirect as exc:
        raise SystemExit(str(exc)) from exc
    _configure_logging()
    config = OBSConfig.from_env()
    config.validate()

    # Start cache-normalizing proxy before any CC sessions
    from obs_agent.cache_proxy_lifecycle import should_attempt_proxy_start, start_cache_proxy
    if should_attempt_proxy_start(cache_proxy_enabled=config.cache_proxy_enabled):
        proxy_proc = start_cache_proxy(config.cache_proxy_port)
        if proxy_proc is None:
            logging.getLogger("obs_agent.cache_proxy").warning(
                "Cache proxy failed to start — sessions will use direct Anthropic API"
            )

    # Record exactly which code is now live, for the cache-prefix gate (vault-3px5).
    try:
        from pathlib import Path as _Path

        from obs_agent.cache_gate import write_running_record

        write_running_record(_Path(__file__).resolve().parents[2])
    except Exception:
        logging.getLogger("obs_agent.cache_gate").warning("could not record running code", exc_info=True)

    asyncio.run(_run_with_bounded_teardown(run_telegram_bot(config), _teardown_grace_seconds()))


if __name__ == "__main__":
    main()
