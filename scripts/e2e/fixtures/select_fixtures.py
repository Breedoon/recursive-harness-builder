#!/usr/bin/env python3
"""Pick the byte-resume fixture set from the frozen-log scan (obs-22 E3).

Input: candidates.jsonl from the frozen-log scan (one row per session: its
last main request, tags, JSONL pairing, "clean" flag). Output: a selection
JSONL for build_fixtures.py.

Rules (deterministic: same input -> same selection):
- only "clean" candidates: JSONL paired, both bodies readable, no JSONL rows
  after the logged request, leaf row an assistant end_turn;
- profile from the JSONL location: the vault project dir -> "obs" (an OBS
  agent: session.py's launch), a prefix-repro cwd -> "harness"
  (scripts/prefix_repro.py's launch); anything else (pytest dirs, worktrees)
  is skipped, its launch config is not reproducible;
- route "local" is skipped: local-model agents are outside the rollout
  population (design §3.1);
- the logged request must be one the CLI itself sent: ``stream`` is true in
  the stored body. scripts/prefix_repro.py's direct probes (cch_collision,
  volatile_flip, ...) replay a CLI body with ``stream: false`` and edited
  sections into the same session; no resume can reproduce them;
- sessions listed in ``--exclude`` (EXCLUDED.json next to the fixtures) are
  skipped: cases the pinned 2.1.59 itself can not resume byte-continuously,
  each root-caused during calibration (rare request shapes, see that file);
- per tag a quota, filled newest-first (the newest sessions are closest to
  today's CLI feature cache), anthropic and cli-proxy routes alternating;
  a session counts toward every tag it carries.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

FROZEN = Path("/workspace/runtime/preserved/cache-proxy-request-log-freeze-20261020T200401Z")

VAULT_PROJECT = "/-workspace-runtime-git-obs-vault-active/"
QUOTA = {"compaction": 4, "fork": 4, "images": 4, "ext_mcp": 3, "subagent": 2, "parallel_tools": 4,
         "thinking": 4, "sdk_mcp": 4, "gpt_route": 4, "harness": 6}
ORDER = ["compaction", "fork", "subagent", "images", "ext_mcp", "parallel_tools", "sdk_mcp",
         "thinking", "gpt_route", "harness"]
ANTHROPIC_PRODUCTION_MIN = 10
TOTAL = 36  # then filled with the newest production sessions, routes alternating


def profile(row: dict) -> str | None:
    p = row.get("jsonl_path") or ""
    if VAULT_PROJECT in p:
        return "obs"
    if "/-workspace-runtime-prefix-repro-cwd/" in p:
        return "harness"
    return None


def cli_made(row: dict) -> bool:
    """The stored wire body has stream=true (every CLI main request streams)."""
    path = FROZEN / "requests" / row["day"] / f"{row['req_id']}.wire.json.gz"
    try:
        return json.loads(gzip.decompress(path.read_bytes())).get("stream") is True
    except (OSError, ValueError):
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidates")
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude", help="JSON object {session_id: reason} of calibration exclusions")
    args = ap.parse_args(argv)
    excluded = json.loads(Path(args.exclude).read_text()) if args.exclude else {}
    rows = [json.loads(l) for l in Path(args.candidates).read_text().splitlines() if l.strip()]
    pool = [r for r in rows if r.get("clean") and r.get("route") != "local" and profile(r)
            and r["session_id"] not in excluded]
    pool.sort(key=lambda r: (-float(r.get("ts") or 0), r["session_id"]))
    chosen: dict[str, dict] = {}
    counts = {t: 0 for t in QUOTA}

    rejected: set[str] = set()

    def take(r):
        if r["session_id"] in chosen or r["session_id"] in rejected:
            return False
        if not cli_made(r):
            rejected.add(r["session_id"])
            return False
        chosen[r["session_id"]] = r
        for t in r.get("tags") or []:
            if t in counts:
                counts[t] += 1
        return True

    for tag in ORDER:
        want = QUOTA[tag]
        by_route = {"anthropic": [], "cli-proxy": []}
        for r in pool:
            if tag in (r.get("tags") or []) and r["session_id"] not in chosen:
                by_route.setdefault(r.get("route"), []).append(r)
        i = 0
        while counts[tag] < want and (by_route["anthropic"] or by_route["cli-proxy"]):
            route = ("anthropic", "cli-proxy")[i % 2]
            i += 1
            if by_route[route]:
                take(by_route[route].pop(0))
    prod_anthropic = [r for r in chosen.values() if r["route"] == "anthropic" and profile(r) == "obs"]
    for r in pool:
        if len(prod_anthropic) >= ANTHROPIC_PRODUCTION_MIN:
            break
        if r["route"] == "anthropic" and profile(r) == "obs" and r["session_id"] not in chosen:
            if take(r):
                prod_anthropic.append(r)
    fill = {"anthropic": [r for r in pool if r["route"] == "anthropic" and profile(r) == "obs"],
            "cli-proxy": [r for r in pool if r["route"] == "cli-proxy" and profile(r) == "obs"]}
    i = 0
    while len(chosen) < TOTAL and (fill["anthropic"] or fill["cli-proxy"]):
        route = ("anthropic", "cli-proxy")[i % 2]
        i += 1
        while fill[route] and fill[route][0]["session_id"] in chosen:
            fill[route].pop(0)
        if fill[route]:
            take(fill[route].pop(0))
    out = []
    for r in sorted(chosen.values(), key=lambda r: r["ts"]):
        tags = sorted(set(r.get("tags") or []))
        prof = profile(r)
        cid = f"{r['day']}-{prof}-{r['route']}-{r['session_id'][:8]}"
        out.append({"case_id": cid, "session_id": r["session_id"], "req_id": r["req_id"], "day": r["day"],
                    "tags": tags + ([f"route_{r['route']}"]), "profile": prof, "jsonl_path": r["jsonl_path"],
                    "note": ("subagent tag = agent-acompact transcript descendant, not a native Task run"
                             if "subagent" in tags else "")})
    Path(args.out).write_text("\n".join(json.dumps(o) for o in out) + "\n")
    tag_cover = {t: sum(t in o["tags"] for o in out) for t in QUOTA}
    print(f"selected {len(out)} cases; per tag: {tag_cover}; "
          f"routes: anthropic={sum(o['tags'][-1] == 'route_anthropic' for o in out)} "
          f"cli-proxy={sum(o['tags'][-1] == 'route_cli-proxy' for o in out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
