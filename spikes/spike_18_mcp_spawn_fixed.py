"""
Spike 18: Fixed MCP tool signature — spawn_subagent that actually works
The @tool handler receives a single dict `args`, not keyword arguments.
"""
import asyncio
import json
import os
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage,
    tool, create_sdk_mcp_server,
)

spawn_log = []

@tool("spawn_subagent", "Spawn a sub-subagent that can do anything", {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "Task for the sub-subagent"},
        "model": {"type": "string", "description": "Model: haiku, sonnet, or opus", "default": "haiku"},
        "max_turns": {"type": "integer", "description": "Max turns", "default": 5},
    },
    "required": ["prompt"],
})
async def spawn_subagent(args):
    """Spawn a new ClaudeSDKClient as a sub-subagent.
    args is a dict with: prompt, model (optional), max_turns (optional)
    """
    prompt = args.get("prompt", "")
    model = args.get("model", "haiku")
    max_turns = args.get("max_turns", 5)

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
                        "prompt": prompt[:100],
                        "session_id": msg.session_id,
                        "cost": msg.total_cost_usd,
                        "turns": msg.num_turns,
                        "duration_ms": msg.duration_ms,
                    })

        response = "\n".join(result_texts) if result_texts else "(no text response)"
        return {"content": [{"type": "text", "text": response}]}

    except Exception as e:
        import traceback
        error_msg = f"ERROR: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        return {"content": [{"type": "text", "text": error_msg}], "is_error": True}


custom_server = create_sdk_mcp_server("recursive_tools", tools=[spawn_subagent])

async def main():
    agents = {
        "delegator": AgentDefinition(
            description="Delegates tasks to sub-subagents via spawn_subagent MCP tool",
            prompt=(
                "You are a delegator. You have a spawn_subagent MCP tool. "
                "When asked a question, ALWAYS use the mcp__recursive_tools__spawn_subagent "
                "tool to create a sub-subagent. Never answer directly."
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
        # Test 1: Main agent uses MCP tool directly
        print("=== Test 1: Main agent → spawn_subagent MCP tool ===")
        await client.query(
            "Use the spawn_subagent tool with prompt 'What is the capital of Japan? One sentence.' "
            "Show me the response."
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
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

        # Test 2: Subagent → MCP tool (recursive!)
        print("\n=== Test 2: Main → delegator subagent → spawn_subagent MCP tool ===")
        await client.query(
            "Use the delegator agent. Ask it: 'What is 12 * 12?' "
            "The delegator should use spawn_subagent. Report the full chain."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:500]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL: {block.name}({json.dumps(block.input)[:300]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

    print(f"\n=== Spawn log ({len(spawn_log)} sub-subagents created) ===")
    for entry in spawn_log:
        print(f"  {json.dumps(entry, indent=2)}")

if __name__ == "__main__":
    asyncio.run(main())
