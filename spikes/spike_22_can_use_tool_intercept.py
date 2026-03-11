"""
Spike 22: Can we intercept Agent tool calls via can_use_tool callback?

Key question: Does the CLI send a can_use_tool control request for the Agent tool?
If yes, we can intercept it, deny it, and handle it ourselves with our own SDK client.

Test with different permission modes to see if any triggers can_use_tool for Agent.
"""
import asyncio
import json
import os
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

intercepted_tools = []


async def my_can_use_tool(tool_name, tool_input, context):
    """Intercept ALL tool calls. Log Agent tool if we see it."""
    print(f"  [INTERCEPT] tool={tool_name}, input_keys={list(tool_input.keys()) if isinstance(tool_input, dict) else '?'}")
    intercepted_tools.append({"name": tool_name, "input": tool_input})

    if tool_name == "Agent":
        print(f"  *** AGENT TOOL INTERCEPTED! ***")
        print(f"  Input: {json.dumps(tool_input, indent=2)[:500]}")
        # Deny it to see if we can replace it
        return {"behavior": "deny", "message": "Intercepted by spike 22"}

    return {"behavior": "allow"}


async def test_permission_mode(mode, label):
    """Test can_use_tool interception with a specific permission mode."""
    print(f"\n=== Test: {label} (mode={mode}) ===")

    options = ClaudeAgentOptions(
        permission_mode=mode,
        model="haiku",
        max_turns=5,
        can_use_tool=my_can_use_tool,
    )
    client = ClaudeSDKClient(options)
    intercepted_tools.clear()

    try:
        async with client:
            await client.query(
                "Use the Agent tool to spawn a haiku agent with the prompt 'say hello'. "
                "If Agent tool is not available, list all tools you have access to."
            )
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  TEXT: {block.text[:300]}")
                elif isinstance(msg, ResultMessage):
                    print(f"  DONE: cost=${msg.total_cost_usd:.4f}")
    except Exception as e:
        print(f"  ERROR: {e}")

    agent_intercepted = any(t["name"] == "Agent" for t in intercepted_tools)
    print(f"  Tools intercepted: {[t['name'] for t in intercepted_tools]}")
    print(f"  Agent tool intercepted: {agent_intercepted}")
    return agent_intercepted


async def main():
    print("=== Spike 22: can_use_tool Interception of Agent Tool ===")

    # Test 1: default mode — CLI should ask permission for risky tools
    r1 = await test_permission_mode("default", "Default mode")

    # Test 2: acceptEdits mode — auto-accept edits, but might still ask for Agent
    r2 = await test_permission_mode("acceptEdits", "AcceptEdits mode")

    # Test 3: bypassPermissions — auto-approve everything, callback might still fire
    r3 = await test_permission_mode("bypassPermissions", "BypassPermissions mode")

    print(f"\n=== RESULTS ===")
    print(f"  Default mode intercepted Agent: {r1}")
    print(f"  AcceptEdits intercepted Agent: {r2}")
    print(f"  BypassPermissions intercepted Agent: {r3}")

    if any([r1, r2, r3]):
        print(f"\n  VERDICT: Agent tool CAN be intercepted via can_use_tool!")
        print(f"  This means we can deny it and handle it ourselves.")
    else:
        print(f"\n  VERDICT: Agent tool CANNOT be intercepted via can_use_tool.")
        print(f"  Need alternative approach.")


if __name__ == "__main__":
    asyncio.run(main())
