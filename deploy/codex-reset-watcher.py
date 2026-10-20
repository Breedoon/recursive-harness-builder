#!/usr/bin/env python3
"""Codex TWO-EVENT reset watcher — v2 2026-10-02 (supersedes same-day v1).

Timeline (Daniel 13:45Z):
  2026-10-03 16:58 UTC  reset #1: normal window -> 100%   = GO  (launch burst)
  2026-10-03 ~17:00 UTC reset #2: global reset, 2-30+ min later,
                          may arrive as usage reset OR BANKED reset = STOP

State machine:
  phase "wait1": poll slow (POLL1, default 300s).
    Event1: used_percent drops >=15  OR  reset_at advances >=60s
      -> GO: exec ACTION_GO (default dry-run) -> phase "wait2", poll fast (POLL2, default 45s)
  phase "wait2": poll fast.
    Event2: used_percent drops >=5 again  OR  banked count changes
      -> STOP: exec ACTION_STOP (default dry-run) -> phase "done", exit unless KEEP_RUNNING=1

Env:
  CODEX_RESET_ACTION_GO    dry-run|stop|start|none     (default dry-run)
  CODEX_RESET_ACTION_STOP  dry-run|stop|start|none     (default dry-run)
  CODEX_RESET_POLL1        secs, default 300   (slow, far from window)
  CODEX_RESET_POLL2        secs, default 45    (fast, 16:50-18:30 window)
  CODEX_RESET_FAST_AFTER   ISO UTC or epoch, default 2026-10-03T16:45:00Z
                           (switch to POLL2 even without event, pre-window)
  CODEX_RESET_FAST_UNTIL   ISO UTC or epoch, default 2026-10-03T18:30:00Z
                           (after this, back to POLL1 if still waiting)
  CODEX_RESET_ONCE=1       single poll, exit (for cron/supervisor later)
  CODEX_RESET_STOP_RESUME  resumable|kill  (default resumable)
    resumable: supervisorctl stop obs-telegram-prod is NOT used; instead
               writes FLAG_FILE only + prints /stop_tree routes for prod daemon
               (fastest resumable: routes stay in crash-resume.json, daemon UP)
    kill:      supervisorctl stop obs-telegram-prod (nothing auto-resumes)

Rate-limit safe: read-only GET, no model traffic; 45s << provider 429 window.
Endpoint/creds: same as /workspace/runtime/bin/usage-probe (chatgpt.com/backend-api/codex/usage).
State/log: /workspace/runtime/logs/codex-reset-watcher/{last-state.json,watcher.log,GO,STOP}
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request

AUTH_GLOB = "/workspace/runtime/cliproxy/auth/codex-*.json"
URL = "https://chatgpt.com/backend-api/codex/usage"
LOG_DIR = "/workspace/runtime/logs/codex-reset-watcher"
GO_FILE = os.path.join(LOG_DIR, "GO")
STOP_FILE = os.path.join(LOG_DIR, "STOP")
STATE_FILE = os.path.join(LOG_DIR, "last-state.json")
STATUS_FILE = "/workspace/runtime/logs/codex-reset-watcher/STATUS"  # Mac notifier reads
PHASE_FILE = os.path.join(LOG_DIR, "phase")  # wait1|go-fired|done



def log(msg: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    line = f"[{dt.datetime.now(dt.UTC).isoformat()}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG_DIR, "watcher.log"), "a") as f:
        f.write(line + "\n")


def read_codex() -> dict:
    files = sorted(glob.glob(AUTH_GLOB))
    if not files:
        raise RuntimeError(f"no auth files match {AUTH_GLOB}")
    a = json.load(open(files[0]))
    headers = {
        "Authorization": "Bearer " + a.get("access_token", ""),
        "chatgpt-account-id": a.get("account_id", ""),
        "User-Agent": "codex_cli_rs/0.0.0",
    }
    req = urllib.request.Request(URL, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def snapshot(raw: dict) -> dict:
    rl = raw.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    rrc = raw.get("rate_limit_reset_credits") or {}
    return {
        "used_percent": pw.get("used_percent"),
        "reset_at": pw.get("reset_at"),
        "limit_reached": rl.get("limit_reached"),
        "banked": rrc.get("available_count"),
        "ts": dt.datetime.now(dt.UTC).isoformat(),
    }


FREEZE_TOOL = os.environ.get(
    "CODEX_RESET_FREEZE_TOOL",
    "/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/Agent Operations/scripts/ops/freeze.py",
)


def freeze_blocks(command: str) -> str:
    """Ask freeze.py whether a declared shared-service freeze covers *command* (obs-15 U1).

    Returns the freeze message when blocked, "" when allowed. Fails open: a missing tool, a
    timeout or any unexpected exit code allows the action. OPS_FREEZE_ACK=<id> in the
    environment is forwarded so freeze.py's own ack rule decides.
    """
    if not os.path.isfile(FREEZE_TOOL):
        return ""
    text = command
    ack = os.environ.get("OPS_FREEZE_ACK", "").strip()
    if ack:
        text = f"OPS_FREEZE_ACK={ack} {command}"
    try:
        r = subprocess.run([sys.executable or "python3", FREEZE_TOOL, "check", "--command", text],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 - fail open
        return ""
    if r.returncode == 3:
        return " ".join((r.stdout or "blocked").split())[:400]
    return ""


def act(action: str, label: str) -> str:
    if action in ("none", "", None):
        return f"({label}: no action)"
    dry = action == "dry-run"
    cmd = None
    if action in ("stop", "dry-run"):
        cmd = ["supervisorctl", "stop", "obs-telegram-prod"]
    elif action == "start":
        cmd = ["supervisorctl", "start", "obs-telegram-prod"]
    if cmd is None:
        return f"({label}: no action for {action})"
    if dry:
        return f"{label} DRY-RUN would exec: {' '.join(cmd)}"
    blocked = freeze_blocks(" ".join(cmd))
    if blocked:
        return f"{label} SKIPPED {' '.join(cmd)}: active shared-service freeze — {blocked}"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return f"{label} executed {' '.join(cmd)} rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}"


def parse_ts(v) -> float:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return float(v)
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def main() -> int:
    # OBS_* env (cache-proxy etc) break urllib TLS to chatgpt.com — 403 (v4 fix)
    for k in [k for k in os.environ if k.startswith("OBS_")]:
        os.environ.pop(k, None)
    global LOG_DIR, GO_FILE, STOP_FILE, STATE_FILE, STATUS_FILE, PHASE_FILE
    LOG_DIR = os.environ.get("CODEX_RESET_LOG_DIR", LOG_DIR)
    GO_FILE = os.path.join(LOG_DIR, "GO")
    STOP_FILE = os.path.join(LOG_DIR, "STOP")
    STATE_FILE = os.path.join(LOG_DIR, "last-state.json")
    STATUS_FILE = os.path.join(LOG_DIR, "STATUS")
    PHASE_FILE = os.path.join(LOG_DIR, "phase")
    poll1 = int(os.environ.get("CODEX_RESET_POLL1", "300"))
    poll2 = int(os.environ.get("CODEX_RESET_POLL2", "45"))
    go_action = os.environ.get("CODEX_RESET_ACTION_GO", "dry-run")
    stop_action = os.environ.get("CODEX_RESET_ACTION_STOP", "stop")  # default ARMED: broad kill switch (Daniel 14:27Z)
    stop_mode = os.environ.get("CODEX_RESET_STOP_MODE", "kill")  # kill=broad supervisorctl stop (Daniel 14:27Z)|codex-only|resumable|dry-run
    once = os.environ.get("CODEX_RESET_ONCE", "") == "1"
    fast_after = parse_ts(os.environ.get("CODEX_RESET_FAST_AFTER", "2026-10-03T16:45:00Z"))
    fast_until = parse_ts(os.environ.get("CODEX_RESET_FAST_UNTIL", "2026-10-03T18:30:00Z"))
    window_start = parse_ts(os.environ.get("CODEX_RESET_WINDOW_START", "2026-10-03T16:50:00Z"))
    confirm_needed = int(os.environ.get("CODEX_RESET_CONFIRM", "2"))
    blind_since = None  # ts when probe first started failing in window
    os.makedirs(LOG_DIR, exist_ok=True)
    prev = None
    if os.path.exists(STATE_FILE):
        try:
            prev = json.load(open(STATE_FILE))
        except Exception:
            prev = None
    phase = "wait1"
    if prev:
        if prev.get("phase2_stop"):
            phase = "done"  # full cycle done; env CODEX_RESET_FORCE=1 to rerun
        elif prev.get("phase1_go"):
            phase = "wait2"
    if os.environ.get("CODEX_RESET_FORCE", "") == "1":
        phase = "wait1"
    go_streak = stop_streak = 0
    go_peak = (prev or {}).get("used_percent") or 0
    post_go_peak = (prev or {}).get("used_percent") or 0
    stop_banked_peak = (prev or {}).get("banked") or 0
    while True:
        now = dt.datetime.now(dt.UTC).timestamp()
        try:
            cur = snapshot(read_codex())
        except Exception as e:
            log(f"ERROR reading usage: {e!r}")
            cur = None
        # Blind tracking: probe fail >5min during fast window -> status blind
        if cur is None:
            if blind_since is None:
                blind_since = now
        else:
            blind_since = None
        poll = poll1
        if phase == "wait1":
            if now >= fast_after:
                poll = poll2
            if cur and prev:
                go_peak = max(go_peak, cur["used_percent"] or 0)
                dp = (cur["used_percent"] or 0) - go_peak
                dr = (cur["reset_at"] or 0) - (prev["reset_at"] or 0)
                # GO gate: window started AND (big drop from peak OR reset advanced >=6h)
                go_now = now >= window_start and (dp <= -30 or dr >= 6 * 3600)
                # streak holds thru flat polls (dp from peak stays neg); reset on back-up
                if dp > 0:
                    go_streak = 0
                elif go_now:
                    go_streak += 1
                if go_streak >= confirm_needed:
                    msg = f"GO RESET1: used {go_peak}->{cur['used_percent']} reset_at {prev['reset_at']}->{cur['reset_at']}"
                    log(msg)
                    open(GO_FILE, "w").write(msg + "\n")
                    open(PHASE_FILE, "w").write("go-fired\n")
                    log(act(go_action, "GO"))
                    phase = "wait2"
                    post_go_peak = cur["used_percent"] or 0
                    prev = cur
                    go_streak = stop_streak = 0
        elif phase == "wait2":
            poll = poll2 if now <= fast_until else poll1
            if cur and prev:
                dp = (cur["used_percent"] or 0) - post_go_peak
                db = (cur["banked"] or 0) - stop_banked_peak
                dr = (cur["reset_at"] or 0) - (prev["reset_at"] or 0)
                post_go_peak = max(post_go_peak, cur["used_percent"] or 0)
                # banked base: rises with events, drops only on consume.
                # +1 flat 2,2: db=+1 poll1, poll2 db=0 BUT streak carries (2>=1 fired)
                # base update AFTER gate: poll2 db0 but streak already 2 → fires ✓
                if (cur["banked"] or 0) < stop_banked_peak:
                    stop_banked_peak = cur["banked"] or 0
                # STOP gate: GO fired AND (drop >=10 from post-GO peak OR banked INCREASE OR reset advanced again)
                # banked decrease = consumed: log+alert only, no stop
                stop_now = dp <= -10 or db >= 1 or dr >= 60
                banked_down = db < 0
                # streak holds thru flat polls; resets only on consume or usage back up
                if banked_down or dp > 0:
                    stop_streak = 0
                elif stop_now:
                    stop_streak += 1
                if banked_down:
                    log(f"BANKED CONSUMED (not STOP): {prev['banked']}->{cur['banked']} ({db})")
                if stop_streak >= confirm_needed:
                    msg = f"STOP RESET2: used {prev['used_percent']}->{cur['used_percent']} banked {prev['banked']}->{cur['banked']} reset_at {prev['reset_at']}->{cur['reset_at']}"
                    log(msg)
                    open(STOP_FILE, "w").write(msg + "\n")
                    if stop_mode == "codex-only":
                        # bot API /stop_tree per gpt chat; daemon UP; Claude/local untouched;
                        # tree-wide (incl children); agents idle, resumable later
                        import json as _j
                        try:
                            snap = _j.load(open("/workspace/runtime/state/crash-resume.json"))
                            chats = sorted({e["chat_id"] for e in snap.get("entries", []) if str(e.get("model", "")).startswith("gpt")})
                        except Exception as e:
                            chats = []
                            log(f"STOP codex-only: snapshot read error {e!r}")
                        for c in chats:
                            r = subprocess.run(["/workspace/runtime/bin/codex-stop-tree.sh.disabled", str(c)], capture_output=True, text=True, timeout=30)
                            log(f"STOP codex-only /stop_tree chat {c}: rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()}")
                        if not chats:
                            log("STOP codex-only: no gpt routes found; NO ACTION")
                    elif stop_mode == "resumable" and stop_action not in ("none",):
                        # fastest resumable: daemon UP, routes stay in crash-resume.json;
                        # operator (or hook) issues /stop_tree per gpt route:
                        #   chat_id -1003775167691 agent 2026-10-01-11-56-try-glm-tensorfold
                        log("STOP(resumable): send /stop_tree to gpt routes; daemon stays UP")
                        log(act("none", "STOP"))
                    else:
                        log(act(stop_action, "STOP"))
                    phase = "done"
                    open(PHASE_FILE, "w").write("done\n")
                    prev = cur
                    if not once:
                        json.dump({**cur, "phase1_go": True, "phase2_stop": True}, open(STATE_FILE, "w"))
                        return 0
        if cur:
            json.dump({**cur, "phase1_go": phase != "wait1", "phase2_stop": phase == "done"}, open(STATE_FILE, "w"))
            # simple status file for Mac notifier (v4)
            try:
                go_f = os.path.exists(GO_FILE)
                stop_f = os.path.exists(STOP_FILE)
                ph = "done" if (go_f and stop_f) else ("go-fired" if go_f else "wait1")
                with open(os.path.join(LOG_DIR, "STATUS"), "w") as f:
                    f.write(f"phase={ph} ts={cur['ts']} used_percent={cur['used_percent']} banked={cur['banked']}\n")
            except Exception:
                pass
            # Mac notifier status file: phase|ts|used%|banked
            with open(STATUS_FILE, "w") as f:
                f.write(f"phase={phase} ts={cur['ts']} used_percent={cur['used_percent']} banked={cur['banked']}\n")
            prev = cur
        # Blind status: probe failing >5min during fast window (Mac notifier alerts)
        if blind_since is not None and now >= fast_after and now <= fast_until:
            if now - blind_since > 300:
                try:
                    with open(STATUS_FILE, "a") as f:
                        f.write(f"blind=1 ts={dt.datetime.now(dt.UTC).isoformat()} secs={int(now-blind_since)}\n")
                except Exception:
                    pass
        if once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
