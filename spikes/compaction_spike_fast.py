"""
Spike: Fast path to compaction.

Strategy: Instead of slowly generating content, use tools to READ large files.
Reading files fills the context much faster than generating text.
Point model at a dir full of big files and tell it to read them all.

Alternative: Send very large user messages (paste big content as prompts).
"""
import asyncio
import json
import sys
import os
import tempfile
from pathlib import Path

os.environ.pop("CLAUDECODE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    TextBlock,
    SystemMessage,
    AssistantMessage,
    ResultMessage,
)

compact_events = []
WORK_DIR = Path(tempfile.mkdtemp(prefix="compact_fast_"))

# Create large files for the model to read
print(f"Work dir: {WORK_DIR}")
for i in range(50):
    # Each file ~10KB of content = ~2.5K tokens
    # 50 files = ~125K tokens from tool results alone
    content = f"# Document {i}: {'Lorem ipsum ' * 500}\n" * 10
    (WORK_DIR / f"doc_{i:02d}.txt").write_text(content)
print(f"Created 50 large files in {WORK_DIR}")


async def on_pre_compact(hook_input, tool_use_id, context):
    """Log everything about compaction."""
    evt = dict(hook_input)
    compact_events.append(evt)
    print(f"\n{'='*60}")
    print(f"PreCompact HOOK FIRED!")
    print(f"  trigger: {evt.get('trigger')}")
    print(f"  custom_instructions: {evt.get('custom_instructions')}")
    print(f"  session_id: {evt.get('session_id')}")
    print(f"  transcript_path: {evt.get('transcript_path')}")
    print(f"  All keys: {list(evt.keys())}")
    print(f"  Full data:")
    print(json.dumps(evt, indent=2, default=str))
    print(f"{'='*60}\n")

    # Allow it to proceed
    return {"continue_": True}


async def main():
    print("=== Spike: Fast compaction via large user messages ===\n")

    # Strategy: send MASSIVE user messages to fill context fast.
    # Each prompt is ~50K chars ≈ 12.5K tokens.
    # Haiku context = 200K tokens.
    # We need ~160K tokens (after system prompt overhead) to trigger.
    # So ~13 turns of 12.5K tokens each should do it.

    big_block = "ABCDEFGHIJ" * 5000  # 50K chars per message

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with just 'OK' to each message. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                {"matcher": None, "hooks": [on_pre_compact]}
            ]
        },
        thinking={"type": "disabled"},
    )

    async with ClaudeSDKClient(options) as client:
        for i in range(30):
            prompt = f"Message {i+1}. Respond with just 'OK'. Here is padding: {big_block}"
            print(f"Turn {i+1}: sending ~{len(prompt)} chars (~{len(prompt)//4} tokens)")

            try:
                await client.query(prompt)
                result = None
                text = ""
                sys_msgs = []
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(msg, SystemMessage):
                        sys_msgs.append(msg)
                        print(f"  SystemMessage: subtype={msg.subtype}")
                        if msg.subtype not in ('init',):
                            print(f"    data: {json.dumps(msg.data, indent=2, default=str)[:500]}")
                    elif isinstance(msg, ResultMessage):
                        result = msg

                if result:
                    print(f"  Response: '{text.strip()[:100]}' | cost=${result.total_cost_usd} | err={result.is_error}")
                    if result.is_error:
                        print(f"  ERROR result: {result.result}")
                        break

            except Exception as e:
                print(f"  EXCEPTION: {type(e).__name__}: {e}")
                break

            if compact_events:
                print(f"\n*** COMPACTION TRIGGERED at turn {i+1}! ***")
                # Send one more message to see post-compaction behavior
                print("\n--- Post-compaction turn ---")
                await client.query("What message number was I on?")
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"  Post-compact response: {block.text[:300]}")
                    elif isinstance(msg, SystemMessage):
                        print(f"  Post-compact SystemMsg: subtype={msg.subtype}")
                    elif isinstance(msg, ResultMessage):
                        print(f"  Post-compact cost: ${msg.total_cost_usd}")
                break

    # Report
    print(f"\n{'='*60}")
    print(f"RESULTS:")
    print(f"  Compaction events: {len(compact_events)}")
    for j, evt in enumerate(compact_events):
        print(f"\n  Event {j+1}:")
        print(json.dumps(evt, indent=4, default=str))

    if not compact_events:
        print("  No compaction triggered")


if __name__ == "__main__":
    asyncio.run(main())
