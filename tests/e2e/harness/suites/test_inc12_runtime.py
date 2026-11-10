"""obs-22.86 / obs-22.68 (increment 12): per-route CLI runtime selection, rollback and the orphan reaper.

Needs ``obs-e2e run --canary-cli <obs-cli binary>``. The lane's own canary file
(``state/obs-cli-canary.json``, the lane's ``/workspace/runtime/state``) names the lane agent. Evidence is
read from the lane log's ``CLI runtime`` lines (OBS's production log), the runtime record
(``state/obs-cli-runtime.sqlite3``), and the CLI processes' argv[0] in the lane. Proof:
``tests/<lane>/inc12/runtime.json``.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid

import pytest

pytestmark = [pytest.mark.suite("inc12"), pytest.mark.needs(all_lanes=False, credential=("dev", "sub"))]

BUNDLED_MARK = "claude_agent_sdk/_bundled/claude"
RUNTIME_RE = re.compile(r"CLI runtime runtime=(\S+) reason=(.*?) model=(\S+) resume=(\S+) agent=(\S+) team=(\S+)")


class _Probe:
    def __init__(self, lane):
        self.lane = lane
        self.state = lane.lane_dir / "state"
        self.file = self.state / "obs-cli-canary.json"
        self.db = self.state / "obs-cli-runtime.sqlite3"
        self.log = lane.lane_dir / "logs" / "telegram-prod.log"
        self.mark = self.log.stat().st_size if self.log.exists() else 0
        self.canary = lane.manifest["canary_cli"]

    def new_window(self):
        self.mark = self.log.stat().st_size if self.log.exists() else 0

    def runtime_lines(self):
        if not self.log.exists():
            return []
        with self.log.open("rb") as handle:
            handle.seek(self.mark)
            text = handle.read().decode("utf-8", "replace")
        return [m.groups() for m in map(RUNTIME_RE.search, text.splitlines()) if m]

    def log_text(self):
        with self.log.open("rb") as handle:
            handle.seek(self.mark)
            return handle.read().decode("utf-8", "replace")

    def write(self, agents, **extra):
        self.file.write_text(json.dumps({"cli_path": self.canary, "agents": agents, "teams": [],
                                         "new_sessions_only": True, **extra}))

    def record(self, session_id):
        if not self.db.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT runtime, cli_path FROM session_runtime WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        return row

    def final_runtime(self):
        """The persisted hash of the canary binary (the first build may show obs-cli:<basename> briefly)."""
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT runtime FROM binary_hash WHERE path=?", (self.canary,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    async def clis(self):
        rows = await self.lane.procs()
        return [r for r in rows if r["args"] and "--output-format" in r["args"]
                and (r["args"][0] == self.canary or BUNDLED_MARK in r["args"][0])]

    async def argv0s(self):
        return sorted({r["args"][0] for r in await self.clis()})


async def test_canary_selection_resume_rollback_and_reaper(lane):
    if not lane.manifest.get("canary_cli"):
        pytest.skip("run with --canary-cli <obs-cli binary>")
    p = _Probe(lane)
    proof: dict = {"canary_cli": p.canary, "canary_cli_sha256": lane.manifest.get("canary_cli_sha256")}
    try:
        await _scenario(lane, p, proof)
    finally:
        target = lane.run_dir / "tests" / lane.name / "inc12"
        target.mkdir(parents=True, exist_ok=True)
        (target / "runtime.json").write_text(json.dumps(proof, indent=2, default=str) + "\n")


async def _scenario(lane, p, proof):
    topic = await lane.new_topic("inc12 runtime")
    code = uuid.uuid4().hex[:8].upper()

    # A. No canary file: production behaviour, nothing recorded, no INFO line.
    assert not p.file.exists()
    first = await lane.turn(topic, f"Remember the codeword {code}. Reply with exactly: A-{code}")
    assert f"A-{code}" in first.text, first.messages
    agent = first.end["agent_name"]
    proof["agent"] = agent
    proof["A"] = {"runtime_lines": p.runtime_lines(), "db_exists": p.db.exists(), "argv0": await p.argv0s()}
    assert p.runtime_lines() == [] and not p.db.exists()
    assert all(BUNDLED_MARK in a for a in proof["A"]["argv0"]) and proof["A"]["argv0"]

    # B. The agent is selected; a fresh session (/clear) spawns obs-cli and is recorded.
    p.write([agent])
    p.new_window()
    await lane.command(topic, "/clear")
    second = await lane.turn(topic, f"Remember the codeword {code}2. Reply with exactly: B-{code}")
    assert f"B-{code}" in second.text, second.messages
    sid = second.end["session_id"]
    lines = p.runtime_lines()
    proof["B"] = {"runtime_lines": lines, "record": p.record(sid), "argv0": await p.argv0s(), "session_id": sid}
    assert lines and lines[-1][0].startswith("obs-cli:") and lines[-1][1] == "selected", lines
    assert proof["B"]["argv0"] == [p.canary], proof["B"]
    runtime_id = p.final_runtime()
    assert runtime_id and runtime_id.startswith("obs-cli:") and lines[-1][0] in (runtime_id, f"obs-cli:{p.canary.rsplit('/', 1)[-1]}"), (lines, runtime_id)
    assert p.record(sid) == (runtime_id, p.canary)

    # B3. A Telegram fork continues the conversation on the parent's binary (the fork inherits the record),
    # and /stop works on obs-cli. The child is named by team here; the file names the parent agent only otherwise.
    team = second.end["team"]
    p.write([agent], teams=[team])
    p.new_window()
    mark = lane.events.mark()
    await lane.command(topic, "/fork")
    child_send = await lane.wait("telegram_send", match=lambda e: e.get("chat_id") == topic.chat_id and e.get("topic_id") not in (None, topic.topic_id), after=mark)
    child_answer = await lane.turn(int(child_send["topic_id"]), f"What codeword with suffix 2 did I give you earlier? Reply with only the codeword.")
    fork_lines = [l for l in p.runtime_lines() if l[3] != "None"]
    proof["B3_fork"] = {"runtime_lines": p.runtime_lines(), "child_session_id": child_answer.end["session_id"], "text": child_answer.text[:80]}
    assert f"{code}2" in child_answer.text, child_answer.messages
    assert child_answer.end["session_id"] != sid
    assert fork_lines and fork_lines[-1][0] == runtime_id and fork_lines[-1][1] == "resume stays on the recorded obs-cli", fork_lines
    p.write([agent])
    p.new_window()
    sent = await lane.send(topic, "Write every integer from 1 through 8000, one number per line, with no other text.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    await lane.command(topic, "/stop")
    end, cleanup = await asyncio.gather(
        lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start),
        lane.wait("stop_cleanup", agent=topic, after=start),
    )
    assert cleanup.get("survivors") == 0, cleanup
    after_stop = await lane.turn(topic, f"Reply with exactly: S-{code}")
    assert f"S-{code}" in after_stop.text and after_stop.end["session_id"] == sid, after_stop.messages
    proof["B3_stop"] = {"cleanup": {k: cleanup.get(k) for k in ("owned", "survivors", "outcome")}, "argv0": await p.argv0s()}
    assert proof["B3_stop"]["argv0"] == [p.canary]

    # B2. Resume after a restart stays on the recorded obs-cli, same session, context intact.
    p.new_window()
    await lane.restart_obs("maintenance")
    third = await lane.turn(topic, f"What was the codeword I gave you, with the suffix 2? Reply with exactly: C-<codeword>")
    proof["B2"] = {"runtime_lines": p.runtime_lines(), "argv0": await p.argv0s(), "session_id": third.end["session_id"], "text": third.text[:80]}
    assert third.end["session_id"] == sid
    assert f"{code}2" in third.text, third.messages
    assert p.runtime_lines() and p.runtime_lines()[-1][0] == runtime_id and "resume" in p.runtime_lines()[-1][1]
    assert proof["B2"]["argv0"] == [p.canary]

    # C. Rollback: the agent leaves the file. The next spawn (resume after a restart) is bundled,
    # the conversation keeps its context, and the record follows.
    p.write([])
    p.new_window()
    await lane.restart_obs("maintenance")
    fourth = await lane.turn(topic, "What was the codeword I gave you, with the suffix 2? Reply with exactly: D-<codeword>")
    proof["C_resume"] = {"runtime_lines": p.runtime_lines(), "argv0": await p.argv0s(),
                         "session_id": fourth.end["session_id"], "record": p.record(sid), "text": fourth.text[:80]}
    assert fourth.end["session_id"] == sid and f"{code}2" in fourth.text, fourth.messages
    assert p.runtime_lines()[-1][0].startswith("bundled-") and p.runtime_lines()[-1][1] == "not selected"
    assert all(BUNDLED_MARK in a for a in proof["C_resume"]["argv0"]) and proof["C_resume"]["argv0"]
    assert p.record(sid)[0].startswith("bundled-")

    # C2. A fresh session after the rollback is bundled too.
    p.new_window()
    await lane.command(topic, "/clear")
    fifth = await lane.turn(topic, f"Reply with exactly: E-{code}")
    proof["C_fresh"] = {"runtime_lines": p.runtime_lines(), "argv0": await p.argv0s(), "session_id": fifth.end["session_id"]}
    assert p.runtime_lines()[-1][0].startswith("bundled-")
    assert all(BUNDLED_MARK in a for a in proof["C_fresh"]["argv0"]) and proof["C_fresh"]["argv0"]

    # D. Orphan reaper. A real obs-cli dies with its daemon here (supervisord signals the group), so
    # orphans are reproduced the way production meets them: processes whose argv[0] is a CLI path, whose
    # parent is gone and whose process-group leader is gone. The canary file is removed first (full
    # rollback), so a restarted OBS can only learn the canary path from its persisted record.
    p.file.unlink()
    bundled = f"/workspace/obs/.venv/lib/python3.12/site-packages/{BUNDLED_MARK}"
    never_named = "/workspace/runtime/e2e/obs-cli-never-named-by-any-canary"

    async def spawn(argv0, *, orphan):
        if orphan:  # the group leader exits at once, leaving a parentless child in a leaderless group
            await lane.exec("setsid", "bash", "-c", f"(exec -a {argv0} sleep 900) </dev/null >/dev/null 2>&1 & exit 0", timeout=20)
        else:  # the leader stays as the live parent: another OBS instance's CLI
            await lane._ctl.call("POST", "/exec", {"argv": ["setsid", "bash", "-c", f"(exec -a {argv0} sleep 900) & wait"], "background": True})

    await spawn(p.canary, orphan=True)       # reaped: canary path, parent and leader gone
    await spawn(bundled, orphan=True)        # reaped: the existing rule
    await spawn(never_named, orphan=True)    # kept: not an agent CLI path
    await spawn(p.canary, orphan=False)      # kept: a live CLI of another OBS keeps its parent and leader

    def find(rows, argv0):
        return [r for r in rows if r["args"] and r["args"][0] == argv0]

    await lane.exec("sh", "-c", "sleep 2", timeout=10)  # the background spawns need a moment to exec
    rows = await lane.procs()
    canary_rows = find(rows, p.canary)
    orphans = {"canary": [r["pid"] for r in canary_rows if r["ppid"] <= 1 or r["ppid"] not in {x["pid"] for x in rows}],
               "canary_live": [r["pid"] for r in canary_rows if r["ppid"] > 1 and r["ppid"] in {x["pid"] for x in rows}],
               "bundled": [r["pid"] for r in find(rows, bundled) if r["ppid"] <= 1], "never_named": [r["pid"] for r in find(rows, never_named)]}
    proof["D_before"] = orphans
    assert orphans["canary"] and orphans["canary_live"] and orphans["bundled"] and orphans["never_named"], (orphans, canary_rows)
    p.new_window()
    await lane.restart_obs("plain")
    for pid in orphans["canary"] + orphans["bundled"]:  # the reaper's SIGKILL follows its SIGTERM after a short grace
        gone = await lane.exec("sh", "-c", f"n=0; while kill -0 {pid} 2>/dev/null && [ $n -lt 120 ]; do sleep 0.5; n=$((n+1)); done; kill -0 {pid} 2>/dev/null && echo ALIVE || echo GONE", timeout=90)
        proof.setdefault("D_wait", {})[str(pid)] = gone["stdout"].strip()
    rows = await lane.procs()
    alive = {pid for pid in orphans["canary_live"] + orphans["never_named"] if any(r["pid"] == pid for r in rows)}
    proof["D_after"] = {"live_controls_alive": sorted(alive), "reaper_lines": [l.split("] ", 1)[-1][:200] for l in p.log_text().splitlines() if "orphan_reaper" in l]}
    for pid in orphans["canary_live"] + orphans["never_named"]:
        await lane.exec("sh", "-c", f"kill -9 {pid} 2>/dev/null; true", timeout=10)
    assert all(v == "GONE" for v in proof["D_wait"].values()), proof["D_wait"]
    assert alive == set(orphans["canary_live"] + orphans["never_named"]), proof["D_after"]
    assert all(str(pid) in " ".join(proof["D_after"]["reaper_lines"]) for pid in orphans["canary"] + orphans["bundled"]), proof["D_after"]
