"""Debug Telegram bot: production TelegramBot class with extra logging."""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("debug_bot2")

from obs_agent.config import OBSConfig
from obs_agent.telegram import TelegramBot
from telegram.ext import Application, MessageHandler, filters

config = OBSConfig.from_env()
config.validate()

# Test with fragment_gap=0 to eliminate buffering
bot = TelegramBot(config, fragment_gap=0.01)

app = Application.builder().token(config.telegram_bot_token).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))


async def main():
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot is ready (fragment_gap=0.01)!")
    stop = asyncio.Event()
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
