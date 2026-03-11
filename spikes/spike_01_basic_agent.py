"""
Spike 01: Basic ClaudeSDKClient with AgentDefinition
Test: Can we define subagents and have the main agent use them?
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)

async def main():
    agents = {
        "researcher": AgentDefinition(
            description="A research assistant that answers questions about Python",
            prompt="You are a Python expert. Answer questions concisely in 1-2 sentences.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=5,
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Sending message asking to use the researcher subagent ===")
        await client.query(
            "Use the researcher agent to explain what asyncio.gather does. Keep it brief."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:500]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:200]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:300]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: session={msg.session_id}, cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
