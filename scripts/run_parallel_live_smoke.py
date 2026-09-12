"""Deprecated historical Telegram live-smoke entry point.

The public launcher is permanently fail-closed. Only data parsing and interval
helpers remain for pure unit/library compatibility; this module contains no
process, credential validation, output creation, or live execution path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Sequence

FORMAL_TEST_REDIRECT = (
    "scripts/run_parallel_live_smoke.py is disabled as a live launcher; use the "
    "canonical host-preflight command in docs/testing.md and obs-live-test"
)

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


def _split_env(env: Mapping[str, str], name: str) -> list[str]:
    return [item.strip() for item in env.get(name, "").split(",") if item.strip()]


def discover_bot_pairs(env: Mapping[str, str] | None = None) -> list[BotPair]:
    """Parse inert caller data; never authenticate or launch a resource."""
    source = os.environ if env is None else env
    usernames = _split_env(source, "OBS_TEST_TELEGRAM_BOT_USERNAMES")
    tokens = _split_env(source, "OBS_TEST_TELEGRAM_BOT_TOKENS")
    primary_username = source.get("OBS_TEST_TELEGRAM_BOT_USERNAME", "").strip()
    primary_token = source.get("OBS_TEST_TELEGRAM_BOT_TOKEN", "").strip()
    if primary_username and primary_token:
        usernames.insert(0, primary_username)
        tokens.insert(0, primary_token)
    return [
        BotPair(username=username, token=token)
        for username, token in zip(usernames, tokens, strict=False)
    ]


def build_worker_env(
    base_env: Mapping[str, str], pair: BotPair, worker_index: int
) -> dict[str, str]:
    """Build an inert mapping for retained unit tests; it starts nothing."""
    env = dict(base_env)
    env.update(SAFE_LIVE_ENV)
    env["OBS_TEST_TELEGRAM_BOT_USERNAME"] = pair.username
    env["OBS_TEST_TELEGRAM_BOT_TOKEN"] = pair.token
    env["OBS_TEST_TELEGRAM_BOT_TOKENS"] = pair.token
    env["OBS_LIVE_PARALLEL_WORKER_INDEX"] = str(worker_index)
    return env


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


def main(_argv: Sequence[str] | None = None) -> int:
    raise SystemExit(FORMAL_TEST_REDIRECT)


if __name__ == "__main__":
    raise SystemExit(main())
