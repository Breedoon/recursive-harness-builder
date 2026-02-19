"""Spike: Prove Telethon can send a message to a bot and read the response.

Prerequisites:
1. Fill in .env with TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION, TELEGRAM_TEST_BOT_USERNAME
2. The test bot must be running (or at least created via BotFather)
3. You must have sent /start to the test bot from the secondary account at least once

Run:
    .venv/bin/python spikes/telethon_spike.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Load .env manually (no extra dependency)
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value and key.strip() not in os.environ:
                os.environ[key.strip()] = value.strip()

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
SESSION = os.environ.get("TELEGRAM_SESSION")
BOT_USERNAME = os.environ.get("TELEGRAM_TEST_BOT_USERNAME")

def check_env():
    missing = []
    if not API_ID:
        missing.append("TELEGRAM_API_ID")
    if not API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not SESSION:
        missing.append("TELEGRAM_SESSION")
    if not BOT_USERNAME:
        missing.append("TELEGRAM_TEST_BOT_USERNAME")
    if missing:
        print(f"Missing env vars: {', '.join(missing)}")
        print("Fill in .env first, then re-run.")
        sys.exit(1)


async def main():
    check_env()

    client = TelegramClient(
        StringSession(SESSION),
        int(API_ID),
        API_HASH,
        sequential_updates=True,
    )

    await client.connect()
    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (ID: {me.id})")

    # Resolve the bot
    bot = await client.get_entity(BOT_USERNAME)
    print(f"Bot resolved: {bot.first_name} (@{bot.username})")

    # Send a test message
    test_message = "Hello from Telethon spike! Can you hear me?"
    print(f"\nSending: {test_message}")
    await client.send_message(bot, test_message)

    # Wait for the bot's response (poll for up to 60 seconds)
    print("Waiting for bot response (up to 60s)...")
    response_text = None

    for attempt in range(60):
        await asyncio.sleep(1)
        # Get recent messages from the bot chat
        messages = await client.get_messages(bot, limit=5)
        for msg in messages:
            # Find the bot's reply (not our own message)
            if msg.out is False and msg.text:
                response_text = msg.text
                break
        if response_text:
            break
        if attempt % 10 == 9:
            print(f"  Still waiting... ({attempt + 1}s)")

    if response_text:
        print(f"\nBot responded ({len(response_text)} chars):")
        print("-" * 60)
        # Show first 500 chars
        if len(response_text) > 500:
            print(response_text[:500] + "\n... (truncated)")
        else:
            print(response_text)
        print("-" * 60)
        print("\nSPIKE PASSED: Telethon can talk to the bot and read responses.")
    else:
        print("\nNo response received within 60s.")
        print("This is expected if the bot isn't running yet.")
        print("The important thing: the message WAS sent successfully.")
        print("Once the bot is running, it will respond.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
