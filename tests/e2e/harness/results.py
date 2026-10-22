"""JUnit aggregation and measured run timing for the E2E runner."""

from __future__ import annotations

import copy
import json
import math
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


def merge_junit(paths: list[Path]) -> ET.ElementTree:
    root = ET.Element("testsuites")
    for path in map(Path, paths):
        source = ET.parse(path).getroot()
        suites = [source] if source.tag == "testsuite" else source.findall(".//testsuite")
        lane = path.stem.removeprefix("junit-")
        for suite in suites:
            merged = copy.deepcopy(suite)
            merged.set("lane", lane)
            for case in merged.findall("testcase"):
                case.set("lane", lane)
            root.append(merged)
    for name in ("tests", "failures", "errors", "skipped"):
        root.set(name, str(sum(int(s.get(name, "0")) for s in root)))
    root.set("time", str(sum(float(s.get("time", "0")) for s in root)))
    return ET.ElementTree(root)


def _timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def compute_timing(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    manifest = _read_json(run_dir / "manifest.json")
    lanes: dict[str, dict] = {}
    waits: list[dict] = []
    for path in sorted((run_dir / "runner").glob("timing-*.jsonl")):
        lane = path.stem.removeprefix("timing-")
        starts, ends, test_starts, test_ends = [], [], [], []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            kind = record.get("kind")
            if kind == "wait":
                event_ts = _timestamp(record["event_ts"])
                wake_ts = _timestamp(record["wake_ts"])
                waits.append({**record, "lane": lane, "event_ts": event_ts,
                              "wake_ts": wake_ts, "latency_s": wake_ts - event_ts})
            elif kind in {"suite_start", "suite_end", "test_start", "test_end"}:
                ts = _timestamp(record["ts"])
                {"suite_start": starts, "suite_end": ends,
                 "test_start": test_starts, "test_end": test_ends}[kind].append(ts)
        start = min(starts or test_starts) if starts or test_starts else None
        end = max(ends or test_ends) if ends or test_ends else None
        lanes[lane] = {"start_ts": start, "end_ts": end,
                       "suite_interval": [start, end],
                       "duration_s": max(0.0, end - start) if start is not None and end is not None else None}
    for row in manifest.get("lanes", []):
        name = row if isinstance(row, str) else row["lane"]
        lanes.setdefault(name, {"start_ts": None, "end_ts": None,
                                "suite_interval": [None, None], "duration_s": None})
    complete = [row for row in lanes.values() if row["start_ts"] is not None and row["end_ts"] is not None]
    overlap = 0.0
    if complete and len(complete) == len(lanes):
        overlap = max(0.0, min(row["end_ts"] for row in complete) - max(row["start_ts"] for row in complete))
    test_phase = (max(row["end_ts"] for row in complete) - min(row["start_ts"] for row in complete)) if complete else 0.0
    started, finished = manifest.get("started_at"), manifest.get("finished_at")
    wall = max(0.0, _timestamp(finished) - _timestamp(started)) if started is not None and finished is not None else None
    latencies = sorted(row["latency_s"] for row in waits)
    result = {
        "lanes": lanes, "wall_time_s": wall, "test_phase_s": test_phase,
        "all_lanes_overlap_s": overlap,
        "overlap_ratio": overlap / wall if wall else 0.0,
        "overlap_ratio_test_phase": overlap / test_phase if test_phase else 0.0,
        "wait_count": len(waits), "waits": waits,
        "wait_latency_median_s": statistics.median(latencies) if latencies else None,
        "wait_latency_p95_s": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
    }
    (run_dir / "timing.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"obs-e2e timing: wall={wall if wall is not None else 'unknown'}s "
          f"overlap={overlap:.3f}s ratio={result['overlap_ratio']:.3f} "
          f"test_ratio={result['overlap_ratio_test_phase']:.3f} "
          f"waits={len(waits)} median={result['wait_latency_median_s']}s p95={result['wait_latency_p95_s']}s", flush=True)
    return result


def write_summary(run_dir: Path, *, junit: ET.ElementTree | None = None,
                  worker_exitcodes: dict[str, int] | None = None,
                  error: str | None = None, exit_code: int | None = None) -> dict:
    run_dir = Path(run_dir)
    manifest = _read_json(run_dir / "manifest.json")
    if junit is None:
        junit_path = run_dir / "junit.xml"
        junit = ET.parse(junit_path) if junit_path.exists() else ET.ElementTree(ET.Element("testsuites"))
    cases = []
    totals = dict.fromkeys(("passed", "xfailed", "failed", "error", "skipped"), 0)
    for suite in junit.getroot().iter("testsuite"):
        for case in suite.findall("testcase"):
            failure, case_error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
            if failure is not None:
                outcome = "failed"
            elif case_error is not None:
                outcome = "error"
            elif skipped is not None:
                outcome = "xfailed" if "xfail" in skipped.get("type", "").lower() else "skipped"
            else:
                outcome = "passed"
            totals[outcome] += 1
            cases.append({"lane": case.get("lane", suite.get("lane")), "name": case.get("name"),
                          "classname": case.get("classname"), "outcome": outcome,
                          "duration_s": float(case.get("time", "0"))})
    worker_exitcodes = worker_exitcodes or {}
    if exit_code is None:
        setup_error = error or any(code not in (0, 1) for code in worker_exitcodes.values())
        exit_code = 2 if setup_error else (1 if any(worker_exitcodes.values()) or
                    any(totals[k] for k in ("failed", "error", "skipped")) or not cases else 0)
    result = {"run_id": manifest.get("run_id", run_dir.name), "exit_code": exit_code,
              "tests": cases, "totals": {**totals, "total": len(cases)},
              "worker_exitcodes": worker_exitcodes, "lanes_excluded": manifest.get("lanes_excluded", []),
              "bots_excluded": manifest.get("bots_excluded", []),
              "requested_lanes": manifest.get("requested_lanes"), "active_lanes": len(manifest.get("lanes", [])),
              "five_lane_count_met": len(manifest.get("lanes", [])) >= 5,
              "error": error}
    result["summary_line"] = (f"obs-e2e {result['run_id']}: exit={exit_code} "
                              f"passed={totals['passed']} xfailed={totals['xfailed']} "
                              f"failed={totals['failed']} errors={totals['error']} skipped={totals['skipped']} "
                              f"lanes_excluded={len(result['lanes_excluded'])} "
                              f"bots_excluded={len(result['bots_excluded'])} "
                              f"lanes={result['active_lanes']}/{result['requested_lanes']}")
    (run_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
