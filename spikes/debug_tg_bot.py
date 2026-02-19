"""Debug Telegram bot: minimal handler with verbose logging."""

import asyncio
import logging
import sys
import traceback

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("debug_bot")

from obs_agent.config import OBSConfig
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, TextEvent, DoneEvent
from obs_agent.session import SessionManager
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

config = OBSConfig.from_env()
config.validate()

hook_state = HookState()
session_mgr = SessionManager(config=config, hook_state=hook_state)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    logger.info("GOT MESSAGE: %s", text)
    try:
        runner = ConversationRunner(session_mgr, hook_state, config)
        parts = []
        logger.info("Starting runner...")
        async for event in runner.run(text):
            if isinstance(event, TextEvent):
                parts.append(event.text)
                logger.info("TEXT EVENT: %s", event.text[:100])
            elif isinstance(event, DoneEvent):
                logger.info("DONE EVENT")
                break
        response = "\n".join(parts) or "(no response)"
        logger.info("SENDING REPLY: %s", response[:100])
        await update.effective_message.reply_text(response)
        logger.info("REPLY SENT")
    except Exception as e:
        logger.error("EXCEPTION: %s", e)
        logger.error(traceback.format_exc())
        try:
            await update.effective_message.reply_text(f"Error: {e}")
        except Exception:
            pass


app = Application.builder().token(config.telegram_bot_token).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


async def main():
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot is ready!")
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
