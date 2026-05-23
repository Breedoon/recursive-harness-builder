from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

SAFE_LIVE_ENV = {
    "OBS_AGENT_MODEL": "haiku",
    "OBS_TEST_TELEGRAM_ISOLATED_RESOURCES": "1",
    "OBS_TEST_TELEGRAM_KILL_EXISTING_DAEMONS": "0",
    "OBS_CACHE_PROXY_ENABLED": "0",
}

DEFAULT_CASES = (
    "tests/test_telegram_live_forum_topics.py::TestTelegramLiveForumTopics::test_live_multi_chat_concurrent_isolation",
    "tests/test_telegram_live_stress.py::TestTelegramLiveStress::test_live_idle_agent_wakes_on_direct_message_without_sleep",
)


@dataclass(frozen=True)
class BotPair:
    username: str
    token: str


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def discover_bot_pairs(env: Mapping[str, str] | None = None) -> list[BotPair]:
    source = env if env is not None else os.environ
    usernames: list[str] = []
    tokens: list[str] = []
    primary_username = (source.get("OBS_TEST_TELEGRAM_BOT_USERNAME") or "").strip()
    primary_token = (source.get("OBS_TEST_TELEGRAM_BOT_TOKEN") or "").strip()
    if primary_username or primary_token:
        if not primary_username or not primary_token:
            raise SystemExit("OBS_TEST_TELEGRAM_BOT_USERNAME and OBS_TEST_TELEGRAM_BOT_TOKEN must both be set")
        usernames.append(primary_username)
        tokens.append(primary_token)
    usernames.extend(_split_csv(source.get("OBS_TEST_TELEGRAM_BOT_USERNAMES")))
    tokens.extend(_split_csv(source.get("OBS_TEST_TELEGRAM_BOT_TOKENS")))
    if len(usernames) != len(tokens):
        raise SystemExit("OBS_TEST_TELEGRAM_BOT_USERNAMES and OBS_TEST_TELEGRAM_BOT_TOKENS counts must match")
    return [BotPair(username=username, token=token) for username, token in zip(usernames, tokens)]


def build_worker_env(base_env: Mapping[str, str], pair: BotPair, worker_index: int) -> dict[str, str]:
    env = dict(base_env)
    env.update(SAFE_LIVE_ENV)
    env["OBS_TEST_TELEGRAM_BOT_USERNAME"] = pair.username
    env["OBS_TEST_TELEGRAM_BOT_TOKEN"] = pair.token
    env["OBS_TEST_TELEGRAM_BOT_USERNAMES"] = pair.username
    env["OBS_TEST_TELEGRAM_BOT_TOKENS"] = pair.token
    env["OBS_LIVE_PARALLEL_WORKER_INDEX"] = str(worker_index)
    return env


def _case_for_worker(index: int) -> str:
    return DEFAULT_CASES[index % len(DEFAULT_CASES)]


def _worker_command(output_dir: Path, worker_index: int, case: str) -> list[str]:
    worker_root = output_dir / f"worker-{worker_index}"
    worker_root.mkdir(parents=True, exist_ok=True)
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-s",
        "--basetemp",
        str(worker_root / "pytest-tmp"),
        case,
    ]


def _load_worker_metadata(worker_root: Path) -> dict:
    for path in (worker_root / "pytest-tmp").glob("**/obs-agent-temp/live-forum-resources.json"):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


def _require_dedicated_pairs(pairs: Sequence[BotPair], max_workers: int, *, allow_shared_bot: bool) -> None:
    if len(pairs) >= max_workers:
        return
    if allow_shared_bot:
        return
    raise SystemExit("parallel live smoke requires one bot username/token pair per worker; pass --allow-shared-bot only for diagnostics")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Telegram live smoke tests in parallel with isolated bot tokens.")
    parser.add_argument("--output-dir", type=Path, default=Path(".parallel-live-smoke"))
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-shared-bot", action="store_true")
    args = parser.parse_args(argv)

    max_workers = max(1, int(args.max_workers))
    pairs = discover_bot_pairs(os.environ)
    _require_dedicated_pairs(pairs, max_workers, allow_shared_bot=args.allow_shared_bot)
    if not pairs:
        raise SystemExit("no Telegram bot credentials configured")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    processes: list[dict[str, object]] = []
    for worker_index in range(max_workers):
        pair = pairs[worker_index % len(pairs)]
        case = _case_for_worker(worker_index)
        command = _worker_command(args.output_dir, worker_index, case)
        env = build_worker_env(os.environ, pair, worker_index)
        if args.dry_run:
            print(" ".join(command))
            continue
        started = time.time()
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(
            {
                "worker_index": worker_index,
                "case": case,
                "command": command,
                "process": process,
                "started_at": started,
                "bot_username": pair.username,
            }
        )

    if args.dry_run:
        return 0

    results: list[dict[str, object]] = []
    rc = 0
    for item in processes:
        process = item["process"]
        assert isinstance(process, subprocess.Popen)
        returncode = process.wait()
        finished = time.time()
        if returncode != 0:
            rc = returncode or 1
        worker_index = int(item["worker_index"])
        worker_root = args.output_dir / f"worker-{worker_index}"
        resource_metadata = _load_worker_metadata(worker_root)
        if not resource_metadata:
            command = item["command"]
            if isinstance(command, list) and command:
                resource_metadata = _load_worker_metadata(Path(str(command[-1])).parent)
        results.append(
            {
                "worker_index": worker_index,
                "case": item["case"],
                "command": item["command"],
                "returncode": returncode,
                "started_at": item["started_at"],
                "finished_at": finished,
                "bot_username": item["bot_username"],
                "resource_metadata": resource_metadata,
            }
        )

    summary = {
        "worker_count": len(results),
        "workers_overlapped": _workers_overlapped(results),
        "dedicated_bot_pairs": len(pairs) >= max_workers,
        "safe_env": SAFE_LIVE_ENV,
        "results": results,
    }
    (args.output_dir / "parallel-live-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    return rc


def _workers_overlapped(results: Sequence[dict[str, object]]) -> bool:
    if len(results) < 2:
        return False
    intervals = sorted(
        (float(item["started_at"]), float(item["finished_at"]))
        for item in results
    )
    latest_finish = intervals[0][1]
    for started, finished in intervals[1:]:
        if started < latest_finish:
            return True
        latest_finish = max(latest_finish, finished)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
