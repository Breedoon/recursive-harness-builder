"""
Spike 05: Can we create a team from the SDK?
Test: Ask the agent to create a team, spawn teammates, assign tasks.
This tests whether the TeamCreate, TaskCreate, SendMessage tools are available
when using ClaudeSDKClient.
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage,
)

async def main():
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=15,
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: Create a team from SDK ===")
        await client.query(
            "I want you to create a team called 'spike-test-team' with description 'Testing team creation from SDK'. "
            "Then create a task with subject 'Say hello' and description 'Output hello world'. "
            "Then list all available tasks. "
            "Tell me exactly what tools you used and what each returned."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:800]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:300]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
