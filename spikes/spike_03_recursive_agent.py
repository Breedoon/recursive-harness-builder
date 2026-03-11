"""
Spike 03: Can a subagent spawn another subagent?
Test: Define nested agents and see if agent A can ask agent B to use agent C.
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)

async def on_subagent_start(event):
    print(f"\n  HOOK START: {json.dumps(event, indent=2, default=str)[:500]}")
    return {}

async def on_subagent_stop(event):
    print(f"\n  HOOK STOP: type={event.get('agent_type', 'N/A')}")
    return {}

async def main():
    agents = {
        "coordinator": AgentDefinition(
            description="Coordinates tasks by delegating to the worker agent",
            prompt=(
                "You are a coordinator. When asked a question, ALWAYS delegate it to "
                "the 'worker' agent using the Agent/Task tool. Never answer directly."
            ),
            model="haiku",
        ),
        "worker": AgentDefinition(
            description="Does actual work - answers questions concisely",
            prompt="You are a worker. Answer questions in one sentence.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=8,
        hooks={
            "SubagentStart": [{"hooks": [on_subagent_start]}],
            "SubagentStop": [{"hooks": [on_subagent_stop]}],
        },
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: main → coordinator → worker (nested delegation) ===")
        await client.query(
            "Use the coordinator agent to find out the capital of France. "
            "The coordinator should delegate to the worker agent."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:500]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:300]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
