"""Run bot with monkey-patched _process_message for tracing."""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("trace")

from obs_agent.config import OBSConfig
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, TextEvent, DoneEvent
from obs_agent.session import SessionManager
from obs_agent.events import StatusEvent
from obs_agent.telegram import TelegramBot, _typing_loop, _CHUNK_DELAY_SECONDS
from obs_agent.telegram_format import md_to_telegram_html, split_message

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, ContextTypes, MessageHandler, filters


async def traced_process_message(self, user_text, update, context):
    """Traced version of _process_message."""
    chat_id = update.effective_message.chat_id
    logger.info("TRACE: start processing '%s'", user_text[:50])

    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(chat_id, context.bot, typing_stop))

    try:
        logger.info("TRACE: creating runner")
        runner = ConversationRunner(
            self._session_manager,
            self._hook_state,
            self._config,
            pending_messages=self._pending_messages,
        )

        logger.info("TRACE: calling runner.run()")
        text_parts = []
        async for event in runner.run(user_text):
            logger.info("TRACE: got event %s", type(event).__name__)
            if isinstance(event, TextEvent):
                text_parts.append(event.text)
            elif isinstance(event, DoneEvent):
                break

        logger.info("TRACE: runner done, %d parts", len(text_parts))
        self._pending_messages = runner.remaining_pending
    except Exception:
        logger.exception("TRACE: runner CRASHED")
        try:
            await update.effective_message.reply_text("(error)")
        except Exception:
            pass
        return
    finally:
        typing_stop.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    full_text = "\n".join(text_parts)
    if not full_text.strip():
        full_text = "(no response)"

    logger.info("TRACE: formatting response (%d chars)", len(full_text))
    html = md_to_telegram_html(full_text)
    chunks = split_message(html)

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(_CHUNK_DELAY_SECONDS)
        try:
            logger.info("TRACE: sending chunk %d/%d (%d chars)", i+1, len(chunks), len(chunk))
            await update.effective_message.reply_text(
                chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            logger.info("TRACE: chunk sent successfully")
        except BadRequest as e:
            if "can't parse entities" in str(e).lower():
                logger.warning("TRACE: HTML failed, sending plain")
                await update.effective_message.reply_text(full_text, disable_web_page_preview=True)
            else:
                raise

    logger.info("TRACE: done!")


# Monkey-patch
TelegramBot._process_message = traced_process_message

config = OBSConfig.from_env()
bot = TelegramBot(config)
app = Application.builder().token(config.telegram_bot_token).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))


async def main():
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot started, waiting for messages...")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

asyncio.run(main())
