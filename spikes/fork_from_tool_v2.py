"""Spike v2: Fork from MCP tool — two-turn approach.

Turn 1: "Hello" — establishes session, we capture session_id
Turn 2: "Use self_fork" — tool has session_id, can fork

Usage: .venv/bin/python spikes/fork_from_tool_v2.py
"""

import asyncio
import json
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

# Mutable state shared with tool closure
_state = {"session_id": None, "fork_session_id": None}


@tool(
    "self_fork",
    "Fork this session and run a subtask. The fork inherits full conversation context. Returns the fork's text response.",
    {
        "task": {
            "type": "string",
            "description": "What the forked session should do",
        }
    },
)
async def self_fork(args: dict) -> dict:
    task = args["task"]
    session_id = _state["session_id"]

    if not session_id:
        return {"content": [{"type": "text", "text": "ERROR: No session ID yet (need at least one completed turn)"}]}

    print(f"\n[SPIKE] Forking session {session_id}")
    print(f"[SPIKE] Task: {task}")

    options = ClaudeAgentOptions(
        resume=session_id,
        fork_session=True,
        max_turns=1,
    )

    result_parts = []
    async for message in query(prompt=task, options=options):
        if hasattr(message, "session_id") and message.session_id:
            _state["fork_session_id"] = message.session_id
            print(f"[SPIKE] Fork session ID: {message.session_id}")
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result_parts.append(block.text)

    result = "\n".join(result_parts) or "(empty fork response)"
    print(f"[SPIKE] Fork response: {result[:200]}")
    return {"content": [{"type": "text", "text": result}]}


def check_cache_hits(session_id: str, label: str):
    """Read JSONL for a session and report cache stats."""
    projects_dir = Path.home() / ".claude" / "projects"
    for jsonl_dir in projects_dir.iterdir():
        if not jsonl_dir.is_dir():
            continue
        fork_file = jsonl_dir / f"{session_id}.jsonl"
        if fork_file.exists():
            print(f"\n[SPIKE] {label} JSONL found: {fork_file}")
            with open(fork_file) as f:
                msg_num = 0
                for line in f:
                    obj = json.loads(line)
                    if obj.get("type") == "assistant" and "message" in obj:
                        usage = obj["message"].get("usage", {})
                        cache_read = usage.get("cache_read_input_tokens", 0)
                        input_tokens = usage.get("input_tokens", 0)
                        cache_create = usage.get("cache_creation_input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        msg_num += 1
                        print(f"  msg {msg_num}: cache_read={cache_read}, fresh_input={input_tokens}, cache_create={cache_create}, output={output_tokens}")
                        if cache_read > 0:
                            ratio = cache_read / max(cache_read + input_tokens, 1) * 100
                            print(f"  -> CACHE HIT: {ratio:.0f}% from cache")
            return
    print(f"[SPIKE] {label} JSONL not found for {session_id}")


async def main():
    server = create_sdk_mcp_server("spike-fork", tools=[self_fork])

    options = ClaudeAgentOptions(
        system_prompt=(
            "You are a test agent. You have a tool called self_fork. "
            "When asked to fork, use it immediately. Keep responses very brief."
        ),
        mcp_servers={"spike-fork": server},
        permission_mode="bypassPermissions",
        max_turns=5,
    )

    client = ClaudeSDKClient(options)
    await client.connect()

    # --- Turn 1: Establish session ---
    print("[SPIKE] === TURN 1: Establishing session ===")
    await client.query("Hello. Just reply with 'Ready.' and nothing else.")

    async for message in client.receive_messages():
        if hasattr(message, "session_id") and message.session_id:
            _state["session_id"] = message.session_id
            print(f"[SPIKE] Session ID captured: {_state['session_id']}")
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"[SPIKE] Turn 1 response: {block.text}")
        if hasattr(message, "duration_ms"):
            print(f"[SPIKE] Turn 1 complete ({message.duration_ms}ms)")
            break

    if not _state["session_id"]:
        print("[SPIKE] FAILED: No session ID after turn 1")
        await client.disconnect()
        return

    # --- Turn 2: Trigger fork ---
    print("\n[SPIKE] === TURN 2: Triggering fork ===")
    await client.query(
        "Use self_fork with task: 'What was the first message in our conversation? Quote it exactly.'"
    )

    async for message in client.receive_messages():
        if hasattr(message, "content") and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"[SPIKE] Turn 2 response: {block.text[:300]}")
        if hasattr(message, "duration_ms"):
            print(f"[SPIKE] Turn 2 complete ({message.duration_ms}ms)")
            break

    await client.disconnect()

    # --- Check cache ---
    print("\n[SPIKE] === CACHE ANALYSIS ===")
    if _state["session_id"]:
        check_cache_hits(_state["session_id"], "MAIN SESSION")
    if _state["fork_session_id"]:
        check_cache_hits(_state["fork_session_id"], "FORK SESSION")
    else:
        print("[SPIKE] No fork session ID — fork may not have executed")

    print("\n[SPIKE] Done.")


if __name__ == "__main__":
    asyncio.run(main())
