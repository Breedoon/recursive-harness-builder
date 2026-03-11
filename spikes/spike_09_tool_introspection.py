"""
Spike 09: Tool introspection — what tools does the main agent see?
And what tools does a subagent see?
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)

async def main():
    agents = {
        "introspector": AgentDefinition(
            description="An agent that reports its available tools",
            prompt="You are an introspection agent. When asked, list ALL tools you have access to. "
                   "List every single tool name. Also note if you have the Agent, Task, TeamCreate, "
                   "TaskCreate, TaskList, SendMessage tools.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=8,
    )

    client = ClaudeSDKClient(options)

    async with client:
        # First ask the main agent
        print("=== Test 1: Main agent tool list ===")
        await client.query(
            "List ALL tools you have access to right now. Be exhaustive — list every single tool name. "
            "Specifically note whether you have: Agent, Task, TeamCreate, TaskCreate, "
            "TaskList, TaskUpdate, SendMessage, TodoWrite tools."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:1500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

        # Then ask via subagent
        print("\n=== Test 2: Subagent tool list ===")
        await client.query(
            "Now use the introspector agent and ask it to list ALL tools it has access to. "
            "It should be exhaustive and note whether it has Agent, Task, TeamCreate, "
            "TaskCreate, SendMessage tools."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:1500]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:300]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:800]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
