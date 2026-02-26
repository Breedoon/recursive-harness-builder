"""Shared utilities for cache-measurement spikes.

Parses session JSONL files and extracts cache metrics from assistant entries.
"""

import json
from pathlib import Path


PROJECTS_DIR = Path.home() / ".claude" / "projects"


def find_session_jsonl(session_id: str) -> Path | None:
    """Find the JSONL file for a given session ID across all projects."""
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def extract_cache_stats(session_id: str) -> list[dict]:
    """Extract cache stats from all assistant entries in a session.

    Returns a list of dicts with keys:
      uuid, cache_read, cache_creation, input_tokens, output_tokens, total_input, cache_rate
    """
    path = find_session_jsonl(session_id)
    if not path:
        print(f"  [!] JSONL not found for session {session_id}")
        return []

    stats = []
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            if obj.get("type") != "assistant" or "message" not in obj:
                continue
            usage = obj["message"].get("usage", {})
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_creation = usage.get("cache_creation_input_tokens", 0)
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_input = cache_read + cache_creation + input_tokens
            cache_rate = cache_read / total_input if total_input > 0 else 0.0

            stats.append({
                "uuid": obj.get("uuid"),
                "cache_read": cache_read,
                "cache_creation": cache_creation,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_input": total_input,
                "cache_rate": cache_rate,
            })
    return stats


def extract_message_uuids(session_id: str) -> list[dict]:
    """Extract all message entries (user + assistant) with their UUIDs.

    Returns list of dicts with: uuid, type, parent_uuid, text_preview
    """
    path = find_session_jsonl(session_id)
    if not path:
        return []

    entries = []
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            if obj.get("type") not in ("user", "assistant"):
                continue
            text = ""
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content[:80]
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")[:80]
                        break
            entries.append({
                "uuid": obj.get("uuid"),
                "type": obj.get("type"),
                "parent_uuid": obj.get("parentUuid"),
                "text_preview": text,
            })
    return entries


def print_cache_report(label: str, stats: list[dict]):
    """Print a formatted cache report."""
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    if not stats:
        print("  No assistant entries found.")
        return

    for i, s in enumerate(stats):
        print(f"\n  Assistant entry #{i + 1} (uuid: {s['uuid'][:12]}...)")
        print(f"    cache_read:     {s['cache_read']:>8,} tokens")
        print(f"    cache_creation: {s['cache_creation']:>8,} tokens")
        print(f"    input (fresh):  {s['input_tokens']:>8,} tokens")
        print(f"    output:         {s['output_tokens']:>8,} tokens")
        print(f"    total input:    {s['total_input']:>8,} tokens")
        print(f"    cache hit rate: {s['cache_rate']:>8.1%}")

    # Summary of last entry (most interesting for forks)
    last = stats[-1]
    print(f"\n  >> Last entry cache rate: {last['cache_rate']:.1%} "
          f"({last['cache_read']:,} cached / {last['total_input']:,} total)")
