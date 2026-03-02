"""
Spike 3: Fork a session at compaction time to extract knowledge.

Goal: When compaction fires, fork the session and ask the forked agent
to summarize everything it learned, writing it to a file. Then allow
or deny compaction.

This tests whether forking works from inside a PreCompact hook.

Questions answered:
- Can we fork from inside a PreCompact hook?
- Can the fork read the conversation history?
- Can the fork write files?
- Is this a viable alternative to custom compaction prompts?
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
    TextBlock,
    AssistantMessage,
    ResultMessage,
)

compact_events = []
WORK_DIR = Path(tempfile.mkdtemp(prefix="compact_fork_"))
SUMMARY_FILE = WORK_DIR / "session_summary.md"
print(f"Work dir: {WORK_DIR}")


async def fork_and_extract(session_id: str, transcript_path: str):
    """Fork the session and ask the agent to summarize what it learned."""
    print(f"  Forking session {session_id} for knowledge extraction...")

    fork_opts = ClaudeAgentOptions(
        model="haiku",
        system_prompt=(
            "You are a knowledge extraction assistant. "
            "Review the conversation so far and write a concise summary "
            "of all key facts, insights, and knowledge discussed. "
            "Format as a markdown list."
        ),
        permission_mode="bypassPermissions",
        max_turns=2,
        resume=session_id,
        fork_session=True,
        thinking={"type": "disabled"},
    )

    try:
        async with ClaudeSDKClient(fork_opts) as fork_client:
            response = await fork_client.send_message(
                "Please summarize all the key knowledge and facts from our conversation "
                "so far. Write a concise markdown summary. Do not use any tools — just "
                "write the summary as text in your response."
            )

            summary_text = ""
            for msg in fork_client.get_conversation_messages():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            summary_text += block.text + "\n"

            if summary_text:
                SUMMARY_FILE.write_text(summary_text)
                print(f"  Fork extracted {len(summary_text)} chars of summary")
                print(f"  Saved to: {SUMMARY_FILE}")
                print(f"  Preview: {summary_text[:200]}...")
            else:
                print(f"  Fork returned no text content")

            if isinstance(response, ResultMessage):
                print(f"  Fork cost: ${response.total_cost_usd}")

    except Exception as e:
        print(f"  Fork ERROR: {e}")


async def on_pre_compact(hook_input, tool_use_id, context):
    """Fork the session for extraction, then block compaction."""
    print(f"\n{'='*60}")
    print(f"PreCompact HOOK — forking for knowledge extraction")

    compact_events.append(dict(hook_input))
    session_id = hook_input.get('session_id')
    transcript_path = hook_input.get('transcript_path')

    # Fork and extract knowledge
    await fork_and_extract(session_id, transcript_path)

    # Block compaction after extraction
    print(f"  Blocking compaction (knowledge already extracted)")
    print(f"{'='*60}\n")
    return {
        "decision": "block",
        "reason": "Knowledge extracted via fork; blocking compaction",
    }


async def main():
    print("=== Spike 3: Fork at Compaction ===\n")

    session_id = None
    prompts = [
        "Explain three important facts about Python programming. Be detailed.",
        "Explain three important facts about Rust programming. Be detailed.",
        "Explain three important facts about Go programming. Be detailed.",
        "Explain three important facts about JavaScript. Be detailed.",
        "Explain three important facts about TypeScript. Be detailed.",
        "Explain three important facts about C++ programming. Be detailed.",
        "Explain three important facts about Java programming. Be detailed.",
        "Explain three important facts about Haskell programming. Be detailed.",
        "Explain three important facts about Elixir programming. Be detailed.",
        "Explain three important facts about Swift programming. Be detailed.",
        "Explain three important facts about Kotlin programming. Be detailed.",
        "Explain three important facts about Scala programming. Be detailed.",
        "Explain three important facts about Clojure programming. Be detailed.",
        "Explain three important facts about Ruby programming. Be detailed.",
        "Explain three important facts about PHP programming. Be detailed.",
    ]

    for i, prompt in enumerate(prompts):
        print(f"\n--- Turn {i+1} ---")
        print(f"Prompt: {prompt[:50]}...")

        opts = ClaudeAgentOptions(
            model="haiku",
            system_prompt="You are a programming expert. Give detailed explanations. Never use tools.",
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

                for msg in client.get_conversation_messages():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"  Response: {len(block.text)} chars")

        except Exception as e:
            print(f"  ERROR: {e}")
            break

        if compact_events:
            print(f"\n*** Compaction blocked after fork extraction ***")
            # Try one more message to see if session survives
            print(f"\n--- Post-compaction turn ---")
            opts2 = ClaudeAgentOptions(
                model="haiku",
                system_prompt="You are a programming expert.",
                permission_mode="bypassPermissions",
                max_turns=1,
                tools=[],
                resume=session_id,
                thinking={"type": "disabled"},
            )
            try:
                async with ClaudeSDKClient(opts2) as client:
                    response = await client.send_message(
                        "What programming languages have we discussed so far? List them."
                    )
                    for msg in client.get_conversation_messages():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    print(f"  Post-compact response: {block.text[:300]}")
            except Exception as e:
                print(f"  Post-compact ERROR: {e}")
            break

    # Report
    print(f"\n{'='*60}")
    print(f"FINAL REPORT")
    print(f"  Work dir: {WORK_DIR}")
    print(f"  Compaction events: {len(compact_events)}")
    if SUMMARY_FILE.exists():
        print(f"\n  Extracted summary ({SUMMARY_FILE.stat().st_size} bytes):")
        print(f"  {SUMMARY_FILE.read_text()[:500]}")
    else:
        print(f"  No summary file created")


if __name__ == "__main__":
    asyncio.run(main())
