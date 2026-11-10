"""obs-22 rc2 (Bead-4/R16): sticky random percentage selection of new conversations.

Needs ``obs-e2e run --canary-cli <obs-cli binary> --suite rc2-percentage``. The lane's own canary
file (``state/obs-cli-canary.json`` = the lane's ``/workspace/runtime/state``) is written by this
test only. Evidence: the lane log's ``CLI runtime`` lines, the runtime record
(``state/obs-cli-runtime.sqlite3``, read-only here) and the argv[0] of the CLI process whose argv
carries ``--resume <session id>``. Proof: ``tests/<lane>/rc2/percentage.json``; the share over all
lanes of a run is summed from those files (the per-lane draw count is too small to judge alone).

Cases (work order E-RC2 step 5):
 (a) no file: bundled, no ``CLI runtime`` line, no runtime database;
 (b) a named agent: its fresh session runs obs-cli (assignment ``named``);
 (c) ``percentage`` 50 with the lane model listed: every new conversation is drawn once and recorded;
     after the stage is raised to 100 and OBS restarted, every one resumes on the binary it was drawn
     onto (same session, context kept), and a new conversation at 100 is always obs-cli;
 (d) rollback by edit: ``percentage`` 0 moves drawn conversations to bundled at their next spawn
     (context kept); raising it again never re-draws them; an empty file and a removed file are bundled.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid

import pytest

pytestmark = [pytest.mark.suite("rc2-percentage"), pytest.mark.needs(all_lanes=True, credential=("dev", "sub"))]  # every lane draws: 8 x lanes

BUNDLED_MARK = "claude_agent_sdk/_bundled/claude"
RUNTIME_RE = re.compile(r"CLI runtime runtime=(\S+) reason=(.*?) model=(\S+) resume=(\S+) agent=(\S+) team=(\S+)")
DRAWS = 8


class _Probe:
    def __init__(self, lane):
        self.lane = lane
        self.file = lane.lane_dir / "state" / "obs-cli-canary.json"
        self.db = lane.lane_dir / "state" / "obs-cli-runtime.sqlite3"
        self.log = lane.lane_dir / "logs" / "telegram-prod.log"
        self.canary = lane.manifest["canary_cli"]
        self.mark = 0
        self.new_window()

    def new_window(self):
        self.mark = self.log.stat().st_size if self.log.exists() else 0

    def lines(self, agent=None):
        if not self.log.exists():
            return []
        with self.log.open("rb") as handle:
            handle.seek(self.mark)
            text = handle.read().decode("utf-8", "replace")
        found = [m.groups() for m in map(RUNTIME_RE.search, text.splitlines()) if m]
        return [l for l in found if agent is None or l[4] == agent]

    def write(self, **fields):
        self.file.write_text(json.dumps({"cli_path": self.canary, "agents": [], "teams": [],
                                         "new_sessions_only": True, **fields}))

    def record(self, session_id):
        if not self.db.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM session_runtime WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    async def argv0_for(self, session_id):
        """argv[0] of the live CLI resumed on this session (None when no such process)."""
        rows = await self.lane.procs()
        for r in rows:
            args = r.get("args") or []
            if "--output-format" in args and "--resume" in args and session_id in args:
                return args[0]
        return None

    async def all_argv0(self):
        rows = await self.lane.procs()
        return sorted({r["args"][0] for r in rows if r.get("args") and "--output-format" in r["args"]})


def _is_obs_cli(runtime):
    return runtime.startswith("obs-cli:")


async def test_rc2_percentage_selection_sticky_and_rollback(lane):
    if not lane.manifest.get("canary_cli"):
        pytest.skip("run with --canary-cli <obs-cli binary>")
    p = _Probe(lane)
    proof: dict = {"run_id": lane.manifest["run_id"], "lane": lane.name, "canary_cli": p.canary,
                   "canary_cli_sha256": lane.manifest.get("canary_cli_sha256")}
    try:
        await _scenario(lane, p, proof)
    finally:
        target = lane.run_dir / "tests" / lane.name / "rc2"
        target.mkdir(parents=True, exist_ok=True)
        (target / "percentage.json").write_text(json.dumps(proof, indent=2, default=str) + "\n")


async def _scenario(lane, p, proof):
    nonce = uuid.uuid4().hex[:6].upper()

    # (a) No canary file: production behaviour, nothing recorded, no INFO line.
    assert not p.file.exists()
    t0 = await lane.new_topic(f"rc2 a {nonce}")
    a = await lane.turn(t0, f"Reply with exactly: A-{nonce}")
    assert f"A-{nonce}" in a.text, a.messages
    agent0 = a.end["agent_name"]
    proof["a"] = {"runtime_lines": p.lines(), "db_exists": p.db.exists(), "cli_argv0": await p.all_argv0()}
    assert proof["a"]["runtime_lines"] == [] and not proof["a"]["db_exists"], proof["a"]
    assert proof["a"]["cli_argv0"] and all(BUNDLED_MARK in x for x in proof["a"]["cli_argv0"]), proof["a"]

    # (b) A named agent: /clear gives a fresh session on obs-cli, recorded as named.
    p.write(agents=[agent0])
    p.new_window()
    await lane.command(t0, "/clear")
    b = await lane.turn(t0, f"Reply with exactly: B-{nonce}")
    assert f"B-{nonce}" in b.text, b.messages
    lines = p.lines(agent0)
    proof["b"] = {"runtime_lines": lines, "record": p.record(b.end["session_id"])}
    assert lines and _is_obs_cli(lines[-1][0]) and lines[-1][1] == "selected" and lines[-1][3] == "None", lines
    assert proof["b"]["record"]["assignment"] == "named" and proof["b"]["record"]["cli_path"] == p.canary, proof["b"]
    model = lines[-1][2]
    proof["model"] = model

    # (c) 50 percent for the lane model: every new conversation is drawn once and recorded at init.
    p.write(percentage=50, models=[model])
    drawn = []
    for i in range(DRAWS):
        p.new_window()
        topic = await lane.new_topic(f"rc2 c{i} {nonce}")
        code = f"K{i}{nonce}"
        turn = await lane.turn(topic, f"Remember the codeword {code}. Reply with exactly: C{i}-{nonce}")
        assert f"C{i}-{nonce}" in turn.text, turn.messages
        agent, sid = turn.end["agent_name"], turn.end["session_id"]
        mine = p.lines(agent)
        assert mine and mine[-1][3] == "None", mine
        runtime, reason = mine[-1][0], mine[-1][1]
        rec = p.record(sid)
        won = _is_obs_cli(runtime)
        assert reason == ("selected by percentage" if won else "percentage draw: bundled"), mine
        assert rec and rec["assignment"] == ("percentage" if won else "percentage-bundled") and rec["percentage"] == 50, rec
        assert rec["created_at"] and rec["agent_name"] == agent and rec["reason"] == reason, rec
        drawn.append({"topic": topic.topic_id, "agent": agent, "session_id": sid, "code": code,
                      "runtime": runtime, "won": won, "record": rec})
    proof["c_draws"] = drawn
    proof["c_share"] = {"drawn": len(drawn), "obs_cli": sum(d["won"] for d in drawn), "percentage": 50}

    # (c) Raising the stage never moves an existing conversation; restart + resume keep each binary.
    p.write(percentage=100, models=[model])
    p.new_window()
    proof["c_restart"] = (await lane.restart_obs("maintenance"))["ready"]
    resumed = []
    for d in drawn:
        turn = await lane.turn(d["topic"], "What was the codeword I asked you to remember? Reply with only the codeword.")
        mine = p.lines(d["agent"])
        argv0 = await p.argv0_for(d["session_id"])
        entry = {"agent": d["agent"], "session_id": turn.end["session_id"], "line": mine[-1] if mine else None,
                 "argv0": argv0, "text": turn.text[:60], "record": p.record(d["session_id"])}
        resumed.append(entry)
        assert turn.end["session_id"] == d["session_id"], (d, turn.end)
        assert d["code"] in turn.text, turn.messages
        assert mine and mine[-1][3] == d["session_id"], mine
        if d["won"]:
            assert _is_obs_cli(mine[-1][0]) and mine[-1][1] == "resume stays on the recorded obs-cli (percentage)", mine
            assert argv0 == p.canary, entry
        else:
            assert mine[-1][0].startswith("bundled-") and mine[-1][1] == "resume stays on the bundled CLI (never drawn)", mine
            assert argv0 and BUNDLED_MARK in argv0, entry
        assert entry["record"]["assignment"] == d["record"]["assignment"], entry
    proof["c_resumed"] = resumed

    # A new conversation at 100 percent is always drawn onto obs-cli.
    p.new_window()
    t100 = await lane.new_topic(f"rc2 c100 {nonce}")
    full = await lane.turn(t100, f"Reply with exactly: F-{nonce}")
    mine = p.lines(full.end["agent_name"])
    proof["c_full"] = {"line": mine[-1] if mine else None, "record": p.record(full.end["session_id"])}
    assert mine and _is_obs_cli(mine[-1][0]) and mine[-1][1] == "selected by percentage", mine

    # (d) Rollback by one edit: percentage 0. Drawn conversations resume on bundled, context kept.
    p.write(percentage=0, models=[model])
    p.new_window()
    proof["d_restart"] = (await lane.restart_obs("maintenance"))["ready"]
    rolled = []
    for d in drawn:
        turn = await lane.turn(d["topic"], "What was the codeword I asked you to remember? Reply with only the codeword.")
        mine = p.lines(d["agent"])
        entry = {"agent": d["agent"], "won": d["won"], "line": mine[-1] if mine else None,
                 "argv0": await p.argv0_for(d["session_id"]), "record": p.record(d["session_id"])}
        rolled.append(entry)
        assert turn.end["session_id"] == d["session_id"] and d["code"] in turn.text, (d, turn.end, turn.messages)
        assert mine and mine[-1][0].startswith("bundled-") and mine[-1][1] == "not selected", mine
        assert entry["argv0"] and BUNDLED_MARK in entry["argv0"], entry
        expected = "unselected" if d["won"] else "percentage-bundled"
        assert entry["record"]["assignment"] == expected and entry["record"]["cli_path"] is None, entry
    proof["d_rolled_back"] = rolled

    # Raising the stage again never re-draws a rolled-back conversation (one resume suffices).
    winners = [d for d in drawn if d["won"]]
    if winners:
        p.write(percentage=100, models=[model])
        p.new_window()
        proof["d2_restart"] = (await lane.restart_obs("maintenance"))["ready"]
        d = winners[0]
        turn = await lane.turn(d["topic"], "Reply with exactly: G-" + nonce)
        mine = p.lines(d["agent"])
        proof["d2_redraw"] = {"agent": d["agent"], "line": mine[-1] if mine else None, "record": p.record(d["session_id"])}
        assert mine and mine[-1][0].startswith("bundled-") and mine[-1][1] == "resume stays on the bundled CLI (never drawn)", mine

    # An empty file and a removed file: bundled for a new conversation.
    p.file.write_text("")
    p.new_window()
    te = await lane.new_topic(f"rc2 empty {nonce}")
    empty = await lane.turn(te, f"Reply with exactly: H-{nonce}")
    mine = p.lines(empty.end["agent_name"])
    proof["d_empty"] = {"line": mine[-1] if mine else None, "record": p.record(empty.end["session_id"])}
    assert mine and mine[-1][0].startswith("bundled-") and mine[-1][1] == "invalid", mine
    assert proof["d_empty"]["record"] is None

    p.file.unlink()
    p.new_window()
    tr = await lane.new_topic(f"rc2 removed {nonce}")
    removed = await lane.turn(tr, f"Reply with exactly: I-{nonce}")
    proof["d_removed"] = {"lines": p.lines(), "record": p.record(removed.end["session_id"])}
    assert proof["d_removed"]["lines"] == [] and proof["d_removed"]["record"] is None, proof["d_removed"]
