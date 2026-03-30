"""
Spike: Test 1M context window behavior with sonnet[1m].

Tests:
1. Where does auto-compaction fire with sonnet[1m]? (~830K expected based on ~84% of 1M)
2. Can we disable compaction per-session (settings file method D from prior report)?
3. Push a session to 800K+ tokens with compaction disabled — reusable session for further testing.
4. Test if custom_instructions in PreCompact hook works now (was always None on SDK v0.1.35).

Cost estimate: ~$5-8 (sonnet 4.6 with prompt caching)
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

RESULTS_FILE = Path(__file__).parent / "sonnet_1m_compaction_results.json"
# ~25K tokens per message (100K chars / 4 chars per token)
PADDING = "ABCDEFGHIJ" * 10_000  # 100K chars


async def on_pre_compact(hook_input, tool_use_id, context):
    """Log pre-compact events and check custom_instructions."""
    print(f"\n  *** PreCompact FIRED! trigger={hook_input.get('trigger')} "
          f"custom_instructions={hook_input.get('custom_instructions')} ***\n", flush=True)
    return {"continue_": True}


def get_total_context(usage: dict) -> int:
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


async def run_session(label: str, model: str, extra_opts: dict,
                      target_tokens: int = 0, max_turns: int = 50):
    """Run a session, push context, report compaction behavior."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"  model={model}, target={target_tokens:,} tokens, max_turns={max_turns}")
    print(f"  extra_opts: {list(extra_opts.keys())}")
    print(f"{'='*70}", flush=True)

    compact_events = []

    async def on_compact(hook_input, tool_use_id, context):
        compact_events.append(dict(hook_input))
        print(f"  *** PreCompact! trigger={hook_input.get('trigger')} "
              f"custom_instructions={hook_input.get('custom_instructions')} ***", flush=True)
        return {"continue_": True}

    opts = ClaudeAgentOptions(
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
        **extra_opts,
    )

    session_id = None
    turn_log = []
    error_msg = None
    last_total = 0

    try:
        async with ClaudeSDKClient(opts) as client:
            session_id = getattr(client, 'session_id', None)

            for i in range(max_turns):
                prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. {PADDING}"

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
                                error_msg = msg.result
                                print(f"  Turn {i+1}: ERROR: {error_msg}", flush=True)

                    entry = {
                        "turn": i + 1,
                        "response": text.strip()[:30],
                        "usage": usage_data,
                        "compact_boundary": compact_boundary,
                    }
                    turn_log.append(entry)

                    if usage_data:
                        total = get_total_context(usage_data)
                        last_total = total
                        cost_input = (usage_data.get("cache_read_input_tokens", 0) * 0.30
                                    + usage_data.get("cache_creation_input_tokens", 0) * 3.75
                                    + usage_data.get("input_tokens", 0) * 3.0) / 1_000_000
                        print(f"  Turn {i+1}: {total:,} tokens (${cost_input:.3f}) | '{text.strip()[:20]}'", flush=True)
                    else:
                        print(f"  Turn {i+1}: no usage | '{text.strip()[:20]}'", flush=True)

                    if error_msg:
                        break
                    if compact_boundary:
                        # Continue 2 more turns to see post-compact behavior
                        print(f"  (continuing 2 more turns post-compact)", flush=True)
                        for j in range(2):
                            await client.query(f"[POST-{j+1}] Reply 'POST OK {j+1}'.")
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
                    print(f"  Turn {i+1}: EXCEPTION {type(e).__name__}: {e}", flush=True)
                    break

    except Exception as e:
        error_msg = str(e)
        print(f"  CLIENT ERROR: {e}", flush=True)

    # Get final session ID
    if not session_id and turn_log:
        session_id = "(check stderr for session ID)"

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
        "error": error_msg,
        "timestamp": datetime.now().isoformat(),
    }

    status = "COMPACTED" if result["compacted"] else ("ERROR" if error_msg else "NO COMPACTION")
    print(f"\n  Result: {status} | session={session_id} | {len(turn_log)} turns | {last_total:,} tokens", flush=True)
    return result


async def main():
    print(f"=== Sonnet 1M Compaction Spike ===")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Padding: {len(PADDING):,} chars per message (~{len(PADDING)//4:,} tokens)\n")

    all_results = []

    # --- TEST 1: sonnet[1m] with compaction ENABLED ---
    # Goal: find where auto-compaction fires (expected ~830K = 84% of 1M)
    # This could take many turns and be expensive, so cap at 40 turns
    r1 = await run_session(
        label="sonnet[1m] — compaction ENABLED (find threshold)",
        model="sonnet[1m]",
        extra_opts={},
        target_tokens=900_000,  # stop if we somehow get past 900K
        max_turns=40,
    )
    all_results.append(r1)

    # --- TEST 2: sonnet[1m] with compaction DISABLED (settings file) ---
    # Goal: push to 800K+ tokens, keep session for reuse
    settings_file = Path(tempfile.mktemp(suffix=".json", prefix="no_compact_"))
    settings_file.write_text(json.dumps({"autoCompactEnabled": False}))
    print(f"\nCreated settings file: {settings_file}")

    r2 = await run_session(
        label="sonnet[1m] — compaction DISABLED (settings file, push to 800K)",
        model="sonnet[1m]",
        extra_opts={"settings": str(settings_file)},
        target_tokens=800_000,
        max_turns=50,
    )
    all_results.append(r2)

    # --- TEST 3: Quick check — does CLAUDE_AUTOCOMPACT_PCT_OVERRIDE work upward now? ---
    # Previously on haiku, 95% and 99% had no effect (ceiling at ~84%).
    # With 1M window, does setting 95% push compaction to 950K?
    # Use fork from Test 1 session if available, otherwise fresh
    if r1.get("session_id") and not r1.get("error"):
        r3 = await run_session(
            label="sonnet[1m] — AUTOCOMPACT_PCT=95% (fork from test 1)",
            model="sonnet[1m]",
            extra_opts={
                "env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "95"},
                "resume": r1["session_id"],
                "fork_session": True,
            },
            target_tokens=960_000,
            max_turns=15,
        )
        all_results.append(r3)

    # Save results
    RESULTS_FILE.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nResults saved to: {RESULTS_FILE}")

    # Final summary
    print(f"\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")
    for r in all_results:
        status = "COMPACTED" if r["compacted"] else ("ERROR" if r["error"] else "NO COMPACTION")
        compact_info = f" at {r['compacted_at_tokens']:,} tokens" if r["compacted_at_tokens"] else ""
        print(f"  {r['label'][:60]}")
        print(f"    {status}{compact_info} | {r['turns_completed']} turns | {r['last_total_tokens']:,} tokens | session={r['session_id']}")

    # Cleanup
    settings_file.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
