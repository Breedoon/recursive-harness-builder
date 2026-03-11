"""
Spike 19b: Quick test — do CLAUDE_CODE_ENABLE_TASKS=1 + CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
inject TaskCreate/SendMessage into SDK-spawned agents?
"""
import asyncio
import json
import os
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

os.environ["CLAUDE_CODE_ENABLE_TASKS"] = "1"
os.environ["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

async def main():
    print("=== Spike 19b: Both env vars set ===")

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=3,
    )

    client = ClaudeSDKClient(options)

    async with client:
        await client.query(
            "List every tool you have access to as a simple numbered list of tool names. Nothing else."
        )

        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage):
                if msg.subtype == "init":
                    tools = sorted(msg.data.get("tools", []))
                    print(f"INIT tools ({len(tools)}): {tools}")
                    # Check specifics
                    task_tools = {"TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}
                    team_tools = {"SendMessage", "TeamCreate", "TeamDelete"}
                    found_task = task_tools.intersection(set(tools))
                    found_team = team_tools.intersection(set(tools))
                    print(f"  Task tools found: {found_task or 'NONE'}")
                    print(f"  Team tools found: {found_team or 'NONE'}")
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"TEXT: {block.text[:2000]}")
            elif isinstance(msg, ResultMessage):
                print(f"DONE: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
