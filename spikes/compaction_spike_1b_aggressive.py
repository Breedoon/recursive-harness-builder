"""
Spike 1b: Aggressively push to compaction.

The 200K context window of haiku needs a LOT of content to fill.
15 x 5K chars = ~75K chars ≈ 25K tokens — only ~12% of context.

Strategy: Ask for MUCH longer outputs (5000+ words), and run many turns.
Also ask the model to repeat/elaborate so it generates maximum tokens.

Alternative: use max_turns > 1 and ask model to use tools (Read many files)
to fill context faster with tool call overhead.
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

compact_events = []
total_chars_generated = 0
RESUME_SESSION = "c4d5bcb2-5f7e-430a-b4fe-5df8319265cc"  # From spike 1


async def on_pre_compact(hook_input, tool_use_id, context):
    """Log and allow compaction."""
    print(f"\n{'='*60}")
    print(f"PreCompact HOOK FIRED!")
    print(f"  trigger: {hook_input.get('trigger')}")
    print(f"  custom_instructions: {hook_input.get('custom_instructions')}")
    print(f"  session_id: {hook_input.get('session_id')}")
    print(f"  transcript_path: {hook_input.get('transcript_path')}")
    print(f"  All keys: {list(hook_input.keys())}")
    print(f"  Full: {json.dumps(dict(hook_input), indent=2, default=str)}")
    print(f"{'='*60}\n")
    compact_events.append(dict(hook_input))
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
    global total_chars_generated

    print("=== Spike 1b: Aggressive push to compaction ===")
    print(f"Resuming session {RESUME_SESSION} (already has ~75K chars)\n")

    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt=(
            "You are a test assistant. Write EXTREMELY long, detailed responses. "
            "Minimum 3000 words per response. Include many examples, code snippets, "
            "technical details, and elaborate explanations. Fill your response with "
            "as much content as possible. Never use tools."
        ),
        permission_mode="bypassPermissions",
        max_turns=1,
        tools=[],
        resume=RESUME_SESSION,
        fork_session=True,  # Fork so we don't pollute the original
        hooks={
            "PreCompact": [
                {"matcher": None, "hooks": [on_pre_compact]}
            ]
        },
        thinking={"type": "disabled"},
    )

    # Generate LOTS of prompts asking for huge outputs
    topics = [
        "the complete history of the x86 instruction set architecture",
        "every major algorithm in computer science with pseudocode",
        "the complete TCP/IP protocol stack with packet diagrams in ASCII",
        "the evolution of JavaScript from 1995 to 2025 with code examples",
        "memory management techniques across all major languages",
        "the complete history of Unix and all its derivatives",
        "compiler design with lexer, parser, and code generation examples",
        "distributed systems consensus algorithms (Paxos, Raft, PBFT)",
        "the complete history of web browsers and rendering engines",
        "GPU architecture from CUDA to modern ML accelerators",
        "functional programming concepts with Haskell and Lisp examples",
        "the complete history of Linux kernel development",
        "microprocessor design from 4004 to M4 with die photos",
        "type theory and its applications in programming languages",
        "the complete history of version control systems",
        "network security protocols (TLS, SSH, IPsec) in extreme detail",
        "database indexing strategies (B-trees, LSM, hash, bitmap)",
        "container orchestration with Kubernetes architecture details",
        "the complete history of storage technology from tapes to NVMe",
        "machine learning from perceptrons to transformers with math",
        "the complete history of computer memory (SRAM, DRAM, HBM)",
        "parallel computing paradigms (SIMD, MIMD, GPU, TPU)",
        "programming language type systems compared in detail",
        "the complete history of computer input devices",
        "graph algorithms with implementations and complexity analysis",
        "the complete history of email protocols and infrastructure",
        "systems programming in Rust with detailed ownership examples",
        "the complete history of video compression codecs",
        "relational algebra and SQL optimization with query plans",
        "the complete history of wireless communication standards",
    ]

    async with ClaudeSDKClient(options) as client:
        for i, topic in enumerate(topics):
            prompt = (
                f"Write an EXTREMELY detailed, comprehensive treatise (at least 3000 words) about {topic}. "
                f"Include: historical timeline, technical specifications, code examples where relevant, "
                f"comparison tables, key figures and their contributions, and future outlook. "
                f"This is turn {i+16} (we've already discussed 15 topics). Be maximally verbose."
            )

            print(f"\n--- Turn {i+16} ---")
            print(f"Topic: {topic[:60]}...")

            try:
                result, text, sys_msgs = await send_and_receive(client, prompt)
                total_chars_generated += len(text)

                if result:
                    print(f"  Session: {result.session_id}")
                    print(f"  Cost: ${result.total_cost_usd}")
                    print(f"  Error: {result.is_error}")

                print(f"  Response: {len(text)} chars ({len(text)//4} approx tokens)")
                print(f"  Running total: {total_chars_generated} chars ({total_chars_generated//4} approx tokens)")

                for sm in sys_msgs:
                    print(f"  SystemMsg: {sm}")

            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
                break

            if compact_events:
                print(f"\n*** COMPACTION at turn {i+16}! ***")
                print(f"Total generated: {total_chars_generated} chars")
                break

    print(f"\n{'='*60}")
    print(f"RESULTS:")
    print(f"  Total chars: {total_chars_generated}")
    print(f"  Approx tokens: {total_chars_generated // 4}")
    print(f"  Compaction events: {len(compact_events)}")
    for j, evt in enumerate(compact_events):
        print(f"\n  Event {j+1}:")
        print(json.dumps(evt, indent=4, default=str))


if __name__ == "__main__":
    asyncio.run(main())
