"""
Spike 07: Can an Agent tool subagent spawn another Agent tool subagent?
This tests the "subagents cannot spawn sub-subagents" limitation.
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage,
)

async def on_subagent_start(event):
    print(f"\n  HOOK START: {json.dumps(event, indent=2, default=str)[:500]}")
    return {}

async def on_subagent_stop(event):
    print(f"\n  HOOK STOP: type={event.get('agent_type', 'N/A')}")
    return {}

async def main():
    # No custom agents defined - use built-in general-purpose
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=15,
        hooks={
            "SubagentStart": [{"hooks": [on_subagent_start]}],
            "SubagentStop": [{"hooks": [on_subagent_stop]}],
        },
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: Can Agent → Agent → Agent work? ===")
        await client.query(
            "Use the Agent tool to spawn a general-purpose agent with this prompt: "
            "'You MUST use the Agent tool to spawn another general-purpose agent "
            "and ask it to calculate 2+2. Report what the sub-agent said.' "
            "Use description 'test recursive agents'. "
            "Then report the full chain of what happened."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:800]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:400]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
