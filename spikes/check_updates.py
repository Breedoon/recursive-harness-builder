"""Check if Telegram bot receives updates - raw API call with long poll."""
import os
import httpx

token = os.environ["OBS_TELEGRAM_PROD_BOT_TOKEN"]

# First check bot identity
r = httpx.get(f"https://api.telegram.org/bot{token}/getMe")
print("Bot info:", r.json())

# Check for pending updates with a 15s long poll
print("\nWaiting 15s for updates... Send a message to the bot NOW")
r = httpx.post(
    f"https://api.telegram.org/bot{token}/getUpdates",
    json={"timeout": 15, "offset": 0},
    timeout=30,
)
data = r.json()
print(f"\ngetUpdates response (ok={data['ok']}, {len(data['result'])} updates):")
for u in data["result"][:5]:
    print(f"  update_id={u['update_id']}")
    if "message" in u:
        msg = u["message"]
        print(f"  from: {msg.get('from', {}).get('id')} ({msg.get('from', {}).get('username')})")
        print(f"  text: {msg.get('text', '(no text)')}")
    else:
        print(f"  keys: {list(u.keys())}")

if not data["result"]:
    print("\nNo updates received. Possible causes:")
    print("  1. You messaged the wrong bot (this is @obsprodbot)")
    print("  2. Another polling instance consumed the updates")
    print("  3. You haven't sent /start to the bot yet")
