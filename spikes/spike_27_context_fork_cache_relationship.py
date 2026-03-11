"""
Spike 27: context:fork cache relationship analysis

Key question: Does the subagent's cache share a prefix with the main agent?
If the system prompt is the same, the cache_read should be high.

Tests:
1. Run main agent turn, note cache stats
2. Run forked skill, note subagent cache stats
3. Run regular Task tool subagent, note cache stats
4. Compare all three — are subagent and task using the same mechanism?
5. Run forked skill TWICE in same session — does second subagent hit cache from first?
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


def extract_all_cache_stats(filepath):
    """Extract cache stats from all assistant entries."""
    entries = analyze_jsonl(filepath)
    stats = []
    for entry in entries:
        if entry.get("type") == "assistant":
            usage = entry.get("message", {}).get("usage", {})
            if usage:
                cr = usage.get("cache_read_input_tokens", 0)
                cc = usage.get("cache_creation_input_tokens", 0)
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                total = cr + cc + inp
                pct = (cr / total * 100) if total > 0 else 0
                stats.append({
                    "cache_read": cr,
                    "cache_creation": cc,
                    "input": inp,
                    "output": out,
                    "total_input": total,
                    "cache_pct": pct,
                })
    return stats


async def main():
    print("="*70)
    print("SPIKE 27: Cache relationship analysis")
    print("="*70)

    before = get_all_files()

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        model="haiku",
        max_turns=15,
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    )

    client = ClaudeSDKClient(options)
    session_id = None

    async with client:
        # Turn 1: Normal message (baseline cache)
        print("\n--- Turn 1: Normal message (baseline) ---")
        await client.query("Say exactly: 'Hello world.' Nothing else.")
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                session_id = msg.session_id

        # Turn 2: Forked skill #1
        print("\n--- Turn 2: Forked skill #1 ---")
        await client.query("Use the forked-greeter skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and "FORKED" in block.text:
                        print(f"  Result: {block.text[:200]}")

        # Turn 3: Forked skill #2 (same skill again — does subagent cache hit?)
        print("\n--- Turn 3: Forked skill #2 (repeat) ---")
        await client.query("Use the forked-greeter skill again please.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and "FORKED" in block.text:
                        print(f"  Result: {block.text[:200]}")

        # Turn 4: Task tool subagent (for comparison)
        print("\n--- Turn 4: Task tool subagent ---")
        await client.query(
            "Use the Task tool (Agent tool) to spawn a haiku subagent with this prompt: "
            "'Say exactly: TASK_AGENT_RESPONSE and nothing else.' Use subagent_type general-purpose."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and "TASK" in block.text:
                        print(f"  Result: {block.text[:200]}")
            elif isinstance(msg, ResultMessage):
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

    # Analyze all files
    new_files = get_all_files() - before
    main_file = None
    subagent_files = []
    task_subagent_files = []

    for f in sorted(new_files, key=lambda f: os.path.getmtime(f)):
        rel = os.path.relpath(f, SESSIONS_BASE)
        if "subagents" in rel:
            subagent_files.append(f)
        else:
            if session_id and session_id in os.path.basename(f):
                main_file = f
            else:
                # Could be a task subagent's main session
                task_subagent_files.append(f)

    print(f"\n{'='*70}")
    print("CACHE ANALYSIS")
    print(f"{'='*70}")

    if main_file:
        stats = extract_all_cache_stats(main_file)
        print(f"\n  MAIN SESSION ({os.path.basename(main_file)}):")
        for i, s in enumerate(stats):
            print(f"    Turn {i}: read={s['cache_read']:,} create={s['cache_creation']:,} "
                  f"input={s['input']:,} total={s['total_input']:,} ({s['cache_pct']:.0f}% cached) "
                  f"output={s['output']:,}")

    for i, sf in enumerate(sorted(subagent_files, key=lambda f: os.path.getmtime(f))):
        rel = os.path.relpath(sf, SESSIONS_BASE)
        stats = extract_all_cache_stats(sf)
        print(f"\n  SUBAGENT #{i+1} ({rel}):")
        for j, s in enumerate(stats):
            print(f"    Entry {j}: read={s['cache_read']:,} create={s['cache_creation']:,} "
                  f"input={s['input']:,} total={s['total_input']:,} ({s['cache_pct']:.0f}% cached) "
                  f"output={s['output']:,}")

    # Look for Task tool subagent sessions too
    for f in new_files:
        rel = os.path.relpath(f, SESSIONS_BASE)
        if f != main_file and f not in subagent_files and "subagents" not in rel:
            stats = extract_all_cache_stats(f)
            if stats:
                print(f"\n  TASK SUBAGENT ({rel}):")
                for j, s in enumerate(stats):
                    print(f"    Entry {j}: read={s['cache_read']:,} create={s['cache_creation']:,} "
                          f"input={s['input']:,} total={s['total_input']:,} ({s['cache_pct']:.0f}% cached) "
                          f"output={s['output']:,}")

    # Summary
    print(f"\n{'='*70}")
    print("KEY FINDINGS")
    print(f"{'='*70}")
    print(f"  Total new files: {len(new_files)}")
    print(f"  Main session files: {1 if main_file else 0}")
    print(f"  Subagent (forked skill) files: {len(subagent_files)}")
    print(f"  Other files (task subagent?): {len(new_files) - len(subagent_files) - (1 if main_file else 0)}")


if __name__ == "__main__":
    asyncio.run(main())
