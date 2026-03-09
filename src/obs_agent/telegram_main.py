"""Entry point for running the Telegram bot standalone.

Usage:
    python -m obs_agent.telegram_main
"""

import asyncio
import logging
import os
from pathlib import Path

from obs_agent.config import OBSConfig
from obs_agent.telegram import run_telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


class _DropGetUpdatesFilter(logging.Filter):
    """Suppress high-volume polling lines while keeping other runtime logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "getUpdates" not in record.getMessage()


def _configure_logging() -> None:
    """Apply pragmatic Telegram logging defaults for daemon usage."""
    keep_poll_logs = (os.environ.get("OBS_TELEGRAM_LOG_POLLING") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if keep_poll_logs:
        return
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        return
    for handler in root_logger.handlers:
        if any(isinstance(active_filter, _DropGetUpdatesFilter) for active_filter in handler.filters):
            continue
        handler.addFilter(_DropGetUpdatesFilter())


def _load_dotenv_if_present() -> None:
    """Load .env vars into os.environ if they are not already set."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.exists():
        return

    for line in env_file.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def main() -> None:
    _load_dotenv_if_present()
    _configure_logging()
    config = OBSConfig.from_env()
    config.validate()
    asyncio.run(run_telegram_bot(config))


if __name__ == "__main__":
    main()
