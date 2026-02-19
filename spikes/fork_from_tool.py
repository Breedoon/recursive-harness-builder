"""Spike: Can we fork a session from within an MCP tool call?

Tests:
1. Create an MCP tool that calls query() with fork_session=True
2. Run a session, trigger the tool, verify fork happens
3. Check the forked session JSONL for cache hits

Usage: .venv/bin/python spikes/fork_from_tool.py
"""

import asyncio
import json
import glob
import os
from pathlib import Path

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    TextBlock,
    query,
    tool,
    create_sdk_mcp_server,
)

# Track the session ID from the main session
_main_session_id = None
_fork_session_id = None


@tool(
    "self_fork",
    "Fork this session and run a subtask. Returns the fork's response.",
    {"task": {"type": "string", "description": "What the fork should do"}},
)
async def self_fork(args: dict) -> dict:
    """MCP tool that forks the current session."""
    global _fork_session_id
    task = args["task"]

    if not _main_session_id:
        return {"content": [{"type": "text", "text": "ERROR: No session ID available"}]}

    print(f"\n[SPIKE] Forking session {_main_session_id} with task: {task}")

    options = ClaudeAgentOptions(
        resume=_main_session_id,
        fork_session=True,
        max_turns=1,
    )

    result_parts = []
    async for message in query(prompt=task, options=options):
        if hasattr(message, "session_id"):
            _fork_session_id = message.session_id
            print(f"[SPIKE] Fork session ID: {_fork_session_id}")
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_parts.append(block.text)

    result = "\n".join(result_parts) or "(no response from fork)"
    print(f"[SPIKE] Fork response length: {len(result)} chars")
    return {"content": [{"type": "text", "text": result}]}


async def main():
    global _main_session_id

    server = create_sdk_mcp_server("spike-fork", tools=[self_fork])

    options = ClaudeAgentOptions(
        system_prompt="You are a test agent. When asked to use self_fork, use it immediately.",
        mcp_servers={"spike-fork": server},
        permission_mode="bypassPermissions",
        max_turns=5,
    )

    client = ClaudeSDKClient(options)
    await client.connect()

    print("[SPIKE] Connected. Sending message...")

    try:
        await client.query(
            "Use the self_fork tool with task: 'Say hello and confirm you can see the conversation history'"
        )
        async for message in client.receive_messages():
            if hasattr(message, "session_id"):
                _main_session_id = message.session_id
                print(f"[SPIKE] Main session ID: {_main_session_id}")
            if hasattr(message, "content") and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"[SPIKE] Response: {block.text[:200]}")
            # ResultMessage signals end of turn
            if hasattr(message, "duration_ms"):
                print(f"[SPIKE] Turn complete ({message.duration_ms}ms)")
                break
    finally:
        await client.disconnect()

    # Check JSONL files for cache hits
    if _fork_session_id:
        print(f"\n[SPIKE] Checking fork JSONL for cache hits...")
        # Find the fork's JSONL
        projects_dir = Path.home() / ".claude" / "projects"
        for jsonl_dir in projects_dir.iterdir():
            fork_file = jsonl_dir / f"{_fork_session_id}.jsonl"
            if fork_file.exists():
                with open(fork_file) as f:
                    for line in f:
                        obj = json.loads(line)
                        if obj.get("type") == "assistant" and "message" in obj:
                            usage = obj["message"].get("usage", {})
                            cache_read = usage.get("cache_read_input_tokens", 0)
                            input_tokens = usage.get("input_tokens", 0)
                            output_tokens = usage.get("output_tokens", 0)
                            print(f"[SPIKE] Fork cache: read={cache_read}, fresh_input={input_tokens}, output={output_tokens}")
                            if cache_read > 0:
                                print(f"[SPIKE] CACHE HIT CONFIRMED! {cache_read} tokens from cache, only {input_tokens} fresh")
                            else:
                                print(f"[SPIKE] NO CACHE HIT - fork didn't reuse parent cache")
                            break
                break
        else:
            print(f"[SPIKE] Fork JSONL not found for session {_fork_session_id}")
    else:
        print("[SPIKE] No fork session ID captured - fork may not have run")


if __name__ == "__main__":
    asyncio.run(main())
