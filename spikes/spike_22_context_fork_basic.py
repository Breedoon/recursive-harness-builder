"""
Spike 22: Basic context:fork skill invocation via SDK

Tests:
1. Can we invoke a skill with context: fork via ClaudeSDKClient?
2. Does it use the Skill tool? The Agent/Task tool?
3. What messages do we see (SystemMessage subtypes)?
4. Compare inline vs forked skill invocation.
"""
import spike_env  # noqa: F401 — must be first
import asyncio
import json
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

PROJECT_DIR = "/tmp/context-fork-spikes"

async def run_skill_test(label: str, prompt: str):
    """Run a single skill invocation and capture all messages."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"PROMPT: {prompt}")
    print(f"{'='*70}")

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=10,
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    )

    client = ClaudeSDKClient(options)
    messages_log = []

    async with client:
        await client.query(prompt)

        async for msg in client.receive_response():
            msg_type = type(msg).__name__
            entry = {"type": msg_type}

            if isinstance(msg, SystemMessage):
                entry["subtype"] = msg.subtype
                entry["data"] = msg.data
                print(f"\n  [{msg_type}] subtype={msg.subtype}")
                print(f"    data: {json.dumps(msg.data, indent=2, default=str)[:600]}")

            elif isinstance(msg, AssistantMessage):
                blocks = []
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        blocks.append({"type": "text", "text": block.text[:500]})
                        print(f"\n  [{msg_type}] TEXT: {block.text[:500]}")
                    elif hasattr(block, 'name'):
                        blocks.append({"type": "tool_use", "name": block.name, "input": block.input})
                        print(f"\n  [{msg_type}] TOOL_USE: {block.name}")
                        print(f"    input: {json.dumps(block.input, indent=2, default=str)[:400]}")
                    elif hasattr(block, 'content'):
                        c = block.content if isinstance(block.content, str) else str(block.content)
                        blocks.append({"type": "tool_result", "content": c[:500]})
                        print(f"\n  [{msg_type}] TOOL_RESULT: {c[:400]}")
                    else:
                        blocks.append({"type": type(block).__name__})
                        print(f"\n  [{msg_type}] {type(block).__name__}")
                entry["blocks"] = blocks

            elif isinstance(msg, ResultMessage):
                entry["session_id"] = msg.session_id
                entry["cost"] = msg.total_cost_usd
                entry["turns"] = msg.num_turns
                entry["duration_ms"] = msg.duration_ms
                print(f"\n  [{msg_type}] session={msg.session_id}")
                print(f"    cost=${msg.total_cost_usd:.4f}, turns={msg.num_turns}, duration={msg.duration_ms}ms")
            else:
                print(f"\n  [{msg_type}] {str(msg)[:300]}")

            messages_log.append(entry)

    return messages_log


async def main():
    print("="*70)
    print("SPIKE 22: Basic context:fork skill invocation")
    print("="*70)

    # Test 1: Inline skill (no context: fork)
    inline_log = await run_skill_test(
        "Inline Skill (no context:fork)",
        "Use the inline-greeter skill now."
    )

    # Test 2: Forked skill (context: fork)
    forked_log = await run_skill_test(
        "Forked Skill (context: fork)",
        "Use the forked-greeter skill now."
    )

    # Summary comparison
    print("\n" + "="*70)
    print("COMPARISON SUMMARY")
    print("="*70)

    for label, log in [("INLINE", inline_log), ("FORKED", forked_log)]:
        tool_uses = [e for e in log if e["type"] == "AssistantMessage"
                     and any(b.get("type") == "tool_use" for b in e.get("blocks", []))]
        system_msgs = [e for e in log if e["type"] == "SystemMessage"]
        result = [e for e in log if e["type"] == "ResultMessage"]

        tools_used = []
        for t in tool_uses:
            for b in t.get("blocks", []):
                if b.get("type") == "tool_use":
                    tools_used.append(b["name"])

        print(f"\n  {label}:")
        print(f"    Tools used: {tools_used}")
        print(f"    System messages: {[s['subtype'] for s in system_msgs]}")
        if result:
            r = result[0]
            print(f"    Cost: ${r.get('cost', 0):.4f}")
            print(f"    Turns: {r.get('turns', '?')}")
            print(f"    Duration: {r.get('duration_ms', '?')}ms")


if __name__ == "__main__":
    asyncio.run(main())
