#!/usr/bin/env python3
"""Find exactly where two Anthropic /v1/messages requests stop sharing a prefix.

Built for the cache proxy's durable request log (src/cache_proxy.py,
RequestLogger; default dir /workspace/runtime/logs/cache-proxy/requests).
Mission cache-proxy-prefix-fix, 2026-10-08, bead vault-mief.2.

Prompt-cache prefix order is tools -> system -> messages, and request-level
settings (model, thinking, output_config/effort, context_management, betas)
can invalidate it too. This tool compares those components in that order,
ignoring only ``cache_control`` (a breakpoint hint, not part of the cached
bytes), and reports the FIRST divergence: component, path, byte offset inside
the canonical JSON of the differing element, and snippets of both sides.

Classification of B relative to A:
  identical         same tools/system/config/messages
  append            A's messages are an exact prefix of B's (the healthy case)
  config-changed    a request-level setting differs (model/thinking/...)
  tools-changed     tools differ (whole cache invalidated)
  system-changed    system blocks differ
  history-changed   an EARLIER message (index < len(A.messages)) differs
  thinking-changed  ...and that message's thinking-block count differs
                    (blocks dropped/added — compare thinking config too)
  shrunk            B has fewer messages than A and they agree up to len(B)

It also lists cache_control breakpoints of both requests and says whether
B keeps a message breakpoint at or after A's last breakpoint (a request with
no message breakpoint can only read the system/tools cache).

Usage:
  prefix_diff.py A B                     # files (.json/.json.gz) or req_ids
  prefix_diff.py --session SID           # every adjacent pair of one session
                                         #   (same model; count_tokens skipped)
  prefix_diff.py --prev-any REQ_ID       # REQ vs the earlier logged request
                                         #   (any session, within --window min)
                                         #   sharing the longest message prefix
                                         #   — for recoveries / new session ids
Use --part pre to compare what the CLI itself sent (catches harness-injected
content the proxy strips only sometimes), --part wire for what Anthropic saw.
Files are secret-redacted with hashed placeholders; equality is preserved.
Options: --part pre|post|wire (default wire), --log-dir DIR, --json,
         --since/--until ISO (for --session), --window MINUTES (default 90)
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys

DEFAULT_LOG_DIR = os.environ.get(
    "CACHE_PROXY_REQUEST_LOG_DIR", "/workspace/runtime/logs/cache-proxy/requests"
)
CONFIG_KEYS = ("model", "thinking", "output_config", "context_management",
               "tool_choice", "temperature", "top_p", "top_k")


# ── loading ──────────────────────────────────────────────────────────────

def _read_bytes(path: str) -> bytes:
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            return f.read()
    with open(path, "rb") as f:
        return f.read()


def find_request_file(req_id: str, part: str, log_dir: str) -> str:
    day = f"{req_id[0:4]}-{req_id[4:6]}-{req_id[6:8]}"
    cand = os.path.join(log_dir, day, f"{req_id}.{part}.json.gz")
    if os.path.exists(cand):
        return cand
    if part == "post":  # post is stored only when it differs from wire
        wire = os.path.join(log_dir, day, f"{req_id}.wire.json.gz")
        if os.path.exists(wire):
            return wire
    hits = glob.glob(os.path.join(log_dir, "*", f"{req_id}*.{part}.json.gz"))
    if len(hits) == 1:
        return hits[0]
    raise FileNotFoundError(f"no unique {part} body for {req_id!r} under {log_dir}")


def load_request(ref: str, part: str = "wire", log_dir: str = DEFAULT_LOG_DIR) -> dict:
    path = ref if os.path.exists(ref) else find_request_file(ref, part, log_dir)
    return json.loads(_read_bytes(path))


def read_index(log_dir: str) -> list[dict]:
    rows = []
    for idx in sorted(glob.glob(os.path.join(log_dir, "*", "index.jsonl"))):
        with open(idx) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    rows.sort(key=lambda r: (r.get("ts", 0), r.get("req_id", "")))
    return rows


# ── canonicalization ─────────────────────────────────────────────────────

def strip_cache_control(obj):
    if isinstance(obj, dict):
        return {k: strip_cache_control(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [strip_cache_control(v) for v in obj]
    return obj


def canon(obj) -> str:
    return json.dumps(strip_cache_control(obj), separators=(",", ":"), ensure_ascii=False)


def _as_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else [content]


def thinking_counts(req: dict) -> list[int]:
    """Number of thinking/redacted_thinking blocks per message."""
    return [sum(1 for b in _as_blocks(m.get("content"))
                if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
            for m in req.get("messages") or []]


def breakpoints(req: dict) -> list[str]:
    out = []
    for i, t in enumerate(req.get("tools") or []):
        if isinstance(t, dict) and "cache_control" in t:
            out.append(f"tools[{i}]")
    sysv = req.get("system")
    if isinstance(sysv, list):
        for i, b in enumerate(sysv):
            if isinstance(b, dict) and "cache_control" in b:
                out.append(f"system[{i}]")
    for mi, m in enumerate(req.get("messages") or []):
        for bi, b in enumerate(_as_blocks(m.get("content"))):
            if isinstance(b, dict) and "cache_control" in b:
                out.append(f"messages[{mi}].content[{bi}]")
    return out


def _last_message_bp(bps: list[str]) -> int | None:
    idx = [int(b.split("[")[1].split("]")[0]) for b in bps if b.startswith("messages[")]
    return max(idx) if idx else None


# ── diffing ──────────────────────────────────────────────────────────────

def _first_byte_diff(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def _snip(s: str, off: int, width: int = 160) -> str:
    lo = max(0, off - width // 2)
    return ("…" if lo else "") + s[lo:off + width // 2] + ("…" if off + width // 2 < len(s) else "")


def _divergence(component: str, path: str, ca: str, cb: str) -> dict:
    off = _first_byte_diff(ca, cb)
    return {"component": component, "path": path, "byte_offset": off,
            "a_len": len(ca), "b_len": len(cb),
            "a_snippet": _snip(ca, off), "b_snippet": _snip(cb, off)}


def _diff_list(component: str, la: list, lb: list):
    for i in range(min(len(la), len(lb))):
        ca, cb = canon(la[i]), canon(lb[i])
        if ca != cb:
            return _divergence(component, f"{component}[{i}]", ca, cb)
    if len(la) != len(lb):
        return {"component": component, "path": f"{component}[len]",
                "byte_offset": None, "a_len": len(la), "b_len": len(lb),
                "a_snippet": f"{len(la)} items", "b_snippet": f"{len(lb)} items"}
    return None


def diff_requests(a: dict, b: dict, a_beta: str | None = None, b_beta: str | None = None) -> dict:
    """Compare two request bodies in cache-prefix order. Pure function."""
    result = {"classification": None, "first_divergence": None,
              "config_diffs": [], "a_messages": len(a.get("messages") or []),
              "b_messages": len(b.get("messages") or []),
              "a_breakpoints": breakpoints(a), "b_breakpoints": breakpoints(b)}

    for k in CONFIG_KEYS:
        if canon(a.get(k)) != canon(b.get(k)):
            result["config_diffs"].append({"key": k, "a": a.get(k), "b": b.get(k)})
    if (a_beta or "") != (b_beta or "") and (a_beta is not None or b_beta is not None):
        result["config_diffs"].append({"key": "anthropic-beta", "a": a_beta, "b": b_beta})

    tools_div = _diff_list("tools", a.get("tools") or [], b.get("tools") or [])
    sys_a, sys_b = a.get("system"), b.get("system")
    if isinstance(sys_a, str):
        sys_a = [{"type": "text", "text": sys_a}]
    if isinstance(sys_b, str):
        sys_b = [{"type": "text", "text": sys_b}]
    sys_div = _diff_list("system", sys_a or [], sys_b or [])

    ma, mb = a.get("messages") or [], b.get("messages") or []
    msg_div = None
    tail_block_append = False
    for i in range(min(len(ma), len(mb))):
        ca, cb = canon(ma[i]), canon(mb[i])
        if ca != cb:
            ba, bb = _as_blocks(ma[i].get("content")), _as_blocks(mb[i].get("content"))
            # The CLI merges an appended continuation into a trailing user tool-
            # result message. Distinguish exact old-block prefix from mutation.
            if (i == len(ma) - 1 and len(mb) >= len(ma)
                    and ma[i].get("role") == "user" and len(bb) > len(ba)
                    and canon({k: v for k, v in ma[i].items() if k != "content"})
                    == canon({k: v for k, v in mb[i].items() if k != "content"})
                    and all(canon(x) == canon(y) for x, y in zip(ba, bb))):
                tail_block_append = True
                continue
            msg_div = _divergence("messages", f"messages[{i}]", ca, cb)
            # Narrow to the block inside the message.
            ba, bb = _as_blocks(ma[i].get("content")), _as_blocks(mb[i].get("content"))
            if ma[i].get("role") != mb[i].get("role"):
                msg_div["path"] += ".role"
            else:
                for j in range(min(len(ba), len(bb))):
                    xa, xb = canon(ba[j]), canon(bb[j])
                    if xa != xb:
                        msg_div = _divergence("messages", f"messages[{i}].content[{j}]", xa, xb)
                        break
                else:
                    msg_div["path"] += f".content[len {len(ba)}→{len(bb)}]"
            msg_div["message_index"] = i
            msg_div["a_role"] = ma[i].get("role")
            break

    ta, tb = thinking_counts(a), thinking_counts(b)
    shared = min(len(ta), len(tb))
    result["a_thinking_blocks"] = sum(ta)
    result["b_thinking_blocks"] = sum(tb[:len(ta)]) if len(tb) >= len(ta) else sum(tb)
    thinking_moved = [i for i in range(shared) if ta[i] != tb[i]]
    result["thinking_count_changed_at"] = thinking_moved[:20]
    if msg_div is not None and msg_div["message_index"] in thinking_moved:
        msg_div["thinking"] = {"a": ta[msg_div["message_index"]], "b": tb[msg_div["message_index"]]}

    if tools_div:
        result["classification"], result["first_divergence"] = "tools-changed", tools_div
    elif sys_div:
        result["classification"], result["first_divergence"] = "system-changed", sys_div
    elif msg_div:
        # Thinking blocks dropped/added in an earlier message is its own class
        # (E1 hypothesis: class-B shrinks track accumulated thinking).
        cls = "thinking-changed" if "thinking" in msg_div else "history-changed"
        result["classification"], result["first_divergence"] = cls, msg_div
    elif len(mb) < len(ma):
        result["classification"] = "shrunk"
    elif result["config_diffs"]:
        result["classification"] = "config-changed"
    elif tail_block_append:
        result["classification"] = "tail-block-append"
        result["tail_block_append"] = True
    elif len(mb) == len(ma):
        result["classification"] = "identical"
    else:
        result["classification"] = "append"
    if result["config_diffs"] and result["classification"] in ("append", "tail-block-append", "identical"):
        result["classification"] = "config-changed"

    a_bp = _last_message_bp(result["a_breakpoints"])
    b_bp = _last_message_bp(result["b_breakpoints"])
    result["a_last_message_breakpoint"] = a_bp
    result["b_last_message_breakpoint"] = b_bp
    result["b_has_message_breakpoint"] = b_bp is not None
    return result


# ── output ───────────────────────────────────────────────────────────────

def format_result(r: dict, label: str = "") -> str:
    lines = []
    head = f"{label}{r['classification']}  msgs {r['a_messages']}→{r['b_messages']}"
    head += f"  msg-breakpoint A={r['a_last_message_breakpoint']} B={r['b_last_message_breakpoint']}"
    if not r["b_has_message_breakpoint"]:
        head += "  !! B HAS NO MESSAGE BREAKPOINT (reads system/tools cache only)"
    lines.append(head)
    if r.get("thinking_count_changed_at"):
        lines.append(f"  thinking-block count differs at messages {r['thinking_count_changed_at']} "
                     f"(A total {r['a_thinking_blocks']}, B over A's span {r['b_thinking_blocks']})")
    for c in r["config_diffs"]:
        lines.append(f"  config {c['key']}: {json.dumps(c['a'])[:200]} → {json.dumps(c['b'])[:200]}")
    d = r["first_divergence"]
    if d:
        lines.append(f"  first divergence: {d['path']} @byte {d['byte_offset']} "
                     f"(len {d['a_len']}→{d['b_len']})")
        lines.append(f"    A: {d['a_snippet']!r}")
        lines.append(f"    B: {d['b_snippet']!r}")
    return "\n".join(lines)


def _beta(row: dict | None) -> str | None:
    if not row:
        return None
    return row.get("upstream_beta") or (row.get("client_headers") or {}).get("anthropic-beta")


def session_pairs(log_dir: str, session: str, part: str, since=None, until=None):
    rows = [r for r in read_index(log_dir) if (r.get("session_id") or "").startswith(session)]
    if since:
        rows = [r for r in rows if r.get("ts_iso", "") >= since]
    if until:
        rows = [r for r in rows if r.get("ts_iso", "") <= until]
    # Pair each request with the previous one of the same model and path:
    # the CLI interleaves side requests (e.g. a small model for titles/topic
    # checks, count_tokens) that share the session id but not the prefix.
    lanes: dict = {}
    pairs = []
    for r in rows:
        if "count_tokens" in (r.get("path") or ""):
            continue
        key = r.get("model")
        if key in lanes:
            pairs.append((lanes[key], r))
        lanes[key] = r
    out = []
    for ra, rb in pairs:
        try:
            a = load_request(ra["req_id"], part, log_dir)
            b = load_request(rb["req_id"], part, log_dir)
        except FileNotFoundError as e:
            out.append({"a": ra["req_id"], "b": rb["req_id"], "error": str(e)})
            continue
        res = diff_requests(a, b, _beta(ra), _beta(rb))
        res.update({"a": ra["req_id"], "b": rb["req_id"],
                    "a_ts": ra.get("ts_iso"), "b_ts": rb.get("ts_iso"),
                    "b_usage": rb.get("usage"), "b_status": rb.get("http_status")})
        out.append(res)
    return out


def prev_any(log_dir: str, req_id: str, part: str, window_min: float):
    rows = read_index(log_dir)
    row = next((r for r in rows if r["req_id"] == req_id), None)
    if row is None:
        raise SystemExit(f"{req_id} not in index")
    b = load_request(req_id, part, log_dir)
    mb = [canon(m) for m in b.get("messages") or []]
    best, best_n = None, -1
    for r in rows:
        if r["ts"] >= row["ts"] or row["ts"] - r["ts"] > window_min * 60:
            continue
        if r.get("model") != row.get("model"):
            continue
        try:
            a = load_request(r["req_id"], part, log_dir)
        except FileNotFoundError:
            continue
        ma = [canon(m) for m in a.get("messages") or []]
        n = 0
        while n < min(len(ma), len(mb)) and ma[n] == mb[n]:
            n += 1
        if n > best_n or (n == best_n and best and r["ts"] > best["ts"]):
            best, best_n = r, n
    if best is None:
        raise SystemExit("no earlier request in window")
    a = load_request(best["req_id"], part, log_dir)
    res = diff_requests(a, b, _beta(best), _beta(row))
    res.update({"a": best["req_id"], "b": req_id, "shared_messages": best_n,
                "a_session": best.get("session_id"), "b_session": row.get("session_id")})
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("refs", nargs="*")
    ap.add_argument("--session")
    ap.add_argument("--prev-any")
    ap.add_argument("--part", default="wire", choices=("pre", "post", "wire"))
    ap.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--window", type=float, default=90.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.session:
        res = session_pairs(args.log_dir, args.session, args.part, args.since, args.until)
        if args.json:
            print(json.dumps(res, indent=1, default=str))
        else:
            counts = {}
            for r in res:
                if "error" in r:
                    print(f"{r['a']} → {r['b']}: {r['error']}")
                    continue
                counts[r["classification"]] = counts.get(r["classification"], 0) + 1
                u = r.get("b_usage") or {}
                print(format_result(r, label=f"{r['b_ts']} {r['b']} cr={u.get('cache_read')} "
                                              f"cc={u.get('cache_creation')} st={r.get('b_status')} | "))
            print(f"\npairs: {len(res)}  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return 0
    if args.prev_any:
        res = prev_any(args.log_dir, args.prev_any, args.part, args.window)
        print(json.dumps(res, indent=1, default=str) if args.json else
              f"A={res['a']} (session {res['a_session']}) shared_messages={res['shared_messages']}\n"
              + format_result(res))
        return 0
    if len(args.refs) != 2:
        ap.error("give two requests, or --session, or --prev-any")
    a = load_request(args.refs[0], args.part, args.log_dir)
    b = load_request(args.refs[1], args.part, args.log_dir)
    res = diff_requests(a, b)
    print(json.dumps(res, indent=1, default=str) if args.json else format_result(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
