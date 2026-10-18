"""DGX Sparks liveness watchdog (bead obs-8, 2026-10-18).

The native TensorFold ``/health`` keeps answering ``ok: true`` while the head is
wedged: on 2026-10-18 the worker rank hit a GPU OOM at 02:23Z and crash-looped,
the head blocked in its submit queue, and ``/health`` reported a frozen
``requests_running`` for seven hours while every local agent hung. On
2026-10-17 the head itself crash-looped for seven hours. Both times a human-run
leased paired restart fixed it in minutes.

This daemon (supervisor program ``spark-watchdog`` on obs-test) therefore:

- judges liveness by *progress*, not by ``ok``: with requests running, the
  engine's progress counters (decode rounds, finished requests, prompt-chunk
  fetches, grouped prefill rows, spill writes/loads) must move. Production
  telemetry (2026-10-17/18, 15 s samples) never showed them still for more than
  225 s while healthy, including during a 935k-token cold prefill, so
  ``STALL_SECONDS`` = 600 is wedged with margin;
- treats an unreachable / non-ok ``/health`` for ``DOWN_SECONDS`` and a rank
  whose Docker ``RestartCount`` keeps climbing as down;
- publishes its verdict to ``STATE_FILE``, which the Spark cache proxy reads
  (``OBS_LOCAL_LIVENESS_FILE``): it fails in-flight requests and holds new ones
  while the upstream is bad, and shows the verdict in its ``/health``;
- runs the Runbook's leased paired restart itself (exact-owner no-clobber lease
  ``/data/runtime/spark.lock``, external-spill check, current recipe,
  ``--spill-keep-builds 0``, all three aliases, ``unless-stopped`` on both
  ranks) and never acts while another owner holds the lease or a controller
  transition is in progress.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

STATE_FILE = Path(os.environ.get("OBS_SPARK_LIVENESS_FILE",
                                 "/workspace/runtime/state/spark-liveness/state.json"))
EVENTS_FILE = STATE_FILE.with_name("events.jsonl")
HEALTH_URL = os.environ.get("OBS_SPARK_NATIVE_HEALTH", "http://192.168.1.241:8888/health")
CONTROLLER = os.environ.get(
    "OBS_SPARK_CONTROLLER",
    "/workspace/runtime/spark/tensorfold/normal-ablit-20261005/spark-tensorfold-mode.py")
POLL_SECONDS = 15.0
STALL_SECONDS = 600.0
DOWN_SECONDS = 300.0
CRASHLOOP_RESTARTS = 2          # RestartCount growth within CRASHLOOP_WINDOW
CRASHLOOP_WINDOW = 600.0
RANK_POLL_SECONDS = 60.0
RESTART_HEALTHY_TIMEOUT = 1200.0
# Host memory (2026-10-18 investigation): both incidents were a kernel OOM after
# MemAvailable on one Spark eroded from ~7 GiB after a fresh start to zero over
# ~30-40 h (driver/unified-memory growth invisible in RSS, ~7-20 GiB/day), then
# a crash loop. A restart frees it, so recycle before it runs out: at the next
# idle moment below MEM_RECYCLE_GIB, at once below MEM_FORCE_GIB.
MEM_RECYCLE_GIB = 3.0
MEM_FORCE_GIB = 1.0
BACKOFF_BASE = 600.0            # gap before the 2nd restart within 6h; doubles after
BACKOFF_MAX = 3600.0
ALIASES = ("local-sparks-glm-5.3-flash-exl3-abliterated",
           "local-sparks-glm-5.3-flash-uncensored",
           "local-sparks-glm-5.3-flash-exl3-4bpw")
BAD_STATES = ("wedged", "down", "restarting", "failed")


def _utc(ts: float | None = None) -> str:
    return _dt.datetime.fromtimestamp(time.time() if ts is None else ts,
                                      _dt.timezone.utc).isoformat()


def progress_vector(health: dict) -> tuple:
    """Counters that move whenever the engine does work (see module docstring)."""
    def g(*keys):
        d = health
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d
    return (g("rounds_total"), g("requests_total"), g("completion_tokens_total"),
            g("kv_shard", "fetch", "rows"), g("multi_prefill", "rows"),
            g("spill", "written_blocks"), g("spill", "loaded"))


@dataclass
class Verdict:
    state: str          # healthy | suspect | wedged | down | recycle | maintenance
    reason: str


@dataclass
class Detector:
    """Pure liveness judgement from successive observations."""

    stall_seconds: float = STALL_SECONDS
    down_seconds: float = DOWN_SECONDS
    _vector: tuple | None = None
    _progress_at: float | None = None
    _down_since: float | None = None
    _rank_history: dict = field(default_factory=dict)   # host -> [(t, count)]
    _mem: dict = field(default_factory=dict)            # host -> MemAvailable GiB

    def reset(self, now: float) -> None:
        self._vector = None
        self._progress_at = now
        self._down_since = None
        self._rank_history.clear()
        self._mem.clear()

    def observe_ranks(self, now: float, ranks: dict[str, dict | None]) -> None:
        for host, info in ranks.items():
            if info and isinstance(info.get("mem_available_gib"), (int, float)):
                self._mem[host] = float(info["mem_available_gib"])
            if not info or not isinstance(info.get("restart_count"), int):
                continue
            hist = self._rank_history.setdefault(host, [])
            hist.append((now, info["restart_count"]))
            while hist and now - hist[0][0] > CRASHLOOP_WINDOW:
                hist.pop(0)

    def _crashloop(self) -> str | None:
        for host, hist in self._rank_history.items():
            if len(hist) >= 2 and hist[-1][1] - hist[0][1] >= CRASHLOOP_RESTARTS:
                return (f"{host} container restarted {hist[-1][1] - hist[0][1]} times "
                        f"in {hist[-1][0] - hist[0][0]:.0f}s")
        return None

    def observe(self, now: float, health: dict | None, *, maintenance: str | None = None) -> Verdict:
        if maintenance:
            self.reset(now)
            return Verdict("maintenance", maintenance)
        loop = self._crashloop()
        if loop:
            return Verdict("down", "crash loop: " + loop)
        ok = isinstance(health, dict) and health.get("ok") is True
        if not ok:
            if self._down_since is None:
                self._down_since = now
            gone = now - self._down_since
            why = "native /health unreachable" if health is None else "native /health not ok"
            if gone >= self.down_seconds:
                return Verdict("down", f"{why} for {gone:.0f}s")
            return Verdict("suspect", f"{why} for {gone:.0f}s")
        self._down_since = None
        vector = progress_vector(health)
        running = health.get("requests_running")
        if self._mem:
            host, low = min(self._mem.items(), key=lambda kv: kv[1])
            if low < MEM_FORCE_GIB:
                return Verdict("recycle", f"{host} MemAvailable {low:.1f} GiB < {MEM_FORCE_GIB} GiB "
                                          f"(kernel OOM imminent); recycling now")
            if low < MEM_RECYCLE_GIB and not running:
                return Verdict("recycle", f"{host} MemAvailable {low:.1f} GiB < {MEM_RECYCLE_GIB} GiB; "
                                          f"recycling while idle")
        if vector != self._vector or self._progress_at is None or not running:
            self._vector = vector
            self._progress_at = now
            return Verdict("healthy", f"requests_running={running}")
        still = now - self._progress_at
        if still >= self.stall_seconds:
            return Verdict("wedged", f"{running} requests running but no engine progress "
                                     f"for {still:.0f}s (/health still ok)")
        if still >= 2 * POLL_SECONDS:
            return Verdict("suspect", f"no engine progress for {still:.0f}s, "
                                      f"requests_running={running}")
        return Verdict("healthy", f"requests_running={running}")


def write_state(state: str, reason: str, *, since: float | None = None, **extra) -> dict:
    prior = read_state() or {}
    now = time.time()
    if since is None:
        since = prior.get("since") if prior.get("state") == state else now
    doc = {"state": state, "reason": reason, "since": since, "since_utc": _utc(since),
           "updated": now, "updated_utc": _utc(now), "pid": os.getpid(), **extra}
    for key in ("restarts", "last_restart", "last_restart_result"):
        if key not in doc and key in prior:
            doc[key] = prior[key]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.replace(STATE_FILE)
    if prior.get("state") != state:
        event("state", state=state, reason=reason, previous=prior.get("state"))
    return doc


def read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def event(kind: str, **fields) -> None:
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a") as f:
        f.write(json.dumps({"utc": _utc(), "event": kind, **fields}) + "\n")
    print(f"{_utc()} {kind} {json.dumps(fields)}", flush=True)


def load_controller():
    spec = importlib.util.spec_from_file_location("spark_tensorfold_mode", CONTROLLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def native_health(timeout: float = 10.0) -> dict | None:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


_RANK_INFO = ("import subprocess,json\n"
              "m=next((int(l.split()[1])/1048576 for l in open('/proc/meminfo') if l.startswith('MemAvailable:')),None)\n"
              "try:\n"
              " x=json.loads(subprocess.check_output(['docker','inspect','glm53-flash-tf']))[0]\n"
              " print(json.dumps({'restart_count':x.get('RestartCount'),'status':x['State'].get('Status'),"
              "'restarting':x['State'].get('Restarting'),'mem_available_gib':m}))\n"
              "except Exception as e:\n print(json.dumps({'error':type(e).__name__,'mem_available_gib':m}))\n")
_LEASE_INFO = ("import json,pathlib,time\n"
               "p=pathlib.Path('/data/runtime/spark.lock')\n"
               "print(json.dumps({'held':p.exists(),'age':time.time()-p.stat().st_mtime if p.exists() else None,"
               "'owner':(json.loads(p.read_text()).get('owner') if p.exists() else None)}))\n")
_TRANSITION_INFO = ("import json,pathlib,sys\n"
                    "p=pathlib.Path(sys.argv[1]+'/state/control-transition.json')\n"
                    "print(p.read_text() if p.exists() else '{}')\n")


class Sparks:
    """The impure side: SSH probes and the leased paired restart."""

    def __init__(self):
        self.ctl = load_controller()

    def _py(self, host: str, script: str, *args: str, timeout: float = 30.0) -> dict | None:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-c",
               shlex.quote(script), *[shlex.quote(a) for a in args]]
        try:
            out = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=True).stdout
            return json.loads(out)
        except Exception:
            return None

    def ranks(self) -> dict[str, dict | None]:
        return {h: self._py(h, _RANK_INFO) for h in (self.ctl.HEAD, self.ctl.WORKER)}

    def maintenance(self) -> str | None:
        """Another owner's lease or a controller transition in progress -> hands off."""
        lease = self._py(self.ctl.HEAD, _LEASE_INFO)
        if (lease and lease.get("held") and str(lease.get("owner") or "").startswith("spark-watchdog-")
                and (lease.get("age") or 0) > 2400):
            # Our own program's lease, left by a watchdog process that died mid-restart.
            self._py(self.ctl.HEAD, "import pathlib;pathlib.Path('/data/runtime/spark.lock').unlink();print('{}')")
            event("lease", owner=lease.get("owner"), action="removed stale watchdog lease")
            return None
        if lease and lease.get("held"):
            return f"lease /data/runtime/spark.lock held by {lease.get('owner')} for {lease.get('age') or 0:.0f}s"
        tr = self._py(self.ctl.HEAD, _TRANSITION_INFO, self.ctl.BASE) or {}
        if tr.get("state") == "reloading" and tr.get("by") != "spark-watchdog":
            return "controller transition in progress (reloading)"
        return None

    def restart(self, reason: str) -> tuple[bool, str]:
        ctl = self.ctl
        owner = "spark-watchdog-" + uuid.uuid4().hex
        acquire = ("import pathlib,os,json,sys,time\n"
                   "p=pathlib.Path('/data/runtime/spark.lock')\n"
                   "f=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)\n"
                   "os.write(f,json.dumps({'owner':sys.argv[1],'purpose':'spark-watchdog automatic paired restart: '+sys.argv[2],"
                   "'created':time.time()}).encode());os.close(f)\nprint('{}')\n")
        if self._py(ctl.HEAD, acquire, owner, reason[:200]) is None:
            return False, "could not take /data/runtime/spark.lock (held by another owner or head unreachable)"
        event("lease", owner=owner, action="acquired")
        try:
            for host in (ctl.HEAD, ctl.WORKER):
                if self._py(host, ctl.STORAGE, "head" if host == ctl.HEAD else "worker") is None:
                    return False, f"{host}: external spill mount unavailable; not restarting onto internal disk"
            try:
                mode = ctl.status().get("loaded_mode") or "off"
            except Exception:
                mode = "off"
            if mode not in ("on", "off"):
                mode = "off"
            run = ("import pathlib,subprocess,json,os,sys,datetime\n"
                   "b=pathlib.Path(sys.argv[1]);mode=sys.argv[2];state=b/'state/control-transition.json'\n"
                   "env=os.environ.copy();env.update(ABLIT='1' if mode=='on' else '0',THINKING='1',PREPARE='0',LOG_KEEP='100000',"
                   "SERVED_NAME='local-sparks-glm-5.3-flash-exl3-4bpw'+('-ablit' if mode=='on' else ''))\n"
                   "if mode=='on':env['HF_TOKEN']=(b/'hf-read-token').read_text().strip()\n"
                   "def write(d):\n t=state.with_suffix('.tmp');t.write_text(json.dumps(d));t.replace(state)\n"
                   "now=lambda:datetime.datetime.now(datetime.timezone.utc).isoformat()\n"
                   "write({'state':'reloading','by':'spark-watchdog','desired':mode,'default':'off','reason':sys.argv[3],'started_utc':now()})\n"
                   "log=b/'logs'/('watchdog-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.log')\n"
                   "with log.open('w') as f:r=subprocess.run(['bash','./start.sh','restart','--spill-keep-builds','0',"
                   + ",".join(f"'--alias','{a}'" for a in ALIASES) +
                   "],cwd=b/'recipe',env=env,stdout=f,stderr=subprocess.STDOUT)\n"
                   "write({'state':'healthy' if r.returncode==0 else 'failed','by':'spark-watchdog','desired':mode,'default':'off',"
                   "'returncode':r.returncode,'log':str(log),'finished_utc':now()})\n"
                   "print(json.dumps({'returncode':r.returncode,'log':str(log)}))\n")
            event("restart", mode=mode, reason=reason)
            result = self._py(ctl.HEAD, run, ctl.BASE, mode, reason[:300], timeout=1800)
            for host in (ctl.HEAD, ctl.WORKER):
                self._py(host, "import subprocess;subprocess.run(['docker','update','--restart','unless-stopped',"
                               "'glm53-flash-tf'],check=True,stdout=subprocess.DEVNULL);print('{}')")
            if not result or result.get("returncode") != 0:
                return False, f"start.sh restart failed: {result}"
            deadline = time.time() + RESTART_HEALTHY_TIMEOUT
            while time.time() < deadline:
                h = native_health()
                if h and h.get("ok") is True and isinstance(h.get("requests_running"), int):
                    return True, f"restarted ({mode}); start.sh smoke passed; log {result.get('log')}"
                time.sleep(POLL_SECONDS)
            return False, "restart finished but native /health never became ok"
        finally:
            release = ("import pathlib,json,sys\np=pathlib.Path('/data/runtime/spark.lock')\n"
                       "d=json.loads(p.read_text()) if p.exists() else {}\n"
                       "if d.get('owner')==sys.argv[1]:p.unlink()\nprint('{}')\n")
            self._py(ctl.HEAD, release, owner)
            event("lease", owner=owner, action="released")


def restart_publishing(sparks, reason: str, **extra) -> tuple[bool, str]:
    """Run the restart while re-publishing "restarting" every POLL_SECONDS.

    The proxy ignores a verdict older than 120 s, and a paired restart takes
    minutes; without this refresh it would forward held requests to the dead
    upstream halfway through. One writer only: the restart runs in a thread.
    """
    result: dict = {}

    def work():
        try:
            result["r"] = sparks.restart(reason)
        except Exception as exc:          # never leave the verdict stuck
            result["r"] = (False, f"restart raised {type(exc).__name__}: {exc}")
    write_state("restarting", reason, **extra)
    t = threading.Thread(target=work, daemon=True, name="spark-restart")
    t.start()
    while t.is_alive():
        write_state("restarting", reason, **extra)
        t.join(POLL_SECONDS)
    return result.get("r", (False, "restart thread ended without a result"))


def run_forever(sparks: Sparks | None = None, detector: Detector | None = None,
                *, act: bool = True, iterations: int | None = None,
                clock=time.time, sleep=time.sleep, health=None) -> None:
    sparks = sparks or Sparks()
    det = detector or Detector()
    health = health or native_health
    det.reset(clock())
    restarts = (read_state() or {}).get("restarts", [])
    last_rank_poll = float("-inf")
    write_state("healthy", "watchdog started")
    n = 0
    while iterations is None or n < iterations:
        n += 1
        now = clock()
        maint = sparks.maintenance()
        if now - last_rank_poll >= RANK_POLL_SECONDS:
            det.observe_ranks(now, sparks.ranks())
            last_rank_poll = now
        verdict = det.observe(now, health(), maintenance=maint)
        if verdict.state in ("wedged", "down", "recycle") and act:
            recent = [r for r in restarts if now - r["t"] < 6 * 3600]
            gap = 0.0 if not recent else min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (len(recent) - 1))
            if recent and now - recent[-1]["t"] < gap:
                write_state("failed", f"{verdict.reason}; next automatic restart in "
                                      f"{gap - (now - recent[-1]['t']):.0f}s", restarts=recent)
            else:
                write_state("restarting", verdict.reason, restarts=recent)
                ok, detail = restart_publishing(sparks, verdict.reason, restarts=recent)
                recent.append({"t": clock(), "utc": _utc(), "ok": ok, "trigger": verdict.reason,
                               "detail": detail})
                restarts = recent[-20:]
                event("restart_result", ok=ok, detail=detail)
                write_state("healthy" if ok else "failed", detail, restarts=restarts,
                            last_restart=_utc(), last_restart_result=detail)
                det.reset(clock())
                last_rank_poll = float("-inf")
                continue
        else:
            write_state(verdict.state, verdict.reason)
        sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--observe-only", action="store_true",
                   help="publish verdicts but never restart")
    p.add_argument("--once", action="store_true", help="print one probe and exit")
    p.add_argument("--restart-now", metavar="REASON",
                   help="run the automatic leased paired restart once (a drill), publishing states")
    a = p.parse_args(argv)
    if a.restart_now:
        write_state("restarting", "manual: " + a.restart_now)
        ok, detail = restart_publishing(Sparks(), "manual: " + a.restart_now)
        event("restart_result", ok=ok, detail=detail, manual=True)
        write_state("healthy" if ok else "failed", detail, last_restart=_utc(), last_restart_result=detail)
        print(json.dumps({"ok": ok, "detail": detail}))
        return 0 if ok else 1
    if a.once:
        s = Sparks()
        print(json.dumps({"health": native_health(), "ranks": s.ranks(),
                          "maintenance": s.maintenance(), "state": read_state()}, indent=1))
        return 0
    run_forever(act=not a.observe_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
