"""
Spike 15b: Debug version — add logging to the MCP spawn tool
"""
import asyncio
import json
import os
import sys
import traceback
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage,
    tool, create_sdk_mcp_server,
)

@tool("spawn_subagent", "Spawn a sub-subagent that can do anything", {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "Task for the sub-subagent"},
    },
    "required": ["prompt"],
})
async def spawn_subagent(prompt: str):
    """Spawn a new ClaudeSDKClient as a sub-subagent."""
    log = [f"spawn_subagent called with prompt: {prompt[:100]}"]
    try:
        options = ClaudeAgentOptions(
            model="haiku",
            permission_mode="bypassPermissions",
            max_turns=3,
        )
        log.append("Created options")

        client = ClaudeSDKClient(options)
        log.append("Created client")

        result_texts = []
        session_id = None

        async with client:
            log.append("Connected to client")
            await client.query(prompt)
            log.append("Sent query")

            async for msg in client.receive_response():
                log.append(f"Received: {type(msg).__name__}")
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            result_texts.append(block.text)
                            log.append(f"Text: {block.text[:100]}")
                elif isinstance(msg, ResultMessage):
                    session_id = msg.session_id
                    log.append(f"Result: cost={msg.total_cost_usd}, turns={msg.num_turns}")

        log.append("Disconnected")
        response = "\n".join(result_texts) if result_texts else "(no text)"
        log.append(f"Final response: {response[:200]}")

        # Return both the response and debug log
        return json.dumps({
            "response": response,
            "debug_log": log,
            "session_id": session_id,
        })

    except Exception as e:
        log.append(f"ERROR: {type(e).__name__}: {e}")
        log.append(traceback.format_exc())
        return json.dumps({
            "error": str(e),
            "debug_log": log,
        })


custom_server = create_sdk_mcp_server("recursive_tools", tools=[spawn_subagent])

async def main():
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        mcp_servers={"recursive_tools": custom_server},
        max_turns=5,
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Debug: MCP spawn_subagent ===")
        await client.query(
            "Call the spawn_subagent tool with prompt 'Say exactly: Hello from sub-subagent! The answer is 42.' "
            "Then show me the FULL raw result from the tool."
        )

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  TEXT: {block.text[:800]}")
                    elif hasattr(block, 'name'):
                        print(f"  TOOL: {block.name}({json.dumps(block.input)[:200]})")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  TOOL_RESULT: {c[:800]}")
            elif isinstance(msg, ResultMessage):
                print(f"\n  RESULT: cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}")

if __name__ == "__main__":
    asyncio.run(main())
