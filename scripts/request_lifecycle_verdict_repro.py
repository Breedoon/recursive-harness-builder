#!/usr/bin/env python3
"""Cheap base/candidate red-green probe for strict lifecycle verdict defects."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile


def _load(name: str, path: Path):
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(repo: Path) -> dict:
    lifecycle_path = repo / "src" / "obs_agent" / "request_lifecycle.py"
    summary_path = repo / "scripts" / "request_lifecycle_summary.py"
    result = {"repo": str(repo), "checks": {}, "pass": False}
    try:
        lifecycle = _load("candidate_request_lifecycle", lifecycle_path)
        summary = _load("candidate_request_lifecycle_summary", summary_path)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        binding_db = root / "bindings.sqlite3"
        store = lifecycle.SessionBindingStore(binding_db)
        store.upsert("s", {
            "root_team_key": "tree", "agent_name": "agent", "lineage": ["tree", "agent"],
            "task_id": "vault-gsku", "attempt_id": "vault-gsku.1",
            "category": "obs", "source": "fixture",
        }, bound_at=1)
        restarted = lifecycle.SessionBindingStore(binding_db, writable=False)
        result["checks"]["restart_binding"] = restarted.get("s", at_ts=2)["attempt_id"] == "vault-gsku.1"

        row = lifecycle.LifecycleRow(
            request_id="r", session_id="s", model="local-test", max_tokens=3,
            receive_wall=2, receive_mono=2,
        )
        row.bind(restarted.get("s", at_ts=2))
        row.mark_upstream_send()
        row.mark_terminal("error", error_class="upstream_http_error", usage={"input_tokens": 1})
        record = row.to_record()
        encoded = json.dumps(record)
        result["checks"]["no_raw_error"] = "error_message" not in encoded
        result["checks"]["missing_not_zero"] = record["usage"]["cache_read_input_tokens"] is None
        result["checks"]["trusted_attempt"] = record["binding"]["attempt_id"] == "vault-gsku.1"
        result["checks"]["native_time_unknown"] = record["durations"]["native_engine_execution_ms"] is None

        logger = lifecycle.RequestLifecycleLogger(root / "logs", max_bytes=500)
        logger.record_transmission_start(row)
        logger.submit(record)
        logger.flush()
        files = list((root / "logs").glob("*.jsonl"))
        result["checks"]["bounded_nonempty_retention"] = bool(files) and sum(
            path.stat().st_size for path in files
        ) <= 500

        manifest = summary.create_manifest(root / "logs")
        replay, _rows = summary.analyze(root / "logs", manifest_path=manifest)
        result["checks"]["manifest_identity"] = bool(replay["manifest"]["manifest_sha256"])
        result["checks"]["loss_visible"] = replay["coverage"]["classified_losses"] >= 1

    result["pass"] = all(result["checks"].values())
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    args = parser.parse_args(argv)
    result = probe(args.repo.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
