#!/usr/bin/env python3
"""Coverage table for a protocol capture made with protocol_capture.py (obs-22 E3).

Reads <capture>/raw/*/{argv.txt,stdin.ndjson,stdout.ndjson}, scenarios.json,
sdk-hook-events.json and command-hooks.jsonl, and checks every item of the
obs-22 scope's OBS<->CLI surface (scope a1-a5, a10-a12) against what was
actually observed. Prints a Markdown table and writes coverage.json. Prints
counts and type names only, never message text.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

# (id, scope ref, description, predicate over the observation dict)
REQUIRED = [
    ("flag:--output-format stream-json", "a1", "launch flag", lambda o: "--output-format=stream-json" in o["flags"]),
    ("flag:--input-format stream-json", "a1", "launch flag", lambda o: "--input-format=stream-json" in o["flags"]),
    ("flag:--verbose", "a1", "launch flag", lambda o: "--verbose" in o["flags"]),
    ("flag:--model", "a1", "launch flag", lambda o: "--model" in o["flag_names"]),
    ("flag:--permission-mode", "a1", "launch flag", lambda o: "--permission-mode" in o["flag_names"]),
    ("flag:--setting-sources", "a1", "launch flag", lambda o: "--setting-sources" in o["flag_names"]),
    ("flag:--settings", "a1", "launch flag", lambda o: "--settings" in o["flag_names"]),
    ("flag:--mcp-config", "a1", "launch flag (sdk server)", lambda o: "--mcp-config" in o["flag_names"]),
    ("flag:--resume", "a1/a10", "launch flag", lambda o: "--resume" in o["flag_names"]),
    ("out:system/init", "a3", "stream-json out", lambda o: ("out", "system", "init") in o["types"]),
    ("out:assistant/text", "a3", "stream-json out", lambda o: "text" in o["assistant_blocks"]),
    ("out:assistant/thinking", "a3", "stream-json out", lambda o: "thinking" in o["assistant_blocks"]),
    ("out:assistant/tool_use", "a3", "stream-json out", lambda o: "tool_use" in o["assistant_blocks"]),
    ("out:assistant/parallel tool_use", "a3/a6", "2+ tool_use in one message", lambda o: o["parallel_tool_use"]),
    ("out:user/tool_result", "a3", "stream-json out", lambda o: o["user_tool_result"]),
    ("out:user/tool_use_result", "a3", "stream-json out", lambda o: o["user_tool_use_result"]),
    ("out:result/success", "a3", "stream-json out", lambda o: ("out", "result", "success") in o["types"]),
    ("out:result fields", "a3", "duration_ms, num_turns, total_cost_usd, usage, session_id",
     lambda o: o["result_fields_ok"]),
    ("out:uuid on streamed messages", "a3", "_sdk_patch raw uuid", lambda o: o["uuid_on_messages"]),
    ("out:system/compact_boundary", "a11", "compaction", lambda o: ("out", "system", "compact_boundary") in o["types"]),
    ("in:control initialize", "a4", "SDK->CLI", lambda o: ("in", "control_request", "initialize") in o["types"]),
    ("in:control interrupt", "a4", "SDK->CLI", lambda o: ("in", "control_request", "interrupt") in o["types"]),
    ("in:control mcp_status", "a4", "SDK->CLI (not P0)", lambda o: ("in", "control_request", "mcp_status") in o["types"]),
    ("in:control set_model", "a4", "SDK->CLI (not P0)", lambda o: ("in", "control_request", "set_model") in o["types"]),
    ("in:control set_permission_mode", "a4", "SDK->CLI (not P0)", lambda o: ("in", "control_request", "set_permission_mode") in o["types"]),
    ("out:control hook_callback", "a4/a5", "CLI->SDK", lambda o: ("out", "control_request", "hook_callback") in o["types"]),
    ("out:control mcp_message", "a4", "CLI->SDK (in-process MCP)", lambda o: ("out", "control_request", "mcp_message") in o["types"]),
    ("out:control can_use_tool", "a4", "CLI->SDK (permission mode default)", lambda o: ("out", "control_request", "can_use_tool") in o["types"]),
    ("in:control_response success", "a4", "framing", lambda o: ("in", "control_response", "success") in o["types"]),
    ("out:control_response success", "a4", "framing", lambda o: ("out", "control_response", "success") in o["types"]),
    ("in:user message", "a3", "SDK->CLI turn input", lambda o: ("in", "user", None) in o["types"]),
] + [
    (f"hook:{ev}", "a5", "SDK callback hook", (lambda ev: lambda o: ev in o["sdk_hooks"])(ev))
    for ev in ("PreCompact", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop")
] + [
    (f"command-hook:{tag}", "a5", "project command hook", (lambda tag: lambda o: tag in o["command_hooks"])(tag))
    for tag in ("pre-bash", "pre-any", "post-any")
]

# Not reachable in print/stream-json mode, so not required of a capture:
#   Notification hook -- in -p mode 2.1.x fires it only for MCP elicitation
#   (src/cli/print.ts executeNotificationHooks; services/mcp/elicitationHandler.ts);
#   the REPL-only paths (permission prompt, idle) never run under the SDK.
NOT_REACHABLE = {
    "hook:Notification": "print mode fires Notification only for MCP elicitation "
                         "(leaked 2.1.88 src/cli/print.ts:1366, src/services/mcp/elicitationHandler.ts)",
    # OBS sets CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 for every session
    # (src/obs_agent/session.py _DEFAULT_SDK_ENV), so 2.1.59's Bash schema has
    # no run_in_background (checked in frozen production bodies 2026-10-19)
    # and these system messages are never produced for OBS agents.
    "out:system/task_notification": "background tasks disabled by OBS (_DEFAULT_SDK_ENV); "
                                    "Bash schema in production bodies has no run_in_background",
    "out:system/task_progress": "same as task_notification",
}


def observe(cap: Path) -> dict:
    tool_uses_by_message: collections.Counter = collections.Counter()
    o = {"flags": set(), "flag_names": set(), "types": collections.Counter(), "assistant_blocks": set(),
         "parallel_tool_use": False, "user_tool_result": False, "user_tool_use_result": False,
         "result_fields_ok": False, "uuid_on_messages": False, "bad_json": 0}
    for inv in sorted((cap / "raw").iterdir()):
        argv = (inv / "argv.txt").read_text().splitlines() if (inv / "argv.txt").exists() else []
        for i, a in enumerate(argv):
            if a.startswith("--"):
                o["flag_names"].add(a)
                if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    o["flags"].add(f"{a}={argv[i + 1]}" if a in ("--output-format", "--input-format") else a)
                else:
                    o["flags"].add(a)
        for fname, d in (("stdin.ndjson", "in"), ("stdout.ndjson", "out")):
            f = inv / fname
            if not f.exists():
                continue
            for line in f.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    m = json.loads(line)
                except ValueError:
                    o["bad_json"] += 1
                    continue
                t = m.get("type")
                st = m.get("subtype") or (m.get("request") or {}).get("subtype") or (m.get("response") or {}).get("subtype")
                o["types"][(d, t, st if t != "user" else None)] += 1
                if d == "out" and t == "assistant":
                    blocks = (m.get("message") or {}).get("content") or []
                    kinds = [b.get("type") for b in blocks if isinstance(b, dict)]
                    o["assistant_blocks"] |= set(kinds)
                    # stream-json emits one line per content block; a parallel
                    # round is 2+ tool_use blocks under one message id
                    mid = (m.get("message") or {}).get("id")
                    tool_uses_by_message[(inv.name, mid)] += kinds.count("tool_use")
                    if tool_uses_by_message[(inv.name, mid)] >= 2:
                        o["parallel_tool_use"] = True
                    if m.get("uuid"):
                        o["uuid_on_messages"] = True
                if d == "out" and t == "user":
                    blocks = (m.get("message") or {}).get("content") or []
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                        o["user_tool_result"] = True
                    if "tool_use_result" in m:
                        o["user_tool_use_result"] = True
                if d == "out" and t == "result":
                    need = {"duration_ms", "num_turns", "total_cost_usd", "usage", "session_id", "is_error"}
                    if need <= set(m):
                        o["result_fields_ok"] = True
    hooks = json.loads((cap / "sdk-hook-events.json").read_text()) if (cap / "sdk-hook-events.json").exists() else []
    o["sdk_hooks"] = collections.Counter(h["event"] for h in hooks)
    o["command_hooks"] = collections.Counter()
    ch = cap / "command-hooks.jsonl"
    if ch.exists():
        for line in ch.read_text().splitlines():
            try:
                o["command_hooks"][(json.loads(line).get("argv") or ["?"])[0]] += 1
            except ValueError:
                pass
    return o


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capture")
    args = ap.parse_args(argv)
    cap = Path(args.capture)
    o = observe(cap)
    rows, missing = [], []
    for rid, ref, desc, pred in REQUIRED:
        ok = bool(pred(o))
        rows.append({"item": rid, "scope": ref, "what": desc, "observed": ok})
        if not ok:
            missing.append(rid)
    extra = sorted({f"{d}:{t}/{s}" for (d, t, s) in o["types"]}
                   - {f"{d}:{t}/{s}" for (d, t, s) in o["types"] if False})
    out = {"capture": str(cap), "rows": rows, "missing": missing, "not_reachable": NOT_REACHABLE,
           "observed_types": {f"{d}:{t}/{s}": n for (d, t, s), n in sorted(o["types"].items(), key=str)},
           "sdk_hooks": dict(o["sdk_hooks"]), "command_hooks": dict(o["command_hooks"]),
           "bad_json_lines": o["bad_json"], "all_types_seen": extra}
    (cap / "coverage.json").write_text(json.dumps(out, indent=1))
    print("| item | scope | what | observed |\n|---|---|---|---|")
    for r in rows:
        print(f"| {r['item']} | {r['scope']} | {r['what']} | {'yes' if r['observed'] else '**NO**'} |")
    for k, why in NOT_REACHABLE.items():
        print(f"| {k} | a5 | not reachable under the SDK: {why} | n/a |")
    print(f"\nmissing: {missing or 'none'}")
    print("all message types seen:", ", ".join(f"{k}={v}" for k, v in out["observed_types"].items()))
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
