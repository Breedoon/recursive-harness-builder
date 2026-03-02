"""
Spike: Trigger compaction with moderate-sized user messages.

Previous spike: 50K chars per message was too aggressive — overflowed in one turn.
This spike: ~20K chars per message (≈5K tokens). Should fill 200K in ~30 turns.
Compaction threshold is typically ~80% of context, so ~25 turns should trigger it.

Key discovery from fast spike: SystemMessage subtype="status" with status="compacting"
is emitted during compaction. The PreCompact hook fires BEFORE that status message.
"""
import asyncio
import json
import sys
import os

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
all_system_messages = []
cumulative_input_chars = 0


async def on_pre_compact(hook_input, tool_use_id, context):
    """Log and allow compaction."""
    evt = dict(hook_input)
    compact_events.append(evt)
    print(f"\n{'='*60}")
    print(f"PreCompact HOOK FIRED!")
    for k, v in evt.items():
        print(f"  {k}: {v}")
    print(f"{'='*60}\n")
    return {"continue_": True}


async def main():
    global cumulative_input_chars
    print("=== Spike: Moderate-paced compaction trigger ===\n")

    # ~20K chars per message ≈ 5K tokens
    # After 25 turns: ~125K input tokens + system prompt + responses
    # Should approach compaction threshold around turn 20-25
    padding = "The quick brown fox jumps over the lazy dog. " * 450  # ~20K chars

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with ONLY the word 'OK' and the message number. Nothing else.",
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
        for i in range(50):
            prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. Padding: {padding}"
            cumulative_input_chars += len(prompt)
            approx_cumulative_tokens = cumulative_input_chars // 4

            print(f"Turn {i+1}: sent ~{len(prompt)//1000}K chars, cumulative ~{approx_cumulative_tokens//1000}K tokens")

            try:
                await client.query(prompt)
                result = None
                text = ""
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(msg, SystemMessage):
                        entry = {"turn": i+1, "subtype": msg.subtype}
                        if msg.subtype != "init":
                            entry["data"] = msg.data
                        all_system_messages.append(entry)
                        if msg.subtype != "init":
                            print(f"  ** SystemMessage: {msg.subtype} -> {json.dumps(msg.data, default=str)[:300]}")
                    elif isinstance(msg, ResultMessage):
                        result = msg

                if result:
                    status = f"'{text.strip()[:50]}' cost=${result.total_cost_usd:.4f}"
                    if result.is_error:
                        status += f" ERROR: {result.result}"
                    print(f"  -> {status}")

                    if result.is_error:
                        break

            except Exception as e:
                print(f"  EXCEPTION: {type(e).__name__}: {e}")
                break

            if compact_events:
                print(f"\n*** COMPACTION TRIGGERED at turn {i+1}! ***")
                print(f"Cumulative input: ~{approx_cumulative_tokens//1000}K tokens")

                # Post-compaction: send a message to verify session continues
                print("\n--- Post-compaction test ---")
                await client.query("What was the last message number you saw before this?")
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"  Post-compact: {block.text[:500]}")
                    elif isinstance(msg, SystemMessage):
                        if msg.subtype != "init":
                            print(f"  Post-compact SystemMsg: {msg.subtype} -> {json.dumps(msg.data, default=str)[:200]}")
                    elif isinstance(msg, ResultMessage):
                        print(f"  Post-compact cost: ${msg.total_cost_usd:.4f}")
                break

    # Full report
    print(f"\n{'='*60}")
    print(f"FULL REPORT")
    print(f"  Total turns: {i+1}")
    print(f"  Cumulative input chars: {cumulative_input_chars}")
    print(f"  Approx input tokens: {cumulative_input_chars // 4}")
    print(f"  Compaction events: {len(compact_events)}")
    print(f"\nNon-init SystemMessages:")
    for sm in all_system_messages:
        if sm.get("subtype") != "init":
            print(f"  Turn {sm['turn']}: {json.dumps(sm, indent=2, default=str)}")
    if compact_events:
        print(f"\nCompaction event details:")
        for evt in compact_events:
            print(json.dumps(evt, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
