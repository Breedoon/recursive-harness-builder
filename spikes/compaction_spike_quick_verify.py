"""
Quick spike: Does autoCompactEnabled=false actually prevent compaction?
Tests the EXACT setup used in production (setting_sources=["project"] + vault cwd).
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

settings_path = VAULT_PATH / ".claude" / "settings.json"
print(f"Settings file: {settings_path}", flush=True)
print(f"  Exists: {settings_path.exists()}", flush=True)
if settings_path.exists():
    content = settings_path.read_text().strip()
    print(f"  Content: {content}", flush=True)
    parsed = json.loads(content)
    print(f"  autoCompactEnabled: {parsed.get('autoCompactEnabled', 'NOT SET')}", flush=True)

padding = "ABCDEFGHIJ" * 3000  # ~30K chars per msg ≈ 7.5K tokens

compact_fired = False
compact_boundaries = []


async def on_pre_compact(hook_input, tool_use_id, context):
    global compact_fired
    compact_fired = True
    evt = dict(hook_input)
    print(f"\n  *** PreCompact HOOK FIRED! ***", flush=True)
    print(f"  trigger={evt.get('trigger')}", flush=True)
    print(f"  session_id={evt.get('session_id')}", flush=True)
    return {"continue_": True}


async def test_compaction(label: str, extra_opts: dict):
    global compact_fired, compact_boundaries
    compact_fired = False
    compact_boundaries = []

    print(f"\n{'='*60}", flush=True)
    print(f"TEST: {label}", flush=True)
    print(f"{'='*60}", flush=True)

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

    last_total = 0

    async with ClaudeSDKClient(opts) as client:
        for i in range(30):
            prompt = f"[{i+1}] OK. {padding}"

            try:
                await client.query(prompt)
                text = ""
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(msg, SystemMessage):
                        if msg.subtype == "compact_boundary":
                            meta = getattr(msg, 'data', {}).get("compact_metadata", {})
                            compact_boundaries.append(i+1)
                            print(f"  Turn {i+1}: COMPACT_BOUNDARY pre_tokens={meta.get('pre_tokens')}", flush=True)
                        elif msg.subtype not in ("init",):
                            print(f"  Turn {i+1}: SystemMsg subtype={msg.subtype}", flush=True)
                    elif isinstance(msg, ResultMessage):
                        if msg.usage:
                            total = (msg.usage.get("input_tokens", 0)
                                   + msg.usage.get("cache_read_input_tokens", 0)
                                   + msg.usage.get("cache_creation_input_tokens", 0))
                            last_total = total
                            print(f"  Turn {i+1}: {total:,} tokens", flush=True)
                        if msg.is_error:
                            print(f"  Turn {i+1}: ERROR {msg.result}", flush=True)
                            break

            except Exception as e:
                print(f"  Turn {i+1}: EXCEPTION {e}", flush=True)
                break

            if compact_fired or compact_boundaries:
                print(f"  COMPACTION HAPPENED at turn {i+1}!", flush=True)
                break
            if last_total > 195000:
                print(f"  Reached {last_total:,} — no compaction, stopping", flush=True)
                break

    compacted = compact_fired or bool(compact_boundaries)
    print(f"\n  RESULT: {'COMPACTED' if compacted else 'NO COMPACTION'}", flush=True)
    print(f"  Hook fired: {compact_fired}", flush=True)
    print(f"  Boundaries: {compact_boundaries}", flush=True)
    print(f"  Last tokens: {last_total:,}", flush=True)
    return compacted, last_total


async def main():
    print("=== Quick Compaction Verification ===\n", flush=True)

    # Test 1: Production mirror
    c1, t1 = await test_compaction(
        "Production mirror: setting_sources=['project'] + vault cwd",
        {"setting_sources": ["project"], "cwd": str(VAULT_PATH)},
    )

    # Test 2: Direct settings file
    c2, t2 = await test_compaction(
        "Direct settings file path",
        {"settings": str(settings_path)},
    )

    # Test 3: Temp settings file (fresh, no other interference)
    tmp = Path(tempfile.mktemp(suffix=".json"))
    tmp.write_text('{"autoCompactEnabled": false}')
    c3, t3 = await test_compaction(
        f"Temp settings file: {tmp}",
        {"settings": str(tmp)},
    )
    tmp.unlink(missing_ok=True)

    # Test 4: Baseline (should compact)
    c4, t4 = await test_compaction(
        "Baseline (no disabling)",
        {},
    )

    # Summary
    print(f"\n{'='*60}", flush=True)
    print("SUMMARY", flush=True)
    print(f"{'='*60}", flush=True)
    tests = [
        ("1: Production mirror", c1, t1),
        ("2: Direct settings file", c2, t2),
        ("3: Temp settings file", c3, t3),
        ("4: Baseline (should compact)", c4, t4),
    ]
    for label, compacted, tokens in tests:
        print(f"  {label}: {'COMPACTED' if compacted else 'NO COMPACTION'} ({tokens:,} tokens)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
