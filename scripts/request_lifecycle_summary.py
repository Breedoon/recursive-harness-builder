#!/usr/bin/env python3
"""Deterministic per-agent/per-tree summary of local-model request lifecycle.

Groups by agent and root tree, reports unknown/lost coverage, avoids
double-counting transcript and proxy sources by declaring one canonical
source per metric.

Usage:
    python scripts/request_lifecycle_summary.py [--dir DIR] [--since HOURS] [--json]

Canonical source declaration:
    - Token usage (input, output, cache): this lifecycle log is canonical.
      Transcript JSONL also records usage, but per-API-call usage is counted
      here to avoid double-counting between proxy and transcript sources.
    - Duration components: this lifecycle log is canonical (monotonic clock).
    - Identity: session cache in the lifecycle logger; coverage of unknown
      sessions is reported explicitly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

DEFAULT_DIR = os.environ.get(
    "OBS_REQUEST_LIFECYCLE_DIR",
    "/workspace/runtime/state/local-gpu/requests",
)


def load_rows(root: str, since_hours: float = 0) -> list[dict]:
    """Load lifecycle JSONL rows, optionally filtered by recency."""
    cutoff = time.time() - since_hours * 3600 if since_hours > 0 else 0
    rows = []
    root_path = Path(root)
    if not root_path.exists():
        return rows
    for name in sorted(root_path.iterdir()):
        if not name.name.endswith(".jsonl"):
            continue
        with open(name) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("ts", 0) >= cutoff:
                    rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    """Build deterministic per-agent and per-tree summaries."""
    # Per-agent accumulator
    by_agent: dict[str, dict] = defaultdict(lambda: {
        "requests": 0,
        "success": 0,
        "error": 0,
        "timeout": 0,
        "client_gone": 0,
        "cancelled": 0,
        "unknown_outcome": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "output_tokens": 0,
        "total_ms": 0,
        "generation_ms": 0,
        "hold_ms": 0,
        "models": set(),
        "sessions": set(),
    })

    # Per-tree accumulator
    by_tree: dict[str, dict] = defaultdict(lambda: {
        "requests": 0,
        "agents": set(),
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "output_tokens": 0,
        "total_ms": 0,
    })

    unknown_count = 0
    total_count = len(rows)

    for row in rows:
        identity = row.get("identity") or {}
        agent_name = identity.get("agent_name") or "unknown"
        root_team = identity.get("root_team_key") or "unknown"
        identity_source = row.get("identity_source", "unknown")

        if identity_source == "unknown":
            unknown_count += 1

        usage = row.get("usage") or {}
        durations = row.get("durations") or {}
        outcome = row.get("outcome", "unknown")

        # Agent accumulation
        a = by_agent[agent_name]
        a["requests"] += 1
        if outcome == "success":
            a["success"] += 1
        elif outcome == "error":
            a["error"] += 1
        elif outcome == "timeout":
            a["timeout"] += 1
        elif outcome == "client_gone":
            a["client_gone"] += 1
        elif outcome == "cancelled":
            a["cancelled"] += 1
        else:
            a["unknown_outcome"] += 1
        a["input_tokens"] += usage.get("input_tokens", 0)
        a["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
        a["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
        a["output_tokens"] += usage.get("output_tokens", 0)
        a["total_ms"] += durations.get("total_ms", 0) or 0
        a["generation_ms"] += durations.get("generation_ms", 0) or 0
        a["hold_ms"] += durations.get("hold_ms", 0) or 0
        model = row.get("model", "")
        if model:
            a["models"].add(model)
        sid = row.get("session_id", "")
        if sid:
            a["sessions"].add(sid)

        # Tree accumulation
        t = by_tree[root_team]
        t["requests"] += 1
        t["agents"].add(agent_name)
        t["input_tokens"] += usage.get("input_tokens", 0)
        t["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
        t["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
        t["output_tokens"] += usage.get("output_tokens", 0)
        t["total_ms"] += durations.get("total_ms", 0) or 0

    # Serialize sets for JSON
    result_agents = {}
    for name, a in sorted(by_agent.items()):
        a_copy = dict(a)
        a_copy["models"] = sorted(a_copy["models"])
        a_copy["session_count"] = len(a_copy.pop("sessions"))
        result_agents[name] = a_copy

    result_trees = {}
    for name, t in sorted(by_tree.items()):
        t_copy = dict(t)
        t_copy["agents"] = sorted(t_copy["agents"])
        t_copy["agent_count"] = len(t_copy["agents"])
        result_trees[name] = t_copy

    return {
        "total_requests": total_count,
        "unknown_identity_requests": unknown_count,
        "unknown_coverage_pct": round(
            100 * unknown_count / total_count, 1) if total_count else 0,
        "canonical_source": "lifecycle_log",
        "canonical_source_note": (
            "Token usage and durations are counted from this lifecycle log only. "
            "Transcript JSONL is NOT mixed in to avoid double-counting."
        ),
        "by_agent": result_agents,
        "by_tree": result_trees,
    }


def print_text(summary: dict) -> None:
    """Human-readable text output."""
    print(f"Total requests: {summary['total_requests']}")
    print(f"Unknown identity: {summary['unknown_identity_requests']} "
          f"({summary['unknown_coverage_pct']}%)")
    print()

    if summary["by_tree"]:
        print("=== Per Tree ===")
        for name, t in summary["by_tree"].items():
            print(f"\n  {name}")
            print(f"    requests: {t['requests']}, agents: {t['agent_count']}")
            inp = t["input_tokens"]
            out = t["output_tokens"]
            cr = t["cache_read_tokens"]
            cc = t["cache_creation_tokens"]
            print(f"    tokens: input={inp:,} cache_read={cr:,} "
                  f"cache_creation={cc:,} output={out:,}")
            if t["total_ms"]:
                print(f"    total time: {t['total_ms'] / 1000:.1f}s")

    if summary["by_agent"]:
        print("\n=== Per Agent ===")
        for name, a in summary["by_agent"].items():
            print(f"\n  {name}")
            print(f"    requests: {a['requests']} "
                  f"(ok={a['success']} err={a['error']} "
                  f"timeout={a['timeout']} gone={a['client_gone']} "
                  f"cancel={a['cancelled']})")
            inp = a["input_tokens"]
            out = a["output_tokens"]
            cr = a["cache_read_tokens"]
            cc = a["cache_creation_tokens"]
            print(f"    tokens: input={inp:,} cache_read={cr:,} "
                  f"cache_creation={cc:,} output={out:,}")
            if a["total_ms"]:
                print(f"    total time: {a['total_ms'] / 1000:.1f}s "
                      f"(gen={a['generation_ms'] / 1000:.1f}s "
                      f"hold={a['hold_ms'] / 1000:.1f}s)")
            print(f"    sessions: {a['session_count']}, "
                  f"models: {', '.join(a['models']) or 'none'}")


def main():
    parser = argparse.ArgumentParser(
        description="Local-model request lifecycle summary")
    parser.add_argument("--dir", default=DEFAULT_DIR,
                        help="Lifecycle log directory")
    parser.add_argument("--since", type=float, default=0,
                        help="Only rows from the last N hours")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON instead of text")
    args = parser.parse_args()

    rows = load_rows(args.dir, args.since)
    if not rows:
        print(f"No lifecycle rows found in {args.dir}", file=sys.stderr)
        sys.exit(0)

    summary = summarize(rows)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print_text(summary)


if __name__ == "__main__":
    main()
