"""
Spike 15: MCP tool that spawns a sub-subagent via ClaudeSDKClient
This is the core feasibility test for recursive subagents.
"""
import asyncio
import json
import os
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
    tool, create_sdk_mcp_server,
)

# Track spawned sub-subagents
spawn_log = []

@tool("spawn_subagent", "Spawn a sub-subagent that can do anything", {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "Task for the sub-subagent"},
        "model": {"type": "string", "description": "Model to use (haiku/sonnet/opus)", "default": "haiku"},
        "max_turns": {"type": "integer", "description": "Max turns", "default": 5},
    },
    "required": ["prompt"],
})
async def spawn_subagent(prompt: str, model: str = "haiku", max_turns: int = 5):
    """Spawn a new ClaudeSDKClient as a sub-subagent."""
    # CRITICAL: unset CLAUDECODE to allow nested sessions
    env_backup = os.environ.pop("CLAUDECODE", None)
    try:
        options = ClaudeAgentOptions(
            model=model,
            permission_mode="bypassPermissions",
            max_turns=max_turns,
        )
        client = ClaudeSDKClient(options)
        result_texts = []

        async with client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            result_texts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    spawn_log.append({
                        "prompt": prompt,
                        "session_id": msg.session_id,
                        "cost": msg.total_cost_usd,
                        "turns": msg.num_turns,
                        "duration_ms": msg.duration_ms,
                    })

        return "\n".join(result_texts) if result_texts else "(no text response)"
    except Exception as e:
        return f"ERROR spawning sub-subagent: {type(e).__name__}: {e}"
    finally:
        if env_backup is not None:
            os.environ["CLAUDECODE"] = env_backup


custom_server = create_sdk_mcp_server("recursive_tools", tools=[spawn_subagent])

async def main():
    # The main agent has our custom MCP tool
    # We also define a normal subagent that has access to the MCP tool
    agents = {
        "delegator": AgentDefinition(
            description="An agent that delegates tasks to sub-subagents",
            prompt=(
                "You are a delegator. You have access to the spawn_subagent MCP tool. "
                "When asked a question, ALWAYS use the spawn_subagent tool to create "
                "a sub-subagent that answers it. Never answer directly yourself."
            ),
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        mcp_servers={"recursive_tools": custom_server},
        max_turns=8,
    )

    client = ClaudeSDKClient(options)

    async with client:
        # Test 1: Main agent uses the MCP tool directly
        print("=== Test 1: Main agent spawns sub-subagent via MCP tool ===")
        await client.query(
            "Use the spawn_subagent tool with prompt 'What is the capital of Japan? Answer in one sentence.' "
            "Report what the sub-subagent said."
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
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

        # Test 2: Subagent uses the MCP tool (this is the recursive test!)
        print("\n=== Test 2: Subagent (delegator) spawns sub-subagent via MCP tool ===")
        await client.query(
            "Use the delegator agent and ask it: 'What is 7 * 8?' "
            "The delegator should use spawn_subagent to get the answer."
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
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    print(f"\n=== Spawn log ({len(spawn_log)} sub-subagents) ===")
    for entry in spawn_log:
        print(f"  {json.dumps(entry, indent=2)}")

if __name__ == "__main__":
    asyncio.run(main())
