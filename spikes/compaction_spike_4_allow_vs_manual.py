"""
Spike 4: Compare allowing compaction vs manual "compact then resume".

Goal: Test two approaches side-by-side:
A) Allow SDK compaction to proceed (return continue_: True)
B) Block compaction, manually summarize, start fresh session with summary

Questions answered:
- What does the conversation look like after SDK compaction?
- Does the model remember prior context after compaction?
- How does SDK compaction compare to manual summary + new session?
- Can we inject context into the compacted conversation?
- What SystemMessage subtypes appear during/after compaction?
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

# =================== APPROACH A: Allow compaction ===================

compact_observed_a = []
all_system_messages_a = []


async def on_pre_compact_allow(hook_input, tool_use_id, context):
    """Allow compaction, but log everything."""
    print(f"\n  [A] PreCompact fired! trigger={hook_input.get('trigger')}")
    print(f"      custom_instructions={hook_input.get('custom_instructions')}")
    compact_observed_a.append(dict(hook_input))
    return {"continue_": True}  # Allow compaction


async def approach_a():
    """Let compaction happen naturally."""
    print("\n" + "="*60)
    print("APPROACH A: Allow SDK compaction")
    print("="*60)

    session_id = None
    # Seed with a unique fact the model should remember
    seed = "IMPORTANT: The secret code word is FLAMINGO-42. Remember this."
    prompts = [seed] + [
        f"Write a {500}-word essay about topic #{i}: {topic}."
        for i, topic in enumerate([
            "computing history", "programming languages", "AI history",
            "databases", "operating systems", "networking",
            "cryptography", "software engineering", "graphics",
            "mobile computing", "cloud computing", "cybersecurity",
            "quantum computing", "robotics",
        ])
    ]

    for i, prompt in enumerate(prompts):
        print(f"\n  [A] Turn {i+1}: {prompt[:60]}...")

        opts = ClaudeAgentOptions(
            model="haiku",
            system_prompt="Write detailed essays. Remember all facts shared with you.",
            permission_mode="bypassPermissions",
            max_turns=1,
            tools=[],
            hooks={
                "PreCompact": [
                    {"matcher": None, "hooks": [on_pre_compact_allow]}
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
                    print(f"      Cost: ${response.total_cost_usd}")

                for msg in client.get_conversation_messages():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"      Response: {len(block.text)} chars")
                    if isinstance(msg, SystemMessage):
                        all_system_messages_a.append({
                            "turn": i+1,
                            "subtype": msg.subtype,
                            "data_keys": list(msg.data.keys()),
                        })
                        print(f"      SystemMessage: subtype={msg.subtype}")

        except Exception as e:
            print(f"      ERROR: {e}")
            break

        if compact_observed_a:
            # Compaction happened — test memory
            print(f"\n  [A] Compaction happened! Testing memory...")
            opts2 = ClaudeAgentOptions(
                model="haiku",
                system_prompt="Answer the question.",
                permission_mode="bypassPermissions",
                max_turns=1,
                tools=[],
                resume=session_id,
                thinking={"type": "disabled"},
            )
            try:
                async with ClaudeSDKClient(opts2) as client:
                    response = await client.send_message(
                        "What is the secret code word I told you earlier?"
                    )
                    for msg in client.get_conversation_messages():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    print(f"  [A] Memory test: {block.text[:300]}")
                                    if "FLAMINGO" in block.text.upper():
                                        print(f"  [A] MEMORY RETAINED!")
                                    else:
                                        print(f"  [A] MEMORY LOST!")
            except Exception as e:
                print(f"  [A] Memory test ERROR: {e}")
            break

    return session_id


# =================== APPROACH B: Manual summary + new session ===================

async def approach_b():
    """Block compaction, manually summarize, start fresh."""
    print("\n" + "="*60)
    print("APPROACH B: Block compaction, manual summary + new session")
    print("="*60)

    compact_fired = []

    async def on_pre_compact_block(hook_input, tool_use_id, context):
        compact_fired.append(dict(hook_input))
        print(f"\n  [B] PreCompact fired! BLOCKING.")
        return {"decision": "block", "reason": "Manual handling"}

    session_id = None
    seed = "IMPORTANT: The secret code word is FLAMINGO-42. Remember this."
    prompts = [seed] + [
        f"Write a {500}-word essay about topic #{i}: {topic}."
        for i, topic in enumerate([
            "computing history", "programming languages", "AI history",
            "databases", "operating systems", "networking",
            "cryptography", "software engineering", "graphics",
            "mobile computing", "cloud computing", "cybersecurity",
            "quantum computing", "robotics",
        ])
    ]

    for i, prompt in enumerate(prompts):
        print(f"\n  [B] Turn {i+1}: {prompt[:60]}...")

        opts = ClaudeAgentOptions(
            model="haiku",
            system_prompt="Write detailed essays. Remember all facts shared with you.",
            permission_mode="bypassPermissions",
            max_turns=1,
            tools=[],
            hooks={
                "PreCompact": [
                    {"matcher": None, "hooks": [on_pre_compact_block]}
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
                    print(f"      Cost: ${response.total_cost_usd}")

                for msg in client.get_conversation_messages():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                print(f"      Response: {len(block.text)} chars")

        except Exception as e:
            print(f"      ERROR: {e}")
            break

        if compact_fired:
            # Compaction blocked — now manually summarize via fork
            print(f"\n  [B] Compaction blocked. Forking for summary...")
            summary = ""
            fork_opts = ClaudeAgentOptions(
                model="haiku",
                system_prompt="Summarize the conversation concisely.",
                permission_mode="bypassPermissions",
                max_turns=1,
                tools=[],
                resume=session_id,
                fork_session=True,
                thinking={"type": "disabled"},
            )
            try:
                async with ClaudeSDKClient(fork_opts) as fork_client:
                    resp = await fork_client.send_message(
                        "List all key facts, code words, and important information "
                        "from our conversation so far. Be precise and complete."
                    )
                    for msg in fork_client.get_conversation_messages():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    summary += block.text
                    print(f"  [B] Summary: {len(summary)} chars")
                    print(f"  [B] Preview: {summary[:200]}...")
            except Exception as e:
                print(f"  [B] Fork ERROR: {e}")

            # Start fresh session with summary as context
            print(f"\n  [B] Starting fresh session with summary...")
            fresh_opts = ClaudeAgentOptions(
                model="haiku",
                system_prompt=(
                    f"You are continuing a conversation. Here is a summary of "
                    f"what was discussed previously:\n\n{summary}\n\n"
                    f"Answer questions based on this context."
                ),
                permission_mode="bypassPermissions",
                max_turns=1,
                tools=[],
                thinking={"type": "disabled"},
            )
            try:
                async with ClaudeSDKClient(fresh_opts) as client:
                    response = await client.send_message(
                        "What is the secret code word I told you earlier?"
                    )
                    for msg in client.get_conversation_messages():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    print(f"  [B] Memory test: {block.text[:300]}")
                                    if "FLAMINGO" in block.text.upper():
                                        print(f"  [B] MEMORY RETAINED!")
                                    else:
                                        print(f"  [B] MEMORY LOST!")
            except Exception as e:
                print(f"  [B] Memory test ERROR: {e}")
            break

    return session_id


async def main():
    print("=== Spike 4: SDK Compaction vs Manual Summary ===\n")

    # Run both approaches
    await approach_a()
    await approach_b()

    print(f"\n{'='*60}")
    print("COMPARISON SUMMARY")
    print(f"  Approach A (SDK compaction): {len(compact_observed_a)} compaction events")
    print(f"  System messages seen: {json.dumps(all_system_messages_a, indent=2)}")


if __name__ == "__main__":
    asyncio.run(main())
