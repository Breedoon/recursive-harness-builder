#!/usr/bin/env python3
"""Quick test: can we run SDK from within a Claude Code session?"""
import sys
import os

# MUST unset CLAUDECODE before importing SDK
if "CLAUDECODE" in os.environ:
    del os.environ["CLAUDECODE"]

print("CLAUDECODE:", os.environ.get("CLAUDECODE", "UNSET"), flush=True)

import asyncio
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage
)


async def test():
    print("Creating client...", flush=True)
    client = ClaudeSDKClient(ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=2,
    ))
    print("Connecting...", flush=True)
    try:
        async with client:
            print("Connected!", flush=True)
            await client.query("Say 'hello' and nothing else.")
            print("Query sent", flush=True)
            async for msg in client.receive_response():
                t = type(msg).__name__
                print(f"MSG: {t}", flush=True)
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            print(f"TEXT: {b.text[:100]}", flush=True)
                elif isinstance(msg, ResultMessage):
                    print(f"SID: {msg.session_id}", flush=True)
        print("DONE", flush=True)
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test())
