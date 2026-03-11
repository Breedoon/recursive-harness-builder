"""
Spike 08: Full team lifecycle — create team, spawn teammate, assign task, get result
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
        max_turns=20,
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: Full team lifecycle ===")
        await client.query(
            "Here's what I want you to do step by step:\n"
            "1. Create a team called 'spike-team-08'\n"
            "2. Spawn a teammate using the Agent tool with team_name='spike-team-08', "
            "name='worker', subagent_type='general-purpose'. "
            "Give it this prompt: 'You are a teammate. When you receive a message, "
            "respond by saying hello and report what tools you have access to. "
            "List all the tool names you can see.'\n"
            "3. Send a message to the worker teammate asking it to list its tools.\n"
            "4. Report what happened at each step.\n"
            "Do each step one at a time and report results."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:1000]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL_USE: {block.name}({json.dumps(block.input)[:400]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:600]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    # Cleanup
    import shutil
    import os
    for path in [os.path.expanduser("~/.claude/teams/spike-team-08"),
                 os.path.expanduser("~/.claude/tasks/spike-team-08")]:
        if os.path.exists(path):
            shutil.rmtree(path)
            print(f"  Cleaned up: {path}")

if __name__ == "__main__":
    asyncio.run(main())
