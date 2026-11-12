"""Join native lane process evidence to post-selection build and completion records."""
from __future__ import annotations

import json
from pathlib import Path

BUNDLED_PATH = "/workspace/obs/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def lane_identity(lane_dir: Path, manifest: dict) -> dict:
    """Missing or incoherent execution proof is setup-invalid, never a binary test result."""
    events = _records(lane_dir / "events.jsonl")
    processes = _records(lane_dir / "cli-processes.jsonl")
    builds = [e for e in events if e.get("kind") == "session_build"]
    ends = [e for e in events if e.get("kind") == "turn_end"]
    rows, errors = [], []
    used_processes, used_ends = set(), set()
    for index, build in enumerate(builds):
        agent = (build.get("team"), build.get("agent_name"))
        later = [e["ts"] for e in builds[index + 1:]
                 if (e.get("team"), e.get("agent_name")) == agent]
        until = min(later) if later else float("inf")
        native = [p for p in processes if (p.get("team"), p.get("agent_name")) == agent
                  and build["ts"] <= p["ts"] < until]
        completions = [e for e in ends if (e.get("team"), e.get("agent_name")) == agent
                       and build["ts"] <= e["ts"] < until]
        # --canary-cli suites deliberately vary the requested runtime by fixture.
        # An explicit --cli-path is never relaxed by what the runtime happened to report.
        requested = manifest.get("cli_path") or (
            build.get("cli_path") if manifest.get("canary_cli") else None) or BUNDLED_PATH
        expected_runtime = "cc-2.1.59" if requested == BUNDLED_PATH else "cli:" + Path(requested).name
        row = {"requested": requested, "session_build": build, "processes": native,
               "turn_ends": completions, "errors": []}
        if not native:
            row["errors"].append("missing independent CLI argv0")
        if not completions:
            row["errors"].append("missing turn_end")
        actual_build = build.get("cli_path") or BUNDLED_PATH
        expected_build = "cc-2.1.59" if requested == BUNDLED_PATH else "cli-path"
        if actual_build != requested or build.get("runtime") != expected_build:
            row["errors"].append("session_build differs from requested CLI")
        if requested == BUNDLED_PATH and build.get("version") != "2.1.59":
            row["errors"].append("bundled version is not pinned 2.1.59")
        for process in native:
            used_processes.add((process["pid"], process["start"]))
            if process.get("argv0") != requested:
                row["errors"].append("native process argv0 differs from requested CLI")
        for end in completions:
            used_ends.add(end["seq"])
            if (end.get("cli_path") or BUNDLED_PATH) != requested or end.get("cli_runtime") != expected_runtime:
                row["errors"].append("turn_end differs from requested CLI")
        row["status"] = "SETUP-INVALID" if row["errors"] else "MATCH"
        rows.append(row)
        errors.extend({"agent_name": agent[1], "build_seq": build["seq"], "reason": error}
                      for error in row["errors"])
    for process in processes:
        if (process["pid"], process["start"]) not in used_processes:
            errors.append({"reason": "CLI process has no matching session_build", "process": process})
    for end in ends:
        if end["seq"] not in used_ends:
            errors.append({"reason": "turn_end has no matching session_build", "turn_end": end})
    return {"lane": lane_dir.name, "requested": manifest.get("cli_path") or BUNDLED_PATH,
            "status": "SETUP-INVALID" if errors else "MATCH" if rows else "NOT-EXERCISED",
            "sessions": rows, "errors": errors}


def run_identity(run_dir: Path, manifest: dict) -> dict:
    lanes = [lane_identity(run_dir / "lanes" / lane["lane"], manifest) for lane in manifest.get("lanes", [])]
    return {"status": "SETUP-INVALID" if any(lane["errors"] for lane in lanes) else
            "MATCH" if any(lane["sessions"] for lane in lanes) else "NOT-EXERCISED", "lanes": lanes}
