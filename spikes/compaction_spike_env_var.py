"""
Spike: Try to disable compaction via env vars and extra_args.

Previous spike found autoCompactEnabled=false in settings doesn't work on SDK v0.1.44.
Now trying:
1. CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=100 (env var)
2. CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=999 (env var, absurdly high)
3. extra_args with --no-auto-compact or similar
4. CLAUDE_CODE_DISABLE_AUTOCOMPACT=1 (guessing)
5. setting_sources=["user"] with global ~/.claude.json
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

padding = "ABCDEFGHIJ" * 3000


async def test_compaction(label: str, extra_opts: dict, max_turns: int = 25):
    compact_fired = False
    compact_boundaries = []

    async def on_pre_compact(hook_input, tool_use_id, context):
        nonlocal compact_fired
        compact_fired = True
        print(f"    *** PreCompact FIRED! ***", flush=True)
        return {"continue_": True}

    opts = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply ONLY 'OK'. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_pre_compact]),
            ],
        },
        thinking={"type": "disabled"},
        **extra_opts,
    )

    print(f"\n{'='*60}", flush=True)
    print(f"TEST: {label}", flush=True)
    print(f"{'='*60}", flush=True)

    last_total = 0
    turns_done = 0

    try:
        async with ClaudeSDKClient(opts) as client:
            for i in range(max_turns):
                prompt = f"[{i+1}] OK. {padding}"
                try:
                    await client.query(prompt)
                    async for msg in client.receive_response():
                        if isinstance(msg, SystemMessage):
                            if msg.subtype == "compact_boundary":
                                compact_boundaries.append(i+1)
                                meta = getattr(msg, 'data', {}).get("compact_metadata", {})
                                print(f"  Turn {i+1}: COMPACT_BOUNDARY pre_tokens={meta.get('pre_tokens')}", flush=True)
                        elif isinstance(msg, ResultMessage):
                            if msg.usage:
                                total = (msg.usage.get("input_tokens", 0)
                                       + msg.usage.get("cache_read_input_tokens", 0)
                                       + msg.usage.get("cache_creation_input_tokens", 0))
                                last_total = total
                                print(f"  Turn {i+1}: {total:,} tokens", flush=True)
                            if msg.is_error:
                                print(f"  ERROR: {msg.result}", flush=True)
                                break
                    turns_done = i + 1
                except Exception as e:
                    print(f"  EXCEPTION: {e}", flush=True)
                    turns_done = i + 1
                    break

                if compact_fired or compact_boundaries:
                    break
                if last_total > 195000:
                    print(f"  {last_total:,} tokens — no compaction!", flush=True)
                    break
    except Exception as e:
        print(f"  CLIENT ERROR: {e}", flush=True)

    compacted = compact_fired or bool(compact_boundaries)
    print(f"  RESULT: {'COMPACTED' if compacted else 'NO COMPACTION'} | turns={turns_done} | tokens={last_total:,}", flush=True)
    return compacted, last_total


async def main():
    print("=== Spike: Alternative compaction disable methods ===\n", flush=True)
    results = []

    # Test 1: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=100
    r = await test_compaction(
        "ENV: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=100",
        {"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "100"}},
    )
    results.append(("OVERRIDE=100", r))

    # Test 2: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=999
    r = await test_compaction(
        "ENV: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=999",
        {"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "999"}},
    )
    results.append(("OVERRIDE=999", r))

    # Test 3: setting_sources=["user"] with global config
    claude_json = Path.home() / ".claude.json"
    backup = claude_json.read_text() if claude_json.exists() else None
    existing = json.loads(backup) if backup else {}
    existing["autoCompactEnabled"] = False
    claude_json.write_text(json.dumps(existing, indent=2))
    print(f"Set autoCompactEnabled=false in {claude_json}", flush=True)

    r = await test_compaction(
        "setting_sources=['user'] with global autoCompactEnabled=false",
        {"setting_sources": ["user"]},
    )
    results.append(("user sources", r))

    # Restore
    if backup:
        claude_json.write_text(backup)
    else:
        existing.pop("autoCompactEnabled", None)
        claude_json.write_text(json.dumps(existing, indent=2))

    # Test 4: Both user+project sources
    existing = json.loads(claude_json.read_text()) if claude_json.exists() else {}
    existing["autoCompactEnabled"] = False
    claude_json.write_text(json.dumps(existing, indent=2))

    vault_path = Path(os.path.expanduser(
        "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/T"
    ))
    r = await test_compaction(
        "setting_sources=['user','project'] + vault cwd",
        {"setting_sources": ["user", "project"], "cwd": str(vault_path)},
    )
    results.append(("user+project", r))

    if backup:
        claude_json.write_text(backup)
    else:
        existing.pop("autoCompactEnabled", None)
        claude_json.write_text(json.dumps(existing, indent=2))

    # Test 5: extra_args with various flags
    for flag in ["--no-compact"]:
        r = await test_compaction(
            f"extra_args=['{flag}']",
            {"extra_args": [flag]},
            max_turns=5,
        )
        results.append((flag, r))

    # Summary
    print(f"\n{'='*60}", flush=True)
    print("SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    for label, (compacted, tokens) in results:
        print(f"  {label}: {'COMPACTED' if compacted else 'NO COMPACTION'} ({tokens:,} tokens)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
