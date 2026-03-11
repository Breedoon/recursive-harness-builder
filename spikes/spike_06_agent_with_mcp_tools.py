"""
Spike 06: Can we give a subagent custom MCP tools?
Test: Define MCP tools and see if subagents can use them.
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
    tool, create_sdk_mcp_server,
)

@tool("get_secret", "Returns a secret number", {"type": "object", "properties": {}})
async def get_secret():
    return "The secret number is 42."

@tool("multiply", "Multiplies two numbers", {
    "type": "object",
    "properties": {
        "a": {"type": "number", "description": "First number"},
        "b": {"type": "number", "description": "Second number"},
    },
    "required": ["a", "b"],
})
async def multiply(a: float, b: float):
    return f"Result: {a * b}"

custom_server = create_sdk_mcp_server("spike_tools", tools=[get_secret, multiply])

async def main():
    agents = {
        "math-helper": AgentDefinition(
            description="A math helper with access to custom tools",
            prompt="You are a math helper. Use the multiply and get_secret tools when asked.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        mcp_servers={"spike_tools": custom_server},
        max_turns=8,
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Test: Can the subagent use our custom MCP tools? ===")
        await client.query(
            "Use the math-helper agent. Ask it to: "
            "1. Get the secret number using the get_secret tool. "
            "2. Multiply that secret number by 7 using the multiply tool. "
            "Report back what it found."
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
