"""Quick test: TS fork from various message types in a compacted session.
Must use the original model (Opus) — Haiku fails with exit code 1.

Usage: .venv/bin/python spikes/quick_ts_fork_types.py
       cat /tmp/quick_ts_fork_types.log
"""
import json, os, subprocess, sys
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects" / "-Users-breedoon-Documents-obs"
SOURCE = "5f13b535-3e9a-4f3a-acf1-c18edc70ebc2"
MODEL = "claude-opus-4-6"
CWD = "/Users/breedoon/Documents/obs"
LOG = Path("/tmp/quick_ts_fork_types.log")
lines = []


def log(s=""):
    lines.append(s)
    sys.stderr.write(s + "\n")


entries = [json.loads(l) for l in (PROJECTS_DIR / f"{SOURCE}.jsonl").read_text().splitlines() if l.strip()]

candidates = {
    "pre_asst_text": 380,
    "pre_asst_tool_use": 381,
    "pre_user_tool_result": 382,
    "pre_asst_thinking": 386,
    "compaction_system": 393,
    "compaction_summary": 394,
    "post_asst_text": 396,
    "post_asst_tool_use": 399,
    "post_user_tool_result": 400,
}
# Add last assistant
for i in range(len(entries) - 1, -1, -1):
    if entries[i].get("type") == "assistant":
        candidates["last_assistant"] = i
        break

log(f"Testing TS fork from {len(candidates)} message types (model={MODEL})")
log(f"{'name':<25} {'idx':<5} {'type':<10} {'block':<12} {'status':<6} {'fork_entries':<13} {'error'}")
log("-" * 100)

for name, idx in candidates.items():
    e = entries[idx]
    uid = e["uuid"]
    etype = e.get("type", "?")
    content = e.get("message", {}).get("content", [])
    block_type = content[0].get("type", "") if isinstance(content, list) and content else ""

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    r = subprocess.run(
        ["node", "ts_fork_message_types.mjs", SOURCE, uid, MODEL, CWD],
        capture_output=True, text=True, timeout=120,
        cwd=CWD + "/spikes", env=env,
    )

    result = {}
    for line in r.stdout.strip().splitlines():
        if line.strip().startswith("{"):
            try:
                result = json.loads(line.strip())
                break
            except:
                pass

    fork_sid = result.get("forkSessionId")
    fork_n = 0
    if fork_sid:
        fp = PROJECTS_DIR / f"{fork_sid}.jsonl"
        if fp.exists():
            fork_n = sum(1 for l in fp.read_text().splitlines() if l.strip())

    err = result.get("error", "") or ""
    status = "OK" if fork_sid and fork_n > 0 else ("EMPTY" if fork_sid else "FAIL")
    log(f"{name:<25} {idx:<5} {etype:<10} {block_type:<12} {status:<6} {fork_n:<13} {err[:40]}")

    # If OK, check if fork has pre-compaction entries
    if fork_n > 0:
        fork_entries = [json.loads(l) for l in (PROJECTS_DIR / f"{fork_sid}.jsonl").read_text().splitlines() if l.strip()]
        # Find compaction breaks
        compactions = [i for i, fe in enumerate(fork_entries)
                       if fe.get("type") == "system" and fe.get("uuid") and not fe.get("parentUuid")]
        pre_compaction_uuids = {entries[i].get("uuid") for i in range(393) if entries[i].get("uuid")}
        fork_uuids = {fe.get("uuid") for fe in fork_entries if fe.get("uuid")}
        pre_in_fork = pre_compaction_uuids & fork_uuids
        log(f"  → {fork_n} entries, compaction_breaks={compactions}, pre_compaction_in_fork={len(pre_in_fork)}")

log()
LOG.write_text("\n".join(lines))
sys.stderr.write(f"\nDone. Results in {LOG}\n")
