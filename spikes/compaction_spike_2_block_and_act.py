"""
Spike 2: Block compaction and perform file I/O in the PreCompact hook.

Goal: When compaction fires, BLOCK it and instead:
1. Read a file (to simulate "gather knowledge")
2. Write a summary file (to simulate "persist knowledge")
3. Then deny compaction

Questions answered:
- Can we do arbitrary work (file I/O) inside a PreCompact hook?
- Does blocking compaction let the session continue?
- What happens to the session after blocking compaction?
- Can we fork from inside the PreCompact hook?
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
    TextBlock,
    SystemMessage,
    AssistantMessage,
    ResultMessage,
)

# Track state
compact_events = []
WORK_DIR = Path(tempfile.mkdtemp(prefix="compaction_spike_"))
print(f"Work dir: {WORK_DIR}")

# Create a file the hook will read
(WORK_DIR / "knowledge.txt").write_text(
    "Important facts learned this session:\n"
    "1. The agent discussed computing history\n"
    "2. The agent covered programming languages\n"
    "3. The agent explained AI evolution\n"
)


async def on_pre_compact(hook_input, tool_use_id, context):
    """Block compaction and do file I/O."""
    print(f"\n{'='*60}")
    print(f"PreCompact HOOK FIRED! (attempt #{len(compact_events)+1})")
    print(f"  trigger: {hook_input.get('trigger')}")
    print(f"  custom_instructions: {hook_input.get('custom_instructions')}")

    compact_events.append(dict(hook_input))

    # === SPIKE: Can we do file I/O inside the hook? ===
    try:
        # Read a file
        knowledge = (WORK_DIR / "knowledge.txt").read_text()
        print(f"  Successfully READ file: {len(knowledge)} chars")

        # Write a summary file
        summary_path = WORK_DIR / f"compaction_summary_{len(compact_events)}.txt"
        summary = (
            f"Compaction Summary #{len(compact_events)}\n"
            f"Time: {datetime.now().isoformat()}\n"
            f"Trigger: {hook_input.get('trigger')}\n"
            f"Session: {hook_input.get('session_id')}\n"
            f"Transcript: {hook_input.get('transcript_path')}\n"
            f"\nKnowledge captured:\n{knowledge}\n"
        )
        summary_path.write_text(summary)
        print(f"  Successfully WROTE summary to: {summary_path}")

        # Read transcript if available
        transcript_path = hook_input.get('transcript_path')
        if transcript_path and os.path.exists(transcript_path):
            transcript = Path(transcript_path).read_text()
            print(f"  Transcript exists: {len(transcript)} chars")
            # Save transcript snapshot
            (WORK_DIR / f"transcript_snapshot_{len(compact_events)}.json").write_text(
                transcript[:10000]  # First 10K chars
            )
            print(f"  Saved transcript snapshot")
        else:
            print(f"  Transcript path: {transcript_path} (exists: {os.path.exists(transcript_path) if transcript_path else 'N/A'})")

    except Exception as e:
        print(f"  ERROR during file I/O: {e}")

    print(f"  Blocking compaction!")
    print(f"{'='*60}\n")

    # Block compaction
    return {
        "decision": "block",
        "reason": "Spike: blocking compaction after saving knowledge",
    }


async def main():
    print("=== Spike 2: Block Compaction + File I/O ===\n")

    session_id = None

    prompts = [
        "Write a detailed 500-word essay about the history of computing.",
        "Write a detailed 500-word essay about programming languages.",
        "Write a detailed 500-word essay about AI history.",
        "Write a detailed 500-word essay about database systems.",
        "Write a detailed 500-word essay about operating systems.",
        "Write a detailed 500-word essay about networking.",
        "Write a detailed 500-word essay about cryptography.",
        "Write a detailed 500-word essay about software engineering.",
        "Write a detailed 500-word essay about computer graphics.",
        "Write a detailed 500-word essay about mobile computing.",
        "Write a detailed 500-word essay about cloud computing.",
        "Write a detailed 500-word essay about cybersecurity.",
        "Write a detailed 500-word essay about quantum computing.",
        "Write a detailed 500-word essay about robotics.",
        "Write a detailed 500-word essay about VR and AR.",
    ]

    for i, prompt in enumerate(prompts):
        print(f"\n--- Turn {i+1}/{len(prompts)} ---")
        print(f"Prompt: {prompt[:50]}...")

        opts = ClaudeAgentOptions(
            model="haiku",
            system_prompt="Write the requested essay. Be thorough.",
            permission_mode="bypassPermissions",
            max_turns=1,
            tools=[],
            hooks={
                "PreCompact": [
                    {"matcher": None, "hooks": [on_pre_compact]}
                ]
            },
            thinking={"type": "disabled"},
        )

        if session_id:
            opts.resume = session_id

        try:
            async with ClaudeSDKClient(opts) as client:
                response = await client.send_message(prompt)

                if isinstance(response, ResultMessage):
                    session_id = response.session_id
                    print(f"  Session: {session_id}")
                    print(f"  Cost: ${response.total_cost_usd}")
                    print(f"  Error: {response.is_error}")

                for msg in client.get_conversation_messages():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"  Response: {len(block.text)} chars")
                    if isinstance(msg, SystemMessage):
                        print(f"  System: subtype={msg.subtype}, keys={list(msg.data.keys())}")

        except Exception as e:
            print(f"  ERROR: {e}")
            # Session may be dead after blocking compaction
            if "compact" in str(e).lower():
                print("  Session died from compaction block — this is expected")
                break

        # After compaction blocked, try sending another message to the same session
        if compact_events:
            print(f"\n*** Compaction was blocked {len(compact_events)} time(s) ***")
            # Continue to see what happens next

    # Report
    print(f"\n{'='*60}")
    print(f"FINAL REPORT")
    print(f"  Work dir: {WORK_DIR}")
    print(f"  Total compaction events: {len(compact_events)}")

    # Check what files were written
    for f in sorted(WORK_DIR.iterdir()):
        print(f"  File: {f.name} ({f.stat().st_size} bytes)")
        if f.suffix == '.txt':
            print(f"    Content preview: {f.read_text()[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
