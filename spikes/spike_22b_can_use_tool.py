"""
Spike 22b: can_use_tool callback interception test.
Writes to /tmp/spike_22b.log

Does the CLI send can_use_tool requests for the Agent tool?
Test in default mode (where permission checks happen).
"""
import asyncio
import json
import os

os.environ.pop("CLAUDECODE", None)

LOG = open("/tmp/spike_22b.log", "w")

def log(msg):
    LOG.write(msg + "\n")
    LOG.flush()

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

intercepted = []


async def my_can_use_tool(tool_name, tool_input, context):
    log(f"  [CAN_USE_TOOL] {tool_name} keys={list(tool_input.keys()) if isinstance(tool_input, dict) else '?'}")
    intercepted.append({"name": tool_name, "input_keys": list(tool_input.keys()) if isinstance(tool_input, dict) else []})

    if tool_name == "Agent":
        log(f"  *** AGENT INTERCEPTED! Input: {json.dumps(tool_input)[:500]}")
        return {"behavior": "deny", "message": "Intercepted by spike 22b"}

    return {"behavior": "allow"}


async def test_mode(mode, prompt):
    log(f"\n--- Mode: {mode} ---")
    intercepted.clear()

    try:
        client = ClaudeSDKClient(ClaudeAgentOptions(
            permission_mode=mode,
            model="haiku",
            max_turns=5,
            can_use_tool=my_can_use_tool,
        ))
        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            log(f"  TEXT: {b.text[:300]}")
                        elif hasattr(b, 'name'):
                            log(f"  TOOL_USE: {b.name}")
                elif isinstance(msg, ResultMessage):
                    log(f"  cost=${msg.total_cost_usd:.4f}")
    except Exception as e:
        log(f"  ERROR: {e}")

    agent_seen = any(t["name"] == "Agent" for t in intercepted)
    log(f"  Intercepted: {[t['name'] for t in intercepted]}")
    log(f"  Agent seen: {agent_seen}")
    return agent_seen


async def main():
    log("=== Spike 22b: can_use_tool Interception ===")

    # Test with default mode (should trigger permission checks)
    r1 = await test_mode("default",
        "Use the Agent tool to spawn a haiku agent that says hello. "
        "If Agent is not available, say 'NO AGENT TOOL'.")

    # Test with acceptEdits
    r2 = await test_mode("acceptEdits",
        "Use the Agent tool to spawn a haiku agent that says hello. "
        "If Agent is not available, say 'NO AGENT TOOL'.")

    # Test bypass but WITH can_use_tool set
    r3 = await test_mode("bypassPermissions",
        "Use the Agent tool to spawn a haiku agent that says hello. "
        "If Agent is not available, say 'NO AGENT TOOL'.")

    log(f"\n=== RESULTS ===")
    log(f"  default: {r1}")
    log(f"  acceptEdits: {r2}")
    log(f"  bypassPermissions: {r3}")
    LOG.close()


asyncio.run(main())
