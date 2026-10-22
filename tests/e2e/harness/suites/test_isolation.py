"""obs-e2e isolation proof, lane side (design-v3 §2.1 probes (i)–(iii), plus the lane
halves of (v) and (vi)). The obs-test halves are tests/e2e/harness/isolation.py.

Each test writes its record to ``<run>/isolation/lane-<lane>.json``; the launcher
merges them into ``isolation/summary.json`` with the obs-test-side probes.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

pytestmark = [pytest.mark.suite("isolation"), pytest.mark.needs(all_lanes=True)]

# Production-only command lines that must never be visible inside a lane.
PRODUCTION_MARKERS = (
    "/etc/supervisor/supervisord.conf", "cli-proxy-api", "qdrant", "vault-search", "sshd",
    "/workspace/runtime/bin/obs-telegram-prod-wrapper.sh", "x11vnc", "websockify", "beads-ui",
    "desktop-commander", "chatgpt", "codex-reset-watcher", "spark-watchdog", "ops-loop-guard",
)
FORBIDDEN_ENV_PREFIXES = ("OBS_PROD_", "OBS_TEST_", "OBS_TELEGRAM_USERBOT_")
FORBIDDEN_ENV_NAMES = {"OBS_TELEGRAM_BOT_TOKENS", "OBS_CLI_RELEASES_FILE", "OBS_PROFILE_FORCED_BY_CALLER"}
SUPERVISOR_ADDS = {"SUPERVISOR_ENABLED", "SUPERVISOR_PROCESS_NAME", "SUPERVISOR_GROUP_NAME", "SUPERVISOR_SERVER_URL"}
PROGRAM_ADDS = {"PYTHONPATH", "OBS_E2E_CODE_DIR"}
WRAPPER_ADDS = {"OBS_KILLSWITCH_SENTINEL"}
SHELL_ADDS = {"PWD", "OLDPWD", "SHLVL", "_"}
AGENT_CLI_MARKER = "claude_agent_sdk/_bundled/claude"


def _record(lane, key: str, value: dict) -> None:
    path = lane.run_dir / "isolation" / f"lane-{lane.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = value
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _args(proc: dict) -> str:
    args = proc.get("args")
    return " ".join(args) if isinstance(args, list) else str(args or "")


async def test_i_lane_sees_only_its_own_processes(lane):
    procs = await lane.procs()
    by_pid = {int(p["pid"]): p for p in procs}
    pid1 = _args(by_pid.get(1, {}))
    orphans_of_tree = [p for p in procs if int(p["pid"]) != 1 and int(p.get("ppid", 0)) not in by_pid]
    production_hits = [
        {"pid": p["pid"], "args": _args(p)[:160]} for p in procs
        if any(marker in _args(p) for marker in PRODUCTION_MARKERS)
    ]
    passed = ("docker-init" in pid1 or "tini" in pid1) and not orphans_of_tree and not production_hits and len(procs) < 1000
    _record(lane, "i", {"pass": passed, "process_count": len(procs), "pid1": pid1,
                        "outside_tree": orphans_of_tree, "production_hits": production_hits,
                        "commands": sorted({_args(p).split(" ")[0] for p in procs})})
    assert passed, {"pid1": pid1, "outside_tree": orphans_of_tree, "production_hits": production_hits}


async def test_ii_host_pid_is_unreachable(lane):
    host_pid = lane.host_sentinel()
    if host_pid is None:
        pytest.fail("run has no host sentinel (launcher starts it when the isolation suite is selected)")
    in_lane = {int(p["pid"]): p for p in await lane.procs()}
    collision = in_lane.get(host_pid)
    probe = await lane._ctl.call("POST", "/probe/kill_tree", {"pid": host_pid}, timeout=60)
    orphans = await lane._ctl.call("POST", "/probe/find_orphans", {}, timeout=60)

    # Control: the same kill path does kill a marker process that IS in the lane.
    marker_argv0 = f"/tmp/obs-e2e-control-{uuid.uuid4().hex[:6]}/{AGENT_CLI_MARKER}"
    bg = await lane._ctl.call("POST", "/exec", {"argv": ["bash", "-c", f'exec -a "{marker_argv0}" sleep 600'],
                                                 "background": True, "timeout": 30}, timeout=60)
    control_pid = int(bg["pid"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # wait for exec -a to replace argv (process state, harness-local)
        procs = {int(p["pid"]): p for p in await lane.procs()}
        if AGENT_CLI_MARKER in _args(procs.get(control_pid, {})):
            break
    control = await lane._ctl.call("POST", "/probe/kill_tree", {"pid": control_pid}, timeout=60)
    after = {int(p["pid"]) for p in await lane.procs()}
    control_killed = control_pid not in after

    lane_pids = set(in_lane)
    orphan_pids = set(int(p) for p in (orphans.get("result") or [])) if isinstance(orphans.get("result"), list) else set()
    passed = (
        collision is None
        and not (isinstance(probe.get("result"), list) and probe["result"])
        and orphan_pids <= lane_pids | after
        and control_killed
    )
    _record(lane, "ii", {"pass": passed, "host_sentinel_pid": host_pid, "pid_collision_in_lane": bool(collision),
                         "kill_tree_host_pid": probe, "find_orphans": orphans,
                         "control_pid": control_pid, "control_kill_tree": control, "control_killed": control_killed})
    assert passed, {"probe": probe, "orphans": orphans, "control": control, "control_killed": control_killed}


async def test_iii_lane_env_is_the_allow_list(lane):
    names_by_pid = await lane.envnames()
    procs = {int(p["pid"]): p for p in await lane.procs()}
    allow = set(lane.info["env_names"]) | {"HOSTNAME"}
    canary = lane.manifest.get("caller_canary")
    findings = []
    per_kind: dict[str, set[str]] = {}
    lanectl_exact = None
    for pid_text, names in names_by_pid.items():
        pid = int(pid_text)
        names = set(names)
        args = _args(procs.get(pid, {}))
        kind = (
            "lanectl" if "tests.e2e.harness.lanectl" in args
            else "supervisord" if "supervisord" in args
            else "relay" if "tests.e2e.harness.relay" in args
            else "wrapper" if "lane-obs-wrapper.sh" in args
            else "telegram_main" if "obs_agent.telegram_main" in args
            else "cache_proxy" if "cache_proxy.py" in args
            else "agent_cli" if AGENT_CLI_MARKER in args
            else "init" if pid == 1
            else "other"
        )
        forbidden = sorted(n for n in names if n.startswith(FORBIDDEN_ENV_PREFIXES) or n in FORBIDDEN_ENV_NAMES)
        if canary and canary in names:
            forbidden.append(canary)
        if forbidden:
            findings.append({"pid": pid, "kind": kind, "forbidden": forbidden})
        extra = names - allow
        if kind == "lanectl":
            lanectl_exact = names == allow
            if not lanectl_exact:
                findings.append({"pid": pid, "kind": kind, "missing": sorted(allow - names), "extra": sorted(extra)})
        elif kind in ("supervisord", "relay"):
            bad = extra - SUPERVISOR_ADDS - PROGRAM_ADDS
            if bad:
                findings.append({"pid": pid, "kind": kind, "unexpected": sorted(bad)})
        elif kind in ("wrapper", "telegram_main", "cache_proxy"):
            bad = extra - SUPERVISOR_ADDS - PROGRAM_ADDS - WRAPPER_ADDS - SHELL_ADDS
            if bad:
                findings.append({"pid": pid, "kind": kind, "unexpected": sorted(bad)})
        per_kind.setdefault(kind, set()).update(extra)
    passed = lanectl_exact is True and not findings
    _record(lane, "iii", {"pass": passed, "allow_list": sorted(allow), "lanectl_equals_allow_list": lanectl_exact,
                          "caller_canary": canary, "findings": findings,
                          "extra_names_by_kind": {k: sorted(v) for k, v in per_kind.items()},
                          "processes": len(names_by_pid)})
    assert passed, findings or {"lanectl_equals_allow_list": lanectl_exact}


@pytest.mark.needs(all_lanes=False, credential="dev")
async def test_v_expired_lane_token_does_not_touch_production(lane):
    """Expire this lane's own token copy, then run a turn on it (probe (v), lane half)."""
    script = (
        "import json,time,os;p=os.path.expanduser('~/.claude/.credentials.json');d=json.load(open(p));"
        "d['claudeAiOauth']['expiresAt']=int((time.time()-3600)*1000);"
        "t=p+'.tmp';open(t,'w').write(json.dumps(d));os.replace(t,p);print('expired')"
    )
    res = await lane.exec("/workspace/obs/.venv/bin/python", "-c", script, timeout=30)
    assert res.get("rc") == 0, res
    topic = await lane.new_topic()
    sent = await lane.send(topic, "Reply with exactly: TOKEN-PROBE")
    end = await lane.wait("turn_end", match={"trigger_message_id": sent.message_id})
    _record(lane, "v_lane", {"expired_copy": res.get("stdout", "").strip(), "turn_failed": end.get("failed"),
                             "turn_subtype": end.get("subtype")})


@pytest.mark.needs(all_lanes=False)
async def test_vi_prod_send_is_update_free(lane):
    """Exercise the runner-side prod-send profile (update-free calls only), once per run."""
    if not lane.manifest.get("prod_send"):
        pytest.fail("this isolation run lacks --prod-send; probe (vi) must exercise prod-send")
    rpc = lane._rpc
    topic = await rpc.call("POST", "/prodsend/topic/new", {"title": f"obs-e2e prod-send {lane.manifest['run_id']}"})
    me = await rpc.call("POST", "/botapi", {"method": "getMe", "params": {}})
    sent = await rpc.call("POST", "/botapi", {"method": "sendMessage", "params": {
        "chat_id": topic["chat_id"], "message_thread_id": topic["topic_id"],
        "text": f"obs-e2e prod-send render check {lane.manifest['run_id']}"}})
    message_id = (sent.get("result") or {}).get("message_id")
    edited = await rpc.call("POST", "/botapi", {"method": "editMessageText", "params": {
        "chat_id": topic["chat_id"], "message_id": message_id, "text": "obs-e2e prod-send render check (edited)"}})
    deleted = await rpc.call("POST", "/botapi", {"method": "deleteMessage", "params": {
        "chat_id": topic["chat_id"], "message_id": message_id}})
    refused = None
    try:
        await rpc.call("POST", "/botapi", {"method": "getUpdates", "params": {"timeout": 0}})
    except RuntimeError as exc:
        refused = str(exc)[:200]
    closed = await rpc.call("POST", "/prodsend/topic/close", {"topic_id": topic["topic_id"]})
    ok = bool(me.get("ok")) and bool(sent.get("ok")) and bool(edited.get("ok")) and bool(deleted.get("ok")) and refused
    _record(lane, "vi_lane", {"pass": bool(ok), "topic": topic, "getMe_ok": me.get("ok"), "sendMessage_ok": sent.get("ok"),
                              "edit_ok": edited.get("ok"), "delete_ok": deleted.get("ok"),
                              "getUpdates_refused": refused, "topic_closed": closed})
    assert ok, {"me": me.get("ok"), "sent": sent.get("ok"), "edited": edited.get("ok"), "deleted": deleted.get("ok"), "refused": refused}
