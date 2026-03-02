"""Spike: What UUIDs does ClaudeSDKClient give us in the message stream?

Uses ClaudeSDKClient (same as our runtime) to send a message that triggers
tool use, then logs every message type and whether it has a uuid.
"""

import asyncio
import sys
import os

# Force unbuffered
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

print("SPIKE STARTING", flush=True)
print(f"CLAUDECODE env: {os.environ.get('CLAUDECODE', 'NOT SET')}", flush=True)

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


async def main():
    print("Creating client...", flush=True)
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        max_turns=3,
    )
    client = ClaudeSDKClient(options)

    print("Connecting...", flush=True)
    await client.connect()
    print("Connected!", flush=True)

    prompt = "What is today's date? Use the Bash tool to run `date +%Y-%m-%d` and tell me."
    print(f">>> Sending: {prompt}", flush=True)
    await client.query(prompt)
    print("Query sent, receiving...", flush=True)

    msg_index = 0
    async for message in client.receive_response():
        msg_index += 1
        msg_type = type(message).__name__

        uuid_attr = getattr(message, "uuid", "NO_ATTR")
        session_id_attr = getattr(message, "session_id", "NO_ATTR")
        parent_tool_use_id = getattr(message, "parent_tool_use_id", "NO_ATTR")

        print(f"--- Message #{msg_index}: {msg_type} ---", flush=True)
        print(f"  uuid:              {uuid_attr}", flush=True)
        print(f"  session_id:        {session_id_attr}", flush=True)
        print(f"  parent_tool_use_id: {parent_tool_use_id}", flush=True)

        if hasattr(message, "content"):
            content = message.content
            if isinstance(content, str):
                preview = content[:80] + "..." if len(content) > 80 else content
                print(f"  content type:      str ({preview})", flush=True)
            elif isinstance(content, list):
                block_types = [type(b).__name__ for b in content]
                print(f"  content blocks:    {block_types}", flush=True)
                for b in content:
                    if hasattr(b, "text"):
                        text = b.text[:120] + "..." if len(b.text) > 120 else b.text
                        print(f"    TextBlock:       {text}", flush=True)
                    elif hasattr(b, "name"):
                        print(f"    ToolUseBlock:    {b.name}({b.input})", flush=True)
                    elif hasattr(b, "tool_use_id"):
                        content_str = str(getattr(b, "content", ""))[:80]
                        print(f"    ToolResultBlock: tool_use_id={b.tool_use_id} content={content_str}", flush=True)

        if hasattr(message, "num_turns"):
            print(f"  num_turns:         {message.num_turns}", flush=True)
            print(f"  total_cost_usd:    {getattr(message, 'total_cost_usd', None)}", flush=True)

        if hasattr(message, "subtype") and hasattr(message, "data"):
            if not hasattr(message, "num_turns"):
                print(f"  subtype:           {message.subtype}", flush=True)
                if isinstance(message.data, dict):
                    print(f"  data.keys:         {list(message.data.keys())[:15]}", flush=True)
                    if "uuid" in message.data:
                        print(f"  data['uuid']:      {message.data['uuid']}", flush=True)

        print(flush=True)

    await client.disconnect()
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
