"""Spike: bypass SessionManager and connect SDK directly inside PTB handler."""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("spike")

from obs_agent.config import OBSConfig
from obs_agent.prompt import build_system_prompt
from obs_agent.hooks import HookState, create_hook_matchers
from obs_agent.tools import create_obs_tools

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, TextBlock

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

config = OBSConfig.from_env()
hook_state = HookState()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message is None or update.effective_message.text is None:
        return
    user_text = update.effective_message.text
    logger.info("Got message: %s", user_text)

    # Build options
    system_prompt = build_system_prompt(config)
    hook_matchers = create_hook_matchers(config, hook_state)
    tool_server = create_obs_tools(config, lambda: None, hook_state=hook_state)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        hooks=hook_matchers,
        mcp_servers={"obs-agent": tool_server},
        cwd=str(config.vault_path),
        permission_mode="bypassPermissions",
        setting_sources=["project"],
    )

    # Connect directly (no create_task wrapper)
    logger.info("Creating client...")
    client = ClaudeSDKClient(options)
    logger.info("Connecting (direct await, no create_task)...")
    await client.connect()
    logger.info("Connected!")

    logger.info("Querying...")
    await client.query(user_text)

    logger.info("Receiving response...")
    text_parts = []
    async for message in client.receive_response():
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                    logger.info("Text: %s", block.text[:100])

    full_text = "\n".join(text_parts) or "(no response)"
    logger.info("Sending reply (%d chars)...", len(full_text))

    await update.effective_message.reply_text(full_text[:4000], disable_web_page_preview=True)
    logger.info("Done!")

    await client.disconnect()


async def main():
    app = Application.builder().token(config.telegram_bot_token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot started. Send a message!")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

asyncio.run(main())
