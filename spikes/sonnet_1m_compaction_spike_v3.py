"""
Spike v3: Test 1M context window compaction behavior with NEW findings.

Key findings from research:
- CLAUDE_CODE_AUTO_COMPACT_WINDOW: new env var, sets effective context capacity
- On 1M models, compaction fires at ~92% = ~920K tokens
- PostCompact hook is new (v2.1.76) — receives compact_summary
- Bug #18264: autoCompactEnabled=false reportedly ignored
- CLAUDE_AUTOCOMPACT_PCT_OVERRIDE can lower % but not raise above ~92%

Tests:
1. haiku: verify autoCompactEnabled=false is indeed broken (bug #18264)
2. haiku: test CLAUDE_CODE_AUTO_COMPACT_WINDOW to set custom threshold
3. sonnet[1m]: push past 200K with defaults (prove 1M works)
4. sonnet[1m]: test PostCompact hook (capture compact_summary)
5. sonnet[1m]: push toward 400K+ with compaction delayed via AUTO_COMPACT_WINDOW
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

RESULTS_FILE = Path(__file__).parent / "sonnet_1m_compaction_results_v3.json"

_PARAGRAPH = (
    "The history of computing is a fascinating journey that spans several decades. "
    "From the earliest mechanical calculators to modern quantum processors, each era "
    "brought innovations that transformed how humans interact with information. Charles "
    "Babbage conceived the Analytical Engine in the 1830s, establishing principles that "
    "would guide computer design for over a century. Ada Lovelace, working alongside "
    "Babbage, wrote what many consider the first computer program. The twentieth century "
    "saw the development of electronic computers, starting with machines like ENIAC and "
    "UNIVAC. These room-sized devices used vacuum tubes and consumed enormous amounts of "
    "power. The invention of the transistor at Bell Labs revolutionized the field, making "
    "computers smaller, faster, and more reliable. The integrated circuit further "
    "accelerated this trend, packing thousands and eventually billions of transistors onto "
    "tiny silicon chips. Personal computers emerged in the 1970s and 1980s, bringing "
    "computational power to homes and offices worldwide. The internet connected these "
    "machines into a global network, fundamentally changing communication, commerce, and "
    "culture. Mobile computing continued the trend toward ubiquity, placing powerful "
    "processors in nearly everyone's pocket. Today, artificial intelligence and machine "
    "learning represent the latest frontier, promising to automate complex cognitive tasks "
    "and unlock new capabilities across every domain of human endeavor. "
)
# ~40K chars = ~10K tokens per message
PADDING = _PARAGRAPH * 40


def get_total_context(usage: dict) -> int:
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


# Collect PostCompact data globally
post_compact_data = []


async def run_session(label: str, model: str, extra_opts: dict,
                      target_tokens: int = 0, max_turns: int = 50,
                      register_post_compact: bool = False):
    """Run a session, push context, report compaction behavior."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"  model={model}, target={target_tokens:,}, max_turns={max_turns}")
    print(f"{'='*70}", flush=True)

    compact_events = []
    local_post_compact = []

    async def on_pre_compact(hook_input, tool_use_id, context):
        compact_events.append(dict(hook_input))
        ci = hook_input.get('custom_instructions')
        print(f"  *** PreCompact! trigger={hook_input.get('trigger')} "
              f"custom_instructions={'<set>' if ci else None} ***", flush=True)
        return {"continue_": True}

    async def on_post_compact(hook_input, tool_use_id, context):
        data = dict(hook_input)
        local_post_compact.append(data)
        post_compact_data.append(data)
        summary = data.get("compact_summary", "")
        print(f"  *** PostCompact! summary_len={len(summary)} chars ***", flush=True)
        if summary:
            print(f"      Summary preview: {summary[:200]}...", flush=True)
        return {"continue_": True}

    hooks = {
        "PreCompact": [HookMatcher(matcher=None, hooks=[on_pre_compact])],
    }
    if register_post_compact:
        hooks["PostCompact"] = [HookMatcher(matcher=None, hooks=[on_post_compact])]

    opts_dict = dict(
        model=model,
        system_prompt="You are a helpful assistant in a context window test. "
                      "Reply with ONLY 'OK N' where N is the message number in brackets.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks=hooks,
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
                prompt = (
                    f"[{i+1}] This is message {i+1} in a context window test. "
                    f"Reply 'OK {i+1}'.\n\n{PADDING}"
                )

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
                            if msg.is_error:
                                err = str(msg.result)[:200]
                                if "rate limit" in err.lower():
                                    raise Exception(f"RateLimit: {err}")
                                error_msg = err
                                print(f"  Turn {i+1}: ERROR: {error_msg}", flush=True)

                    turn_log.append({
                        "turn": i + 1,
                        "usage": usage_data,
                        "compact_boundary": compact_boundary,
                    })

                    if usage_data:
                        total = get_total_context(usage_data)
                        last_total = total
                        cost_turn = (usage_data.get("cache_read_input_tokens", 0) * 0.30
                                    + usage_data.get("cache_creation_input_tokens", 0) * 3.75
                                    + usage_data.get("input_tokens", 0) * 3.0) / 1_000_000
                        cumulative_cost += cost_turn
                        print(f"  Turn {i+1}: {total:,} tokens (${cumulative_cost:.2f} cum) | '{text.strip()[:20]}'", flush=True)

                    if error_msg:
                        break
                    if compact_boundary:
                        # 2 post-compact turns
                        for j in range(2):
                            await client.query(f"[POST-{j+1}] Reply 'POST OK'.")
                            async for msg2 in client.receive_response():
                                if isinstance(msg2, ResultMessage) and msg2.usage:
                                    print(f"    Post-compact {j+1}: {get_total_context(msg2.usage):,} tokens", flush=True)
                        break
                    if target_tokens and total >= target_tokens:
                        print(f"\n  TARGET REACHED: {total:,} >= {target_tokens:,}", flush=True)
                        break

                except Exception as e:
                    if "ratelimit" in str(e).lower() or "rate limit" in str(e).lower():
                        print(f"  Turn {i+1}: Rate limited, waiting 30s...", flush=True)
                        await asyncio.sleep(30)
                        continue
                    error_msg = str(e)[:200]
                    print(f"  Turn {i+1}: {type(e).__name__}: {error_msg}", flush=True)
                    break

    except Exception as e:
        error_msg = str(e)[:200]
        print(f"  CLIENT ERROR: {e}", flush=True)

    result = {
        "label": label,
        "model": model,
        "session_id": session_id,
        "turns": len(turn_log),
        "last_total_tokens": last_total,
        "compact_events": len(compact_events),
        "compacted": any(t.get("compact_boundary") for t in turn_log),
        "compacted_at_turn": next((t["turn"] for t in turn_log if t.get("compact_boundary")), None),
        "compacted_at_tokens": next(
            (t["compact_boundary"].get("pre_tokens") for t in turn_log if t.get("compact_boundary")),
            None
        ),
        "post_compact_summaries": len(local_post_compact),
        "cost": round(cumulative_cost, 3),
        "error": error_msg,
    }

    status = "COMPACTED" if result["compacted"] else ("ERROR" if error_msg else "NO COMPACTION")
    ct = f" at {result['compacted_at_tokens']:,}tok" if result.get("compacted_at_tokens") else ""
    print(f"\n  RESULT: {status}{ct} | session={session_id} | {len(turn_log)} turns | {last_total:,} tokens | ~${cumulative_cost:.2f}", flush=True)
    return result


async def main():
    print(f"=== 1M Compaction Spike v3 ===")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Padding: {len(PADDING):,} chars (~{len(PADDING)//4:,} tokens)\n")

    all_results = []
    settings_file = Path(tempfile.mktemp(suffix=".json", prefix="no_compact_"))
    settings_file.write_text(json.dumps({"autoCompactEnabled": False}))

    # =====================================================================
    # PHASE 1: Haiku — cheap mechanism verification
    # =====================================================================
    print("PHASE 1: Haiku mechanism tests (cheap)")
    print("="*70)

    # Test 1A: Confirm autoCompactEnabled=false is broken (bug #18264)
    r1a = await run_session(
        "1A: haiku + autoCompactEnabled=false (settings file — expect BROKEN)",
        model="haiku",
        extra_opts={"settings": str(settings_file)},
        target_tokens=190_000,
        max_turns=30,
    )
    all_results.append(r1a)

    # Test 1B: CLAUDE_CODE_AUTO_COMPACT_WINDOW=50000 (force earlier compaction)
    r1b = await run_session(
        "1B: haiku + AUTO_COMPACT_WINDOW=50000 (should compact at ~46K)",
        model="haiku",
        extra_opts={"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "50000"}},
        target_tokens=190_000,
        max_turns=15,
    )
    all_results.append(r1b)

    # Test 1C: CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000 + PCT=50 (compound)
    r1c = await run_session(
        "1C: haiku + WINDOW=200000 + PCT=50 (should compact at ~100K)",
        model="haiku",
        extra_opts={"env": {
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
        }},
        target_tokens=190_000,
        max_turns=20,
    )
    all_results.append(r1c)

    # =====================================================================
    # PHASE 2: sonnet[1m] — the real deal
    # =====================================================================
    print(f"\n\n{'='*70}")
    print("PHASE 2: sonnet[1m] tests")
    print("="*70)

    # Test 2A: sonnet[1m] with defaults — push past 200K, find natural threshold
    # With 1M window, 92% threshold = ~920K. We'll stop at 400K to save cost.
    r2a = await run_session(
        "2A: sonnet[1m] default — push to 400K (compaction at ~920K expected)",
        model="sonnet[1m]",
        extra_opts={},
        target_tokens=400_000,
        max_turns=45,
        register_post_compact=True,
    )
    all_results.append(r2a)

    # Test 2B: sonnet[1m] + AUTO_COMPACT_WINDOW=500000 (force compaction at ~460K)
    # This tests the new env var on a 1M model
    if r2a.get("session_id") and not r2a.get("error"):
        r2b = await run_session(
            "2B: sonnet[1m] + WINDOW=500000 (should compact at ~460K)",
            model="sonnet[1m]",
            extra_opts={
                "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "500000"},
                "resume": r2a["session_id"],
                "fork_session": True,
            },
            target_tokens=500_000,
            max_turns=15,
            register_post_compact=True,
        )
        all_results.append(r2b)

    # Cleanup
    settings_file.unlink(missing_ok=True)

    # Save results
    RESULTS_FILE.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nResults saved to: {RESULTS_FILE}")

    # Save PostCompact data if any
    if post_compact_data:
        pc_file = Path(__file__).parent / "sonnet_1m_post_compact_data.json"
        pc_file.write_text(json.dumps(post_compact_data, indent=2, default=str))
        print(f"PostCompact data saved to: {pc_file}")

    # Final summary
    print(f"\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")
    for r in all_results:
        status = "COMPACTED" if r["compacted"] else ("ERROR" if r["error"] else "NO COMPACTION")
        ct = f" at {r['compacted_at_tokens']:,}tok" if r.get("compacted_at_tokens") else ""
        pc = f" [PostCompact: {r['post_compact_summaries']}]" if r.get("post_compact_summaries") else ""
        print(f"  {r['label']}")
        print(f"    {status}{ct}{pc} | {r['turns']} turns | {r['last_total_tokens']:,} tokens | ~${r['cost']}")
        if r.get("session_id"):
            print(f"    session={r['session_id']}")


if __name__ == "__main__":
    asyncio.run(main())
