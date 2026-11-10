#!/usr/bin/env python3
"""Promote a passing OBS candidate to production with one command (obs-22 E-TP, AD-16).

Why: since 2026-11-10 (AD-15, AD-16) the only tests are E2E-grade runs in isolated container lanes plus the
intentional cache regression and gate. Getting a candidate that passed them into production used to be a hand
sequence (fast-forward, push, restart, then check which code runs). This script does exactly that sequence, but
only after every precondition holds, names every failed one, and never improvises. See docs/testing.md.

Run from the CANDIDATE'S worktree (the script is that worktree's own copy):

  /workspace/obs/.venv/bin/python scripts/promote.py --candidate <40-hex sha> \\
      --lane-run <e2e run id> [--lane-run <id> ...] --gate-report <cache-regression run dir> \\
      --approval-source <shared_guard id with obs-source> --approval-restart <id with obs-restart> [--dry-run]

Verifications (all are run, every failure is collected and named; any failure = exit 3 and NOTHING is written):
  1. the worktree HEAD is the candidate (full sha) and `git status --porcelain` is empty;
  2. every --lane-run is green on exactly the candidate: summary.json exit_code 0, no skip/fail/error;
     manifest.json code.commit == candidate and code.dirty false; the set holds a `smoke` run with 5 active
     lanes and an `isolation --prod-send` run whose isolation/summary.json has all_pass true;
  3. the gate: <gate-report>/gate.json is a PASS (or the documented rerun-admitted shape: not PASS, but
     cache_gate.find_passing_report admits exactly this report) for exactly the candidate, clean tree,
     both providers, full scenario set; and `scripts/cache_gate.py check` in this worktree exits 0;
  4. both approval ids exist in `shared_guard.py status --json` with scope obs-source / obs-restart, are not
     revoked and not expired, and carry no other candidate (a partial-gate approval cannot be used here);
  5. no freeze covers OBS restarts (obs-restart, obs-source or all-shared);
  6. the production checkout's HEAD is an ancestor of the candidate, is clean (tracked changes and anything
     untracked under src/scripts/deploy; other untracked files are only noted), and equals origin/main.

Then (without --dry-run), stopping at the first failure and printing the rollback pointer:
  receipt line; `git merge --ff-only` in the production checkout; `git push origin <sha>:refs/heads/main`;
  `python -m obs_agent.maintenance_restart` (never --plain, never --skip-cache-gate); wait for a new
  `admitted` row for the candidate in obs-admitted-releases/launches.jsonl and for obs-running-code.json to
  name the candidate; then write the promotion record /workspace/runtime/state/obs-promotions/<ts>-<sha12>.json.
  Each git step runs with OPS_SHARED_ACK=<its approval id>; the restart with the restart approval's id.

--dry-run runs every verification and prints each planned command and write exactly; it performs none of them.
There is no prompt, no approval creation and no bypass flag, and a freeze is never acknowledged: if one covers
OBS restarts the command refuses. There is no automatic rollback; the pointer printed on failure is for a human.

Exit codes: 0 done (or dry run clean), 2 usage, 3 a verification failed (nothing changed), 4 a step failed after
the verifications (the rollback pointer was printed).

Standard library only. Optional overrides (--prod-checkout, --runs-root, --approvals-json, --freeze-json,
--state-root, --now, --wait-seconds) exist so every refusal can be rehearsed on scratch copies; defaults are production.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OPS_DIR = Path("/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/Agent Operations/scripts/ops")
DEFAULT_PROD = Path("/workspace/obs")
DEFAULT_RUNS = Path("/workspace/runtime/e2e/runs")
DEFAULT_STATE = Path("/workspace/runtime/state")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
RUN_ID = re.compile(r"[A-Za-z0-9._-]+")
REQUIRED_PROVIDERS = ("anthropic", "codex")
FREEZE_SCOPES = ("obs-restart", "obs-source", "all-shared")
IDENTITY_DIRS = ("src/", "scripts/", "deploy/")
WAIT_SECONDS = 600
POLL_SECONDS = 5
EXIT_VERIFY, EXIT_STEP = 3, 4


def utc(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def out(msg: str = "") -> None:
    print(msg, flush=True)


def run(argv, cwd=None, env=None, timeout=120):
    """(rc, stdout, stderr); never raises for a failing command, rc -1 when it cannot run."""
    full_env = dict(os.environ)
    full_env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    full_env.update(env or {})
    try:
        p = subprocess.run([str(a) for a in argv], cwd=str(cwd) if cwd else None, env=full_env,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def git(repo, *args, timeout=120):
    return run(["git", "-C", repo, *args], timeout=timeout)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, f"cannot read {path}: {type(exc).__name__}: {exc}"


def sha_match(a, b) -> bool:
    a, b = str(a or "").lower(), str(b or "").lower()
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


class Checks:
    def __init__(self):
        self.rows = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"check": name, "ok": bool(ok), "detail": detail})
        out(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
        return bool(ok)

    @property
    def failed(self):
        return [r for r in self.rows if not r["ok"]]


# --- 1. worktree ------------------------------------------------------------------------------------------
def verify_worktree(c: Checks, cand: str) -> None:
    rc, head, err = git(REPO, "rev-parse", "HEAD")
    head = head.strip()
    c.add("1a worktree HEAD is the candidate", rc == 0 and head == cand,
          f"HEAD {head or err.strip()} vs candidate {cand}" if head != cand else f"{REPO} at {head}")
    rc, porc, err = git(REPO, "status", "--porcelain")
    lines = [ln for ln in porc.splitlines() if ln.strip()]
    c.add("1b worktree is clean", rc == 0 and not lines,
          f"{len(lines)} path(s) changed or untracked, e.g. {lines[:3]}" if lines else (err.strip() or "git status empty"))


# --- 2. lane runs -----------------------------------------------------------------------------------------
def verify_lane_runs(c: Checks, cand: str, runs_root: Path, ids: list[str]) -> dict:
    smoke_ok, iso_ok = [], []
    for rid in ids:
        name = f"2 lane-run {rid}"
        if not RUN_ID.fullmatch(rid):
            c.add(name, False, "not a run id")
            continue
        d = runs_root / rid
        summary, e1 = load_json(d / "summary.json")
        manifest, e2 = load_json(d / "manifest.json")
        if summary is None or manifest is None:
            c.add(name, False, e1 or e2)
            continue
        problems = []
        totals = summary.get("totals") or {}
        if summary.get("exit_code") != 0:
            problems.append(f"summary exit_code {summary.get('exit_code')!r} (error {summary.get('error')!r})")
        for key in ("skipped", "failed", "error"):
            if totals.get(key) != 0:
                problems.append(f"totals.{key} = {totals.get(key)!r} (a skip is never green)" if key == "skipped"
                                else f"totals.{key} = {totals.get(key)!r}")
        if not totals.get("total"):
            problems.append("no test ran (totals.total is 0 or missing)")
        code = manifest.get("code") or {}
        if code.get("commit") != cand:
            problems.append(f"manifest code.commit {code.get('commit')!r} is not the candidate")
        if code.get("dirty") is not False:
            problems.append(f"manifest code.dirty {code.get('dirty')!r} (must be false)")
        suites = manifest.get("selected_suites")
        if not isinstance(suites, list):
            suites = [s for s in str(manifest.get("suite") or "").split(",") if s]
        iso_pass = None
        if "isolation" in suites:
            iso, e3 = load_json(d / "isolation" / "summary.json")
            iso_pass = bool(iso and iso.get("all_pass") is True)
            if not iso_pass:
                problems.append(e3 or "isolation/summary.json all_pass is not true")
        c.add(name, not problems, "; ".join(problems) if problems else
              f"suites {suites}, exit 0, {totals.get('total')} tests, {summary.get('active_lanes')} active lane(s), commit ok")
        if not problems:
            if "smoke" in suites and (summary.get("active_lanes") or 0) >= 5:
                smoke_ok.append(rid)
            if iso_pass and manifest.get("prod_send") is True:
                iso_ok.append(rid)
    c.add("2 set has a smoke run with 5 active lanes", bool(smoke_ok), ", ".join(smoke_ok) or "none of the listed runs qualifies")
    c.add("2 set has an isolation --prod-send run with all_pass", bool(iso_ok), ", ".join(iso_ok) or "none of the listed runs qualifies")
    return {"smoke_runs": smoke_ok, "isolation_runs": iso_ok}


# --- 3. gate ----------------------------------------------------------------------------------------------
def verify_gate(c: Checks, cand: str, gate_dir: Path) -> dict:
    info = {"report": str(gate_dir)}
    gate, err = load_json(gate_dir / "gate.json")
    if gate is None:
        c.add("3a gate.json readable", False, err)
    else:
        info["finished_at"] = gate.get("finished_at")
        problems = []
        code = gate.get("code") or {}
        if code.get("head") != cand:
            problems.append(f"gate code.head {code.get('head')!r} is not the candidate")
        if code.get("dirty") != "clean":
            problems.append(f"gate code.dirty {code.get('dirty')!r} (must be 'clean')")
        missing = [p for p in REQUIRED_PROVIDERS if p not in (gate.get("providers") or [])]
        if missing:
            problems.append(f"providers missing {missing} (a gate needs anthropic and codex)")
        if gate.get("scenarios") not in (None, "all"):
            problems.append(f"not the full scenario set ({gate.get('scenarios')!r})")
        if gate.get("result") != "PASS":
            snippet = (f"import json,sys; sys.path.insert(0, {str(REPO / 'src')!r}); from pathlib import Path; "
                       f"from obs_agent import cache_gate as g; t = g.code_identity(Path({str(REPO)!r})); "
                       f"d, notes = g.find_passing_report(t, Path({str(gate_dir.parent)!r})); "
                       f"print(json.dumps({{'dir': str(d) if d else None, 'notes': notes}}))")
            rc, so, se = run([sys.executable, "-c", snippet], cwd=REPO)
            admitted = None
            try:
                admitted = json.loads(so.strip().splitlines()[-1]).get("dir") if rc == 0 and so.strip() else None
            except ValueError:
                pass
            if admitted and Path(admitted).resolve() == gate_dir.resolve():
                info["rerun_admitted"] = True
            else:
                problems.append(f"result {gate.get('result')!r} and not the documented rerun-admitted shape "
                                f"(cache_gate admits {admitted!r})")
        c.add("3a gate.json is a PASS for exactly the candidate, clean, both providers", not problems,
              "; ".join(problems) if problems else
              f"result {gate.get('result')}, finished_at {gate.get('finished_at')}" + (", rerun-admitted" if info.get("rerun_admitted") else ""))
    rc, so, se = run([sys.executable, REPO / "scripts" / "cache_gate.py", "check"], cwd=REPO)
    first = (so.strip().splitlines() or [se.strip()[:200]])[0][:300]
    c.add("3b scripts/cache_gate.py check exits 0 in the candidate worktree", rc == 0, f"exit {rc}: {first}")
    return info


# --- 4. approvals -----------------------------------------------------------------------------------------
def approval_rows(path: Path | None):
    if path:
        data, err = load_json(path)
    else:
        rc, so, se = run([sys.executable, OPS_DIR / "shared_guard.py", "status", "--json"])
        try:
            data, err = (json.loads(so), None) if rc == 0 else (None, f"shared_guard.py status exited {rc}: {se.strip()[:200]}")
        except ValueError as exc:
            data, err = None, f"shared_guard.py status gave non-JSON: {exc}"
    if err:
        return None, err
    if isinstance(data, dict):
        data = data.get("active", data.get("approvals"))
    if not isinstance(data, list):
        return None, "approvals JSON is neither a list nor {\"active\": [...]}"
    return data, None


def verify_approval(c: Checks, rows, aid: str, scope: str, now: dt.datetime, cand: str) -> dict:
    name = f"4 approval {aid} ({scope})"
    match = [r for r in rows if r.get("id") == aid]
    if not match:
        c.add(name, False, f"no row with that id among {len(rows)} (`status` lists only active approvals: missing, revoked or expired)")
        return {}
    live = [r for r in match if not r.get("revoked_at")]
    if not live:
        c.add(name, False, f"revoked at {match[-1].get('revoked_at')} by {match[-1].get('revoked_by')}")
        return {}
    row = live[-1]
    problems = []
    until = parse_iso(row.get("until"))
    if row.get("until") and until is None:
        problems.append(f"unreadable until {row.get('until')!r}")
    elif until and until <= now:
        problems.append(f"expired at {row.get('until')} (now {utc(now)})")
    if scope not in (row.get("scope") or []):
        problems.append(f"scope {row.get('scope')} lacks {scope}")
    if row.get("partial_gate"):
        problems.append("partial-gate approval (a provider's half waived): this command needs a full gate; use the hand route")
    for label, value in (("code.head", (row.get("code") or {}).get("head")),
                         ("user_waiver.candidate", (row.get("user_waiver") or {}).get("candidate")),
                         ("waiver.candidate", (row.get("waiver") or {}).get("candidate"))):
        if value and not sha_match(value, cand):
            problems.append(f"{label} {value} is another candidate")
    c.add(name, not problems, "; ".join(problems) if problems else
          f"scope {row.get('scope')}, until {row.get('until')}, for {row.get('for')}")
    return {"id": aid, "scope": row.get("scope"), "until": row.get("until"), "for": row.get("for"),
            "approved_by": row.get("approved_by"), "approved_at": row.get("approved_at")}


# --- 5. freeze --------------------------------------------------------------------------------------------
def verify_freeze(c: Checks, path: Path | None, now: dt.datetime) -> None:
    if path:
        data, err = load_json(path)
    else:
        rc, so, se = run([sys.executable, OPS_DIR / "freeze.py", "status", "--json"])
        try:
            data, err = (json.loads(so), None) if rc == 0 else (None, f"freeze.py status exited {rc}: {se.strip()[:200]}")
        except ValueError as exc:
            data, err = None, f"freeze.py status gave non-JSON: {exc}"
    if err:
        c.add("5 no freeze covers OBS restarts", False, f"cannot verify (fail closed): {err}")
        return
    rows = data.get("active", []) if isinstance(data, dict) else data
    hits = []
    for r in rows or []:
        until = parse_iso(r.get("until"))
        if r.get("lifted_at") or (until and until <= now):
            continue
        covered = [s for s in (r.get("scope") or []) if s in FREEZE_SCOPES]
        if covered:
            hits.append(f"{r.get('id')} (scope {covered}, by {r.get('declared_by')}: {str(r.get('reason'))[:120]})")
    c.add("5 no freeze covers OBS restarts or OBS source", not hits, "; ".join(hits) if hits else "no active covering freeze")


# --- 6. production checkout -------------------------------------------------------------------------------
def verify_prod(c: Checks, cand: str, prod: Path) -> dict:
    info = {"checkout": str(prod)}
    rc, head, err = git(prod, "rev-parse", "HEAD")
    head = head.strip()
    info["head_before"] = head
    if rc != 0:
        c.add("6 production checkout readable", False, err.strip()[:200])
        return info
    rc, _, _ = git(prod, "cat-file", "-e", f"{cand}^{{commit}}")
    if rc != 0:
        c.add("6a production HEAD is an ancestor of the candidate", False, f"candidate {cand[:12]} is not in the production checkout's object store")
    else:
        rc, _, _ = git(prod, "merge-base", "--is-ancestor", head, cand)
        c.add("6a production HEAD is an ancestor of the candidate (fast-forward possible)", rc == 0,
              f"production HEAD {head[:12]}" + ("" if rc == 0 else " is not an ancestor: main has moved or diverged"))
    rc, porc, err = git(prod, "status", "--porcelain")
    lines = [ln for ln in porc.splitlines() if ln.strip()]
    blocking = [ln for ln in lines if not (ln.startswith("??") and not ln[3:].startswith(IDENTITY_DIRS))]
    noted = [ln for ln in lines if ln not in blocking]
    c.add("6b production checkout is clean", rc == 0 and not blocking,
          f"{len(blocking)} blocking path(s), e.g. {blocking[:3]}" if blocking else
          ("clean" + (f" (untracked outside src/scripts/deploy, noted only: {noted[:3]})" if noted else "")))
    info["untracked_noted"] = noted
    rc, so, err = git(prod, "ls-remote", "origin", "refs/heads/main", timeout=90)
    remote = so.split()[0] if rc == 0 and so.split() else ""
    c.add("6c production HEAD equals origin main", rc == 0 and remote == head,
          f"origin main {remote or err.strip()[:150]} vs production HEAD {head}" if remote != head else f"both {head[:12]}")
    info["origin_main"] = remote
    return info


# --- rollback pointer and the post-restart wait -----------------------------------------------------------
def read_launches(state: Path):
    rows = []
    try:
        for line in (state / "obs-admitted-releases" / "launches.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def rollback_pointer(state: Path, prod_head_before: str) -> dict:
    admitted = [r for r in read_launches(state) if r.get("decision") == "admitted"]
    last = admitted[-1] if admitted else None
    cur, _ = load_json(state / "obs-admitted-releases" / "current.json")
    return {"production_main_before": prod_head_before,
            "last_admitted_launch": {"at": last.get("at"), "selected": last.get("selected"),
                                     "head": (last.get("code") or {}).get("head")} if last else None,
            "current_json": {"repo": cur.get("repo"), "head": (cur.get("code") or {}).get("head")} if isinstance(cur, dict) else None}


def print_rollback(rb: dict) -> None:
    out("ROLLBACK POINTER (no automatic rollback; a human decides):")
    out(f"  production main before this promotion: {rb['production_main_before']}")
    out(f"  last admitted release in launches.jsonl: {json.dumps(rb['last_admitted_launch'])}")
    out(f"  current.json (what cache_launch falls back to): {json.dumps(rb['current_json'])}")
    out("  cache_launch itself falls back to that admitted release on a crash; restoring main by hand follows "
        "procedures/registry/shared-changes.md (emergency restore of the last-known-good only).")


def wait_for_admission(state: Path, cand: str, t0: dt.datetime, wait: int = WAIT_SECONDS):
    """(ok, detail, evidence): a new `admitted` launch for the candidate, then a running-code record naming it."""
    deadline = time.monotonic() + wait
    admitted = None
    while True:
        for row in read_launches(state):
            at = parse_iso(row.get("at"))
            if not at or at <= t0:
                continue
            if row.get("decision") != "admitted":
                return False, f"launch after restart was {row.get('decision')!r}, not admitted: {str(row.get('reason'))[:300]}", {"launch": row}
            if (row.get("code") or {}).get("head") == cand:
                admitted = admitted or row
        if admitted:
            running, _ = load_json(state / "obs-running-code.json")
            started = parse_iso((running or {}).get("started_at"))
            alive = False
            if isinstance(running, dict) and running.get("pid"):
                try:
                    os.kill(int(running["pid"]), 0)
                    alive = True
                except (OSError, ValueError):
                    alive = False
            if (isinstance(running, dict) and running.get("head") == cand and started and started > t0
                    and alive and running.get("repo") == admitted.get("selected")):
                return True, "admitted and running", {"launch": admitted, "running": running}
        if time.monotonic() >= deadline:
            why = "no new admitted launch row for the candidate" if not admitted else \
                "admitted, but obs-running-code.json does not show a live process of that release started after the restart"
            return False, f"timed out after {wait}s: {why}", {"launch": admitted}
        time.sleep(POLL_SECONDS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog="See the module docstring and docs/testing.md.")
    ap.add_argument("--candidate", required=True, help="full 40-hex commit sha of the candidate")
    ap.add_argument("--lane-run", action="append", required=True, default=[], help="E2E run id (repeat)")
    ap.add_argument("--gate-report", required=True, help="cache_regression run directory holding gate.json")
    ap.add_argument("--approval-source", required=True, help="shared_guard approval id with scope obs-source")
    ap.add_argument("--approval-restart", required=True, help="shared_guard approval id with scope obs-restart")
    ap.add_argument("--dry-run", action="store_true", help="verify and print the plan; change nothing")
    ap.add_argument("--prod-checkout", type=Path, default=DEFAULT_PROD, help="rehearsal override")
    ap.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS, help="rehearsal override")
    ap.add_argument("--approvals-json", type=Path, help="rehearsal override for shared_guard.py status --json")
    ap.add_argument("--freeze-json", type=Path, help="rehearsal override for freeze.py status --json")
    ap.add_argument("--state-root", type=Path, default=DEFAULT_STATE, help="rehearsal override")
    ap.add_argument("--now", help="ISO time used for approval and freeze expiry (rehearsal override)")
    ap.add_argument("--wait-seconds", type=int, default=WAIT_SECONDS, help="how long to wait for the admitted launch (rehearsal override)")
    a = ap.parse_args(argv)

    cand = a.candidate.lower()
    if not FULL_SHA.fullmatch(cand):
        ap.error("--candidate must be the full 40-hex commit sha")
    now = parse_iso(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    if now is None:
        ap.error("--now is not ISO-8601")
    gate_dir, prod, runs, state = Path(a.gate_report), a.prod_checkout, a.runs_root, a.state_root
    started = dt.datetime.now(dt.timezone.utc)

    out(f"promote.py: candidate {cand} ({'DRY RUN' if a.dry_run else 'LIVE'}), checked at {utc(now)}")
    c = Checks()
    verify_worktree(c, cand)
    lanes = verify_lane_runs(c, cand, runs, a.lane_run)
    gate = verify_gate(c, cand, gate_dir)
    rows, err = approval_rows(a.approvals_json)
    if rows is None:
        c.add("4 approvals readable", False, f"cannot verify (fail closed): {err}")
        approvals = {}
    else:
        approvals = {"source": verify_approval(c, rows, a.approval_source, "obs-source", now, cand),
                     "restart": verify_approval(c, rows, a.approval_restart, "obs-restart", now, cand)}
    verify_freeze(c, a.freeze_json, now)
    prodinfo = verify_prod(c, cand, prod)

    if c.failed:
        out(f"REFUSED: {len(c.failed)} of {len(c.rows)} check(s) failed; nothing was changed or written:")
        for r in c.failed:
            out(f"  - {r['check']}: {r['detail']}")
        return EXIT_VERIFY
    out(f"all {len(c.rows)} checks passed")

    py = prod / ".venv" / "bin" / "python"
    sid, rid = a.approval_source, a.approval_restart
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    record_path = state / "obs-promotions" / f"{stamp}-{cand[:12]}.json"
    receipt = f"receipt: gate PASS at {gate.get('finished_at')}, report {gate_dir}, changing now"
    steps = [
        ("merge", f"OPS_SHARED_ACK={sid} git -C {prod} merge --ff-only {cand}"),
        ("push", f"OPS_SHARED_ACK={sid} git -C {prod} push origin {cand}:refs/heads/main"),
        ("restart", f"cd {prod} && OPS_SHARED_ACK={rid} {py} -m obs_agent.maintenance_restart"),
        ("wait", f"poll up to {a.wait_seconds}s: {state}/obs-admitted-releases/launches.jsonl for a new `admitted` row with code.head "
                 f"{cand[:12]}, and {state}/obs-running-code.json naming it with a live pid started after the restart"),
        ("record", f"write {record_path}"),
    ]
    rb = rollback_pointer(state, prodinfo.get("head_before", ""))
    if a.dry_run:
        out(receipt)
        out("DRY RUN: nothing below is performed. Planned, in order:")
        for i, (_, text) in enumerate(steps, 1):
            out(f"  {i}. {text}")
        out("  on any failure after step 1: stop, print the rollback pointer, no automatic rollback. Pointer as of now:")
        print_rollback(rb)
        return 0

    record = {"candidate": cand, "started_at": utc(started), "inputs": {
        "argv": sys.argv[1:], "lane_runs": a.lane_run, "gate_report": str(gate_dir), "approvals": approvals,
        "prod_checkout": str(prod), "runs_root": str(runs), "state_root": str(state), "checks_now": utc(now)},
        "checks": c.rows, "lanes": lanes, "gate": gate, "prod_before": prodinfo, "rollback_pointer": rb, "steps": [], "outcome": "running"}

    def finish(outcome: str, rc: int) -> int:
        record["outcome"], record["finished_at"] = outcome, utc(dt.datetime.now(dt.timezone.utc))
        try:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = record_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
            tmp.replace(record_path)
            out(f"promotion record: {record_path}")
        except OSError as exc:
            out(f"could not write the promotion record {record_path}: {exc}")
        if rc:
            out(f"FAILED: {outcome}")
            print_rollback(rb)
        return rc

    def step(name: str, argv, **kw) -> bool:
        t = utc(dt.datetime.now(dt.timezone.utc))
        rc, so, se = run(argv, **kw)
        record["steps"].append({"step": name, "at": t, "rc": rc, "stdout": so[-1500:], "stderr": se[-1500:]})
        out(f"step {name}: exit {rc}")
        return rc == 0

    out(receipt)
    if not step("merge", ["git", "-C", prod, "merge", "--ff-only", cand], env={"OPS_SHARED_ACK": sid}):
        return finish("failed at merge (production HEAD unchanged unless git says otherwise)", EXIT_STEP)
    rc, head, _ = git(prod, "rev-parse", "HEAD")
    if head.strip() != cand:
        return finish(f"failed after merge: production HEAD is {head.strip()!r}, not the candidate", EXIT_STEP)
    if not step("push", ["git", "-C", prod, "push", "origin", f"{cand}:refs/heads/main"], env={"OPS_SHARED_ACK": sid}, timeout=300):
        return finish("failed at push (production checkout is already at the candidate, origin main is not)", EXIT_STEP)
    rc, so, _ = git(prod, "ls-remote", "origin", "refs/heads/main", timeout=90)
    if not (so.split() and so.split()[0] == cand):
        return finish(f"failed after push: origin main reads {so.strip()!r}", EXIT_STEP)
    t0 = dt.datetime.now(dt.timezone.utc)
    record["restart_requested_at"] = utc(t0)
    if not step("restart", [py, "-m", "obs_agent.maintenance_restart"], cwd=prod, env={"OPS_SHARED_ACK": rid}, timeout=300):
        return finish("failed at the maintenance restart (main is promoted, the running code may be the old release)", EXIT_STEP)
    ok, detail, evidence = wait_for_admission(state, cand, t0, a.wait_seconds)
    record["steps"].append({"step": "wait", "ok": ok, "detail": detail, "evidence": evidence})
    out(f"step wait: {detail}")
    if not ok:
        return finish(f"failed after the restart: {detail}", EXIT_STEP)
    out(f"PROMOTED {cand[:12]}: admitted release {evidence['launch'].get('selected')}, pid {evidence['running'].get('pid')}")
    return finish("promoted", 0)


if __name__ == "__main__":
    raise SystemExit(main())
