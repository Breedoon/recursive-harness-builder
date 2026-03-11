"""
Spike 02: ClaudeSDKClient with SubagentStart/SubagentStop hooks
Test: Can we intercept subagent lifecycle to observe what happens?
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)

subagent_events = []

async def on_subagent_start(event):
    print(f"\n  HOOK SUBAGENT_START: {json.dumps(event, indent=2, default=str)[:500]}")
    subagent_events.append(("start", event))
    return {}

async def on_subagent_stop(event):
    print(f"\n  HOOK SUBAGENT_STOP: {json.dumps(event, indent=2, default=str)[:500]}")
    subagent_events.append(("stop", event))
    return {}

async def main():
    agents = {
        "helper": AgentDefinition(
            description="A helpful assistant that answers questions briefly",
            prompt="You are a helpful assistant. Always respond in exactly one sentence.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=5,
        hooks={
            "SubagentStart": [{"hooks": [on_subagent_start]}],
            "SubagentStop": [{"hooks": [on_subagent_stop]}],
        },
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Sending message to trigger subagent use ===")
        await client.query("Use the helper agent to tell me what 2+2 is.")

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:300]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:200]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:300]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    print(f"\n=== Captured {len(subagent_events)} subagent events ===")
    for kind, evt in subagent_events:
        print(f"  {kind}: {json.dumps(evt, indent=2, default=str)[:500]}")

if __name__ == "__main__":
    asyncio.run(main())
