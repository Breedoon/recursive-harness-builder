"""
Spike 11: PreToolUse hook to observe Task tool calls
Can we see the actual tool call parameters when a subagent is spawned?
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)

tool_calls = []

async def on_pre_tool_use(event):
    """Capture all tool calls to see what parameters the Task tool gets."""
    tool_calls.append(event)
    tool_name = event.get("tool_name", "unknown")
    tool_input = event.get("tool_input", {})
    print(f"\n  PRE_TOOL_USE: {tool_name}")
    print(f"    input: {json.dumps(tool_input, indent=2, default=str)[:600]}")
    return {}

async def on_post_tool_use(event):
    """Capture tool results."""
    tool_name = event.get("tool_name", "unknown")
    result = event.get("tool_result", "")
    if isinstance(result, str):
        result_preview = result[:300]
    else:
        result_preview = json.dumps(result, default=str)[:300]
    print(f"\n  POST_TOOL_USE: {tool_name}")
    print(f"    result: {result_preview}")
    return {}

async def main():
    agents = {
        "helper": AgentDefinition(
            description="Answers math questions briefly",
            prompt="Answer math questions in one sentence.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=5,
        hooks={
            "PreToolUse": [{"hooks": [on_pre_tool_use]}],
            "PostToolUse": [{"hooks": [on_post_tool_use]}],
        },
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: Observe Task tool call parameters ===")
        await client.query("Use the helper agent to tell me what 5*5 is.")

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"\n  TEXT: {block.text[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    print(f"\n=== Captured {len(tool_calls)} tool call events ===")

if __name__ == "__main__":
    asyncio.run(main())
