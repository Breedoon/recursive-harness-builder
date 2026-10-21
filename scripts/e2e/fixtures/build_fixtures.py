#!/usr/bin/env python3
"""Build byte-resume fixture cases (contract C2, obs-22 E3) from the frozen request log.

Input: a selection JSONL, one object per case:
  {"case_id": "...", "session_id": "...", "req_id": "...", "day": "YYYY-MM-DD",
   "tags": [...], "profile": "harness" | "obs", "jsonl_path": "...",
   "note": "..."}
(``select_fixtures.py`` writes it from the scan candidates.)

Output, per case, under --out (default /workspace/runtime/e2e/fixtures/byte-resume):
  <case_id>/session.jsonl        the real 2.1.59 session, cut at the logged point
  <case_id>/last_request.json.gz the logged post-proxy ("wire") body, as stored
  <case_id>/pre_request.json.gz  the logged CLI ("pre") body, when stored
  <case_id>/headers.json         the logged client headers (already redacted by the proxy)
  <case_id>/index_row.json       the frozen index row of that request
  <case_id>/meta.json            case id, tags, model, route, conversation key,
                                 launch profile, source paths, sha256 of every file

Bodies are copied byte for byte from the frozen log; they are the proxy's
redacted form (secrets, signatures and image data replaced by equality-keeping
placeholders), which is why the checker captures candidates through a proxy
that applies the identical redaction. Nothing under the frozen log is modified.

Cutting the session (``session.jsonl``): rows are kept in file order up to the
last row written no later than the logged request's end (ts + duration + 5 s).
The number of dropped rows is recorded; 0 means the session ended there.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

FROZEN = Path("/workspace/runtime/preserved/cache-proxy-request-log-freeze-20261020T200401Z")
OUT = Path("/workspace/runtime/e2e/fixtures/byte-resume")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _ts(row: dict) -> float | None:
    t = row.get("timestamp")
    if not isinstance(t, str):
        return None
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def index_row(day: str, req_id: str) -> dict:
    with open(FROZEN / "requests" / day / "index.jsonl") as f:
        for line in f:
            if req_id in line:
                r = json.loads(line)
                if r.get("req_id") == req_id:
                    return r
    raise KeyError(f"{req_id} not in {day} index")


def conversation_key(wire: dict) -> str | None:
    """sha256(model NUL first-message NUL) -- the key of the proxy's variant/TTL pins.

    Same formula as cache_proxy.pin_volatile_config / pin_cache_ttl. Computed on
    the stored (redacted) body, so it equals the proxy's key only when the first
    message holds nothing the redactor replaced; recorded for orientation.
    """
    msgs = wire.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k != "cache_control"}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    first = json.dumps(strip(msgs[0]), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(str(wire.get("model", "")).encode() + b"\0" + first + b"\0").hexdigest()


def cut_session(src: Path, dst: Path, end_ts: float) -> dict:
    kept, dropped, last_row = [], 0, None
    lines = src.read_bytes().split(b"\n")
    for raw in lines:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            dropped += 1  # torn tail
            continue
        t = _ts(row)
        if t is not None and t > end_ts:
            dropped += 1
            continue
        kept.append(raw)
        last_row = row
    dst.write_bytes(b"\n".join(kept) + b"\n")
    return {"rows_kept": len(kept), "rows_dropped": dropped,
            "last_row_type": (last_row or {}).get("type"),
            "last_row_stop_reason": ((last_row or {}).get("message") or {}).get("stop_reason"),
            "cwd": next((json.loads(r).get("cwd") for r in kept if b'"cwd"' in r), None),
            "versions": sorted({json.loads(r).get("version") for r in kept if b'"version"' in r} - {None})}


def build_case(sel: dict, out_root: Path) -> dict:
    case = out_root / sel["case_id"]
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    day, req_id = sel["day"], sel["req_id"]
    row = index_row(day, req_id)
    if row.get("session_id") != sel["session_id"]:
        raise ValueError("index row session id does not match the selection")
    src_dir = FROZEN / "requests" / day
    wire_src = src_dir / f"{req_id}.wire.json.gz"
    shutil.copyfile(wire_src, case / "last_request.json.gz")
    pre_src = src_dir / f"{req_id}.pre.json.gz"
    if pre_src.exists():
        shutil.copyfile(pre_src, case / "pre_request.json.gz")
    (case / "headers.json").write_text(json.dumps(row.get("client_headers") or {}, indent=1))
    (case / "index_row.json").write_text(json.dumps(row, indent=1))
    end_ts = float(row["ts"]) + (row.get("duration_ms") or 0) / 1000 + 5
    cut = cut_session(Path(sel["jsonl_path"]), case / "session.jsonl", end_ts)
    wire = json.loads(gzip.decompress((case / "last_request.json.gz").read_bytes()))
    meta = {
        "case_id": sel["case_id"],
        "format": "obs-22 C2 v1",
        "tags": sorted(set(sel.get("tags") or [])),
        "profile": sel.get("profile", "obs"),
        "session_id": sel["session_id"],
        "req_id": req_id,
        "day": day,
        "model": row.get("model"),
        "route": row.get("route"),
        "obs_origin": row.get("obs_origin"),
        "anthropic_beta": (row.get("client_headers") or {}).get("anthropic-beta"),
        "conversation_key": conversation_key(wire),
        "wire_sha256_unredacted": row.get("wire_sha256"),
        "logged_norm": row.get("norm"),
        "logged_messages": len(wire.get("messages") or []),
        "logged_tools": [t.get("name") for t in wire.get("tools") or []],
        "session_cut": cut,
        "note": sel.get("note", ""),
        "source": {"index": str(src_dir / "index.jsonl"), "wire": str(wire_src),
                   "pre": str(pre_src) if pre_src.exists() else None,
                   "jsonl": sel["jsonl_path"]},
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    meta["sha256"] = {p.name: _sha(p) for p in sorted(case.iterdir()) if p.name != "meta.json"}
    (case / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("selection")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    built = []
    for line in Path(args.selection).read_text().splitlines():
        if not line.strip():
            continue
        sel = json.loads(line)
        try:
            m = build_case(sel, out)
            built.append({"case_id": m["case_id"], "tags": m["tags"], "dropped": m["session_cut"]["rows_dropped"]})
            print(f"built {m['case_id']}: tags={m['tags']} rows={m['session_cut']['rows_kept']} "
                  f"dropped={m['session_cut']['rows_dropped']}", flush=True)
        except Exception as e:  # report and continue
            print(f"FAILED {sel.get('case_id')}: {type(e).__name__}: {e}", flush=True)
    (out / "INDEX.json").write_text(json.dumps(built, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
