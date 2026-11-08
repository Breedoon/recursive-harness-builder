"""obs-22.86 / obs-22.68 (increment 12): per-route CLI runtime selection, rollback and the orphan reaper.

Needs ``obs-e2e run --canary-cli <obs-cli binary>``. The lane's own canary file
(``state/obs-cli-canary.json``, the lane's ``/workspace/runtime/state``) names the lane agent. Evidence is
read from the lane log's ``CLI runtime`` lines (OBS's production log), the runtime record
(``state/obs-cli-runtime.sqlite3``), and the CLI processes' argv[0] in the lane. Proof:
``tests/<lane>/inc12/runtime.json``.
"""
from __future__ import annotations

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
    runtime_id = lines[-1][0]
    assert p.record(sid) == (runtime_id, p.canary)

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

    # D. Orphan: back on obs-cli, kill the lane's OBS; its obs-cli child is reaped by the restarted OBS.
    p.write([agent])
    await lane.command(topic, "/clear")
    sixth = await lane.turn(topic, f"Reply with exactly: F-{code}")
    before = await p.clis()
    assert [r["args"][0] for r in before] == [p.canary], before
    orphan_pids = [r["pid"] for r in before]
    p.new_window()
    await lane.restart_obs("kill")
    after = {r["pid"] for r in await p.clis()}
    log = p.log_text()
    proof["D"] = {"orphan_pids": orphan_pids, "still_alive": sorted(set(orphan_pids) & after),
                  "reaper_lines": [line.split("] ", 1)[-1][:160] for line in log.splitlines() if "orphan_reaper" in line]}
    assert not set(orphan_pids) & after, proof["D"]
    assert any(str(pid) in " ".join(proof["D"]["reaper_lines"]) for pid in orphan_pids), proof["D"]
