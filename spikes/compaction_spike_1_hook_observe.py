"""
Spike 1: Observe compaction behavior via PreCompact hook.

Goal: Push haiku to compaction and observe what PreCompact hook receives.
Strategy: Use a cheap model, send lots of messages to fill context,
watch for the PreCompact hook callback.

Questions answered:
- When does compaction trigger?
- What does the PreCompact hook input look like?
- What fields are available (trigger, custom_instructions)?
- Can we block compaction?
"""
import asyncio
import json
import sys
import os

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

# Collect all PreCompact hook calls
compact_events = []


async def on_pre_compact(hook_input, tool_use_id, context):
    """Observe what the PreCompact hook receives."""
    print(f"\n{'='*60}")
    print(f"PreCompact HOOK FIRED!")
    print(f"  trigger: {hook_input.get('trigger')}")
    print(f"  custom_instructions: {hook_input.get('custom_instructions')}")
    print(f"  session_id: {hook_input.get('session_id')}")
    print(f"  transcript_path: {hook_input.get('transcript_path')}")
    print(f"  All keys: {list(hook_input.keys())}")
    print(f"  Full input: {json.dumps(dict(hook_input), indent=2, default=str)}")
    print(f"{'='*60}\n")

    compact_events.append(dict(hook_input))

    # Allow compaction to proceed so we can see what happens after
    return {"continue_": True}


async def send_and_receive(client, prompt):
    """Send a prompt and collect the response."""
    await client.query(prompt)
    result = None
    text = ""
    system_msgs = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text += block.text
        elif isinstance(msg, SystemMessage):
            system_msgs.append({"subtype": msg.subtype, "keys": list(msg.data.keys())})
        elif isinstance(msg, ResultMessage):
            result = msg
    return result, text, system_msgs


async def main():
    print("=== Spike 1: Observe PreCompact Hook ===")
    print("Using haiku to push to compaction quickly...\n")

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt="You are a test assistant. Write the requested essays. Be thorough and detailed. Never use tools.",
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        hooks={
            "PreCompact": [
                {
                    "matcher": None,
                    "hooks": [on_pre_compact],
                }
            ]
        },
        thinking={"type": "disabled"},
    )

    prompts = [
        "Write a detailed 500-word essay about the history of computing, covering Babbage, Turing, ENIAC, and modern CPUs.",
        "Write a detailed 500-word essay about the history of programming languages, from Fortran through Python and Rust.",
        "Write a detailed 500-word essay about AI history, from the Dartmouth conference to modern LLMs.",
        "Write a detailed 500-word essay about database systems, from hierarchical to distributed.",
        "Write a detailed 500-word essay about operating systems, from batch processing to containers.",
        "Write a detailed 500-word essay about computer networking, from modems to 5G.",
        "Write a detailed 500-word essay about cryptography, from Caesar to post-quantum.",
        "Write a detailed 500-word essay about software engineering, from waterfall to DevOps.",
        "Write a detailed 500-word essay about computer graphics, from wireframes to ray tracing.",
        "Write a detailed 500-word essay about mobile computing, from PDAs to AR glasses.",
        "Write a detailed 500-word essay about cloud computing, from mainframes to serverless.",
        "Write a detailed 500-word essay about cybersecurity, from early viruses to APTs.",
        "Write a detailed 500-word essay about quantum computing, from Feynman to superconducting qubits.",
        "Write a detailed 500-word essay about robotics, from industrial arms to humanoids.",
        "Write a detailed 500-word essay about VR and AR, from Sensorama to Apple Vision Pro.",
    ]

    async with ClaudeSDKClient(options) as client:
        for i, prompt in enumerate(prompts):
            print(f"\n--- Turn {i+1}/{len(prompts)} ---")
            print(f"Sending: {prompt[:60]}...")

            try:
                result, text, sys_msgs = await send_and_receive(client, prompt)

                if result:
                    print(f"  Session: {result.session_id}")
                    print(f"  Cost: ${result.total_cost_usd}")
                    print(f"  Turns: {result.num_turns}")
                    print(f"  Error: {result.is_error}")

                print(f"  Response length: {len(text)} chars")
                if text:
                    print(f"  Preview: {text[:100]}...")

                for sm in sys_msgs:
                    print(f"  SystemMessage: {sm}")

            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
                break

            if compact_events:
                print(f"\n*** COMPACTION TRIGGERED after turn {i+1}! ***")
                break

    # Summary
    print(f"\n{'='*60}")
    print(f"RESULTS:")
    print(f"  Total turns sent: {i+1}")
    print(f"  Compaction events: {len(compact_events)}")
    for j, evt in enumerate(compact_events):
        print(f"\n  Event {j+1}:")
        print(f"    trigger: {evt.get('trigger')}")
        print(f"    custom_instructions: {evt.get('custom_instructions')}")
        print(f"    All data: {json.dumps(evt, indent=4, default=str)}")

    if not compact_events:
        print("\n  No compaction triggered — may need more turns or larger content")


if __name__ == "__main__":
    asyncio.run(main())
