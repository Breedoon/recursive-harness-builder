"""
Spike 23: context:fork JSONL and session analysis

After running skills with context: fork, examine the JSONL files to understand:
1. Does forked skill create a new session file?
2. What tool does it use internally (Task? Agent? Something else?)
3. How does the JSONL structure differ from inline?
4. Can we see the subagent session?
"""
import spike_env  # noqa: F401
import asyncio
import json
import os
import glob
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    TextBlock, AssistantMessage, ResultMessage, SystemMessage,
)

PROJECT_DIR = "/tmp/context-fork-spikes"
# Session files for this project
SESSIONS_DIR = os.path.expanduser("~/.claude/projects/-tmp-context-fork-spikes")


def get_session_files(before_set=None):
    """Get all JSONL session files, optionally filtering new ones."""
    pattern = os.path.join(SESSIONS_DIR, "*.jsonl")
    files = set(glob.glob(pattern))
    if before_set is not None:
        return files - before_set
    return files


def analyze_jsonl(filepath):
    """Parse and analyze a JSONL session file."""
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


def print_jsonl_summary(filepath, label=""):
    """Print a summary of a JSONL file."""
    entries = analyze_jsonl(filepath)
    print(f"\n  {'='*60}")
    print(f"  JSONL: {os.path.basename(filepath)} {label}")
    print(f"  Entries: {len(entries)}")
    print(f"  {'='*60}")

    for i, entry in enumerate(entries):
        etype = entry.get("type", "?")
        uuid = entry.get("uuid", "")[:12] if entry.get("uuid") else "none"
        parent = entry.get("parentUuid", "")[:12] if entry.get("parentUuid") else "none"

        # Extract key content
        detail = ""
        if etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            detail = block["text"][:100]
                        elif block.get("type") == "tool_result":
                            detail = f"tool_result({block.get('content', '')[:80]})"
                        elif block.get("type") == "tool_use":
                            detail = f"tool_use({block.get('name', '')})"
            elif isinstance(content, str):
                detail = content[:100]
        elif etype == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            detail = f"text: {block['text'][:100]}"
                        elif block.get("type") == "tool_use":
                            detail = f"tool_use: {block.get('name', '')}({json.dumps(block.get('input', {}))[:80]})"
                        elif block.get("type") == "thinking":
                            detail = "thinking..."
            # Cache stats
            usage = msg.get("usage", {})
            if usage:
                cr = usage.get("cache_read_input_tokens", 0)
                cc = usage.get("cache_creation_input_tokens", 0)
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                detail += f" | cache_read={cr} cache_create={cc} input={inp} output={out}"
        elif etype == "system":
            subtype = entry.get("subtype", "")
            detail = f"subtype={subtype}"
        elif etype == "queue-operation":
            detail = "queue metadata"
        elif etype == "progress":
            detail = "progress"

        print(f"  [{i:3d}] {etype:15s} uuid={uuid:12s} parent={parent:12s} | {detail[:120]}")

    return entries


async def run_and_capture(label, prompt):
    """Run a query and return the session ID."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"{'='*70}")

    before_files = get_session_files()

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
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                session_id = msg.session_id
                print(f"  Session ID: {session_id}")
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

    # Check for new session files
    after_files = get_session_files()
    new_files = after_files - before_files
    print(f"\n  New JSONL files created: {len(new_files)}")
    for f in new_files:
        print(f"    {os.path.basename(f)}")

    return session_id, new_files


async def main():
    print("="*70)
    print("SPIKE 23: context:fork JSONL analysis")
    print(f"Sessions dir: {SESSIONS_DIR}")
    print("="*70)

    # Make sure sessions dir exists
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    # Snapshot existing files
    initial_files = get_session_files()
    print(f"Initial session files: {len(initial_files)}")

    # Test 1: Inline skill
    inline_sid, inline_new = await run_and_capture(
        "Inline Skill",
        "Use the inline-greeter skill now."
    )

    # Test 2: Forked skill
    forked_sid, forked_new = await run_and_capture(
        "Forked Skill",
        "Use the forked-greeter skill now."
    )

    # Analyze all new JSONL files
    print(f"\n{'='*70}")
    print("JSONL ANALYSIS")
    print(f"{'='*70}")

    all_new = get_session_files(initial_files)
    print(f"\nTotal new JSONL files: {len(all_new)}")

    for f in sorted(all_new):
        basename = os.path.basename(f)
        label = ""
        if inline_sid and inline_sid in basename:
            label = "(INLINE session)"
        elif forked_sid and forked_sid in basename:
            label = "(FORKED session)"
        else:
            label = "(UNKNOWN — possibly subagent session)"
        print_jsonl_summary(f, label)


if __name__ == "__main__":
    asyncio.run(main())
