"""Send a test message to the prod bot using Telethon (as the test user account)."""
import os
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session = os.environ["TELEGRAM_SESSION"]

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()

    me = await client.get_me()
    print(f"Logged in as: {me.first_name} (ID: {me.id})")

    # Send message to prod bot
    entity = await client.get_entity("obsprodbot")
    print(f"Bot entity: {entity.first_name} (@{entity.username}, ID: {entity.id})")

    msg = await client.send_message(entity, "Hello from Telethon test!")
    print(f"Sent message ID: {msg.id}")

    await client.disconnect()

asyncio.run(main())
