"""Test ConversationRunner directly — no Telegram, just the SDK path."""
import asyncio
import logging

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from obs_agent.config import OBSConfig
from obs_agent.hooks import HookState
from obs_agent.runner import ConversationRunner, TextEvent, DoneEvent
from obs_agent.session import SessionManager
from obs_agent.events import StatusEvent

async def main():
    config = OBSConfig.from_env()
    hook_state = HookState()
    session_mgr = SessionManager(config=config, hook_state=hook_state)

    runner = ConversationRunner(session_mgr, hook_state, config)

    print("--- Starting runner.run('Hello, who are you?') ---")
    try:
        async for event in runner.run("Hello, who are you?"):
            if isinstance(event, TextEvent):
                print(f"[TEXT] {event.text[:200]}")
            elif isinstance(event, StatusEvent):
                print(f"[STATUS] {event.type}: {event.summary}")
            elif isinstance(event, DoneEvent):
                print("[DONE]")
                break
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    print("--- Runner finished ---")

asyncio.run(main())
