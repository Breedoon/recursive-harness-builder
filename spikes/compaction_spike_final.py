"""
Spike: Proper PreCompact hook test with HookMatcher (not raw dict).

Previous spikes used plain dicts for hook matchers, but the SDK's
_convert_hooks_to_internal_format uses hasattr() which fails on dicts.
Must use HookMatcher dataclass instances.

This spike:
1. Uses proper HookMatcher instances
2. Forks the session already near compaction (from fast2 spike)
3. Tests PreCompact hook firing + what we can do inside it
4. Tests whether we can do file I/O inside the hook
5. Tests whether blocking works
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

# State
compact_events = []
WORK_DIR = Path(tempfile.mkdtemp(prefix="compact_final_"))
NEAR_COMPACT_SESSION = "cec20d87-81f4-4ada-afcd-567f61b98091"

print(f"Work dir: {WORK_DIR}")


async def on_pre_compact(hook_input, tool_use_id, context):
    """PreCompact hook — log everything and test file I/O."""
    evt = dict(hook_input)
    compact_events.append(evt)

    print(f"\n{'='*60}")
    print(f"PreCompact HOOK FIRED!")
    print(f"  trigger: {evt.get('trigger')}")
    print(f"  custom_instructions: {evt.get('custom_instructions')}")
    print(f"  session_id: {evt.get('session_id')}")
    print(f"  transcript_path: {evt.get('transcript_path')}")
    print(f"  cwd: {evt.get('cwd')}")
    print(f"  permission_mode: {evt.get('permission_mode')}")
    print(f"  All keys: {list(evt.keys())}")

    # === Test: Can we do file I/O? ===
    try:
        summary = (
            f"PreCompact Summary\n"
            f"Time: {datetime.now().isoformat()}\n"
            f"Trigger: {evt.get('trigger')}\n"
            f"Session: {evt.get('session_id')}\n"
            f"Pre-compact data: {json.dumps(evt, indent=2, default=str)}\n"
        )
        path = WORK_DIR / "precompact_capture.txt"
        path.write_text(summary)
        print(f"  FILE I/O: Successfully wrote {path} ({len(summary)} bytes)")
    except Exception as e:
        print(f"  FILE I/O ERROR: {e}")

    # === Test: Can we read the transcript? ===
    transcript_path = evt.get('transcript_path')
    if transcript_path:
        try:
            tp = Path(transcript_path)
            if tp.exists():
                content = tp.read_text()
                print(f"  TRANSCRIPT: {len(content)} chars at {transcript_path}")
                # Save a snapshot
                snapshot = WORK_DIR / "transcript_snapshot.json"
                snapshot.write_text(content[:50000])  # First 50KB
                print(f"  TRANSCRIPT: Saved snapshot ({min(len(content), 50000)} chars)")
            else:
                print(f"  TRANSCRIPT: Path doesn't exist: {transcript_path}")
        except Exception as e:
            print(f"  TRANSCRIPT ERROR: {e}")

    print(f"{'='*60}\n")

    # Allow compaction
    return {"continue_": True}


async def on_pre_tool_use(hook_input, tool_use_id, context):
    """Confirm PreToolUse fires to validate hook registration."""
    print(f"  [PreToolUse] tool={hook_input.get('tool_name')}")
    return {"continue_": True}


async def main():
    print("=== Spike: PreCompact hook with proper HookMatcher ===\n")
    print(f"Forking session {NEAR_COMPACT_SESSION} (already ~167K tokens)")

    padding = "The quick brown fox jumps over the lazy dog. " * 450  # ~20K chars

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with ONLY 'OK N' where N is the message number. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        resume=NEAR_COMPACT_SESSION,
        fork_session=True,
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_pre_compact]),
            ],
            "PreToolUse": [
                HookMatcher(matcher=None, hooks=[on_pre_tool_use]),
            ],
        },
        thinking={"type": "disabled"},
    )

    async with ClaudeSDKClient(options) as client:
        for i in range(25):
            prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. Padding: {padding}"
            print(f"Turn {i+1}: ~{len(prompt)//1000}K chars")

            try:
                await client.query(prompt)
                text = ""
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text += block.text
                    elif isinstance(msg, SystemMessage):
                        if msg.subtype != "init":
                            print(f"  ** SystemMsg: {msg.subtype} -> {json.dumps(msg.data, default=str)[:300]}")
                    elif isinstance(msg, ResultMessage):
                        status = f"'{text.strip()[:50]}' cost=${msg.total_cost_usd:.4f}"
                        if msg.is_error:
                            status += f" ERROR: {msg.result}"
                        print(f"  -> {status}")
                        if msg.is_error:
                            break

            except Exception as e:
                print(f"  EXCEPTION: {type(e).__name__}: {e}")
                break

            if compact_events:
                print(f"\n*** COMPACTION TRIGGERED at turn {i+1}! ***")

                # Post-compaction test
                print("\n--- Post-compaction memory test ---")
                await client.query("What was the first message number you remember?")
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"  Post-compact: {block.text[:500]}")
                    elif isinstance(msg, ResultMessage):
                        print(f"  Cost: ${msg.total_cost_usd:.4f}")
                break

    # Report
    print(f"\n{'='*60}")
    print(f"FINAL REPORT")
    print(f"  Compaction events: {len(compact_events)}")
    print(f"  Work dir: {WORK_DIR}")

    # Check files
    for f in sorted(WORK_DIR.iterdir()):
        size = f.stat().st_size
        print(f"  File: {f.name} ({size} bytes)")
        if f.suffix == '.txt' and size < 2000:
            print(f"    Content: {f.read_text()[:500]}")

    if compact_events:
        print(f"\nPreCompact event data:")
        for evt in compact_events:
            print(json.dumps(evt, indent=2, default=str))
    else:
        print("\n  *** PreCompact STILL NOT FIRED ***")
        print("  This is a definitive finding: the Python SDK may not relay")
        print("  PreCompact hooks, or the CLI may not call them for auto-compact.")


if __name__ == "__main__":
    asyncio.run(main())
