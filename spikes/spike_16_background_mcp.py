"""
Spike 16: Can a BACKGROUND subagent use MCP tools?
Tests the documented limitation that background agents can't access MCP tools.
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
    tool, create_sdk_mcp_server,
)

@tool("get_magic_number", "Returns a magic number only accessible via MCP", {
    "type": "object",
    "properties": {},
})
async def get_magic_number():
    return "The magic number is 42."

custom_server = create_sdk_mcp_server("test_mcp", tools=[get_magic_number])

async def main():
    agents = {
        "mcp-tester": AgentDefinition(
            description="Tests MCP tool access by calling get_magic_number",
            prompt=(
                "You MUST call the get_magic_number MCP tool (it may appear as "
                "mcp__test_mcp__get_magic_number). Report what it returns. "
                "If the tool is not available, say 'MCP TOOL NOT AVAILABLE'."
            ),
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        mcp_servers={"test_mcp": custom_server},
        max_turns=8,
    )

    client = ClaudeSDKClient(options)

    async with client:
        # Test 1: Foreground subagent with MCP
        print("=== Test 1: FOREGROUND subagent calling MCP tool ===")
        await client.query(
            "Use the mcp-tester agent (foreground, NOT background) to call get_magic_number and report."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:500]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL: {block.name}({json.dumps(block.input)[:200]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  RESULT: {c[:300]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  DONE: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

        # Test 2: Background subagent with MCP
        print("\n=== Test 2: BACKGROUND subagent calling MCP tool ===")
        await client.query(
            "Use the mcp-tester agent IN THE BACKGROUND (run_in_background=true) to call "
            "get_magic_number. Then check and report the result."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:500]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL: {block.name}({json.dumps(block.input)[:200]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  DONE: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
