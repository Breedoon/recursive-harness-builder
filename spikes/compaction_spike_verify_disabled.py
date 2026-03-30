"""
Spike: Verify whether autoCompactEnabled=false actually works
when using setting_sources=["project"] with the vault's .claude/settings.json.

The user reports compaction IS happening despite the setting being false.
Let's reproduce and investigate.

Tests:
1. setting_sources=["project"] with vault cwd (mirrors production)
2. settings file path directly (spike report said this works)
3. Baseline with no compaction disabling (should compact)
4. setting_sources=["project"] with a DIFFERENT cwd that has the setting
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
    AssistantMessage,
    ResultMessage,
)

VAULT_PATH = Path(os.path.expanduser(
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/T"
))

# Verify settings file exists and has the right content
settings_path = VAULT_PATH / ".claude" / "settings.json"
print(f"Settings file: {settings_path}")
print(f"  Exists: {settings_path.exists()}")
if settings_path.exists():
    print(f"  Content: {settings_path.read_text().strip()}")

# Large padding to fill context fast
padding = "The quick brown fox jumps over the lazy dog. " * 500  # ~25K chars per msg


async def run_test(label: str, opts_kwargs: dict, max_turns: int = 25) -> dict:
    """Run a test and report whether compaction fired."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"  Options: {opts_kwargs}")
    print(f"{'='*60}")

    compact_events = []
    compact_boundaries = []

    async def on_pre_compact(hook_input, tool_use_id, context):
        evt = dict(hook_input)
        compact_events.append(evt)
        print(f"    *** PreCompact FIRED! trigger={evt.get('trigger')} ***")
        print(f"    *** session_id={evt.get('session_id')} ***")
        return {"continue_": True}

    base_opts = dict(
        model="haiku",
        system_prompt="Reply with ONLY 'OK'. Nothing else. One word.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_pre_compact]),
            ],
        },
        thinking={"type": "disabled"},
    )
    base_opts.update(opts_kwargs)

    options = ClaudeAgentOptions(**base_opts)

    session_id = None
    last_total = 0
    error_msg = None
    turns_done = 0

    try:
        async with ClaudeSDKClient(options) as client:
            for i in range(max_turns):
                prompt = f"[MSG-{i+1}] Reply 'OK'. Padding: {padding}"
                try:
                    response = await client.send_message(prompt)

                    text = ""
                    for msg in client.get_conversation_messages():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    text += block.text
                        elif isinstance(msg, SystemMessage):
                            if msg.subtype == "compact_boundary":
                                meta = getattr(msg, 'data', {}).get("compact_metadata", {})
                                compact_boundaries.append({
                                    "turn": i+1,
                                    "pre_tokens": meta.get("pre_tokens"),
                                })
                                print(f"    Turn {i+1} COMPACT_BOUNDARY! pre_tokens={meta.get('pre_tokens')}")

                    if isinstance(response, ResultMessage):
                        session_id = response.session_id
                        if response.usage:
                            total = (response.usage.get("input_tokens", 0)
                                   + response.usage.get("cache_read_input_tokens", 0)
                                   + response.usage.get("cache_creation_input_tokens", 0))
                            last_total = total
                            print(f"  Turn {i+1}: total={total:,} tokens | '{text.strip()[:30]}'")
                        if response.is_error:
                            error_msg = str(response.result)[:200]
                            print(f"    ERROR: {error_msg}")
                            break

                    turns_done = i + 1

                except Exception as e:
                    error_msg = f"{type(e).__name__}: {e}"
                    print(f"  Turn {i+1}: EXCEPTION {error_msg[:200]}")
                    turns_done = i + 1
                    break

                # Stop after compaction or if we're well past threshold
                if compact_events or compact_boundaries:
                    print(f"\n  Compaction detected at turn {i+1}!")
                    break
                if last_total > 190000:
                    print(f"\n  Reached {last_total:,} tokens without compaction — stopping")
                    break

    except Exception as e:
        error_msg = f"Client error: {type(e).__name__}: {e}"
        print(f"  CLIENT ERROR: {error_msg[:300]}")

    compacted = bool(compact_events) or bool(compact_boundaries)
    status = "COMPACTED" if compacted else ("ERROR" if error_msg else "NO COMPACTION")
    print(f"\n  Result: {status}")
    print(f"    Hook events: {len(compact_events)}")
    print(f"    Boundary events: {len(compact_boundaries)}")
    print(f"    Turns: {turns_done}")
    print(f"    Last total tokens: {last_total:,}")

    return {
        "label": label,
        "compacted": compacted,
        "hook_events": len(compact_events),
        "boundary_events": len(compact_boundaries),
        "turns": turns_done,
        "last_total": last_total,
        "error": error_msg,
    }


async def main():
    print("=== Spike: Verify autoCompactEnabled=false behavior ===\n")
    results = []

    # Test 1: Mirror production setup exactly
    print("\n>>> TEST 1: Production mirror (setting_sources=['project'], vault cwd)")
    r = await run_test(
        "1: setting_sources=['project'] + vault cwd",
        {
            "setting_sources": ["project"],
            "cwd": str(VAULT_PATH),
        },
    )
    results.append(r)

    # Test 2: Direct settings file path
    print("\n>>> TEST 2: Direct settings file path")
    r = await run_test(
        "2: settings=<file path>",
        {
            "settings": str(settings_path),
        },
    )
    results.append(r)

    # Test 3: Create a temp settings file with the flag
    tmp_settings = Path(tempfile.mktemp(suffix=".json", prefix="compact_test_"))
    tmp_settings.write_text(json.dumps({"autoCompactEnabled": False}))
    print(f"\n>>> TEST 3: Temp settings file at {tmp_settings}")
    r = await run_test(
        "3: settings=<temp file path>",
        {
            "settings": str(tmp_settings),
        },
    )
    results.append(r)

    # Test 4: Baseline — should compact
    print("\n>>> TEST 4: Baseline (no disabling, should compact)")
    r = await run_test(
        "4: Baseline (default, should compact)",
        {},
    )
    results.append(r)

    # Final comparison
    print(f"\n{'='*60}")
    print("FINAL COMPARISON")
    print(f"{'='*60}")
    print(f"{'Test':<55} {'Compacted?':<12} {'Turns':<8} {'Tokens':>12}")
    print("-" * 90)
    for r in results:
        status = "YES" if r["compacted"] else ("ERR" if r["error"] else "NO")
        print(f"{r['label']:<55} {status:<12} {r['turns']:<8} {r['last_total']:>12,}")

    # Cleanup
    tmp_settings.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
