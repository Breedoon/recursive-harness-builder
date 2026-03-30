"""
Spike v2: Test 1M context window compaction behavior.

v1 hit rate limits on sonnet. This version:
- Uses haiku for quick mechanism verification (cheap, fast)
- Uses opus[1m] for the actual 1M push (user has Max plan capacity)
- Smaller padding per message to avoid rate limit bursts
- Adds retry logic for transient rate limits

Tests:
1. haiku baseline: verify settings file disable still works
2. opus[1m] with compaction enabled: where does it compact?
3. opus[1m] with compaction disabled: push to 400K+ (save session for reuse)
"""
import asyncio
import json
import sys
import os
import tempfile
import time
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

RESULTS_FILE = Path(__file__).parent / "sonnet_1m_compaction_results_v2.json"
# ~10K tokens per message (40K chars / 4 chars per token)
PADDING = "ABCDEFGHIJ" * 4_000  # 40K chars


def get_total_context(usage: dict) -> int:
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


async def run_session(label: str, model: str, extra_opts: dict,
                      target_tokens: int = 0, max_turns: int = 50,
                      retry_on_ratelimit: bool = True):
    """Run a session, push context, report compaction behavior."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"  model={model}, target={target_tokens:,} tokens, max_turns={max_turns}")
    print(f"{'='*70}", flush=True)

    compact_events = []

    async def on_compact(hook_input, tool_use_id, context):
        compact_events.append(dict(hook_input))
        print(f"  *** PreCompact! trigger={hook_input.get('trigger')} "
              f"custom_instructions={hook_input.get('custom_instructions')} ***", flush=True)
        return {"continue_": True}

    # Remove resume/fork_session from extra_opts if they're in there
    # (they conflict with ClaudeAgentOptions constructor)
    opts_dict = dict(
        model=model,
        system_prompt="Reply with ONLY 'OK N' where N is the message number. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_compact]),
            ],
        },
        thinking={"type": "disabled"},
    )
    opts_dict.update(extra_opts)
    opts = ClaudeAgentOptions(**opts_dict)

    session_id = None
    turn_log = []
    error_msg = None
    last_total = 0
    cumulative_cost = 0.0

    try:
        async with ClaudeSDKClient(opts) as client:
            for i in range(max_turns):
                prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. {PADDING}"

                retries = 3 if retry_on_ratelimit else 0
                for attempt in range(retries + 1):
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
                                    meta = msg.data.get("compact_metadata", {})
                                    compact_boundary = meta
                                    print(f"  Turn {i+1}: *** COMPACTED *** pre_tokens={meta.get('pre_tokens'):,}", flush=True)
                                elif msg.subtype not in ("init",):
                                    print(f"  Turn {i+1}: SystemMsg: {msg.subtype}", flush=True)
                            elif isinstance(msg, ResultMessage):
                                usage_data = msg.usage
                                if not session_id:
                                    session_id = getattr(msg, 'session_id', None)
                                if msg.is_error and "rate limit" in str(msg.result).lower():
                                    raise Exception(f"Rate limit: {msg.result}")
                                elif msg.is_error:
                                    error_msg = msg.result
                                    print(f"  Turn {i+1}: ERROR: {error_msg}", flush=True)

                        # Success — break retry loop
                        break

                    except Exception as e:
                        if "rate limit" in str(e).lower() and attempt < retries:
                            wait = 15 * (attempt + 1)
                            print(f"  Turn {i+1}: Rate limited, waiting {wait}s (attempt {attempt+1}/{retries})...", flush=True)
                            await asyncio.sleep(wait)
                            continue
                        raise

                entry = {
                    "turn": i + 1,
                    "response": text.strip()[:30] if 'text' in dir() else "",
                    "usage": usage_data,
                    "compact_boundary": compact_boundary,
                }
                turn_log.append(entry)

                if usage_data:
                    total = get_total_context(usage_data)
                    last_total = total
                    cost_turn = (usage_data.get("cache_read_input_tokens", 0) * 0.30
                                + usage_data.get("cache_creation_input_tokens", 0) * 3.75
                                + usage_data.get("input_tokens", 0) * 3.0) / 1_000_000
                    cumulative_cost += cost_turn
                    print(f"  Turn {i+1}: {total:,} tokens (turn=${cost_turn:.3f}, cum=${cumulative_cost:.3f}) | '{text.strip()[:20]}'", flush=True)

                if error_msg:
                    break
                if compact_boundary:
                    # Continue 2 more turns post-compact
                    for j in range(2):
                        await client.query(f"[POST-{j+1}] Reply 'POST OK'.")
                        async for msg2 in client.receive_response():
                            if isinstance(msg2, ResultMessage) and msg2.usage:
                                t2 = get_total_context(msg2.usage)
                                print(f"    Post-compact {j+1}: {t2:,} tokens", flush=True)
                    break
                if target_tokens and total >= target_tokens:
                    print(f"\n  *** TARGET REACHED: {total:,} >= {target_tokens:,} ***", flush=True)
                    break

    except Exception as e:
        error_msg = str(e)
        print(f"  EXCEPTION: {type(e).__name__}: {e}", flush=True)

    result = {
        "label": label,
        "model": model,
        "session_id": session_id,
        "turns_completed": len(turn_log),
        "last_total_tokens": last_total,
        "compact_events": len(compact_events),
        "compacted": any(t.get("compact_boundary") for t in turn_log),
        "compacted_at_turn": next((t["turn"] for t in turn_log if t.get("compact_boundary")), None),
        "compacted_at_tokens": next(
            (t["compact_boundary"].get("pre_tokens") for t in turn_log if t.get("compact_boundary")),
            None
        ),
        "cumulative_cost_estimate": round(cumulative_cost, 3),
        "error": error_msg,
        "timestamp": datetime.now().isoformat(),
    }

    status = "COMPACTED" if result["compacted"] else ("ERROR" if error_msg else "NO COMPACTION")
    print(f"\n  Result: {status} | session={session_id} | {len(turn_log)} turns | {last_total:,} tokens | cost~${cumulative_cost:.3f}", flush=True)
    return result


async def main():
    print(f"=== Sonnet/Opus 1M Compaction Spike v2 ===")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Padding: {len(PADDING):,} chars per message (~{len(PADDING)//4:,} tokens)\n")

    all_results = []

    # --- TEST 1: haiku baseline — verify settings file disable works ---
    settings_file = Path(tempfile.mktemp(suffix=".json", prefix="no_compact_"))
    settings_file.write_text(json.dumps({"autoCompactEnabled": False}))

    r1 = await run_session(
        label="haiku — compaction DISABLED (settings file, baseline verification)",
        model="haiku",
        extra_opts={"settings": str(settings_file)},
        target_tokens=190_000,  # just past old haiku threshold
        max_turns=30,
    )
    all_results.append(r1)

    # --- TEST 2: opus[1m] with compaction ENABLED ---
    # Goal: find where compaction fires (~830K expected = 84% of 1M)
    # Cap at 35 turns to limit cost
    r2 = await run_session(
        label="opus[1m] — compaction ENABLED (find threshold)",
        model="opus[1m]",
        extra_opts={},
        target_tokens=900_000,
        max_turns=35,
    )
    all_results.append(r2)

    # --- TEST 3: opus[1m] with compaction DISABLED ---
    # Goal: push to 400K+ (enough to prove 1M works, cost-efficient)
    r3 = await run_session(
        label="opus[1m] — compaction DISABLED (push to 400K)",
        model="opus[1m]",
        extra_opts={"settings": str(settings_file)},
        target_tokens=400_000,
        max_turns=50,
    )
    all_results.append(r3)

    # --- TEST 4: opus[1m] with CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=95 ---
    # Does the override work upward now with 1M context?
    if r2.get("session_id") and r2.get("compacted"):
        # Fork from test 2's compacted session and push from there
        r4 = await run_session(
            label="opus[1m] — AUTOCOMPACT_PCT=95% (test upward override)",
            model="opus[1m]",
            extra_opts={"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "95"}},
            target_tokens=960_000,
            max_turns=10,
        )
        all_results.append(r4)

    # Cleanup
    settings_file.unlink(missing_ok=True)

    # Save results
    RESULTS_FILE.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nResults saved to: {RESULTS_FILE}")

    # Final summary
    print(f"\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")
    for r in all_results:
        status = "COMPACTED" if r["compacted"] else ("ERROR" if r["error"] else "NO COMPACTION")
        compact_info = f" at {r['compacted_at_tokens']:,} tokens" if r.get("compacted_at_tokens") else ""
        print(f"  {r['label'][:65]}")
        print(f"    {status}{compact_info} | {r['turns_completed']} turns | {r['last_total_tokens']:,} tokens")
        print(f"    session={r['session_id']} | cost~${r['cumulative_cost_estimate']}")


if __name__ == "__main__":
    asyncio.run(main())
