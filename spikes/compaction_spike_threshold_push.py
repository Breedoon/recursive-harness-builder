"""
Spike: Push compaction threshold via CLAUDE_AUTOCOMPACT_PCT_OVERRIDE.

Question: Can we set the threshold to 99% so compaction effectively never fires?

Prior findings (v0.1.35) said override only works DOWNWARD. But that was old SDK.
Let's test systematically on v0.1.44:
  - Default (no override) — baseline
  - 50 — should compact earlier
  - 80 — default behavior?
  - 90 — later than default?
  - 95 — pushing it
  - 99 — effectively disabled?

Also test project-level settings:
  - autoCompactThreshold in .claude/settings.json (might be a real setting)
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
    HookMatcher,
    TextBlock,
    SystemMessage,
    ResultMessage,
)

padding = "ABCDEFGHIJ" * 3000  # ~30K chars ≈ ~7.5K tokens per msg


async def test(label: str, extra_opts: dict, max_turns: int = 25):
    compact_turn = None
    compact_pre_tokens = None

    async def on_pre_compact(hook_input, tool_use_id, context):
        nonlocal compact_turn, compact_pre_tokens
        compact_pre_tokens = hook_input.get("pre_tokens") or hook_input.get("session_id")
        print(f"    *** PreCompact: {dict(hook_input)} ***", flush=True)
        return {"continue_": True}

    opts = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply ONLY 'OK'.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={"PreCompact": [HookMatcher(matcher=None, hooks=[on_pre_compact])]},
        thinking={"type": "disabled"},
        **extra_opts,
    )

    print(f"\n{'='*60}", flush=True)
    print(f"TEST: {label}", flush=True)
    print(f"{'='*60}", flush=True)

    last_total = 0
    compacted = False
    error_at = None

    try:
        async with ClaudeSDKClient(opts) as client:
            for i in range(max_turns):
                try:
                    await client.query(f"[{i+1}] OK. {padding}")
                    async for msg in client.receive_response():
                        if isinstance(msg, SystemMessage):
                            if msg.subtype == "compact_boundary":
                                compact_turn = i + 1
                                meta = getattr(msg, 'data', {}).get("compact_metadata", {})
                                compact_pre_tokens = meta.get("pre_tokens")
                                compacted = True
                                print(f"  Turn {i+1}: COMPACT_BOUNDARY pre_tokens={compact_pre_tokens}", flush=True)
                        elif isinstance(msg, ResultMessage):
                            if msg.usage:
                                total = sum(msg.usage.get(k, 0) for k in [
                                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"
                                ])
                                last_total = total
                                print(f"  Turn {i+1}: {total:,} tokens", flush=True)
                            if msg.is_error:
                                error_at = (i+1, str(msg.result)[:80])
                                print(f"  Turn {i+1}: ERROR: {msg.result}", flush=True)
                                break
                except Exception as e:
                    error_at = (i+1, str(e)[:80])
                    print(f"  Turn {i+1}: EXCEPTION: {e}", flush=True)
                    break

                if compacted:
                    break
                if error_at:
                    break
    except Exception as e:
        print(f"  CLIENT ERROR: {e}", flush=True)

    if compacted:
        status = f"COMPACTED at turn {compact_turn} (pre_tokens={compact_pre_tokens})"
    elif error_at:
        status = f"ERROR at turn {error_at[0]}: {error_at[1]}"
    else:
        status = f"NO COMPACTION (reached {last_total:,} tokens)"
    print(f"  → {status}", flush=True)
    return {
        "compacted": compacted,
        "compact_turn": compact_turn,
        "compact_pre_tokens": compact_pre_tokens,
        "last_total": last_total,
        "error": error_at,
    }


async def main():
    print("=== Spike: Push compaction threshold ===\n", flush=True)
    results = []

    # Test env var at different percentages
    for pct in [50, 80, 90, 95, 99]:
        r = await test(
            f"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={pct}",
            {"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(pct)}},
        )
        results.append((f"PCT={pct}", r))

    # Test with no override (baseline)
    r = await test(
        "Baseline (no override)",
        {},
    )
    results.append(("Baseline", r))

    # Test project-level settings with autoCompactThreshold
    tmpdir = Path(tempfile.mkdtemp(prefix="compact_threshold_"))
    claude_dir = tmpdir / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"

    # Try autoCompactThreshold as a percentage
    settings.write_text(json.dumps({"autoCompactThreshold": 99}))
    r = await test(
        "Project: autoCompactThreshold=99",
        {"setting_sources": ["project"], "cwd": str(tmpdir)},
    )
    results.append(("Proj threshold=99", r))

    # Try autoCompactThreshold as token count
    settings.write_text(json.dumps({"autoCompactThreshold": 999999}))
    r = await test(
        "Project: autoCompactThreshold=999999",
        {"setting_sources": ["project"], "cwd": str(tmpdir)},
    )
    results.append(("Proj threshold=999999", r))

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    # Summary
    print(f"\n{'='*60}", flush=True)
    print("SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"{'Label':<25} {'Compacted?':<12} {'Turn':<6} {'Pre-tokens':<12} {'Last Total':>12}", flush=True)
    print("-" * 70, flush=True)
    for label, r in results:
        c = "YES" if r["compacted"] else ("ERR" if r["error"] else "NO")
        turn = str(r["compact_turn"] or "-")
        pre = str(r["compact_pre_tokens"] or "-")
        print(f"{label:<25} {c:<12} {turn:<6} {pre:<12} {r['last_total']:>12,}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
