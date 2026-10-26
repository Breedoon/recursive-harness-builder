#!/usr/bin/env python3
"""Exactly-once local request lifecycle replay and attribution summary."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any
import uuid

ROW_SCHEMA = "obs-local-request-lifecycle/2"
COVERAGE_SCHEMA = "obs-local-request-coverage/1"
MANIFEST_SCHEMA = "obs-local-request-replay-manifest/1"
SUMMARY_SCHEMA = "obs-local-request-summary/2"
DEFAULT_DIR = os.environ.get(
    "OBS_REQUEST_LIFECYCLE_DIR", "/workspace/runtime/state/local-gpu/requests"
)
MANIFEST_MAX_BYTES = int(
    os.environ.get("OBS_LOCAL_ATTRIBUTION_MANIFEST_MAX_GB", "4") or "4"
) * 10**9
MANIFEST_MAX_COUNT = int(os.environ.get("OBS_LOCAL_ATTRIBUTION_MANIFEST_MAX_COUNT", "200") or "200")
TOKEN_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)
ACCOUNTING_KEYS = (
    "proxy_reserved_token_hours",
    "native_memory_token_hours",
    "proxy_forwarded_stream_hours",
    "native_stream_hours",
    "reference_slot_hours",
    "hardware_gpu_hours",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _parse_time(value: str | float | int | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _source_files(root: str | os.PathLike[str]) -> list[Path]:
    path = Path(root)
    if not path.exists():
        return []
    return sorted(
        item for item in path.iterdir()
        if item.is_file() and item.suffix == ".jsonl"
        and (item.name.startswith(("requests-", "coverage-")) or item.name[:4].isdigit())
    )


def _file_overlaps_window(path: Path, start_ts: float | None, end_ts: float | None) -> bool:
    if start_ts is None and end_ts is None:
        return True
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    return True
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    return True
                if not isinstance(row, dict):
                    return True
                ts = row.get("ts")
                if not isinstance(ts, (int, float)):
                    return True
                if start_ts is not None and float(ts) < start_ts:
                    continue
                if end_ts is not None and float(ts) >= end_ts:
                    continue
                return True
    except OSError:
        return True
    return False


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _prune_manifest_root(base: Path, current: Path) -> None:
    lock_path = base / ".manifest-retention.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        snapshots = sorted(
            (path for path in base.iterdir() if path.is_dir()),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        sizes = {path: _directory_bytes(path) for path in snapshots}
        total = sum(sizes.values())
        while len(snapshots) > MANIFEST_MAX_COUNT or total > MANIFEST_MAX_BYTES:
            victim = next((path for path in snapshots if path != current), None)
            if victim is None:
                break
            snapshots.remove(victim)
            total -= sizes[victim]
            shutil.rmtree(victim, ignore_errors=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def create_manifest(
    root: str | os.PathLike[str],
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    output_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Copy exact source bytes into an immutable replay directory and hash them."""
    source_root = Path(root)
    base = Path(output_root) if output_root else source_root / "manifests"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    snapshot = base / f"{stamp}-{uuid.uuid4().hex[:8]}"
    snapshot.mkdir(parents=True, exist_ok=False)
    entries = []
    for source in _source_files(source_root):
        if not _file_overlaps_window(source, start_ts, end_ts):
            continue
        target = snapshot / source.name
        shutil.copyfile(source, target)
        entries.append(
            {
                "kind": "jsonl",
                "source": str(source),
                "snapshot": target.name,
                "size": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )
    retention_state = source_root / "retention-state.json"
    if retention_state.is_file():
        target = snapshot / retention_state.name
        shutil.copyfile(retention_state, target)
        entries.append({
            "kind": "retention_state",
            "source": str(retention_state),
            "snapshot": target.name,
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        })
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created_at": time.time(),
        "source_root": str(source_root),
        "window": {"from": start_ts, "to": end_ts},
        "row_schema": ROW_SCHEMA,
        "coverage_schema": COVERAGE_SCHEMA,
        "summary_schema": SUMMARY_SCHEMA,
        "summary_code_sha256": _code_sha256(),
        "manifest_retention": {
            "max_bytes": MANIFEST_MAX_BYTES,
            "max_count": MANIFEST_MAX_COUNT,
        },
        "files": entries,
    }
    canonical = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    manifest["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    manifest_path = snapshot / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _prune_manifest_root(base, snapshot)
    return manifest_path


def verify_manifest(path: str | os.PathLike[str]) -> tuple[dict[str, Any], list[Path]]:
    manifest_path = Path(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unsupported replay manifest schema")
    stored_digest = manifest.pop("manifest_sha256", None)
    canonical = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    computed_digest = hashlib.sha256(canonical).hexdigest()
    manifest["manifest_sha256"] = stored_digest
    if stored_digest != computed_digest:
        raise ValueError("replay manifest digest mismatch")
    if manifest.get("summary_code_sha256") != _code_sha256():
        raise ValueError("replay analyzer identity differs from the manifest")
    files = []
    for entry in manifest.get("files") or []:
        snapshot = manifest_path.parent / str(entry.get("snapshot") or "")
        if not snapshot.is_file() or snapshot.stat().st_size != int(entry.get("size") or -1):
            raise ValueError(f"replay source missing or size changed: {snapshot}")
        if _sha256(snapshot) != entry.get("sha256"):
            raise ValueError(f"replay source digest mismatch: {snapshot}")
        files.append(snapshot)
    return manifest, files


def _load_files(
    files: list[Path], *, start_ts: float | None = None, end_ts: float | None = None
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    raw_lines = malformed_lines = schema_errors = 0
    source_stats = []
    for path in files:
        file_raw = file_malformed = file_rows = file_coverage = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                raw_lines += 1
                file_raw += 1
                text = line.strip()
                if not text:
                    malformed_lines += 1
                    file_malformed += 1
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    malformed_lines += 1
                    file_malformed += 1
                    continue
                if not isinstance(record, dict):
                    schema_errors += 1
                    continue
                ts = record.get("ts")
                if not isinstance(ts, (int, float)):
                    schema_errors += 1
                    continue
                if start_ts is not None and float(ts) < start_ts:
                    continue
                if end_ts is not None and float(ts) >= end_ts:
                    continue
                record["_source"] = path.name
                record["_line"] = line_number
                if record.get("schema") == ROW_SCHEMA or (
                    record.get("request_id") and record.get("usage") is not None
                    and not record.get("kind")
                ):
                    rows.append(record)
                    file_rows += 1
                elif record.get("schema") == COVERAGE_SCHEMA:
                    coverage.append(record)
                    file_coverage += 1
                else:
                    schema_errors += 1
        source_stats.append(
            {
                "file": path.name,
                "raw_lines": file_raw,
                "malformed_lines": file_malformed,
                "rows": file_rows,
                "coverage_events": file_coverage,
            }
        )
    return {
        "rows": rows,
        "coverage": coverage,
        "stats": {
            "raw_lines": raw_lines,
            "malformed_lines": malformed_lines,
            "schema_errors": schema_errors,
            "sources": source_stats,
        },
    }


def load_evidence(
    root: str | os.PathLike[str] | None = None,
    *,
    manifest_path: str | os.PathLike[str] | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> dict[str, Any]:
    manifest = None
    retention_state = None
    retention_error = None
    if manifest_path:
        manifest, declared_files = verify_manifest(manifest_path)
        files = [path for path in declared_files if path.suffix == ".jsonl"]
        retention_path = next(
            (path for path in declared_files if path.name == "retention-state.json"), None
        )
    else:
        source_root = Path(root or DEFAULT_DIR)
        files = _source_files(source_root)
        retention_path = source_root / "retention-state.json"
    if retention_path is not None and retention_path.is_file():
        try:
            retention_state = json.loads(retention_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            retention_error = f"{type(exc).__name__}: {exc}"
    evidence = _load_files(files, start_ts=start_ts, end_ts=end_ts)
    evidence["manifest"] = manifest
    evidence["manifest_path"] = str(manifest_path) if manifest_path else None
    evidence["retention_state"] = retention_state
    evidence["retention_error"] = retention_error
    evidence["window"] = {"from": start_ts, "to": end_ts}
    return evidence


def _canonical_record(row: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in row.items() if not key.startswith("_")},
        separators=(",", ":"),
        sort_keys=True,
    )


def deduplicate_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kept: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    duplicate_exact = 0
    duplicate_conflicts: list[str] = []
    missing_request_id = 0
    for row in sorted(rows, key=lambda item: (str(item.get("_source")), int(item.get("_line") or 0))):
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            missing_request_id += 1
            continue
        fingerprint = hashlib.sha256(_canonical_record(row).encode()).hexdigest()
        if request_id not in kept:
            kept[request_id] = row
            fingerprints[request_id] = fingerprint
        elif fingerprints[request_id] == fingerprint:
            duplicate_exact += 1
        else:
            duplicate_conflicts.append(request_id)
    return list(kept.values()), {
        "duplicate_exact": duplicate_exact,
        "duplicate_conflicts": sorted(set(duplicate_conflicts)),
        "missing_request_id": missing_request_id,
    }


def _matches(row: dict[str, Any], filters: dict[str, str | None]) -> bool:
    binding = row.get("binding") or {}
    mapping = {
        "agent": binding.get("agent_name"),
        "tree": binding.get("root_team_key"),
        "task": binding.get("task_id"),
        "attempt": binding.get("attempt_id"),
        "category": binding.get("category"),
        "pool": row.get("pool"),
    }
    return all(value is None or mapping[key] == value for key, value in filters.items())


def _metric() -> dict[str, Any]:
    return {"value": 0.0, "observed_requests": 0, "total_requests": 0, "complete": True}


def _accumulator() -> dict[str, Any]:
    return {
        "requests": 0,
        "outcomes": Counter(),
        "tokens": {key: _metric() for key in TOKEN_KEYS},
        "accounting": {key: _metric() for key in ACCOUNTING_KEYS},
        "models": set(),
        "sessions": set(),
        "engines": set(),
    }


def _add_row(acc: dict[str, Any], row: dict[str, Any]) -> None:
    acc["requests"] += 1
    acc["outcomes"][row.get("outcome") or "unknown"] += 1
    usage = row.get("usage") or {}
    available = row.get("usage_available") or {}
    for key in TOKEN_KEYS:
        metric = acc["tokens"][key]
        metric["total_requests"] += 1
        if available.get(key) is True and isinstance(usage.get(key), (int, float)):
            metric["value"] += usage[key]
            metric["observed_requests"] += 1
        else:
            metric["complete"] = False
    accounting = row.get("accounting") or {}
    for key in ACCOUNTING_KEYS:
        metric = acc["accounting"][key]
        metric["total_requests"] += 1
        if isinstance(accounting.get(key), (int, float)):
            metric["value"] += accounting[key]
            metric["observed_requests"] += 1
        else:
            metric["complete"] = False
    if row.get("model"):
        acc["models"].add(row["model"])
    if row.get("session_id"):
        acc["sessions"].add(row["session_id"])
    if row.get("engine"):
        acc["engines"].add(row["engine"])


def _finalize(acc: dict[str, Any]) -> dict[str, Any]:
    result = dict(acc)
    result["outcomes"] = dict(sorted(acc["outcomes"].items()))
    for family in ("tokens", "accounting"):
        result[family] = {}
        for key, metric in acc[family].items():
            item = dict(metric)
            if item["observed_requests"] == 0:
                item["value"] = None
            elif family == "tokens":
                item["value"] = int(item["value"])
            else:
                item["value"] = round(float(item["value"]), 9)
            result[family][key] = item
    result["models"] = sorted(acc["models"])
    result["engines"] = sorted(acc["engines"])
    result["session_count"] = len(acc["sessions"])
    result.pop("sessions", None)
    return result


def summarize_evidence(
    evidence: dict[str, Any], *, filters: dict[str, str | None] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    filters = filters or {key: None for key in ("agent", "tree", "task", "attempt", "category", "pool")}
    unique_rows, dedupe = deduplicate_rows(evidence["rows"])
    rows = [row for row in unique_rows if _matches(row, filters)]

    starts = {
        event.get("request_id"): event
        for event in evidence["coverage"]
        if event.get("kind") == "transmission_start" and event.get("request_id")
        and all(
            value is None or event.get({"attempt": "attempt_id", "task": "task_id", "category": "category", "pool": "pool"}.get(key, key)) in (None, value)
            for key, value in filters.items()
            if key in {"attempt", "task", "category", "pool"}
        )
    }
    losses = [
        event for event in evidence["coverage"]
        if event.get("kind") == "loss"
        and all(
            value is None or event.get({"attempt": "attempt_id", "task": "task_id", "category": "category", "pool": "pool"}.get(key, key)) in (None, value)
            for key, value in filters.items()
            if key in {"attempt", "task", "category", "pool"}
        )
    ]
    startup_failures = sum(event.get("kind") == "startup_failure" for event in evidence["coverage"])
    loss_by_id = {event.get("request_id"): event for event in losses if event.get("request_id")}
    loss_events_without_id = sum(not event.get("request_id") for event in losses)
    row_ids = {row["request_id"] for row in rows}
    transmitted_row_ids = {
        row["request_id"] for row in rows
        if (row.get("lifecycle") or {}).get("upstream_sent") is not None
    }
    rows_with_start = transmitted_row_ids & set(starts)
    classified_loss_ids = set(loss_by_id) - transmitted_row_ids
    unclassified_loss_ids = set(starts) - transmitted_row_ids - set(loss_by_id)
    rows_without_start = transmitted_row_ids - set(starts)
    loss_codes = Counter(event.get("code") or "unknown" for event in losses)
    if unclassified_loss_ids:
        loss_codes["crash_gap_or_incomplete"] += len(unclassified_loss_ids)
    conservation_ok = (
        len(starts)
        == len(rows_with_start) + len(classified_loss_ids) + len(unclassified_loss_ids)
        and not rows_without_start
    )
    stats = evidence["stats"]
    retention = evidence.get("retention_state") or {}
    requested_start = (evidence.get("window") or {}).get("from")
    latest_pruned_ts = retention.get("latest_pruned_ts")
    retention_truncated = bool(retention.get("pruned_files")) and (
        requested_start is None
        or not isinstance(latest_pruned_ts, (int, float))
        or float(latest_pruned_ts) >= float(requested_start)
    )
    retention_error = evidence.get("retention_error")
    complete = (
        conservation_ok
        and not retention_truncated
        and not retention_error
        and stats["malformed_lines"] == 0
        and stats["schema_errors"] == 0
        and dedupe["missing_request_id"] == 0
        and not dedupe["duplicate_conflicts"]
        and not classified_loss_ids
        and not unclassified_loss_ids
        and loss_events_without_id == 0
        and startup_failures == 0
    )

    dimensions: dict[str, defaultdict[str, dict[str, Any]]] = {
        name: defaultdict(_accumulator)
        for name in ("agent", "tree", "task", "attempt", "category", "pool")
    }
    unknown_binding = 0
    for row in rows:
        binding = row.get("binding") or {}
        keys = {
            "agent": binding.get("agent_name") or "unattributed",
            "tree": binding.get("root_team_key") or "unattributed",
            "task": binding.get("task_id") or "unassigned",
            "attempt": binding.get("attempt_id") or "unassigned",
            "category": binding.get("category") or "unassigned",
            "pool": row.get("pool") or "unknown",
        }
        if row.get("binding_source") != "trusted_runtime_state":
            unknown_binding += 1
        for name, key in keys.items():
            _add_row(dimensions[name][key], row)

    result_dimensions = {
        name: {key: _finalize(value) for key, value in sorted(groups.items())}
        for name, groups in dimensions.items()
    }
    manifest = evidence.get("manifest") or {}
    summary = {
        "schema": SUMMARY_SCHEMA,
        "generated_at": manifest.get("created_at") or time.time(),
        "filters": filters,
        "canonical_sources": {
            "request_tokens": "lifecycle transmission rows",
            "proxy_times": "lifecycle monotonic durations",
            "native_engine_times": "unavailable unless explicit native events exist",
            "hardware_times": "separate hardware sampler join; never added to proxy/stream time",
            "copied_history": "excluded by construction: transcript rows are never ingested",
        },
        "manifest": {
            "available": bool(manifest),
            "path": evidence.get("manifest_path"),
            "manifest_sha256": manifest.get("manifest_sha256"),
            "summary_code_sha256": manifest.get("summary_code_sha256") or _code_sha256(),
            "row_schema": manifest.get("row_schema") or ROW_SCHEMA,
        },
        "coverage": {
            "complete": complete,
            "availability": "complete" if complete else "incomplete",
            "raw_lines": stats["raw_lines"],
            "malformed_lines": stats["malformed_lines"],
            "schema_errors": stats["schema_errors"],
            "startup_failures": startup_failures,
            "retention_truncated": retention_truncated,
            "retention_error": retention_error,
            "retention_state": retention or None,
            "transmission_starts": len(starts),
            "unique_rows": len(rows),
            "transmitted_rows": len(transmitted_row_ids),
            "nontransmitted_rows": len(row_ids - transmitted_row_ids),
            "rows_with_start": len(rows_with_start),
            "rows_without_start": len(rows_without_start),
            "classified_losses": len(classified_loss_ids),
            "loss_events_without_request_id": loss_events_without_id,
            "derived_crash_gap_losses": len(unclassified_loss_ids),
            "unclassified_losses": len(unclassified_loss_ids),
            "loss_codes": dict(sorted(loss_codes.items())),
            "duplicate_exact": dedupe["duplicate_exact"],
            "duplicate_conflicts": dedupe["duplicate_conflicts"],
            "missing_request_id": dedupe["missing_request_id"],
            "conservation_equation": (
                "transmission_starts = unique_rows_with_start + durable_classified_losses "
                "+ derived_crash_gap_or_incomplete_losses"
            ),
            "conservation_ok": conservation_ok,
            "unknown_binding_rows": unknown_binding,
            "unknown_binding_fraction": round(unknown_binding / len(rows), 6) if rows else None,
            "copied_history_rows_ingested": 0,
            "unclassified_loss_request_ids": sorted(unclassified_loss_ids),
        },
        "total_requests": len(rows),
        "dimensions": result_dimensions,
        "source_stats": stats["sources"],
    }
    return summary, rows


def analyze(
    root: str | os.PathLike[str] = DEFAULT_DIR,
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    manifest_path: str | os.PathLike[str] | None = None,
    create_snapshot: bool = False,
    manifest_output_root: str | os.PathLike[str] | None = None,
    filters: dict[str, str | None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if create_snapshot and manifest_path:
        raise ValueError("choose manifest_path or create_snapshot, not both")
    if create_snapshot:
        manifest_path = create_manifest(
            root, start_ts=start_ts, end_ts=end_ts, output_root=manifest_output_root
        )
    evidence = load_evidence(
        root, manifest_path=manifest_path, start_ts=start_ts, end_ts=end_ts
    )
    return summarize_evidence(evidence, filters=filters)


def print_text(summary: dict[str, Any]) -> None:
    coverage = summary["coverage"]
    print(f"Requests: {summary['total_requests']}")
    print(
        f"Coverage: {coverage['availability']} | starts={coverage['transmission_starts']} "
        f"rows={coverage['unique_rows']} classified_losses={coverage['classified_losses']} "
        f"unclassified_losses={coverage['unclassified_losses']}"
    )
    print(
        f"Malformed={coverage['malformed_lines']} duplicates={coverage['duplicate_exact']} "
        f"conflicts={len(coverage['duplicate_conflicts'])} unknown_binding={coverage['unknown_binding_rows']}"
    )
    for name in ("pool", "attempt", "task", "tree", "agent", "category"):
        groups = summary["dimensions"][name]
        if not groups:
            continue
        print(f"\n=== Per {name.title()} ===")
        for key, value in groups.items():
            tokens = value["tokens"]
            print(
                f"{key}: requests={value['requests']} "
                f"fresh={tokens['input_tokens']['value']} "
                f"cache_read={tokens['cache_read_input_tokens']['value']} "
                f"cache_write={tokens['cache_creation_input_tokens']['value']} "
                f"output={tokens['output_tokens']['value']}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=DEFAULT_DIR)
    parser.add_argument("--since", type=float, default=0.0, help="Last N hours")
    parser.add_argument("--from", dest="from_time")
    parser.add_argument("--to", dest="to_time")
    parser.add_argument("--manifest")
    parser.add_argument("--create-manifest", action="store_true")
    parser.add_argument("--manifest-output-root")
    parser.add_argument("--agent")
    parser.add_argument("--tree")
    parser.add_argument("--task")
    parser.add_argument("--attempt")
    parser.add_argument("--category")
    parser.add_argument("--pool")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    start_ts = _parse_time(args.from_time)
    if args.since > 0:
        start_ts = time.time() - args.since * 3600
    end_ts = _parse_time(args.to_time)
    filters = {
        key: getattr(args, key)
        for key in ("agent", "tree", "task", "attempt", "category", "pool")
    }
    try:
        summary, _rows = analyze(
            args.dir,
            start_ts=start_ts,
            end_ts=end_ts,
            manifest_path=args.manifest,
            create_snapshot=args.create_manifest,
            manifest_output_root=args.manifest_output_root,
            filters=filters,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"request lifecycle replay failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_text(summary)
    if summary["total_requests"] == 0:
        return 2
    return 0 if summary["coverage"]["complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
