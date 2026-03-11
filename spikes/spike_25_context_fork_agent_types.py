"""
Spike 25: context:fork with different agent types

Tests:
1. context: fork with agent: Explore — what tools?
2. context: fork with agent: Plan — what tools?
3. context: fork with no agent field — default?
4. Compare subagent JSONL cache stats across agent types
5. Does the subagent session inherit from main or start fresh?
"""
import spike_env  # noqa: F401
import asyncio
import json
import os
import glob
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

PROJECT_DIR = "/tmp/context-fork-spikes"
SESSIONS_BASE = os.path.expanduser("~/.claude/projects/-private-tmp-context-fork-spikes")


def get_all_files():
    pattern = os.path.join(SESSIONS_BASE, "**/*.jsonl")
    return set(glob.glob(pattern, recursive=True))


def analyze_jsonl(filepath):
    entries = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


async def run_skill(skill_name, label):
    """Run a skill and capture results + subagent info."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"{'='*70}")

    before = get_all_files()

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=10,
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    )

    client = ClaudeSDKClient(options)
    session_id = None
    init_tools = []

    async with client:
        await client.query(f"Use the {skill_name} skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage):
                if msg.subtype == "init":
                    init_tools = msg.data.get("tools", [])
                    print(f"  Main agent tools ({len(init_tools)}): {init_tools[:5]}...")
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        # Only print the result part
                        text = block.text
                        if any(kw in text for kw in ["FORKED_", "RESPONSE", "TOOLS", "PLAN"]):
                            print(f"  Agent response: {text[:300]}")
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                print(f"  Session: {session_id}")
                print(f"  Cost: ${msg.total_cost_usd:.4f}, Turns: {msg.num_turns}")

    # Find subagent JSONL
    new_files = get_all_files() - before
    subagent_files = [f for f in new_files if "subagents" in f]

    if subagent_files:
        for sf in subagent_files:
            entries = analyze_jsonl(sf)
            print(f"\n  Subagent JSONL: {os.path.relpath(sf, SESSIONS_BASE)}")
            print(f"  Subagent entries: {len(entries)}")

            for entry in entries:
                if entry.get("type") == "assistant":
                    usage = entry.get("message", {}).get("usage", {})
                    cr = usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation_input_tokens", 0)
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    total = cr + cc + inp
                    pct = (cr / total * 100) if total > 0 else 0
                    print(f"  Subagent cache: read={cr:,} create={cc:,} input={inp:,} total={total:,} ({pct:.0f}% cached) output={out:,}")

                    content = entry.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            print(f"  Subagent text: {block['text'][:200]}")
    else:
        print("  No subagent files found (skill ran inline?)")

    return {
        "label": label,
        "session_id": session_id,
        "main_tools": init_tools,
        "subagent_files": subagent_files,
    }


async def main():
    print("="*70)
    print("SPIKE 25: context:fork agent types")
    print("="*70)

    results = []

    # Test 1: Forked with default agent (no agent: field)
    r = await run_skill("forked-greeter", "Forked (default agent, no agent: field)")
    results.append(r)

    # Test 2: Forked with agent: Explore
    r = await run_skill("forked-explore", "Forked with agent: Explore")
    results.append(r)

    # Test 3: Forked with agent: Plan
    r = await run_skill("forked-plan", "Forked with agent: Plan")
    results.append(r)

    # Summary
    print("\n" + "="*70)
    print("SUMMARY: Agent type comparison")
    print("="*70)
    for r in results:
        print(f"\n  {r['label']}:")
        print(f"    Main tools: {len(r['main_tools'])}")
        print(f"    Subagent files: {len(r['subagent_files'])}")


if __name__ == "__main__":
    asyncio.run(main())
