"""
Spike 28: context:fork with allowed-tools restriction

Tests:
1. Does allowed-tools in frontmatter restrict the subagent's tool set?
2. What happens when the subagent tries to use a restricted tool?
3. Full raw subagent prompt extraction
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


async def main():
    print("="*70)
    print("SPIKE 28: allowed-tools restriction with context:fork")
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

    async with client:
        print("\n--- Invoking forked-restricted skill ---")
        await client.query("Use the forked-restricted skill now.")
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"  Main agent: {block.text[:500]}")
            elif isinstance(msg, ResultMessage):
                print(f"  Cost: ${msg.total_cost_usd:.4f}")

    # Analyze subagent
    new_files = get_all_files() - before
    for f in sorted(new_files, key=lambda f: os.path.getmtime(f)):
        if "subagents" in f:
            entries = analyze_jsonl(f)
            print(f"\n  --- Subagent JSONL ({len(entries)} entries) ---")
            for i, entry in enumerate(entries):
                etype = entry.get("type", "?")
                if etype == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        print(f"\n  [{i}] USER PROMPT (full, {len(content)} chars):")
                        print(f"  {content}")
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    print(f"\n  [{i}] USER TEXT ({len(block['text'])} chars):")
                                    print(f"  {block['text']}")
                                elif block.get("type") == "tool_result":
                                    print(f"\n  [{i}] TOOL_RESULT:")
                                    print(f"  {str(block.get('content', ''))[:300]}")
                elif etype == "assistant":
                    usage = entry.get("message", {}).get("usage", {})
                    cr = usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation_input_tokens", 0)
                    inp = usage.get("input_tokens", 0)
                    total = cr + cc + inp
                    pct = (cr / total * 100) if total > 0 else 0

                    content = entry.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                print(f"\n  [{i}] ASSISTANT TEXT:")
                                print(f"  {block['text'][:400]}")
                                print(f"  Cache: read={cr:,} create={cc:,} input={inp:,} ({pct:.0f}% cached)")
                            elif block.get("type") == "tool_use":
                                print(f"\n  [{i}] TOOL_USE: {block.get('name', '')}({json.dumps(block.get('input', {}))[:200]})")


if __name__ == "__main__":
    asyncio.run(main())
