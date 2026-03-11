"""
Spike 12: Raw message stream — what do we see at the message level?
See all message types including system messages about subagent lifecycle.
"""
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AgentDefinition,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

async def main():
    agents = {
        "helper": AgentDefinition(
            description="Answers questions in one sentence",
            prompt="Answer in one sentence.",
            model="haiku",
        ),
    }

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        agents=agents,
        max_turns=5,
    )

    client = ClaudeSDKClient(options)

    async with client:
        print("=== Capturing ALL messages from subagent interaction ===")
        await client.query("Use the helper agent to tell me what 7+7 is.")

        async for msg in client.receive_response():
            msg_type = type(msg).__name__
            print(f"\n--- {msg_type} ---")

            if isinstance(msg, SystemMessage):
                print(f"  subtype: {msg.subtype}")
                print(f"  data: {json.dumps(msg.data, indent=2, default=str)[:800]}")
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  text: {block.text[:300]}")
                    elif hasattr(block, 'name'):
                        print(f"  tool_use: {block.name}")
                        print(f"    input: {json.dumps(block.input, indent=2, default=str)[:400]}")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        print(f"  tool_result: {c[:400]}")
                    else:
                        print(f"  other_block: {type(block).__name__}: {str(block)[:200]}")
            elif isinstance(msg, ResultMessage):
                print(f"  session_id: {msg.session_id}")
                print(f"  cost: ${msg.total_cost_usd:.4f}")
                print(f"  turns: {msg.num_turns}")
                print(f"  duration_ms: {msg.duration_ms}")
                # Check for any extra attributes
                for attr in ['usage', 'model', 'stop_reason']:
                    if hasattr(msg, attr):
                        print(f"  {attr}: {getattr(msg, attr)}")
            else:
                # Unknown message type — dump everything
                print(f"  raw: {str(msg)[:500]}")

if __name__ == "__main__":
    asyncio.run(main())
