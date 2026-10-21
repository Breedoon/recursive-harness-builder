#!/usr/bin/env python3
"""Derive ``expected_manifest.json`` for byte-resume fixtures (obs-22 E3, design-v2 §10.3).

The manifest schema ``obs-cli-manifest/1`` is E5's; this writes, per case, the
manifest the OBS-owned binary should bootstrap from the fixture's last logged
wire body (bootstrap source "log", design-v2 §1.6.3), so the checker can compare
it with the binary's sidecar under the §10.3 equality rules.

Every field gets a provenance in ``meta.json`` -> ``expected_manifest``:
  derived          read off the logged bytes / index row
  rule             computed by a design-v2 rule from logged facts (cited)
  profile-default  the production profile value design-v2 §10.3 shows; not
                   observable in the log
  unknown          no source; the field is written as null and E5 must settle it
A case is skipped (no file, reason recorded) when its logged system or tools
hold a redaction placeholder (design-v2 §1.6.3 rejects such bodies) or its
system shape matches none of the modes a-d. A missing manifest never excuses a
byte mismatch: the byte comparison stays the gate.

Usage: derive_manifest.py [--fixtures DIR] [--case ID ...]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

FIXTURES = Path("/workspace/runtime/e2e/fixtures/byte-resume")
TTL_SECONDS = {"1h": 3600, "5m": 360}  # design-v2 §1.6.3 warmth: 1h, else 360 s
SESSION_RE = re.compile(r"_session_[0-9a-fA-F-]{36}$")


def pyjson(o) -> str:
    # INVARIANT(cache-bytes): tools_json is the proxy's serialization of the
    # tools array (json.dumps, compact separators, ensure_ascii), which is also
    # exactly the substring of the logged wire bytes. Any other dump (spaces,
    # ensure_ascii=False, sort_keys) breaks byte equality with the binary.
    # Doc: vault:Projects/Personal Projects/OBS Platform/CLI Runtime/Components/Fixtures and Byte Checker#Expected manifest
    return json.dumps(o, separators=(",", ":"))


def system_mode(system) -> str | None:
    """design-v2 §1.5.2 rule 1, read back from the wire system blocks."""
    if not isinstance(system, list):
        return None
    cc = [b.get("cache_control") for b in system]
    if len(system) == 2:
        return "d"
    if not any(cc):
        return "c"
    if len(system) == 3 and cc[1] and cc[2]:
        return "a"
    if len(system) == 3 and not cc[1] and cc[2] and cc[2].get("scope") == "global":
        return "b"
    return None


def derive(case: Path) -> tuple[dict | None, dict]:
    meta = json.loads((case / "meta.json").read_text())
    raw = gzip.decompress((case / "last_request.json.gz").read_bytes())
    wire = json.loads(raw)
    row = json.loads((case / "index_row.json").read_text())
    prov: dict = {}
    if b"[REDACTED:" in pyjson(wire.get("system")).encode() or b"[REDACTED:" in pyjson(wire.get("tools")).encode():
        return None, {"skipped": "redaction placeholder in logged system/tools (design-v2 §1.6.3)"}
    mode = system_mode(wire.get("system"))
    if mode is None:
        return None, {"skipped": "system blocks match none of the modes a-d"}
    names = [t.get("name", "") for t in wire.get("tools") or []]
    servers: list[str] = []
    for n in names:
        if n.startswith("mcp__"):
            s = n.split("__")[1]
            if s not in servers:
                servers.append(s)
    ttls = [((b.get("cache_control") or {}).get("ttl") or "5m") for b in wire.get("system") or []
            if b.get("cache_control")]
    ttl = "1h" if "1h" in ttls else "5m"
    pre_path = case / "pre_request.json.gz"
    uid = None
    if pre_path.exists():
        uid = ((json.loads(gzip.decompress(pre_path.read_bytes())).get("metadata") or {}).get("user_id"))
    template = SESSION_RE.sub("_session_{session}", uid) if isinstance(uid, str) and SESSION_RE.search(uid) else None
    betas = [b for b in (row.get("upstream_beta") or "").split(",") if b]
    tools_json = pyjson(wire.get("tools") or [])
    assert tools_json.encode() in raw, "tools_json is not a substring of the logged wire bytes"
    now = (meta.get("bootstrap_inputs") or {}).get("now") or float(row["ts"]) + 1
    route = meta.get("route")
    profile = meta.get("profile")
    cred = "shared-claude" if (route == "anthropic" and profile == "obs") else None
    man = {
        "schema": "obs-cli-manifest/1",
        "session_id": meta["session_id"],
        "created_at": None,
        "created_by": None,
        "builder_epoch": 1,
        "route": route,
        "model": wire.get("model"),
        "system_mode": mode,
        "system": wire.get("system"),
        "tools_json": tools_json,
        "tools_sha256": hashlib.sha256(tools_json.encode()).hexdigest(),
        "tool_selector": {
            "enable_tasks": "TaskCreate" in names and "TodoWrite" not in names,
            "agent_teams": any(n in names for n in ("TeamCreate", "TeamDelete", "SendMessage")),
            "mcp_servers": servers,
            "resource_tools": "ListMcpResourcesTool" in names,
            "deny": [],
            "allow": ["Bash", "Read"] if profile == "harness" else [],
        },
        "key_order": list(wire.keys()),
        "params": {"max_tokens": wire.get("max_tokens"), "thinking": wire.get("thinking"),
                   "temperature": wire.get("temperature"), "context_management": wire.get("context_management"),
                   "output_config": wire.get("output_config")},
        "ttl": ttl,
        "betas": betas,
        "metadata_user_id_template": template,
        "flags": {"tengu_system_prompt_global_cache": mode == "b",
                  "tengu_prompt_cache_1h_allowlist": ["sdk"],
                  "tengu_marble_anvil": "context_management" in wire,
                  "tengu_oak_drum": False},
        "profiles": {"credential": cred, "transport": "cc-2.1.59", "content": "none",
                     "content_sha256": hashlib.sha256(b"").hexdigest()},
        "bootstrap": {"source": "log", "verified": True, "req_id": meta["req_id"],
                      "last_logged_ts": float(row["ts"]), "warm": (now - float(row["ts"])) < TTL_SECONDS[ttl],
                      "reason": ""},
    }
    prov = {
        "derived": ["session_id", "route", "model", "system", "tools_json", "tools_sha256", "key_order", "params",
                    "betas (index upstream_beta: what went upstream)", "metadata_user_id_template (pre body)",
                    "bootstrap.req_id", "bootstrap.last_logged_ts"],
        "rule": ["system_mode (§1.5.2 rule 1, read back from the blocks)", "ttl (system breakpoint ttl)",
                 "tool_selector.* (from the tool names; mcp_servers in first-appearance order of the name-sorted "
                 "list, NOT the connection order §1.10 means)",
                 "flags.tengu_system_prompt_global_cache (= mode b)", "flags.tengu_marble_anvil (= context_management present)",
                 "bootstrap.warm (now = logged ts + 1 s, §6.1)", "bootstrap.verified (fixture is the logged row itself)"],
        "profile-default": ["builder_epoch 1", "flags.tengu_prompt_cache_1h_allowlist", "flags.tengu_oak_drum",
                            "profiles.transport", "profiles.content"],
        "unknown": ["profiles.content_sha256 (no rule-set encoding is defined; sha256 of empty bytes written)"]
                   + ([] if cred else ["profiles.credential (no C5 profile id for this route/profile; null)"])
                   + ([] if template else ["metadata_user_id_template (no pre body)"]),
        "excluded_from_equality": ["created_at", "created_by", "bootstrap.reason"],
    }
    return man, prov


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", default=str(FIXTURES))
    ap.add_argument("--case", action="append")
    a = ap.parse_args(argv)
    root = Path(a.fixtures)
    n_ok = n_skip = 0
    for case in sorted(p for p in root.iterdir() if (p / "meta.json").exists()):
        if a.case and case.name not in a.case:
            continue
        man, prov = derive(case)
        target = case / "expected_manifest.json"
        if man is None:
            target.unlink(missing_ok=True)
            n_skip += 1
        else:
            target.write_text(json.dumps(man, indent=1))
            n_ok += 1
        meta = json.loads((case / "meta.json").read_text())
        meta["expected_manifest"] = prov
        meta["sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(case.iterdir()) if p.name != "meta.json" and p.is_file()}
        (case / "meta.json").write_text(json.dumps(meta, indent=1))
        print(f"{'wrote' if man else 'SKIP '} {case.name} {prov.get('skipped', man and man['system_mode'])}")
    print(f"expected manifests: {n_ok} written, {n_skip} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
