"""
Spike: Disable compaction and handle context overflow manually.

Tests:
1. Can we disable compaction via settings JSON?
2. What happens when context fills up with compaction disabled?
3. Can we detect "about to overflow" and fork proactively?
4. Can we fork right before compaction, disable compaction on the fork,
   and use it for extraction?

Methods to disable compaction:
- `settings` param: JSON string with autoCompactEnabled: false
- `extra_args`: pass CLI flags
- Environment var: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE (threshold %)
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

WORK_DIR = Path(tempfile.mkdtemp(prefix="compact_noauto_"))
NEAR_COMPACT_SESSION = "cec20d87-81f4-4ada-afcd-567f61b98091"
print(f"Work dir: {WORK_DIR}")


async def test_disable_via_settings():
    """Test disabling compaction via settings JSON."""
    print("\n" + "="*60)
    print("TEST A: Disable compaction via settings JSON")
    print("="*60)

    compact_events = []
    system_messages = []

    async def on_pre_compact(hook_input, tool_use_id, context):
        compact_events.append(dict(hook_input))
        print(f"  ** PreCompact FIRED! trigger={hook_input.get('trigger')}")
        return {"continue_": True}

    padding = "The quick brown fox jumps over the lazy dog. " * 450

    # Method 1: settings JSON string
    settings_json = json.dumps({"autoCompactEnabled": False})

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with ONLY 'OK N'. Nothing else.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        resume=NEAR_COMPACT_SESSION,
        fork_session=True,
        settings=settings_json,
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_pre_compact]),
            ],
        },
        thinking={"type": "disabled"},
    )

    async with ClaudeSDKClient(options) as client:
        for i in range(25):
            prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. Padding: {padding}"
            print(f"  Turn {i+1}: ~{len(prompt)//1000}K chars")

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
                            system_messages.append({"turn": i+1, "subtype": msg.subtype, "data": msg.data})
                            print(f"    ** {msg.subtype}: {json.dumps(msg.data, default=str)[:200]}")
                    elif isinstance(msg, ResultMessage):
                        print(f"    -> '{text.strip()[:30]}' cost=${msg.total_cost_usd:.4f} err={msg.is_error}")
                        if msg.is_error:
                            print(f"    Error: {msg.result}")
                            return {"compact_events": compact_events, "system_messages": system_messages,
                                    "error_turn": i+1, "error": msg.result}
            except Exception as e:
                print(f"    EXCEPTION: {type(e).__name__}: {e}")
                return {"compact_events": compact_events, "system_messages": system_messages,
                        "error_turn": i+1, "error": str(e)}

    return {"compact_events": compact_events, "system_messages": system_messages}


async def test_fork_no_compact():
    """Fork with compaction disabled — use for knowledge extraction."""
    print("\n" + "="*60)
    print("TEST B: Fork with compaction disabled for extraction")
    print("="*60)

    # Fork a session near compaction and disable compaction on the fork
    settings_json = json.dumps({"autoCompactEnabled": False})

    fork_opts = ClaudeAgentOptions(
        model="haiku",
        system_prompt=(
            "You are a knowledge extraction agent. Summarize the conversation "
            "history thoroughly. Include ALL key facts, decisions, and context."
        ),
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        resume=NEAR_COMPACT_SESSION,
        fork_session=True,
        settings=settings_json,
        thinking={"type": "disabled"},
    )

    print(f"  Forking session {NEAR_COMPACT_SESSION} with compaction disabled...")

    try:
        async with ClaudeSDKClient(fork_opts) as client:
            await client.query(
                "Please provide a comprehensive summary of EVERYTHING discussed in this "
                "conversation. Include all message numbers, patterns, and any key information. "
                "Be thorough — this summary will be used to preserve knowledge."
            )
            text = ""
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text += block.text
                elif isinstance(msg, SystemMessage):
                    if msg.subtype != "init":
                        print(f"    ** {msg.subtype}: {json.dumps(msg.data, default=str)[:200]}")
                elif isinstance(msg, ResultMessage):
                    print(f"    Cost: ${msg.total_cost_usd:.4f}")
                    print(f"    Error: {msg.is_error}")

            if text:
                path = WORK_DIR / "fork_extraction.md"
                path.write_text(text)
                print(f"    Extracted {len(text)} chars")
                print(f"    Saved to: {path}")
                print(f"    Preview: {text[:300]}...")
            else:
                print(f"    No text extracted")

    except Exception as e:
        print(f"    EXCEPTION: {type(e).__name__}: {e}")


async def test_threshold_override():
    """Test CLAUDE_AUTOCOMPACT_PCT_OVERRIDE env var."""
    print("\n" + "="*60)
    print("TEST C: Threshold override via env var")
    print("="*60)

    compact_events = []

    async def on_pre_compact(hook_input, tool_use_id, context):
        compact_events.append(dict(hook_input))
        print(f"  ** PreCompact at {hook_input.get('trigger')}")
        return {"continue_": True}

    padding = "The quick brown fox jumps over the lazy dog. " * 450

    # Set very low threshold (50%) so compaction triggers earlier
    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with ONLY 'OK N'.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                HookMatcher(matcher=None, hooks=[on_pre_compact]),
            ],
        },
        env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"},
        thinking={"type": "disabled"},
    )

    async with ClaudeSDKClient(options) as client:
        for i in range(20):
            prompt = f"[MSG-{i+1}] Reply 'OK {i+1}'. Padding: {padding}"
            print(f"  Turn {i+1}")

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
                            print(f"    ** {msg.subtype}: {json.dumps(msg.data, default=str)[:200]}")
                    elif isinstance(msg, ResultMessage):
                        print(f"    -> '{text.strip()[:30]}' cost=${msg.total_cost_usd:.4f} err={msg.is_error}")
                        if msg.is_error:
                            break
            except Exception as e:
                print(f"    EXCEPTION: {e}")
                break

            if compact_events:
                print(f"  Compaction triggered at turn {i+1} with 50% threshold")
                break

    return {"compact_events": compact_events, "threshold": "50%"}


async def main():
    print("=== Spike: Compaction control ===\n")

    # Test A: Disable compaction
    result_a = await test_disable_via_settings()
    print(f"\n  Result A: {len(result_a.get('compact_events', []))} compaction events")
    print(f"  Non-init SystemMessages: {len(result_a.get('system_messages', []))}")
    if result_a.get('error'):
        print(f"  Error at turn {result_a.get('error_turn')}: {result_a.get('error')}")

    # Test B: Fork with no compaction for extraction
    await test_fork_no_compact()

    # Test C: Threshold override
    result_c = await test_threshold_override()
    print(f"\n  Result C: {len(result_c.get('compact_events', []))} compaction events with {result_c['threshold']} threshold")

    print(f"\n{'='*60}")
    print("WORK DIR FILES:")
    for f in sorted(WORK_DIR.iterdir()):
        print(f"  {f.name} ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
