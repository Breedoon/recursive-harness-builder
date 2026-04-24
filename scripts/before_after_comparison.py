#!/usr/bin/env python3
"""Before/after comparison for CLAUDEMD_MARKER false positive fix.

Analyzes JSONL session files to compute adjusted cache hit rates, then
simulates the impact of the Bug 1 fix (CLAUDEMD false positive in
_is_strippable_system_reminder) by scanning proxy log request bodies.

Methodology:
- Adjusted cache hit rate from JSONL analysis skill v1.1
- Fork detection via sessionId (NOT UUID comparison)
- Baseline = min(cache_read) from non-zero turns after turn 0
- HIT: rate >= 0.95 or (abs_delta < 2000 and rate >= 0.50)
- MISS: rate < 0.05
- EDGE: everything else (treated as MISS)

Usage:
    python scripts/before_after_comparison.py [--days N] [--verbose]
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JSONL_DIR = Path(os.path.expanduser(
    "~/.claude/projects/-Users-breedoon-Library-Mobile-Documents-iCloud-md-obsidian-Documents-T"
))
PROXY_LOG_DIRS = [
    Path("/tmp/cache-proxy-log/bodies"),
    Path(os.path.expanduser("~/Downloads/cache-proxy-log-as-of-2026-04-21-2pm/bodies")),
    Path(os.path.expanduser("~/Documents/obs/.obs-agent/cache-proxy-log/bodies")),
]
CLAUDEMD_MARKER = "As you answer the user's questions, you can use the following context:"
CLAUDEMD_MARKER_CHECK_LIMIT = 300  # Fixed version checks only first 300 chars


def parse_args():
    p = argparse.ArgumentParser(description="Before/after cache hit comparison")
    p.add_argument("--days", type=int, default=3, help="Analyze sessions from last N days")
    p.add_argument("--verbose", action="store_true", help="Show per-session details")
    p.add_argument("--proxy-bodies", action="store_true",
                   help="Also scan proxy log bodies for false positive instances")
    return p.parse_args()


# ── JSONL Analysis ──────────────────────────────────────────────────────


def load_session(jsonl_path: Path) -> list[dict]:
    """Load all entries from a JSONL file."""
    entries = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    return entries


def extract_assistant_turns(entries: list[dict]) -> list[dict]:
    """Extract deduplicated assistant turns with usage data.

    Multiple assistant entries per API call share the same message.id.
    Deduplicate by message.id, keeping the one with usage data.
    """
    seen_ids = {}
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        usage = msg.get("usage")
        if not usage:
            continue
        msg_id = msg.get("id", entry.get("uuid", ""))
        cr = usage.get("cache_read_input_tokens", 0)
        cc = usage.get("cache_creation_input_tokens", 0)
        ip = usage.get("input_tokens", 0)
        total = cr + cc + ip
        if total == 0:
            continue

        # Keep the entry with usage (or first seen)
        if msg_id not in seen_ids or seen_ids[msg_id]["total"] == 0:
            seen_ids[msg_id] = {
                "msg_id": msg_id,
                "uuid": entry.get("uuid", ""),
                "session_id": entry.get("sessionId", ""),
                "cache_read": cr,
                "cache_creation": cc,
                "input_tokens": ip,
                "total": total,
                "timestamp": entry.get("timestamp", ""),
                "has_1h_ttl": (
                    usage.get("cache_creation", {}).get("ephemeral_1h_input_tokens", 0) > 0
                ),
            }
    # Return in order of appearance
    result = []
    seen_order = []
    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        msg_id = msg.get("id", entry.get("uuid", ""))
        if msg_id in seen_ids and msg_id not in seen_order:
            seen_order.append(msg_id)
            result.append(seen_ids[msg_id])
    return result


def compute_baseline(turns: list[dict]) -> int:
    """Compute baseline = min(cache_read) from non-zero turns after turn 0."""
    candidates = []
    for t in turns[1:]:  # Skip turn 0
        if t["cache_read"] > 0:
            candidates.append(t["cache_read"])
    return min(candidates) if candidates else 0


def classify_turn(turn: dict, prev_turn: dict, baseline: int) -> dict:
    """Classify a turn's cache behavior using adjusted rate."""
    adjusted_read = turn["cache_read"] - baseline
    adjusted_prefix = prev_turn["total"] - baseline

    if adjusted_prefix <= 0:
        rate = 0.0
    else:
        rate = adjusted_read / adjusted_prefix

    abs_delta = abs(adjusted_prefix - adjusted_read)

    if rate >= 0.95 or (abs_delta < 2000 and rate >= 0.50):
        status = "HIT"
    elif rate < 0.05:
        status = "MISS"
    else:
        status = "EDGE"

    return {
        "status": status,
        "rate": rate,
        "adjusted_read": adjusted_read,
        "adjusted_prefix": adjusted_prefix,
        "abs_delta": abs_delta,
        "cache_read": turn["cache_read"],
        "total": turn["total"],
        "prev_total": prev_turn["total"],
        "baseline": baseline,
        "is_fork_point": turn["session_id"] != prev_turn["session_id"],
    }


def analyze_session(jsonl_path: Path) -> dict:
    """Analyze a single session's cache behavior."""
    session_id = jsonl_path.stem
    entries = load_session(jsonl_path)
    if not entries:
        return None

    turns = extract_assistant_turns(entries)
    if len(turns) < 2:
        return None

    # Get session time range
    timestamps = []
    for e in entries:
        ts = e.get("timestamp", "")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except (ValueError, TypeError):
                pass
    if not timestamps:
        return None

    first_ts = min(timestamps)
    last_ts = max(timestamps)

    baseline = compute_baseline(turns)
    classifications = []
    fork_transitions = []

    for i in range(1, len(turns)):
        c = classify_turn(turns[i], turns[i - 1], baseline)
        c["turn_idx"] = i
        c["session_id"] = turns[i]["session_id"]
        c["prev_session_id"] = turns[i - 1]["session_id"]
        c["timestamp"] = turns[i]["timestamp"]
        classifications.append(c)

        if c["is_fork_point"]:
            fork_transitions.append(c)

    hits = sum(1 for c in classifications if c["status"] == "HIT")
    misses = sum(1 for c in classifications if c["status"] == "MISS")
    edges = sum(1 for c in classifications if c["status"] == "EDGE")
    fork_hits = sum(1 for f in fork_transitions if f["status"] == "HIT")
    fork_misses = sum(1 for f in fork_transitions if f["status"] != "HIT")

    return {
        "session_id": session_id,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "total_turns": len(turns),
        "classified_turns": len(classifications),
        "hits": hits,
        "misses": misses,
        "edges": edges,
        "hit_rate": hits / len(classifications) if classifications else 0,
        "baseline": baseline,
        "fork_transitions": len(fork_transitions),
        "fork_hits": fork_hits,
        "fork_misses": fork_misses,
        "has_1h_ttl": any(t.get("has_1h_ttl") for t in turns),
        "classifications": classifications,
        "fork_details": fork_transitions,
    }


# ── Proxy Body Analysis ────────────────────────────────────────────────


def scan_proxy_bodies_for_false_positives() -> dict:
    """Scan proxy log bodies for CLAUDEMD_MARKER false positive instances.

    The bug: _is_strippable_system_reminder() checks full text for
    CLAUDEMD_MARKER. A changed_files system-reminder containing a diff
    of CLAUDE.md will have the marker deep in the text (not in the first
    300 chars), causing the proxy to NOT strip it (false negative for
    stripping = false positive for CLAUDEMD detection).
    """
    results = {
        "dirs_checked": [],
        "files_scanned": 0,
        "false_positive_count": 0,
        "false_positive_files": [],
        "total_system_reminders": 0,
        "correctly_identified_claudemd": 0,
    }

    for body_dir in PROXY_LOG_DIRS:
        if not body_dir.exists():
            continue
        results["dirs_checked"].append(str(body_dir))

        for f in sorted(body_dir.glob("*_pre.json")):
            results["files_scanned"] += 1
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue

            messages = data.get("messages", [])
            for msg in messages:
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = block.get("text", "")
                    if "<system-reminder>" not in text:
                        continue

                    results["total_system_reminders"] += 1

                    has_marker_full = CLAUDEMD_MARKER in text
                    has_marker_first300 = CLAUDEMD_MARKER in text[:CLAUDEMD_MARKER_CHECK_LIMIT]

                    if has_marker_first300:
                        results["correctly_identified_claudemd"] += 1
                    elif has_marker_full:
                        # False positive: marker found deep in text (e.g., in a diff)
                        # Old code would NOT strip this (preserves as "CLAUDE.md")
                        # But it's actually a changed_files or other dynamic reminder
                        results["false_positive_count"] += 1
                        results["false_positive_files"].append({
                            "file": str(f),
                            "marker_pos": text.index(CLAUDEMD_MARKER),
                            "text_len": len(text),
                            "text_preview": text[:200].replace("\n", "\\n"),
                        })

    return results


# ── Output ──────────────────────────────────────────────────────────────


def print_summary(sessions: list[dict], args):
    """Print overall summary statistics."""
    if not sessions:
        print("No sessions found in the specified time range.")
        return

    total_turns = sum(s["classified_turns"] for s in sessions)
    total_hits = sum(s["hits"] for s in sessions)
    total_misses = sum(s["misses"] for s in sessions)
    total_edges = sum(s["edges"] for s in sessions)
    total_forks = sum(s["fork_transitions"] for s in sessions)
    total_fork_hits = sum(s["fork_hits"] for s in sessions)
    total_fork_misses = sum(s["fork_misses"] for s in sessions)

    overall_rate = total_hits / total_turns if total_turns else 0
    fork_rate = total_fork_hits / total_forks if total_forks else 0

    print("=" * 70)
    print("ADJUSTED CACHE HIT RATE ANALYSIS")
    print("=" * 70)
    print(f"Sessions analyzed:    {len(sessions)}")
    print(f"Total classified turns: {total_turns}")
    print(f"  HIT:   {total_hits:5d}  ({total_hits/total_turns:.1%})" if total_turns else "")
    print(f"  MISS:  {total_misses:5d}  ({total_misses/total_turns:.1%})" if total_turns else "")
    print(f"  EDGE:  {total_edges:5d}  ({total_edges/total_turns:.1%})" if total_turns else "")
    print(f"\nOverall adjusted hit rate: {overall_rate:.1%}")
    print(f"\nFork transitions:     {total_forks}")
    if total_forks:
        print(f"  Fork HIT:   {total_fork_hits:5d}  ({fork_rate:.1%})")
        print(f"  Fork MISS:  {total_fork_misses:5d}  ({1 - fork_rate:.1%})")

    # Break down misses by type
    fork_point_misses = []
    continuation_misses = []
    for s in sessions:
        for c in s["classifications"]:
            if c["status"] != "HIT":
                if c["is_fork_point"]:
                    fork_point_misses.append(c)
                else:
                    continuation_misses.append(c)

    print(f"\n--- Miss Breakdown ---")
    print(f"Fork-point misses:    {len(fork_point_misses)}")
    print(f"Continuation misses:  {len(continuation_misses)}")

    # Classify continuation misses
    cold_zeros = [m for m in continuation_misses if m["cache_read"] == 0]
    partial = [m for m in continuation_misses
               if m["cache_read"] > 0 and m["status"] == "EDGE"]
    other_misses = [m for m in continuation_misses
                    if m["cache_read"] == 0 and m["status"] == "MISS"
                    and m not in cold_zeros]

    print(f"  Cold zeros (cr=0):  {len(cold_zeros)}  (likely daemon restarts)")
    print(f"  Partial/EDGE:       {len(partial)}")

    if args.verbose:
        print(f"\n--- Per-Session Details ---")
        for s in sorted(sessions, key=lambda x: x["first_ts"]):
            rate_str = f"{s['hit_rate']:.0%}"
            fork_str = f"forks={s['fork_transitions']}({s['fork_hits']}H/{s['fork_misses']}M)" if s["fork_transitions"] else "no forks"
            ttl_str = "1h" if s["has_1h_ttl"] else "5m"
            print(f"  {s['session_id'][:8]}  turns={s['classified_turns']:3d}  "
                  f"rate={rate_str:>4s}  {fork_str}  ttl={ttl_str}  "
                  f"base={s['baseline']:,}  "
                  f"{s['first_ts'].strftime('%m-%d %H:%M')}-{s['last_ts'].strftime('%H:%M')}")

        # Show fork miss details
        if fork_point_misses:
            print(f"\n--- Fork-Point Miss Details ---")
            for m in fork_point_misses:
                print(f"  turn={m['turn_idx']:3d}  cr={m['cache_read']:>8,}  "
                      f"prev_tot={m['prev_total']:>8,}  "
                      f"adj_rate={m['rate']:.1%}  base={m['baseline']:,}  "
                      f"sid={m['session_id'][:8]}←{m['prev_session_id'][:8]}")


def print_proxy_analysis(proxy_results: dict):
    """Print proxy body analysis results."""
    print(f"\n{'=' * 70}")
    print("PROXY BODY FALSE POSITIVE ANALYSIS")
    print("=" * 70)
    print(f"Directories checked: {len(proxy_results['dirs_checked'])}")
    for d in proxy_results["dirs_checked"]:
        print(f"  {d}")
    print(f"Request bodies scanned:     {proxy_results['files_scanned']}")
    print(f"Total system-reminder blocks: {proxy_results['total_system_reminders']}")
    print(f"Correctly identified CLAUDE.md: {proxy_results['correctly_identified_claudemd']}")
    print(f"FALSE POSITIVES (Bug 1):    {proxy_results['false_positive_count']}")

    if proxy_results["false_positive_files"]:
        print(f"\n--- False Positive Instances ---")
        for fp in proxy_results["false_positive_files"][:10]:
            print(f"  File: {os.path.basename(fp['file'])}")
            print(f"    Marker at char {fp['marker_pos']} (text len={fp['text_len']})")
            print(f"    Preview: {fp['text_preview'][:120]}...")

    # Estimate impact
    if proxy_results["false_positive_count"] > 0:
        print(f"\n--- Estimated Fix Impact ---")
        print(f"The fix (checking first {CLAUDEMD_MARKER_CHECK_LIMIT} chars instead of full text)")
        print(f"would correctly strip {proxy_results['false_positive_count']} blocks that are")
        print(f"currently being preserved as 'CLAUDE.md' but are actually changed_files diffs.")
        print(f"Each false positive creates a byte-different prefix → potential cache miss.")
    else:
        print(f"\nNo false positives detected in captured bodies.")
        print(f"Either: (1) the bug is rare, (2) no CLAUDE.md edits during captured sessions,")
        print(f"or (3) SAVE_BODIES was off during affected sessions.")


def main():
    args = parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    print(f"Scanning sessions from last {args.days} days (since {cutoff.strftime('%Y-%m-%d %H:%M')} UTC)...")
    print(f"JSONL dir: {JSONL_DIR}")

    if not JSONL_DIR.exists():
        print(f"ERROR: JSONL directory not found: {JSONL_DIR}")
        sys.exit(1)

    # Scan all JSONL files
    jsonl_files = sorted(JSONL_DIR.glob("*.jsonl"))
    print(f"Found {len(jsonl_files)} JSONL files total")

    sessions = []
    skipped = 0
    for jf in jsonl_files:
        # Quick check: skip files not modified in the time range
        mtime = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            skipped += 1
            continue

        result = analyze_session(jf)
        if result and result["first_ts"].replace(tzinfo=timezone.utc) >= cutoff:
            sessions.append(result)

    print(f"Analyzed {len(sessions)} sessions ({skipped} skipped by mtime)")

    print_summary(sessions, args)

    if args.proxy_bodies:
        proxy_results = scan_proxy_bodies_for_false_positives()
        print_proxy_analysis(proxy_results)

    print(f"\n{'=' * 70}")
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
