"""
Spike 24: context:fork — context inheritance and tool availability

Tests:
1. Does a forked skill see conversation history? (code word test)
2. What tools does the forked subagent get vs inline?
3. Does CLAUDE.md load in the forked context?
4. Cache comparison: forked subagent vs main agent
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


def find_new_files(before):
    """Find new JSONL files created after 'before' snapshot."""
    pattern = os.path.join(SESSIONS_BASE, "**/*.jsonl")
    after = set(glob.glob(pattern, recursive=True))
    return after - before


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


def extract_cache_stats(entries):
    """Extract cache stats from assistant entries."""
    stats = []
    for entry in entries:
        if entry.get("type") == "assistant":
            usage = entry.get("message", {}).get("usage", {})
            if usage:
                stats.append({
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                })
    return stats


async def multi_turn_test():
    """Two-turn session: set code word, then invoke skills to recall it."""
    print("\n" + "="*70)
    print("TEST: Multi-turn context inheritance")
    print("="*70)

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

    async with client:
        # Turn 1: Establish a code word
        print("\n--- Turn 1: Setting code word ---")
        await client.query(
            "Remember this secret code word: PURPLE_ELEPHANT_42. "
            "Just acknowledge you've stored it. Say exactly: 'Code word stored.'"
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  Response: {block.text[:200]}")
            elif isinstance(msg, ResultMessage):
                session_id = msg.session_id
                print(f"  Session: {session_id}")

        # Turn 2: Ask to recall via inline skill
        print("\n--- Turn 2: Recall via inline skill ---")
        await client.query("Use the inline-recall skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  Response: {block.text[:300]}")
            elif isinstance(msg, ResultMessage):
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

        # Turn 3: Ask to recall via forked skill
        print("\n--- Turn 3: Recall via forked skill ---")
        await client.query("Use the forked-recall skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  Response: {block.text[:300]}")
            elif isinstance(msg, ResultMessage):
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

    # Analyze JSONL
    new_files = find_new_files(before)
    print(f"\n--- JSONL Analysis ---")
    print(f"New files: {len(new_files)}")

    for filepath in sorted(new_files, key=lambda f: os.path.getmtime(f)):
        rel = os.path.relpath(filepath, SESSIONS_BASE)
        entries = analyze_jsonl(filepath)
        is_subagent = "subagents" in rel

        print(f"\n  File: {rel} ({'SUBAGENT' if is_subagent else 'MAIN'}, {len(entries)} entries)")

        # Show content summary
        for i, entry in enumerate(entries):
            etype = entry.get("type", "?")
            if etype == "user":
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            print(f"    [{i}] user: {block['text'][:120]}")
                elif isinstance(content, str):
                    print(f"    [{i}] user: {content[:120]}")
            elif etype == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                usage = msg.get("usage", {})
                cr = usage.get("cache_read_input_tokens", 0)
                cc = usage.get("cache_creation_input_tokens", 0)
                inp = usage.get("input_tokens", 0)
                total_in = cr + cc + inp
                pct = (cr / total_in * 100) if total_in > 0 else 0

                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                print(f"    [{i}] assistant text: {block['text'][:120]}")
                                print(f"         cache: read={cr:,} create={cc:,} input={inp:,} ({pct:.0f}% cached)")
                            elif block.get("type") == "tool_use":
                                print(f"    [{i}] tool_use: {block.get('name', '')}")


async def tool_listing_test():
    """Compare tools available inline vs forked."""
    print("\n" + "="*70)
    print("TEST: Tool listing comparison")
    print("="*70)

    before = get_all_files()

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=10,
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    )

    client = ClaudeSDKClient(options)

    async with client:
        # Inline tool listing
        print("\n--- Inline tool listing ---")
        await client.query("Use the inline-tool-lister skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        if "INLINE_TOOLS" in block.text or "INLINE_TOOL_COUNT" in block.text:
                            print(f"  {block.text[:500]}")

        # Forked tool listing
        print("\n--- Forked tool listing ---")
        await client.query("Use the forked-tool-lister skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  {block.text[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

    # Check subagent JSONL for tool listing
    new_files = find_new_files(before)
    for filepath in sorted(new_files, key=lambda f: os.path.getmtime(f)):
        if "subagents" in filepath:
            entries = analyze_jsonl(filepath)
            for entry in entries:
                if entry.get("type") == "assistant":
                    content = entry.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block["text"]
                            if "FORKED_TOOLS" in text or "FORKED_TOOL_COUNT" in text:
                                print(f"\n  SUBAGENT RAW RESPONSE:")
                                print(f"  {text[:500]}")


async def main():
    print("="*70)
    print("SPIKE 24: Context inheritance and tool availability")
    print("="*70)

    await multi_turn_test()
    await tool_listing_test()


if __name__ == "__main__":
    asyncio.run(main())
