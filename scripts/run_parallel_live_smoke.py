#!/usr/bin/env python3
"""Run a small parallel live Telegram smoke batch.

This is an opt-in live-test orchestrator. It launches separate pytest processes
concurrently, with isolated Telegram resources enabled, so the live suite can
prove separate-test parallelism instead of only concurrency inside one test.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DEFAULT_SELECTORS = [
    "tests/test_telegram_live_forum_topics.py::TestTelegramLiveForumTopics::test_live_multi_chat_concurrent_isolation",
    "tests/test_telegram_live_stress.py::TestIdleWakeWithoutSleep::test_live_idle_agent_wakes_on_direct_message_without_sleep",
]

SAFE_LIVE_ENV = {
    "OBS_AGENT_MODEL": "haiku",
    "OBS_TEST_TELEGRAM_ISOLATED_RESOURCES": "1",
    "OBS_TEST_TELEGRAM_KILL_EXISTING_DAEMONS": "0",
    "OBS_CACHE_PROXY_ENABLED": "0",
}


@dataclass(frozen=True)
class BotPair:
    username: str
    token: str


def _csv_env(name: str, env: dict[str, str]) -> list[str]:
    raw = env.get(name, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def discover_bot_pairs(env: dict[str, str] | None = None) -> list[BotPair]:
    env = env or os.environ
    usernames: list[str] = []
    tokens: list[str] = []

    primary_username = env.get("OBS_TEST_TELEGRAM_BOT_USERNAME", "").strip()
    primary_token = env.get("OBS_TEST_TELEGRAM_BOT_TOKEN", "").strip()
    if primary_username and primary_token:
        usernames.append(primary_username)
        tokens.append(primary_token)

    usernames.extend(_csv_env("OBS_TEST_TELEGRAM_BOT_USERNAMES", env))
    tokens.extend(_csv_env("OBS_TEST_TELEGRAM_BOT_TOKENS", env))

    index = 2
    while True:
        username = env.get(f"OBS_TEST_TELEGRAM_BOT_USERNAME_{index}", "").strip()
        token = env.get(f"OBS_TEST_TELEGRAM_BOT_TOKEN_{index}", "").strip()
        if not username and not token:
            break
        if username and token:
            usernames.append(username)
            tokens.append(token)
        index += 1

    pairs: list[BotPair] = []
    seen: set[tuple[str, str]] = set()
    for username, token in zip(usernames, tokens):
        pair = (username, token)
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(BotPair(username=username, token=token))
    return pairs


def build_worker_env(base_env: dict[str, str], bot_pair: BotPair, worker_index: int) -> dict[str, str]:
    env = dict(base_env)
    env.update(SAFE_LIVE_ENV)
    env["OBS_TEST_TELEGRAM_BOT_USERNAME"] = bot_pair.username
    env["OBS_TEST_TELEGRAM_BOT_TOKEN"] = bot_pair.token
    env["OBS_TEST_TELEGRAM_BOT_TOKENS"] = bot_pair.token
    env["OBS_LIVE_PARALLEL_WORKER_INDEX"] = str(worker_index)
    return env


def build_pytest_command(selector: str, worker_root: Path, pytest_timeout: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        selector,
        "-q",
        "-s",
        "--tb=short",
        f"--timeout={pytest_timeout}",
        "--basetemp",
        str(worker_root / "pytest-tmp"),
    ]


def _load_resource_metadata(worker_root: Path) -> list[dict[str, object]]:
    metadata: list[dict[str, object]] = []
    for path in sorted(worker_root.rglob("live-forum-resources.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - diagnostic only
            payload = {"metadata_path": str(path), "error": repr(exc)}
        else:
            payload["metadata_path"] = str(path)
        metadata.append(payload)
    return metadata


def _intervals_overlap(results: Sequence[dict[str, object]]) -> bool:
    intervals = [
        (float(result["started_at"]), float(result["ended_at"]))
        for result in results
        if "started_at" in result and "ended_at" in result
    ]
    for idx, first in enumerate(intervals):
        for second in intervals[idx + 1:]:
            if max(first[0], second[0]) < min(first[1], second[1]):
                return True
    return False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selector",
        action="append",
        dest="selectors",
        help="Pytest selector to run. Repeat for multiple selectors. Defaults to a two-test core smoke batch.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/obs-live-parallel-smoke"))
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--pytest-timeout", type=int, default=420)
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument(
        "--allow-shared-bot",
        action="store_true",
        help="Allow multiple pytest workers to share one bot token. This is for diagnosis only; proof runs should use dedicated bot pairs.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without running pytest.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selectors = args.selectors or list(DEFAULT_SELECTORS)
    if len(selectors) < 2:
        raise SystemExit("parallel live smoke requires at least two selectors")
    if args.max_workers < 2:
        raise SystemExit("parallel live smoke requires --max-workers >= 2")

    bot_pairs = discover_bot_pairs()
    if not bot_pairs:
        raise SystemExit("No Telegram bot credentials found in OBS_TEST_TELEGRAM_BOT_USERNAME/TOKEN")
    if len(bot_pairs) < min(args.max_workers, len(selectors)) and not args.allow_shared_bot:
        raise SystemExit(
            "Parallel proof requires one bot username/token pair per worker. "
            "Set OBS_TEST_TELEGRAM_BOT_USERNAMES and OBS_TEST_TELEGRAM_BOT_TOKENS, "
            "or pass --allow-shared-bot for a diagnostic run that may hit Bot API polling conflicts."
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    workers: list[dict[str, object]] = []
    base_env = os.environ.copy()
    for index, selector in enumerate(selectors[: args.max_workers]):
        worker_root = args.output_dir / f"worker-{index}"
        worker_root.mkdir(parents=True, exist_ok=True)
        bot_pair = bot_pairs[index] if index < len(bot_pairs) else bot_pairs[0]
        command = build_pytest_command(selector, worker_root, args.pytest_timeout)
        workers.append(
            {
                "index": index,
                "selector": selector,
                "worker_root": worker_root,
                "command": command,
                "env": build_worker_env(base_env, bot_pair, index),
                "bot_username": bot_pair.username,
            }
        )

    if args.dry_run:
        for worker in workers:
            print(" ".join(str(part) for part in worker["command"]))
        return 0

    running: list[dict[str, object]] = []
    for worker in workers:
        log_path = Path(worker["worker_root"]) / "pytest.log"
        log_handle = log_path.open("w", encoding="utf-8")
        started_at = time.time()
        process = subprocess.Popen(
            worker["command"],
            cwd=Path(__file__).resolve().parents[1],
            env=worker["env"],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        worker.update({"process": process, "log_path": log_path, "log_handle": log_handle, "started_at": started_at})
        running.append(worker)

    deadline = time.time() + args.worker_timeout
    results: list[dict[str, object]] = []
    for worker in running:
        process: subprocess.Popen = worker["process"]  # type: ignore[assignment]
        remaining = max(1.0, deadline - time.time())
        timed_out = False
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            returncode = process.returncode
        ended_at = time.time()
        worker["log_handle"].close()  # type: ignore[index,union-attr]
        worker_root = Path(worker["worker_root"])
        results.append(
            {
                "index": worker["index"],
                "selector": worker["selector"],
                "returncode": returncode,
                "timed_out": timed_out,
                "started_at": worker["started_at"],
                "ended_at": ended_at,
                "duration_seconds": round(ended_at - float(worker["started_at"]), 3),
                "log_path": str(worker["log_path"]),
                "bot_username": worker["bot_username"],
                "resource_metadata": _load_resource_metadata(worker_root),
            }
        )

    summary = {
        "selectors": selectors[: args.max_workers],
        "safe_env": SAFE_LIVE_ENV,
        "output_dir": str(args.output_dir),
        "dedicated_bot_pairs": len(bot_pairs) >= len(workers),
        "worker_count": len(workers),
        "workers_overlapped": _intervals_overlap(results),
        "results": results,
    }
    summary_path = args.output_dir / "parallel-live-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(f"parallel live summary: {summary_path}")

    failed = [result for result in results if result["returncode"] != 0 or result["timed_out"]]
    if failed:
        return 1
    if not summary["workers_overlapped"]:
        print("workers did not overlap in wall-clock time", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
