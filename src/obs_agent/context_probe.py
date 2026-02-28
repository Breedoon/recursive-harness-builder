"""Authoritative context-window probe via Claude CLI /context."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ContextProbe:
    used_tokens: int
    window_tokens: int
    used_pct: float | None = None


def _parse_scaled_number(raw: str, suffix: str) -> int:
    value = float(raw.replace(",", ""))
    s = suffix.lower()
    if s == "k":
        value *= 1_000
    elif s == "m":
        value *= 1_000_000
    return int(round(value))


def parse_context_markdown(markdown: str) -> ContextProbe | None:
    """Parse Claude `/context` markdown output.

    Expected line shape:
      **Tokens:** 37k / 200k (18%)
    """
    match = re.search(
        r"tokens\W*:\W*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM]?)\s*/\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM]?)\s*\((\d+(?:\.\d+)?)%\)",
        markdown,
        re.IGNORECASE,
    )
    if not match:
        return None
    used = _parse_scaled_number(match.group(1), match.group(2))
    window = _parse_scaled_number(match.group(3), match.group(4))
    pct = float(match.group(5))
    if used < 0 or window <= 0:
        return None
    return ContextProbe(used_tokens=used, window_tokens=window, used_pct=pct)


async def probe_context_via_claude_cli(
    *,
    session_id: str | None,
    cwd: Path,
    timeout_seconds: float = 12.0,
) -> ContextProbe | None:
    """Query Claude CLI `/context` for the given session without mutating session state."""
    if not session_id:
        return None

    cmd = [
        "claude",
        "-r",
        session_id,
        "-p",
        "/context",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
    ]
    for attempt in range(2):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except (OSError, asyncio.TimeoutError):
            continue

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").lower()
            if "no conversation found" in err and attempt == 0:
                await asyncio.sleep(0.3)
                continue
            return None

        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

        if payload.get("is_error"):
            return None

        result_text = payload.get("result")
        if not isinstance(result_text, str):
            return None
        return parse_context_markdown(result_text)
    return None
