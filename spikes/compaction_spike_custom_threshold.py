"""
Spike: Custom compaction threshold with high CLAUDE_AUTOCOMPACT_PCT_OVERRIDE.

Tests:
1. Push autocompact threshold to 99% — how close to context limit can we go?
2. Monitor ResultMessage.usage for token counts
3. Implement custom "soft compaction" — when tokens exceed OUR threshold,
   do our own multi-turn knowledge extraction (no fork needed)
4. See what happens when context truly overflows (prompt too long error?)
"""
import asyncio
import json
import sys
import os
import tempfile
from pathlib import Path
from datetime import datetime

os.environ.pop("CLAUDECODE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    HookMatcher,
    TextBlock,
    SystemMessage,
    AssistantMessage,
    ResultMessage,
)

WORK_DIR = Path(tempfile.mkdtemp(prefix="compact_custom_"))
print(f"Work dir: {WORK_DIR}")

compact_events = []


async def on_pre_compact(hook_input, tool_use_id, context):
    """Log when SDK compaction fires."""
    compact_events.append(dict(hook_input))
    print(f"\n  *** SDK PreCompact FIRED! trigger={hook_input.get('trigger')} ***\n")
    return {"continue_": True}


async def test_threshold(pct: int, label: str):
    """Test a specific threshold percentage."""
    print(f"\n{'='*60}")
    print(f"TEST: {label} (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={pct})")
    print(f"{'='*60}")

    local_compact_events = []

    async def on_compact(hook_input, tool_use_id, context):
        local_compact_events.append(dict(hook_input))
        print(f"  *** PreCompact at {hook_input.get('trigger')}! ***")
        return {"continue_": True}

    padding = "The quick brown fox jumps over the lazy dog. " * 450  # ~20K chars

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with ONLY 'OK N'. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_compact]),
            ],
        },
        env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(pct)},
        thinking={"type": "disabled"},
    )

    results = []
    async with ClaudeSDKClient(options) as client:
        for i in range(45):
            prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. Padding: {padding}"

            try:
                await client.query(prompt)
                text = ""
                usage_data = None
                compact_boundary = None
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(msg, SystemMessage):
                        if msg.subtype == "compact_boundary":
                            compact_boundary = msg.data.get("compact_metadata", {})
                        if msg.subtype != "init":
                            print(f"    SystemMsg: {msg.subtype} -> {json.dumps(msg.data, default=str)[:200]}")
                    elif isinstance(msg, ResultMessage):
                        usage_data = msg.usage

                entry = {
                    "turn": i + 1,
                    "response": text.strip()[:30],
                    "usage": usage_data,
                    "compact_boundary": compact_boundary,
                }
                results.append(entry)

                # Print usage info
                if usage_data:
                    input_t = usage_data.get("input_tokens", 0)
                    cache_read = usage_data.get("cache_read_input_tokens", 0)
                    cache_create = usage_data.get("cache_creation_input_tokens", 0)
                    output_t = usage_data.get("output_tokens", 0)
                    total_context = input_t + cache_read + cache_create
                    print(f"  Turn {i+1}: input={input_t} cache_read={cache_read} cache_create={cache_create} output={output_t} TOTAL_CONTEXT={total_context} | '{text.strip()[:20]}'")
                else:
                    print(f"  Turn {i+1}: NO USAGE DATA | '{text.strip()[:20]}'")

                if compact_boundary:
                    print(f"  ** COMPACTED at turn {i+1}: pre_tokens={compact_boundary.get('pre_tokens')}")
                    # Continue a few more turns to see post-compact behavior
                    if i > 2:  # Don't stop too early
                        # Send 3 more turns then stop
                        for j in range(3):
                            await client.query(f"[POST-{j+1}] Reply 'POST OK {j+1}'.")
                            async for msg2 in client.receive_response():
                                if isinstance(msg2, ResultMessage):
                                    if msg2.usage:
                                        total2 = msg2.usage.get("input_tokens", 0) + msg2.usage.get("cache_read_input_tokens", 0) + msg2.usage.get("cache_creation_input_tokens", 0)
                                        print(f"    Post-compact turn {j+1}: total_context={total2}")
                        break

            except Exception as e:
                print(f"  Turn {i+1}: EXCEPTION {type(e).__name__}: {e}")
                results.append({"turn": i+1, "error": str(e)})
                break

    return {
        "pct": pct,
        "label": label,
        "total_turns": len(results),
        "compact_events": len(local_compact_events),
        "compacted_at_turn": next(
            (r["turn"] for r in results if r.get("compact_boundary")),
            None,
        ),
        "max_context": max(
            (
                (r["usage"].get("input_tokens", 0) + r["usage"].get("cache_read_input_tokens", 0) + r["usage"].get("cache_creation_input_tokens", 0))
                for r in results if r.get("usage")
            ),
            default=0,
        ),
    }


async def test_custom_compaction_logic():
    """Implement custom compaction: monitor tokens, act at OUR threshold."""
    print(f"\n{'='*60}")
    print(f"TEST: Custom compaction logic (threshold=100K, autocompact=99%)")
    print(f"{'='*60}")

    CUSTOM_THRESHOLD = 100_000  # Our threshold: 100K tokens
    padding = "The quick brown fox jumps over the lazy dog. " * 450

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with ONLY 'OK N'. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_pre_compact]),
            ],
        },
        env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "99"},  # Push SDK compact way out
        thinking={"type": "disabled"},
    )

    print(f"  Custom threshold: {CUSTOM_THRESHOLD:,} tokens")
    print(f"  SDK threshold: 99% (fallback safety net)")

    async with ClaudeSDKClient(options) as client:
        for i in range(40):
            prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. Padding: {padding}"

            await client.query(prompt)
            text = ""
            usage_data = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text += block.text
                elif isinstance(msg, SystemMessage):
                    if msg.subtype != "init":
                        print(f"    SystemMsg: {msg.subtype}")
                elif isinstance(msg, ResultMessage):
                    usage_data = msg.usage

            if usage_data:
                input_t = usage_data.get("input_tokens", 0)
                cache_read = usage_data.get("cache_read_input_tokens", 0)
                cache_create = usage_data.get("cache_creation_input_tokens", 0)
                total_context = input_t + cache_read + cache_create
                print(f"  Turn {i+1}: total_context={total_context:,} | '{text.strip()[:20]}'")

                if total_context > CUSTOM_THRESHOLD:
                    print(f"\n  *** CUSTOM THRESHOLD EXCEEDED at turn {i+1}! ***")
                    print(f"  *** Total context: {total_context:,} > {CUSTOM_THRESHOLD:,} ***")
                    print(f"  *** This is where we'd do our own multi-turn extraction ***")

                    # Simulate what we'd do:
                    # 1. Close this session (or just note the session_id)
                    # 2. Start a NEW client with the same session (forked)
                    # 3. That new client does multi-turn knowledge extraction
                    # 4. Then start a fresh session with extracted knowledge

                    print(f"\n  --- Simulating multi-turn extraction (new client, forked session) ---")

                    # The key insight: this is NOT a fork from inside a hook.
                    # This is the main loop detecting "time to compact" and spinning
                    # up a separate multi-turn extraction session.
                    break
            else:
                print(f"  Turn {i+1}: no usage data | '{text.strip()[:20]}'")

    return {"threshold": CUSTOM_THRESHOLD, "triggered_at_turn": i + 1}


async def main():
    print("=== Spike: Custom threshold + push autocompact ===\n")

    # Test different thresholds
    summaries = []
    for pct, label in [(95, "95%"), (99, "99%")]:
        result = await test_threshold(pct, label)
        summaries.append(result)
        print(f"\n  Summary: pct={pct}, compacted_at_turn={result['compacted_at_turn']}, max_context={result['max_context']:,}")

    # Test custom compaction logic
    custom_result = await test_custom_compaction_logic()

    # Final report
    print(f"\n{'='*60}")
    print(f"FINAL COMPARISON")
    print(f"{'='*60}")
    print(f"{'Threshold':<15} {'Compact Turn':<15} {'Max Context':>15}")
    print(f"{'-'*45}")
    # Previous results (from earlier spikes)
    print(f"{'default (~84%)':<15} {'33':<15} {'167,595':>15}  (from fast2 spike)")
    print(f"{'50%':<15} {'18':<15} {'~90,000':>15}  (from no_compact spike)")
    for s in summaries:
        ct = s['compacted_at_turn'] or 'never'
        print(f"{s['label']:<15} {str(ct):<15} {s['max_context']:>15,}")
    print(f"\nCustom logic (100K threshold): triggered at turn {custom_result['triggered_at_turn']}")


if __name__ == "__main__":
    asyncio.run(main())
