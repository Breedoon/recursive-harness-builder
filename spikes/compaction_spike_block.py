"""
Spike: Block compaction and test consequences.

Now that we know PreCompact fires with HookMatcher, test:
1. Blocking compaction (decision: "block")
2. What happens to the session after blocking
3. Whether we can fork from inside the hook for knowledge extraction
4. Whether the hook gets called again on the next turn
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

compact_events = []
fork_results = []
WORK_DIR = Path(tempfile.mkdtemp(prefix="compact_block_"))
NEAR_COMPACT_SESSION = "cec20d87-81f4-4ada-afcd-567f61b98091"
print(f"Work dir: {WORK_DIR}")


async def fork_and_extract(session_id):
    """Fork session and extract knowledge summary."""
    print(f"  FORK: Starting fork of session {session_id}...")
    try:
        fork_opts = ClaudeAgentOptions(
            model="haiku",
            system_prompt="Summarize the conversation concisely. List key facts.",
            permission_mode="bypassPermissions",
            max_turns=1,
            tools=[],
            resume=session_id,
            fork_session=True,
            thinking={"type": "disabled"},
        )
        async with ClaudeSDKClient(fork_opts) as fork_client:
            await fork_client.query(
                "Briefly list all the message numbers and any key facts from our conversation."
            )
            text = ""
            async for msg in fork_client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text += block.text
                elif isinstance(msg, ResultMessage):
                    print(f"  FORK: Cost ${msg.total_cost_usd:.4f}, session {msg.session_id}")

            if text:
                path = WORK_DIR / f"fork_summary_{len(fork_results)+1}.txt"
                path.write_text(text)
                fork_results.append(text)
                print(f"  FORK: Extracted {len(text)} chars")
                print(f"  FORK: Preview: {text[:200]}...")
            else:
                print(f"  FORK: No text returned")

    except Exception as e:
        print(f"  FORK ERROR: {type(e).__name__}: {e}")


async def on_pre_compact(hook_input, tool_use_id, context):
    """Block compaction, optionally fork first."""
    evt = dict(hook_input)
    compact_events.append(evt)
    call_num = len(compact_events)

    print(f"\n{'='*60}")
    print(f"PreCompact HOOK #{call_num}!")
    print(f"  trigger: {evt.get('trigger')}")
    print(f"  session_id: {evt.get('session_id')}")

    # Test: Fork for extraction on first call
    if call_num == 1:
        await fork_and_extract(evt['session_id'])

    # Save hook data
    path = WORK_DIR / f"precompact_{call_num}.json"
    path.write_text(json.dumps(evt, indent=2, default=str))
    print(f"  Wrote: {path}")

    print(f"  DECISION: BLOCK compaction")
    print(f"{'='*60}\n")

    return {
        "decision": "block",
        "reason": f"Blocked by spike (call #{call_num})",
    }


async def main():
    print("=== Spike: Block compaction ===\n")
    print(f"Forking session {NEAR_COMPACT_SESSION}\n")

    padding = "The quick brown fox jumps over the lazy dog. " * 450

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="Reply with ONLY 'OK N' where N is the message number.",
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
                        print(f"  -> '{text.strip()[:50]}' cost=${msg.total_cost_usd:.4f} err={msg.is_error}")
                        if msg.is_error:
                            print(f"     Error detail: {msg.result}")

            except Exception as e:
                print(f"  EXCEPTION: {type(e).__name__}: {e}")
                break

            # After first compaction block, send a few more to see behavior
            if len(compact_events) >= 3:
                print(f"\nStopping after {len(compact_events)} compaction blocks")
                break

    # Report
    print(f"\n{'='*60}")
    print(f"FINAL REPORT")
    print(f"  Compaction events (blocked): {len(compact_events)}")
    print(f"  Fork extractions: {len(fork_results)}")
    print(f"  Work dir: {WORK_DIR}")

    for f in sorted(WORK_DIR.iterdir()):
        size = f.stat().st_size
        print(f"\n  File: {f.name} ({size} bytes)")
        if size < 2000:
            print(f"    {f.read_text()[:500]}")


if __name__ == "__main__":
    asyncio.run(main())
