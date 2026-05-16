"""Shared helpers for persistent minimal live Telegram test vaults."""

from __future__ import annotations

import os
from pathlib import Path


_DEFAULT_LIVE_TEST_VAULT = Path("/tmp/obs-telegram-live-test-vault")


def ensure_live_test_vault(path: str | Path | None = None) -> Path:
    """Return a minimal vault used by live Telegram tests.

    The default path is stable across runs so external manual testing can attach
    to the same test workspace. Callers that need parallel isolation can pass an
    explicit path, which takes precedence over the environment override.
    """

    raw = str(path) if path is not None else (os.environ.get("OBS_TELEGRAM_LIVE_TEST_VAULT") or "").strip()
    vault = (Path(raw).expanduser() if raw else _DEFAULT_LIVE_TEST_VAULT).resolve()

    claude_dir = vault / ".claude"
    (claude_dir / "system").mkdir(parents=True, exist_ok=True)
    (claude_dir / "skills").mkdir(parents=True, exist_ok=True)
    (claude_dir / "memory").mkdir(parents=True, exist_ok=True)
    (claude_dir / "topics").mkdir(parents=True, exist_ok=True)
    (claude_dir / "drafts").mkdir(parents=True, exist_ok=True)

    (vault / "CLAUDE.md").write_text(
        (
            "# OBS Live Telegram Test Vault\n\n"
            "This workspace is intentionally minimal and isolated from production.\n"
            "For integration tests, prefer creating files under test-specific folders.\n"
        ),
        encoding="utf-8",
    )
    (claude_dir / "memory.md").write_text("# Memory\n\nLive test memory parent.\n", encoding="utf-8")
    (claude_dir / "skills.md").write_text("# Skills\n\nLive test skills parent.\n", encoding="utf-8")
    (claude_dir / "system.md").write_text("# System\n\nLive test system parent.\n", encoding="utf-8")

    return vault

