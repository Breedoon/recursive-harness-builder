"""
Spike 23b: Deep JSONL analysis of context:fork sessions

Now that we know session files are at -private-tmp-... and forked skills
create subagents/ directories, let's analyze the structure in detail.
"""
import spike_env  # noqa: F401
import json
import os
import glob
from pathlib import Path

SESSIONS_BASE = os.path.expanduser("~/.claude/projects/-private-tmp-context-fork-spikes")


def analyze_jsonl(filepath):
    """Parse a JSONL file into entries."""
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


def print_entries(entries, label=""):
    """Print detailed entry summary."""
    print(f"\n  {'='*65}")
    print(f"  {label} ({len(entries)} entries)")
    print(f"  {'='*65}")

    for i, entry in enumerate(entries):
        etype = entry.get("type", "?")
        uuid_str = entry.get("uuid", "")[:12] if entry.get("uuid") else "---"
        parent_str = entry.get("parentUuid", "")[:12] if entry.get("parentUuid") else "---"

        detail = ""
        usage_str = ""

        if etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        bt = block.get("type", "?")
                        if bt == "text":
                            parts.append(f'text:"{block["text"][:80]}"')
                        elif bt == "tool_result":
                            tc = block.get("content", "")
                            if isinstance(tc, list):
                                tc = str(tc)[:60]
                            parts.append(f'tool_result:"{str(tc)[:80]}"')
                        elif bt == "tool_use":
                            parts.append(f'tool_use:{block.get("name", "")}')
                detail = " | ".join(parts)
            elif isinstance(content, str):
                detail = content[:100]

        elif etype == "assistant":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            parts = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        bt = block.get("type", "?")
                        if bt == "text":
                            parts.append(f'text:"{block["text"][:60]}"')
                        elif bt == "tool_use":
                            inp = json.dumps(block.get("input", {}))[:60]
                            parts.append(f'{block.get("name", "")}({inp})')
                        elif bt == "thinking":
                            parts.append("thinking")
            detail = " | ".join(parts)

            usage = msg.get("usage", {})
            if usage:
                cr = usage.get("cache_read_input_tokens", 0)
                cc = usage.get("cache_creation_input_tokens", 0)
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                total_in = cr + cc + inp
                pct = (cr / total_in * 100) if total_in > 0 else 0
                usage_str = f" [CR={cr:,} CC={cc:,} IN={inp:,} OUT={out:,} | {pct:.0f}% cached]"

        elif etype == "system":
            detail = f'subtype={entry.get("subtype", "?")}'
        elif etype == "queue-operation":
            detail = "queue metadata"
        elif etype == "progress":
            detail = "progress indicator"

        line = f"  [{i:3d}] {etype:15s} uuid={uuid_str:12s} parent={parent_str:12s}"
        if detail:
            line += f" | {detail[:90]}"
        if usage_str:
            line += f"\n        {usage_str}"
        print(line)


def main():
    print("="*70)
    print("SPIKE 23b: Deep JSONL analysis")
    print(f"Base: {SESSIONS_BASE}")
    print("="*70)

    # Find all JSONL files including subagents
    all_files = glob.glob(os.path.join(SESSIONS_BASE, "**/*.jsonl"), recursive=True)
    all_files.sort(key=lambda f: os.path.getmtime(f))

    print(f"\nTotal JSONL files found: {len(all_files)}")
    for f in all_files:
        rel = os.path.relpath(f, SESSIONS_BASE)
        size = os.path.getsize(f)
        print(f"  {rel:60s} ({size:,} bytes)")

    # Analyze each file
    for filepath in all_files:
        rel = os.path.relpath(filepath, SESSIONS_BASE)
        entries = analyze_jsonl(filepath)
        is_subagent = "subagents" in rel

        label = f"{'SUBAGENT: ' if is_subagent else 'MAIN: '}{rel}"
        print_entries(entries, label)


if __name__ == "__main__":
    main()
