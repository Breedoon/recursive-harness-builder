"""Cache-readiness guard for agent-originated wakes (bead vault-vokl).

An agent that messages (SendInboxMessage), resumes (AgentTask resume) or forks from
(AgentTask session_source) another agent's session makes that session's next request read its
whole context. If the session has been idle longer than its provider's prompt-cache lifetime,
that read is billed at full price — for a 500k-token agent, a single "heads-up" costs as much as
dozens of normal turns. Daniel, 2026-10-15: block such wakes by default, say why, point to the
transcript so a cheap fork can search it instead, and allow an explicit override.

Per-model-family cold thresholds (seconds since the target's last model call), configurable with
``OBS_CACHE_READINESS_TTLS`` (comma-separated ``family=seconds|off``):

* ``claude`` — 3480 s. Claude entries live 1 hour ("clot is basically always one hour"); the
  1-2 minute safety margin is subtracted so the guard calls a session cold slightly early.
* ``gpt`` — 1800 s. GPT/Codex caching is a gradient, not a fixed TTL; 30 minutes is Daniel's soft
  border. (No provider-side "will this hit cache" probe is used: none was found that is simple to
  call per message; see the bead.)
* ``local`` — off. Local GLM/Qwen agents are not guarded for now (Daniel accepted the risk).

Human (Telegram) messages never pass through this guard: it runs only on agent tool calls.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_TTLS: dict[str, int | None] = {"claude": 3480, "gpt": 1800, "local": None}
_TAIL_BYTES = 512 * 1024


def load_ttls(raw: str | None = None) -> dict[str, int | None]:
    ttls = dict(DEFAULT_TTLS)
    raw = os.environ.get("OBS_CACHE_READINESS_TTLS", "") if raw is None else raw
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        family, value = (item.strip().lower() for item in part.split("=", 1))
        if not family:
            continue
        if value in ("off", "none", "0", ""):
            ttls[family] = None
            continue
        try:
            ttls[family] = int(float(value))
        except ValueError:
            continue
    return ttls


def model_family(model: str | None) -> str | None:
    name = (model or "").strip().lower()
    if not name:
        return None
    if name.startswith("local-") or any(tag in name for tag in ("glm", "qwen", "llama", "gemma", "mistral")):
        return "local"
    if "claude" in name or any(tag in name for tag in ("opus", "sonnet", "haiku", "fable")):
        return "claude"
    if name.startswith(("gpt", "o1", "o3", "o4", "codex")) or "gpt" in name or "codex" in name:
        return "gpt"
    return None


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class LastCall:
    timestamp: float | None
    model: str | None
    context_tokens: int | None
    source: str  # "assistant-record" | "mtime" | "none"


def last_model_call(jsonl_path: Path | None) -> LastCall:
    """Timestamp, model and context size of the last assistant record in a session JSONL.

    Reads only the file's tail. Falls back to the file mtime when no assistant record is in the
    tail (the mtime is the last write of any kind, so it never makes a session look colder).
    """
    if jsonl_path is None:
        return LastCall(None, None, None, "none")
    path = Path(jsonl_path)
    try:
        size = path.stat().st_size
        mtime = path.stat().st_mtime
    except OSError:
        return LastCall(None, None, None, "none")
    try:
        with open(path, "rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return LastCall(mtime, None, None, "mtime")
    for line in reversed(tail.splitlines()):
        if '"assistant"' not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        tokens = 0
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                tokens += value
        stamp = _parse_ts(record.get("timestamp"))
        return LastCall(
            stamp if stamp is not None else mtime,
            message.get("model") if isinstance(message.get("model"), str) else None,
            tokens or None,
            "assistant-record",
        )
    return LastCall(mtime, None, None, "mtime")


@dataclass
class Readiness:
    cold: bool
    family: str | None
    ttl_seconds: int | None
    idle_seconds: float | None
    context_tokens: int | None
    jsonl_path: Path | None
    reason: str


def assess(
    *,
    jsonl_path: Path | None,
    model_hint: str | None = None,
    running: bool = False,
    now: float | None = None,
    ttls: dict[str, int | None] | None = None,
) -> Readiness:
    """Is waking this session likely to re-bill its whole context?"""
    if running:
        return Readiness(False, None, None, 0.0, None, jsonl_path, "target is running")
    call = last_model_call(jsonl_path)
    family = model_family(call.model) or model_family(model_hint)
    ttls = load_ttls() if ttls is None else ttls
    ttl = ttls.get(family) if family else None
    if call.timestamp is None:
        return Readiness(False, family, ttl, None, call.context_tokens, jsonl_path, "no transcript activity found")
    idle = max(0.0, (time.time() if now is None else now) - call.timestamp)
    if family is None:
        return Readiness(False, None, None, idle, call.context_tokens, jsonl_path, "unknown model family")
    if ttl is None:
        return Readiness(False, family, None, idle, call.context_tokens, jsonl_path, f"guard off for {family}")
    cold = idle > ttl
    return Readiness(
        cold,
        family,
        ttl,
        idle,
        call.context_tokens,
        jsonl_path,
        "idle past cache lifetime" if cold else "within cache lifetime",
    )


def cold_warning(readiness: Readiness, *, action: str, target: str) -> str:
    idle_min = int((readiness.idle_seconds or 0) // 60)
    ttl_min = int((readiness.ttl_seconds or 0) // 60)
    size = f", ~{readiness.context_tokens:,} tokens of context" if readiness.context_tokens else ""
    transcript = str(readiness.jsonl_path) if readiness.jsonl_path else "unknown"
    return (
        f"cold-cache guard: {action} {target} not sent — it has been idle {idle_min} min, past the "
        f"{ttl_min}-min {readiness.family} prompt-cache lifetime{size}, so waking it re-bills its whole "
        f"context. Cheaper: launch a fresh agent or fork to search its transcript at {transcript}. "
        "If the wake is really needed (it is small, it is waiting on your result, or you were "
        "told to), repeat the call with allow_cold_wake=true."
    )
