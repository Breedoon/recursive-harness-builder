"""
Spike: Actually disable compaction — three approaches.

A) settings JSON param with autoCompactEnabled: false
B) setting_sources=["user"] to inherit global ~/.claude.json config
C) settings JSON file on disk pointed to by --settings

Goal: find an approach that ACTUALLY prevents compaction from firing.
Reuse the session near compaction threshold to test quickly.
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

NEAR_COMPACT_SESSION = "cec20d87-81f4-4ada-afcd-567f61b98091"  # ~167K tokens
padding = "The quick brown fox jumps over the lazy dog. " * 450


async def run_test(label: str, extra_opts: dict, turns: int = 20):
    """Run a test with given options and report whether compaction fired."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"  Extra opts: {extra_opts}")
    print(f"{'='*60}")

    compact_events = []
    system_events = []

    async def on_pre_compact(hook_input, tool_use_id, context):
        compact_events.append(dict(hook_input))
        print(f"    *** PreCompact FIRED! trigger={hook_input.get('trigger')} ***")
        return {"continue_": True}

    base_opts = dict(
        model="haiku",
        system_prompt="Reply with ONLY 'OK N'. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        resume=NEAR_COMPACT_SESSION,
        fork_session=True,
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_pre_compact]),
            ],
        },
        thinking={"type": "disabled"},
    )
    base_opts.update(extra_opts)
    options = ClaudeAgentOptions(**base_opts)

    error_msg = None
    last_total = 0
    async with ClaudeSDKClient(options) as client:
        for i in range(turns):
            prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. Padding: {padding}"
            try:
                await client.query(prompt)
                text = ""
                usage_data = None
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(msg, SystemMessage):
                        if msg.subtype not in ("init",):
                            system_events.append({"turn": i+1, "subtype": msg.subtype})
                            print(f"    Turn {i+1} SystemMsg: {msg.subtype}")
                            if msg.subtype == "compact_boundary":
                                meta = msg.data.get("compact_metadata", {})
                                print(f"      pre_tokens={meta.get('pre_tokens')}")
                    elif isinstance(msg, ResultMessage):
                        usage_data = msg.usage
                        if msg.is_error:
                            error_msg = msg.result
                            print(f"    Turn {i+1} ERROR: {error_msg}")

                if usage_data:
                    total = usage_data.get("input_tokens", 0) + usage_data.get("cache_read_input_tokens", 0) + usage_data.get("cache_creation_input_tokens", 0)
                    last_total = total
                    print(f"  Turn {i+1}: total={total:,} | '{text.strip()[:20]}'")
                else:
                    print(f"  Turn {i+1}: no usage | '{text.strip()[:20]}'")

                if error_msg:
                    break

            except Exception as e:
                error_msg = str(e)
                print(f"  Turn {i+1}: EXCEPTION {type(e).__name__}: {e}")
                break

    result = {
        "label": label,
        "compact_events": len(compact_events),
        "system_events": system_events,
        "error": error_msg,
        "turns_completed": i + 1,
        "last_total_tokens": last_total,
        "compaction_happened": any(e["subtype"] == "compact_boundary" for e in system_events),
    }
    status = "COMPACTED" if result["compaction_happened"] else ("ERROR" if error_msg else "NO COMPACTION")
    print(f"\n  Result: {status} | {len(compact_events)} hook calls | {i+1} turns | last_total={last_total:,}")
    return result


async def main():
    print("=== Spike: Disable compaction ===\n")
    results = []

    # --- Test A: settings JSON string ---
    r = await run_test(
        "A: settings JSON string (autoCompactEnabled=false)",
        {"settings": json.dumps({"autoCompactEnabled": False})},
    )
    results.append(r)

    # --- Test B: setting_sources=["user"] to load global config ---
    # First set global config
    print("\n  Setting global autoCompactEnabled=false...")
    claude_json = Path.home() / ".claude.json"
    existing = json.loads(claude_json.read_text()) if claude_json.exists() else {}
    existing["autoCompactEnabled"] = False
    claude_json.write_text(json.dumps(existing, indent=2))
    print(f"  Wrote autoCompactEnabled=false to {claude_json}")

    r = await run_test(
        "B: setting_sources=['user'] (global config)",
        {"setting_sources": ["user"]},
    )
    results.append(r)

    # --- Test C: setting_sources=["user","project"] (both) ---
    r = await run_test(
        "C: setting_sources=['user','project']",
        {"setting_sources": ["user", "project"]},
    )
    results.append(r)

    # --- Test D: settings file on disk ---
    settings_file = Path(tempfile.mktemp(suffix=".json", prefix="compact_settings_"))
    settings_file.write_text(json.dumps({"autoCompactEnabled": False}))
    print(f"\n  Created settings file: {settings_file}")

    r = await run_test(
        f"D: settings file path ({settings_file})",
        {"settings": str(settings_file)},
    )
    results.append(r)

    # --- Test E: both settings JSON + setting_sources=["user"] ---
    r = await run_test(
        "E: settings JSON + setting_sources=['user']",
        {
            "settings": json.dumps({"autoCompactEnabled": False}),
            "setting_sources": ["user"],
        },
    )
    results.append(r)

    # --- Cleanup: restore global config ---
    print("\n  Restoring global config (removing autoCompactEnabled)...")
    existing = json.loads(claude_json.read_text())
    existing.pop("autoCompactEnabled", None)
    claude_json.write_text(json.dumps(existing, indent=2))
    print(f"  Cleaned up {claude_json}")

    # Final report
    print(f"\n{'='*60}")
    print("FINAL COMPARISON")
    print(f"{'='*60}")
    print(f"{'Test':<55} {'Compacted?':<12} {'Turns':<8} {'Last Tokens':>12}")
    print("-" * 90)
    for r in results:
        status = "YES" if r["compaction_happened"] else ("ERR" if r["error"] else "NO")
        print(f"{r['label']:<55} {status:<12} {r['turns_completed']:<8} {r['last_total_tokens']:>12,}")


if __name__ == "__main__":
    asyncio.run(main())
