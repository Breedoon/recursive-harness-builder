"""Generate a Telethon StringSession for the secondary test account.

Run this once interactively:
    .venv/bin/python spikes/generate_session.py

It will:
1. Ask for your API ID and API hash (from https://my.telegram.org)
2. Ask for the phone number of your SECONDARY test account
3. Send a verification code to that account's Telegram app (or SMS)
4. Ask you to enter the code
5. Print a StringSession string — paste this into .env as TELEGRAM_SESSION
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API ID (from my.telegram.org): "))
api_hash = input("API hash (from my.telegram.org): ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    me = client.get_me()
    print(f"\nLogged in as: {me.first_name} (ID: {me.id})")
    print(f"\nYour StringSession (paste into .env as TELEGRAM_SESSION):\n")
    print(client.session.save())
