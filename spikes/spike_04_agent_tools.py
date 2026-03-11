"""
Spike 04: What tools does a subagent get? Can we restrict them?
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
)

async def on_subagent_stop(event):
    print(f"\n  HOOK STOP: type={event.get('agent_type')}, transcript={event.get('agent_transcript_path', 'N/A')}")
    return {}

async def main():
    agents = {
        "reader-only": AgentDefinition(
            description="An agent that can only read files",
            prompt="You are a file reader. Read files when asked. Report what you find.",
            tools=["Read", "Glob", "Grep"],
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=5,
        hooks={
            "SubagentStop": [{"hooks": [on_subagent_stop]}],
        },
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: reader-only agent reads pyproject.toml ===")
        await client.query(
            "Use the reader-only agent to read /Users/breedoon/Documents/obs/pyproject.toml "
            "and tell me the project name and version."
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
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
