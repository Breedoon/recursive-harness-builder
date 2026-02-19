"""Quick debug script to run the Telegram bot with verbose logging."""
import asyncio
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from obs_agent.config import OBSConfig
from obs_agent.telegram import run_telegram_bot

asyncio.run(run_telegram_bot(OBSConfig.from_env()))
