"""Entry point for running the Telegram bot standalone.

Usage:
    python -m obs_agent.telegram_main
"""

import asyncio
import logging

from obs_agent.config import OBSConfig
from obs_agent.telegram import run_telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def main() -> None:
    config = OBSConfig.from_env()
    config.validate()
    asyncio.run(run_telegram_bot(config))


if __name__ == "__main__":
    main()
