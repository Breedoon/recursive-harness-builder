"""
Spike 26: Deep probing of context:fork — CLAUDE.md, tools, cwd, history

Tests:
1. Does forked subagent see CLAUDE.md content?
2. Tool inventory for default agent vs Explore agent
3. Working directory inheritance
4. Conversation history isolation
5. Detailed JSONL comparison of subagent sessions
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


async def run_probe(skill_name, label, pre_message=None):
    """Run a probe skill and capture everything."""
    print(f"\n{'='*70}")
    print(f"PROBE: {label}")
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
    main_init_tools = []

    async with client:
        if pre_message:
            print(f"  Pre-message: {pre_message[:100]}")
            await client.query(pre_message)
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    main_init_tools = msg.data.get("tools", [])
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"  Pre-response: {block.text[:100]}")

        print(f"  Invoking: {skill_name}")
        await client.query(f"Use the {skill_name} skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage) and msg.subtype == "init" and not main_init_tools:
                main_init_tools = msg.data.get("tools", [])
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        if "PROBE_" in text or "completed" in text.lower():
                            print(f"  Main agent says: {text[:400]}")
            elif isinstance(msg, ResultMessage):
                print(f"  Session: {msg.session_id}")
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

    # Analyze subagent
    new_files = get_all_files() - before
    subagent_files = [f for f in new_files if "subagents" in f]

    if subagent_files:
        for sf in subagent_files:
            entries = analyze_jsonl(sf)
            print(f"\n  --- Subagent JSONL: {os.path.relpath(sf, SESSIONS_BASE)} ---")

            for entry in entries:
                etype = entry.get("type", "?")
                if etype == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        print(f"  Subagent user prompt ({len(content)} chars):")
                        print(f"    {content[:300]}")
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                print(f"  Subagent user prompt ({len(block['text'])} chars):")
                                print(f"    {block['text'][:300]}")

                elif etype == "assistant":
                    usage = entry.get("message", {}).get("usage", {})
                    cr = usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation_input_tokens", 0)
                    inp = usage.get("input_tokens", 0)
                    total = cr + cc + inp
                    pct = (cr / total * 100) if total > 0 else 0
                    print(f"  Subagent cache: read={cr:,} create={cc:,} input={inp:,} ({pct:.0f}% cached) total_input={total:,}")

                    content = entry.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            print(f"  Subagent response:")
                            print(f"    {block['text'][:400]}")

    print(f"\n  Main agent tool count: {len(main_init_tools)}")
    return main_init_tools


async def main():
    print("="*70)
    print("SPIKE 26: Deep context probing")
    print("="*70)

    # Probe 1: Default forked context (tests CLAUDE.md, tools, cwd, history)
    tools1 = await run_probe(
        "forked-context-probe",
        "Default agent fork — CLAUDE.md + tools + cwd + history",
        pre_message="Remember: the secret word is BANANA_SPLIT_99. Just say 'Got it.'"
    )

    # Probe 2: Explore agent fork
    tools2 = await run_probe(
        "forked-explore-probe",
        "Explore agent fork — CLAUDE.md + tools + cwd + history",
        pre_message="Remember: the secret word is MANGO_TANGO_77. Just say 'Got it.'"
    )


if __name__ == "__main__":
    asyncio.run(main())
