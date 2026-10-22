"""Extract an OBS identity and execute exactly one isolated cold-notice scenario."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[1]
HARNESS = Path("/workspace/obs/worktrees/obs-22-e1")
IMAGE = "obs-e2e:b4f1276048e1"
BASE = "6b3136cf02c8655e8bedc45f371e9bb579851dd6"
PYTHON = "/workspace/obs/.venv/bin/python"
ROOT = Path("/workspace/runtime/e2e/ruoi")
VAULT_OPS = Path("/workspace/runtime/git/obs-vault-active/Projects/Personal Projects/Agentic/Agent Operations/scripts/ops")
SCENARIOS = ("1", "2", "3", "3b-i", "3b-ii", "4", "5a", "5b", "5c", "6", "7-human", "7-inbox", "8")


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", "-C", str(WORKTREE), *args], text=True).strip()


def docker_module():
    spec = importlib.util.spec_from_file_location("ruoi_docker_helper", HARNESS / "tests/e2e/harness/docker.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def expression(node, context):
    """Evaluate only data-producing AST forms used by the three source argv lists."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return context[node.id]
    if isinstance(node, ast.List):
        return [expression(v, context) for v in node.elts]
    if isinstance(node, ast.JoinedStr):
        return "".join(str(expression(v, context)) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        value = expression(node.value, context)
        return repr(value) if node.conversion == 114 else str(value)
    if isinstance(node, ast.Subscript):
        return expression(node.value, context)[expression(node.slice, context)]
    if isinstance(node, ast.Slice):
        return slice(*(expression(v, context) if v else None for v in (node.lower, node.upper, node.step)))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return expression(node.left, context) + expression(node.right, context)
    if isinstance(node, ast.IfExp):
        return expression(node.body if expression(node.test, context) else node.orelse, context)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not expression(node.operand, context)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "join"):
        value = expression(node.func.value, context)
        return getattr(value, node.func.attr)(*(expression(arg, context) for arg in node.args))
    raise ValueError(f"Unsupported source data expression: {ast.dump(node)}")


def control_fixture(site, source_dir):
    names = {"rounds-cleanup": ("rounds.py", "notify_item_cleanup"),
             "rounds-failure": ("rounds.py", "notify_item_failure"),
             "post-reboot": ("post_reboot.py", "post")}
    filename, function = names[site]
    path = source_dir / filename
    raw = path.read_text()
    tree = ast.parse(raw, filename=str(path))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
    context = {"AGENTCTL": "obs-agentctl", "HOST_TEAM": "2026-10-22-18-00-ruoi-e2e", "REGISTRY_ROOT": "ruoi-parent",
               "team": "2026-10-22-18-00-ruoi-e2e", "to": "ruoi-parent", "bead": "obs-22", "key": "2026-10-22-18-00-ruoi-e2e",
               "reason": "isolated reproduction", "bad": [], "text": "RUOI isolated boot result",
               "child": {"agent_name": "isolated-child", "host": "ruoi-parent", "stop_due": "fixture-stop",
                         "stop_requested_at": "fixture-request", "cleanup_observed_at": "fixture-cleanup",
                         "launch_parent_message_id": 1, "launch_at": "fixture-launch", "hard_deadline": "fixture-deadline"}}
    for node in fn.body:
        if isinstance(node, ast.Assign):
            targets = [n.id for n in node.targets if isinstance(n, ast.Name)]
            if any(n in ("text", "summary") for n in targets):
                value = expression(node.value, context)
                for target in targets:
                    context[target] = value
    lists = [n for n in ast.walk(fn) if isinstance(n, ast.List) and any(
        isinstance(v, ast.Constant) and v.value == "message" for v in n.elts)]
    if len(lists) != 1:
        raise ValueError(f"Expected one message argv in {function}, found {len(lists)}")
    values = expression(lists[0], context)
    original = list(values)
    for flag, replacement in (("--team", "2026-10-22-18-00-ruoi-e2e"), ("--to", "ruoi-parent")):
        values[values.index(flag) + 1] = replacement
    argv = [PYTHON, "-m", "obs_agent.control", *values[1:]]
    return {"site": site, "source": str(path), "source_sha256": digest(path),
            "function": function, "line": lists[0].lineno,
            "argv_expression": ast.get_source_segment(raw, lists[0]),
            "original_argv": original, "argv": argv,
            "fixture_context": context, "source_imported": False}


def environment(run_id):
    r = "/run/ruoi"
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": r + "/home", "LANG": "C.UTF-8",
        "PYTHONPATH": r + "/code/src:/drivers", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:29181", "ANTHROPIC_API_KEY": "ruoi-e2e-dummy-key",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "OBS_CACHE_READINESS_TTLS": "claude=20",
        "OBS_CONTROL_DISABLE": "0", "OBS_CONTROL_DIR": r + "/control", "OBS_STATE_DIR": r + "/state",
        "OBS_SESSION_REPLAY_PROVENANCE_DIR": r + "/replay", "OBS_JSONL_TORN_BACKUP_DIR": r + "/torn",
        "OBS_START_STEP_LOG": r + "/start-step.jsonl", "OBS_SCHEDULE_OWNERS_FILE": r + "/schedule-owners.json",
        "OBS_BEADS_ISSUES_FILE": r + "/issues.jsonl", "OPS_FREEZE_STATE": r + "/freezes.json",
        "OBS_RUNNING_CODE_FILE": r + "/running-code.json", "OBS_CACHE_GATE_LOG": r + "/cache-gate-bypass.jsonl",
        "OBS_SPARK_LIVENESS_FILE": r + "/spark-liveness.json", "OBS_KILLSWITCH_SENTINEL": r + "/state/killswitch",
        "OBS_SHUTDOWN_SENTINEL": r + "/state/shutdown", "OBS_ORPHAN_CLI_REAP": "0",
        "OBS_STOP_KILL_GRACE_SECONDS": "20", "OBS_LOOP_STALL_DISABLE": "1",
        "OBS_RUNTIME_LOG_FILE": r + "/daemon.log", "OBS_TELEGRAM_LOG_FILE": r + "/daemon.log",
        "RUOI_E2E_CONTAINER": run_id,
    }


def summarize(root, manifest, returncode, error):
    events = []
    for line in (root / "container.log").read_text(errors="replace").splitlines():
        if line.startswith("E2E "):
            events.append(json.loads(line[4:]))
    (root / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    stub_path = root / "stub.jsonl"
    stub = [json.loads(v) for v in stub_path.read_text().splitlines()] if stub_path.exists() else []
    daemon_path = root / "daemon.log"
    lines = daemon_path.read_text(errors="replace").splitlines() if daemon_path.exists() else []
    key_lines = [line for line in lines if any(k in line for k in (
        "Auto-delivering queued updates route=", "[run_and_send] preflight route=",
        "[cache_readiness] held ", "[cache_readiness] blocked inbox wake"))]
    final = next((e for e in reversed(events) if e.get("event") == "final"), {})
    summary = {**manifest, "end_utc": utc(), "container_exit_code": returncode, "host_error": error,
               "harness_ok": returncode == 0 and bool(final), "events": events,
               "raw_log_paths": {k: str(root / v) for k, v in (
                   ("container", "container.log"), ("daemon", "daemon.log"), ("stub", "stub.jsonl"),
                   ("events", "events.jsonl"), ("telegram", "telegram.jsonl"))},
               "key_log_lines": key_lines,
               "held_marker_count": sum("[cache_readiness] held " in line for line in lines),
               "blocked_inbox_marker_count": sum("[cache_readiness] blocked inbox wake" in line for line in lines),
               "stub_usage_is_not_billing": True, "final": final}
    notice_events = [e for e in events if e.get("event") in ("notice", "message_boundary", "bounce_boundary", "control_boundary")]
    result_observations = []
    for event in notice_events:
        recipient = event.get("recipient", "parent")
        when = event.get("notice_at", event["at"])
        boundaries = [e for e in events if e is not event and e.get("recipient", "parent") == recipient
                      and e.get("event") in ("notice", "message_boundary", "bounce_boundary", "control_boundary", "human_wake_boundary")
                      and e["at"] > event["at"]]
        next_boundary = min(boundaries, key=lambda e: e["at"]) if boundaries else None
        cutoff = next_boundary["at"] if next_boundary else final.get("at", float("inf"))
        later = [row for row in stub if row.get("role") == recipient and row.get("kind") != "count_tokens" and when <= row.get("t", 0) < cutoff]
        snapshot = event.get("snapshot", {})
        end_snapshot = next_boundary.get("snapshot", {}) if next_boundary else final.get("agents", {}).get(recipient, {})
        before_count = snapshot.get("jsonl", {}).get("rows", 0)
        end_count = end_snapshot.get("jsonl", {}).get("rows", 0)
        starts = [e for e in events if e.get("event") == "delivery_log_boundary" and
                  e.get("recipient") == recipient and when <= e["at"] < cutoff and "[run_and_send] preflight route=" in e.get("line", "")]
        held_rows = [row for row in end_snapshot.get("inbox", []) if row.get("wake") is False]
        outcome = "new-recipient-turn" if starts else "running-turn-continuation" if later else "held-no-request" if held_rows else "no-recipient-request"
        result_observations.append({**event, "recipient_requests_after": len(later),
                                    "recipient_request_sizes_after": [r["bytes"] for r in later],
                                    "recipient_turn_started": bool(starts), "new_turn_preflight_lines": [e["line"] for e in starts],
                                    "observed_outcome": outcome, "observation_end_at": cutoff,
                                    "observation_end_event": next_boundary.get("event") if next_boundary else "final",
                                    "recipient_jsonl_new_rows": max(0, end_count - before_count),
                                    "recipient_jsonl_new_row_types": end_snapshot.get("jsonl", {}).get("row_types", [])[before_count:],
                                    "recipient_request_numbers_after": [r["n"] for r in later]})
    summary["observations"] = result_observations
    summary["tool_results_seen"] = [{"request": row.get("n"), "role": row.get("role"),
                                      "results": row["tool_results"]} for row in stub if row.get("tool_results")]
    summary["hint_lines"] = [v for row in stub for v in row.get("hint_lines", [])]
    summary["harness_faults"] = [r for r in stub if r.get("kind") == "stub-error"]
    summary["harness_faults"].extend({"request": row.get("n"), "tool": result.get("tool"), "text": result.get("text")}
                                     for row in stub for result in row.get("tool_results", [])
                                     if any(marker in result.get("text", "") for marker in (
                                         "Input validation error:", "No task found with ID", "Cannot use AgentTaskOutput:")))
    if summary["harness_faults"]:
        summary["harness_ok"] = False
    (root / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    return summary


def ledger(path, summary):
    if not path:
        return
    path = Path(path)
    if not path.exists():
        path.write_text("# RUOI E2E runs — Sol-S\n\nSynthetic stub usage is not billing. Each run uses the real OBS daemon paths, SDK and bundled CLI inside one isolated container.\n")
    subset = {k: summary.get(k) for k in ("run_id", "scenario", "identity", "identity_sha", "identity_src_tree", "fixed_candidate_src_tree", "fixture_team", "driver_hashes",
              "harness_sha", "image", "image_id", "container_name", "start_utc", "end_utc", "container_exit_code",
              "harness_ok", "harness_faults", "host_error", "raw_log_paths", "observations", "held_marker_count",
              "blocked_inbox_marker_count", "hint_lines")}
    subset["inboxes"] = {role: data.get("inbox") for role, data in summary.get("final", {}).get("agents", {}).items()}
    subset["tool_results_seen"] = summary.get("tool_results_seen")
    subset["key_log_lines"] = summary.get("key_log_lines")
    with path.open("a") as stream:
        stream.write("\n## " + summary["run_id"] + "\n\n")
        stream.write("Full record: `" + str(Path(summary["run_root"]) / "summary.json") + "`\n\n")
        stream.write("```json\n" + json.dumps(subset, indent=2, default=str) + "\n```\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--identity", choices=("RP", "AP"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--control-site", choices=("rounds-cleanup", "rounds-failure", "post-reboot"))
    parser.add_argument("--control-source-dir", type=Path, default=VAULT_OPS)
    parser.add_argument("--ledger")
    parser.add_argument("--rerun-reason")
    parser.add_argument("--s1-mode", choices=("error", "stop"), default="error")
    args = parser.parse_args()
    sha = git("rev-parse", args.sha + "^{commit}")
    if args.identity == "RP" and sha != BASE:
        parser.error("RP identity must be the specified base")
    if args.scenario.startswith("7-") and args.identity != "AP":
        parser.error("Surfacing is fixed-only")
    if args.scenario == "6" and not args.control_site:
        parser.error("S6 requires a specific source site")
    docker = docker_module()
    inspected = docker._run(["image", "inspect", IMAGE], check=False)
    if inspected.returncode:
        raise SystemExit("Required existing obs-e2e image is missing; image not rebuilt")
    image_id = json.loads(inspected.stdout)[0]["Id"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    run_id = "-".join(v for v in (stamp, args.identity.lower(), args.scenario, args.control_site, uuid.uuid4().hex[:6]) if v)
    root = ROOT / run_id
    for directory in ("home", "state", "vault/.claude", "control", "code"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    archive = subprocess.check_output(["git", "-C", str(WORKTREE), "archive", sha])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(root / "code", filter="data")
    (root / "vault/CLAUDE.md").write_text("# Isolated RUOI E2E vault\n\nScripted model-free scenarios.\n")
    (root / "vault/.claude/settings.json").write_text(json.dumps({"apiKeyHelper": "echo ruoi-e2e-dummy-key"}) + "\n")
    fixture = None
    if args.scenario == "6":
        fixture = control_fixture(args.control_site, args.control_source_dir)
        (root / "control-site.json").write_text(json.dumps(fixture, indent=2) + "\n")
    env = environment(run_id)
    env_file = root / "container.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()))
    name = "obs-e2e-ruoi-" + run_id
    manifest = {"run_id": run_id, "run_root": str(root), "scenario": args.identity + "-" + args.scenario,
                "identity": args.identity, "identity_sha": sha, "identity_src_tree": git("rev-parse", sha + ":src"),
                "fixed_candidate_src_tree": git("rev-parse", "ccbc012decd186f3cd3e0cd0b68b528e50e64450:src") if args.identity == "AP" else None,
                "fixture_team": "2026-10-22-18-00-ruoi-e2e",
                "s1_mode": args.s1_mode if args.scenario == "1" else None, "start_utc": utc(),
                "driver_hashes": {p.name: digest(p) for p in sorted(HERE.iterdir()) if p.is_file()},
                "harness_sha": subprocess.check_output(["git", "-C", str(HARNESS), "rev-parse", "HEAD"], text=True).strip(),
                "docker_helper_sha256": digest(HARNESS / "tests/e2e/harness/docker.py"),
                "image": IMAGE, "image_id": image_id, "container_name": name,
                "network": "none", "control_site": fixture,
                "rerun_reason": args.rerun_reason, "credentials": "synthetic API key only; no model spend"}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    launched = False
    rc = None
    error = None
    try:
        docker.run_container(name, IMAGE, role="ruoi-cold-notice", run_id=run_id, lane="L-RUOI",
                             env_file=env_file, network="none", add_host_gateway=False,
                             mounts=[docker.Mount(root, "/run/ruoi"),
                                     docker.Mount(Path("/workspace/obs/.venv"), "/workspace/obs/.venv", ro=True),
                                     docker.Mount(HERE, "/drivers", ro=True)],
                             command=[PYTHON, "/drivers/daemon.py", "--root", "/run/ruoi", "--scenario", args.scenario,
                                      "--s1-mode", args.s1_mode],
                             workdir="/run/ruoi/vault")
        launched = True
        inspected = docker.inspect(name)
        evidence = {"Id": inspected.get("Id"), "State": inspected.get("State"), "Mounts": inspected.get("Mounts"),
                    "HostConfig": {k: inspected.get("HostConfig", {}).get(k) for k in (
                        "NetworkMode", "Memory", "MemorySwap", "NanoCpus", "PidsLimit", "SecurityOpt", "CgroupParent")},
                    "User": inspected.get("Config", {}).get("User"), "Image": inspected.get("Image")}
        (root / "container-inspect.json").write_text(json.dumps(evidence, indent=2) + "\n")
        waited = docker._run(["wait", name], timeout=330)
        rc = int(waited.stdout.strip().splitlines()[-1])
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        (root / "container.log").write_text(docker.logs(name) if launched else "")
        if launched:
            docker.rm_run(run_id)
    summary = summarize(root, manifest, rc, error)
    ledger(args.ledger, summary)
    print(json.dumps({"run_id": run_id, "root": str(root), "harness_ok": summary["harness_ok"],
                      "exit_code": rc, "error": error,
                      "observations": [{"event": v["event"], "recipient": v.get("recipient"),
                                        "requests_after": v["recipient_requests_after"],
                                        "cold": v.get("snapshot", {}).get("readiness", {}).get("cold")}
                                       for v in summary["observations"]]}, indent=2), flush=True)
    return 0 if summary["harness_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
