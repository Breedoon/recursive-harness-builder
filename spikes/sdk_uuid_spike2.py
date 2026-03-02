"""Spike 2: Check if raw message data has UUIDs that the SDK types drop.

Monkey-patch the message parser to capture raw data before parsing.
"""

import asyncio
import sys
import os

sys.stdout.reconfigure(line_buffering=True)

print("SPIKE2 STARTING", flush=True)

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal import message_parser

# Monkey-patch to capture raw data
_original_parse = message_parser.parse_message
_raw_messages = []

def _capturing_parse(data):
    _raw_messages.append(dict(data) if isinstance(data, dict) else data)
    return _original_parse(data)

message_parser.parse_message = _capturing_parse


async def main():
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        max_turns=3,
    )
    client = ClaudeSDKClient(options)
    await client.connect()

    prompt = "What is today's date? Use Bash to run `date +%Y-%m-%d` and tell me."
    print(f">>> Sending: {prompt}", flush=True)
    await client.query(prompt)

    msg_index = 0
    async for message in client.receive_response():
        msg_index += 1

    await client.disconnect()

    print(f"\n=== RAW DATA FOR ALL {len(_raw_messages)} MESSAGES ===\n", flush=True)
    for i, raw in enumerate(_raw_messages):
        if not isinstance(raw, dict):
            print(f"#{i+1}: non-dict: {type(raw)}", flush=True)
            continue
        msg_type = raw.get("type", "?")
        uuid = raw.get("uuid", "NOT_PRESENT")
        parent_uuid = raw.get("parentUuid", "NOT_PRESENT")
        session_id = raw.get("sessionId", raw.get("session_id", "NOT_PRESENT"))

        print(f"#{i+1} type={msg_type}", flush=True)
        print(f"  uuid:       {uuid}", flush=True)
        print(f"  parentUuid: {parent_uuid}", flush=True)
        print(f"  sessionId:  {session_id}", flush=True)

        # Show content summary
        msg = raw.get("message", {})
        if isinstance(msg, dict):
            role = msg.get("role", "?")
            content = msg.get("content", "?")
            if isinstance(content, str):
                print(f"  role={role} content(str): {content[:80]}", flush=True)
            elif isinstance(content, list):
                types = [b.get("type", "?") for b in content if isinstance(b, dict)]
                print(f"  role={role} content(blocks): {types}", flush=True)
        print(flush=True)

    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
