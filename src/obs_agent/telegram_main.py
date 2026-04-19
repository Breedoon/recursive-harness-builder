"""Entry point for running the Telegram bot standalone.

Usage:
    python -m obs_agent.telegram_main
"""

import asyncio
import logging
import os
from pathlib import Path

from obs_agent.config import OBSConfig
from obs_agent.runtime_env import bootstrap_runtime_env
from obs_agent.telegram import run_telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


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
    bootstrap_runtime_env()
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

    asyncio.run(run_telegram_bot(config))


if __name__ == "__main__":
    main()
