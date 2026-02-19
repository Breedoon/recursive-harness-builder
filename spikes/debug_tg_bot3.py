"""Debug Telegram bot: TelegramBot class but bypass FragmentBuffer."""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("debug_bot3")

from obs_agent.config import OBSConfig
from obs_agent.telegram import TelegramBot
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

config = OBSConfig.from_env()
config.validate()

bot = TelegramBot(config)


async def handle_message_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Call _process_message directly, bypassing FragmentBuffer."""
    if update.effective_message is None or update.effective_message.text is None:
        return
    text = update.effective_message.text
    logger.info("DIRECT HANDLER: %s", text)
    await bot._process_message(text, update, context)
    logger.info("DIRECT HANDLER DONE")


app = Application.builder().token(config.telegram_bot_token).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_direct))


async def main():
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot is ready (direct handler)!")
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
