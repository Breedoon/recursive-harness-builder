"""Test: can ConversationRunner work inside python-telegram-bot's event loop?

This simulates the exact code path: PTB handler → FragmentBuffer create_task → _process_message → runner.run()
"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("test")

from obs_agent.config import OBSConfig
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, TextEvent, DoneEvent
from obs_agent.session import SessionManager
from obs_agent.events import StatusEvent


async def simulate_process_message():
    """Simulate what _process_message does."""
    config = OBSConfig.from_env()
    hook_state = HookState()
    session_mgr = SessionManager(config=config, hook_state=hook_state)

    logger.info("Getting client...")
    try:
        client = await session_mgr.get_client()
        logger.info("Client connected: %s", client)
    except Exception:
        logger.exception("Failed to get client")
        return

    runner = ConversationRunner(session_mgr, hook_state, config)

    logger.info("Starting runner.run()...")
    try:
        text_parts = []
        async for event in runner.run("Hello"):
            if isinstance(event, TextEvent):
                text_parts.append(event.text)
                logger.info("TextEvent: %s", event.text[:100])
            elif isinstance(event, DoneEvent):
                logger.info("DoneEvent")
                break
        logger.info("Runner done, %d parts", len(text_parts))
    except Exception:
        logger.exception("Runner error")


async def main():
    # Simulate PTB's pattern: handler fires create_task for the actual work
    logger.info("Simulating PTB handler → create_task → process_message")
    task = asyncio.create_task(simulate_process_message())

    # Meanwhile, simulate the polling loop continuing
    for i in range(30):
        await asyncio.sleep(1)
        logger.info("Polling tick %d (task done=%s)", i, task.done())
        if task.done():
            # Check for exception
            if task.exception():
                logger.error("Task exception: %s", task.exception())
            break

    if not task.done():
        logger.warning("Task still running after 30s — hanging!")
        task.cancel()


asyncio.run(main())
