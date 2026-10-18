from __future__ import annotations

"""Cache-normalizing proxy for Claude Code API requests.

Intercepts POST /v1/messages requests and applies idempotent normalization
rules to maximize prompt cache hits across session restarts and forks.
All other requests are forwarded unmodified. CC's native cache_control
placement is left untouched — cache_control is not part of the cache key
(it's a breakpoint hint only), so normalizing it is unnecessary.

CACHE-PREFIX WARNING — THIS FILE IS EASY TO BREAK AND BREAKS SILENTLY
Every request of the whole agent fleet passes through here. Prompt caching
only works when a request starts with exactly the same bytes as an earlier
one; change one byte of old history, the system prompt, the tool list or a
request setting and the API quietly re-bills the whole conversation (no error,
just cost: one changed system section re-billed 391k tokens on 2026-10-12).
Six such break mechanisms were found in October 2026; unit tests caught none.
Before committing ANY change here (or in jsonl_fork/jsonl_health/jsonl_replay,
session resume, telegram recovery/fork code) run the live check
    .venv/bin/python scripts/cache_regression.py
on a scratch proxy and again on production after a maintenance restart, and
follow docs/cache-proxy-safety.md.

FORK/RESUME CACHE CORRECTNESS, NOT OPTIONAL TEXT CLEANUP
The live CLI adds context that it never persists to JSONL. Forks and resumes
rebuild history from JSONL, so those additions MUST NOT survive normalization:
one differing historical block breaks the shared prefix from that point on.
Even useful inbox/correction text in native hook additionalContext is transient.
Deliver it as a normal, persisted query instead; NEVER exempt it here to fix
message delivery. Do not rewrite historical JSONL to imitate live-only context.

Before changing normalization, capture the actual bundled CLI's live request,
its JSONL, and actual fork AND resume requests; compare normalized shared history
(not just notification visibility or synthetic request fixtures). Run
scripts/cache_regression.py; tests/test_cache_proxy_reminder_span.py is a quick
unit regression only -- unit success alone does not prove provider cache-hit
behavior. See README.md's critical
maintainer warning and docs/inflight-message-delivery.md for the delivery boundary.

Normalizations (applied in order):
1. Billing header: replaced with fixed value
2. String→list: bare-string user content converted to list format
3. Claude Code system-reminders: every reminder span stripped from all user
   messages, including spans nested inside tool_result content
   (tool_result text is always whitespace-trimmed: the live CLI trims it, the
   JSONL replay does not)
4. Git status: normalized in system prompt to fixed placeholder
   (also: the CLI's dynamic-boundary marker line is removed from the system)
5. Tool sorting: tools[] sorted alphabetically by name
6. Metadata: session-specific IDs normalized
7. Tool schema sanitization: array types get ``items`` added (non-Claude only)
After normalization:
8. Variant pin: the "# Output efficiency" and "# MCP Server Instructions"
   system sections and the context_management field stay as first seen for
   each conversation (pin_volatile_config) — server-side flag refreshes and
   MCP servers (re)connecting with instructions otherwise flip them.
9. GPT route only: per-conversation provider cache key (assign_conversation_cache_key).
10. Anthropic route only: Haiku effort strip and mid-session effort pin.

Usage:
    python cache_proxy.py [port]  # default: 18923

Client:
    ANTHROPIC_BASE_URL=http://localhost:18923

"""
import gzip
import hashlib
import itertools
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import httpx

try:
    from obs_agent.config import resolve_model as _resolve_obs_model
except Exception:  # pragma: no cover - keep proxy usable as a standalone script
    def _resolve_obs_model(model: str) -> str:
        return model

ANTHROPIC_UPSTREAM = "https://api.anthropic.com"
CLI_PROXY_UPSTREAM = os.environ.get("CLI_PROXY_BASE_URL", "http://127.0.0.1:8317")
CLI_PROXY_API_KEY = os.environ.get("CLI_PROXY_API_KEY", "sk-anything")
# Local Anthropic-compatible provider (the authenticated local LLM gate).
# Inherited from the daemon's environment — the proxy is spawned by
# cache_proxy_lifecycle.start_cache_proxy() without an explicit env, and the
# prod wrapper exports OBS_LOCAL_LLM_BASE_URL before launching the daemon.
# Empty means "no local upstream configured"; local-* requests then fail
# explicitly rather than falling back to a hosted upstream.
LOCAL_UPSTREAM = os.environ.get("OBS_LOCAL_LLM_BASE_URL", "").strip().rstrip("/")
# Credential the local gate expects. Used ONLY to recognise a local request on
# endpoints that carry no model name (see _forward_simple). Never logged.
LOCAL_AUTH_TOKEN = os.environ.get("OBS_LOCAL_LLM_AUTH_TOKEN", "").strip()


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


# Local-route liveness (bead obs-3, 2026-10-16). A local model can legitimately
# send nothing for many minutes: a cold 500k-token prefill on the Sparks, a queue
# behind other prefills, or the 3090 gate holding requests while the GPUs are
# lent out. The bundled CLI drops any request that receives no bytes for about
# five minutes ("Request timed out"), retries it from scratch eleven times and so
# kills the turn after an hour. Meanwhile this proxy kept every abandoned request
# open upstream for its 600 s read timeout, so the retries piled more prefills
# onto the same slots. On the local route only, the proxy therefore:
#   - answers with its own 200 headers when upstream has not responded within
#     LOCAL_HEADER_WAIT_SECONDS (fast upstream errors, such as the overflow 400
#     translation, still pass through with their real status);
#   - writes an Anthropic `ping` event (streaming) or a JSON-insignificant space
#     (non-streaming) every LOCAL_KEEPALIVE_SECONDS of upstream silence;
#   - closes the upstream request as soon as a keepalive write shows the client
#     has gone, so abandoned prefills stop occupying slots;
#   - gives up only after LOCAL_UPSTREAM_SILENCE_SECONDS without an upstream
#     byte, and then reports a retryable overloaded_error instead of hanging.
# Response bytes only: the request sent upstream is unchanged (cache-neutral).
LOCAL_KEEPALIVE_SECONDS = _env_seconds("OBS_LOCAL_KEEPALIVE_SECONDS", 15.0)
LOCAL_HEADER_WAIT_SECONDS = _env_seconds("OBS_LOCAL_HEADER_WAIT_SECONDS", 20.0)
LOCAL_UPSTREAM_SILENCE_SECONDS = _env_seconds("OBS_LOCAL_UPSTREAM_SILENCE_SECONDS", 1800.0)
SSE_PING = b'event: ping\ndata: {"type": "ping"}\n\n'

# Upstream liveness verdict (bead obs-8, 2026-10-18). A wedged Spark head keeps
# answering /health ok while sending nothing, so every request waited the full
# LOCAL_UPSTREAM_SILENCE_SECONDS (twice, with the CLI's non-stream fallback).
# The Spark watchdog (obs_agent.spark_watchdog) judges liveness by engine
# progress and writes its verdict to this file; when it is fresh and bad:
#   - requests already in flight are failed at the next keepalive tick with a
#     retryable overloaded_error (streaming: SSE error, so the CLI falls back to
#     a non-streaming request; non-streaming: the chunked body is broken);
#   - new requests are held with keepalives until the verdict is healthy again
#     (the watchdog's paired restart), then forwarded unchanged, so the
#     agent's turn continues instead of failing; after
#     LOCAL_RECOVERY_WAIT_SECONDS they fail with the same retryable error.
# Unset, missing or stale (older than LOCAL_LIVENESS_MAX_AGE) = no effect.
LOCAL_LIVENESS_FILE = os.environ.get("OBS_LOCAL_LIVENESS_FILE", "").strip()
LOCAL_LIVENESS_MAX_AGE = _env_seconds("OBS_LOCAL_LIVENESS_MAX_AGE", 120.0)
LOCAL_RECOVERY_WAIT_SECONDS = _env_seconds("OBS_LOCAL_RECOVERY_WAIT_SECONDS", 2400.0)
LOCAL_LIVENESS_POLL_SECONDS = 5.0
_LOCAL_BAD_STATES = ("wedged", "down", "restarting", "failed")


def _local_liveness() -> dict | None:
    """The watchdog's fresh verdict, or None (unconfigured, unreadable or stale)."""
    if not LOCAL_LIVENESS_FILE:
        return None
    try:
        with open(LOCAL_LIVENESS_FILE, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if time.time() - float(doc.get("updated", 0)) > LOCAL_LIVENESS_MAX_AGE:
            return None
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _local_upstream_bad(doc: dict | None) -> bool:
    return isinstance(doc, dict) and doc.get("state") in _LOCAL_BAD_STATES
DEFAULT_PORT = 18923
# The port the production instance listens on; a different port means this is a
# test/private instance (used only for a startup hygiene warning).
try:
    PROD_PORT = int(os.environ.get("OBS_PROD_CACHE_PROXY_PORT") or 28925)
except ValueError:
    PROD_PORT = 28925
_CODEBASE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Capture at module load, never read a potentially newer on-disk file at health time.
with open(__file__, "rb") as _source_file:
    _LOADED_SOURCE_SHA256 = hashlib.sha256(_source_file.read()).hexdigest()
_LOADED_AT = time.time()
LOG_DIR = os.environ.get(
    "CACHE_PROXY_LOG_DIR",
    os.path.join(_CODEBASE_ROOT, ".obs-agent", "cache-proxy-log"),
)
BODY_DIR = os.path.join(LOG_DIR, "bodies")
USAGE_LOG = os.path.join(LOG_DIR, "usage.jsonl")

SKILL_MARKER = "The following skills are available for use with the Skill tool:"
CLAUDEMD_MARKER = "As you answer the user's questions, you can use the following context:"

# Legacy debug capture: req{NNN}_pre/post.json keyed by a per-process counter,
# overwritten on every restart. Kept for the existing test fixtures. For
# production diagnosis use the durable request log below.
SAVE_BODIES = os.environ.get("CACHE_PROXY_SAVE_BODIES", "").lower() in ("1", "true")

# Durable, session-keyed request log (2026-10-08, mission cache-proxy-prefix-fix,
# bead vault-mief.2; Daniel: "They can enable logging requests and go off of
# their own logs to find errors"). ON by default on the production port (see
# request_log_enabled). One record per /v1/messages
# request: the exact bytes the CLI sent (pre), the body after normalize_request()
# (post — stored only when it differs from wire), the exact bytes forwarded
# upstream (wire), and one index line with session id, HTTP status and usage.
# This is the instrument for finding prefix breakage: diff consecutive requests
# of one session with scripts/prefix_diff.py. Capture runs on a background
# thread and NEVER alters, delays or fails the proxied request. Bodies contain
# conversation content: the directory is outside the vault, auth headers are
# never written, and secret-looking values inside bodies and index lines are
# replaced by hashed placeholders (redact_secrets).
# Standing pool (Daniel 2026-10-08 23:55Z: "ideally we should have like ongoing
# some like gigabyte of pool for all ongoing requests to be saved from all
# chats. So like if things like this happen there at least there are logs"):
# a ring buffer over bodies + index, oldest-first eviction, default 60 GB.
# 1 GB holds only ~1–2 h at 2026-10-09 volume (~580 MB/h gzip), which would
# defeat "so there are logs"; Daniel 2026-10-09 00:21Z: "60 is fine. I don't
# mind that." 60 GB ≈ 30–60 h+ of history. Override: CACHE_PROXY_REQUEST_LOG_MAX_GB
# (takes effect at the next proxy start).
# Default: ON for the production port, OFF for test/private instances (agents'
# shells inherit CACHE_PROXY_LOG_DIR from the daemon, so a test proxy would
# otherwise write into the production log). CACHE_PROXY_REQUEST_LOG=1/0 forces.
_REQUEST_LOG_ENV = os.environ.get("CACHE_PROXY_REQUEST_LOG", "").strip().lower()


def request_log_enabled(port: int) -> bool:
    if _REQUEST_LOG_ENV in ("1", "true", "on"):
        return True
    if _REQUEST_LOG_ENV in ("0", "false", "off"):
        return False
    return port == PROD_PORT


REQUEST_LOG_DIR = os.environ.get("CACHE_PROXY_REQUEST_LOG_DIR") or os.path.join(LOG_DIR, "requests")
try:
    REQUEST_LOG_MAX_BYTES = int(float(os.environ.get("CACHE_PROXY_REQUEST_LOG_MAX_GB", "60")) * 1e9)
except ValueError:
    REQUEST_LOG_MAX_BYTES = int(60e9)

# Fixed billing header to replace the per-process/per-turn one.
#
# cc_version here is the ONLY place the Anthropic API learns our Claude Code
# version: it travels in the request body (system[0]), not in any HTTP header
# (the User-Agent version is ignored for this check). Newer models gate on it
# and reject old clients with claude_code_version_too_old. We previously sent
# cc_version=0, which is treated as ancient and fails that gate. Pin a value
# at/above the newest model's floor so gated models work without upgrading the
# bundled CLI. Must stay a constant (not the real per-process value) to keep
# the cached prefix stable.
#
# Format matters: the value must keep the 4-segment "major.minor.patch.build"
# shape the client normally sends (e.g. 2.1.59.a37). A bare "2.1.251" is
# REJECTED — verified by live test — so the build suffix must be retained.
CC_VERSION = os.environ.get("CACHE_PROXY_CC_VERSION", "2.1.280.a37")
FIXED_BILLING_HEADER = (
    f"x-anthropic-billing-header: cc_version={CC_VERSION}; cc_entrypoint=sdk-py; cch=0;"
)

# Running stats
stats = {
    "requests": 0,
    "skill_stripped": 0, "skill_missing": 0,
    "billing_normalized": 0, "strings_converted": 0,
    "reminders_stripped": 0, "messages_placeholdered": 0,
    "cch_restored": 0,
    "tool_results_trimmed": 0,
    "volatile_pins_applied": 0,
    "git_status_normalized": 0,
    "tools_sorted": 0, "metadata_normalized": 0,
    "errors": 0,
    "routed_anthropic": 0, "routed_cli_proxy": 0, "routed_local": 0,
    "local_unconfigured": 0,
    "schemas_sanitized": 0,
    "local_usage_rewrites": 0,
    "local_overflow_translations": 0,
    "local_keepalives": 0,
    "local_early_commits": 0,
    "local_client_gone": 0,
    "local_upstream_silence": 0,
    "local_nonstream_aborts": 0,
    "local_recovery_holds": 0,
    "local_wedge_aborts": 0,
}


# ── Model routing ────────────────────────────────────────────────────────

_CONTEXT_SUFFIX_RE = re.compile(r'\[\d+[mk]\]$', re.IGNORECASE)


def _strip_context_suffix(model: str) -> str:
    """Strip [1m], [200k], etc. context-window suffix from model name."""
    return _CONTEXT_SUFFIX_RE.sub('', model)


def _normalize_model_name(model: str) -> str:
    """Resolve OBS shorthand and strip Claude Code's context suffix artifact."""
    normalized = model.strip()
    if not normalized:
        return ""
    return _strip_context_suffix(_resolve_obs_model(normalized))


# Matches a JSON "model": "local-…" pair in a raw request body, tolerating
# arbitrary whitespace. Only used on the body-did-not-parse path.
_RAW_LOCAL_MODEL_RE = re.compile(rb'"model"\s*:\s*"local-', re.IGNORECASE)


def _route_label(upstream: str, is_local: bool) -> str:
    """Short label for logs and the usage JSONL."""
    if is_local:
        return "local"
    if upstream == ANTHROPIC_UPSTREAM:
        return "anthropic"
    return "cli-proxy"


def _is_local_model(model: str) -> bool:
    """True if *model* names a local Anthropic-compatible provider."""
    return _normalize_model_name(model).lower().startswith("local-")


def _resolve_upstream(model: str) -> str:
    """Determine upstream URL based on model name.

    local-* models → the local LLM gate (OBS_LOCAL_LLM_BASE_URL).
    Claude models  → Anthropic API (direct, no CLIProxyAPI dependency).
    Everything else → CLIProxyAPI (local proxy for GPT, Gemini, etc.).

    Returns "" for a local-* model when no local upstream is configured.
    Callers must treat that as a hard error: local traffic must never fall
    back to a hosted upstream (wrong provider, and it would ship the gate
    credential to a third party).
    """
    clean = _normalize_model_name(model)
    if clean.lower().startswith("local-"):
        return LOCAL_UPSTREAM
    if clean.startswith("claude"):
        return ANTHROPIC_UPSTREAM
    return CLI_PROXY_UPSTREAM


# ── Normalization steps ──────────────────────────────────────────────────


def normalize_billing_header(body: dict) -> int:
    """Rule 1: Replace per-process billing header in system[0] with fixed value.

    CACHE-PREFIX WARNING: must give byte-identical output for the same conversation
    in every process (live, resume, fork). Live check: scripts/cache_regression.py.

    The billing header contains cc_version (git hash) and cch (content hash)
    that change per-process and per-turn. The Anthropic API normalizes this
    for cache matching, but we fix it for clean diffs.
    """
    system = body.get("system")
    if not system or not isinstance(system, list):
        return 0
    count = 0
    for block in system:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        if text.startswith("x-anthropic-billing-header:"):
            block["text"] = FIXED_BILLING_HEADER
            count += 1
    stats["billing_normalized"] += count
    return count


def normalize_user_content_structure(body: dict) -> int:
    """Rule 2: Convert bare-string user message content to list format.

    CACHE-PREFIX WARNING: must give byte-identical output for the same conversation
    in every process (live, resume, fork). Live check: scripts/cache_regression.py.

    CC sometimes demotes older user message content from
    [{type: "text", text: "..."}] to "..." between turns.
    Same logical content, different JSON serialization → cache miss.
    """
    messages = body.get("messages", [])
    count = 0
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [{"type": "text", "text": content}]
            count += 1
    stats["strings_converted"] += count
    return count


def normalize_skill_listing(body: dict) -> dict:
    """Legacy helper: strip the skill listing block from all user messages.

    normalize_request now strips all Claude Code <system-reminder> blocks in
    one pass. This helper remains for older focused unit tests and callers.
    """
    messages = body.get("messages", [])
    if not messages:
        return {"action": "no_messages"}

    # Find and remove the skill listing block
    for msg_idx, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block_idx, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "text":
                if SKILL_MARKER in block.get("text", ""):
                    # Remove the skill block
                    content.pop(block_idx)
                    # Remove empty user messages left after extraction
                    if len(content) == 0:
                        messages.pop(msg_idx)
                    stats["skill_stripped"] += 1
                    return {
                        "action": "stripped",
                        "from": f"msg[{msg_idx}].content[{block_idx}]",
                    }

    stats["skill_missing"] += 1
    return {"action": "not_found"}


def _is_strippable_system_reminder(block: dict) -> bool:
    if not isinstance(block, dict) or block.get("type") != "text":
        return False
    text = block.get("text", "")
    return "<system-reminder>" in text


# A single reminder span: the opening tag through its closing tag (DOTALL), or —
# when the opening tag is never closed — through the end of that block's text.
# Whitespace on both sides of the span is consumed: Claude Code splices reminders
# into existing text with a "\n\n" separator in the live path and leaves a
# trailing newline after the closing tag in the replayed (JSONL) path, so the two
# forms only converge if the adjoining whitespace goes with the span.
_REMINDER_SPAN_RE = re.compile(
    r"\s*<system-reminder>(?:.*?</system-reminder>\s*|.*\Z)",
    re.DOTALL,
)

# Kept for a user message (or a tool_result) that would otherwise be left with no
# content at all. Fixed literal, non-empty and non-whitespace: a message is never
# dropped, so `messages` can never become empty and a conversation can never end
# on an assistant turn because of this rule.
REMINDER_PLACEHOLDER = "[reminder stripped]"


def strip_reminder_spans(text: str) -> tuple[str, int]:
    """Remove every reminder span from *text*.

    CACHE-PREFIX WARNING: do not preserve ``hook additional context:`` (or other live-only
    exceptions) here. The CLI omits it from JSONL; keeping it makes the parent
    diverge from the fork/resume history. Fix delivery at the persisted-query
    boundary and prove actual live/fork/resume prefix equality instead.

    Returns ``(new_text, spans_removed)``. Pure function of the input, so replays
    of the same conversation produce byte-identical output.

    When at least one span is removed the result is additionally ``strip()``ed:
    Claude Code trims the surrounding string when it splices a reminder into a
    tool result in the live path but not when the same content is replayed from
    the JSONL, so the two forms differ by leading/trailing whitespace exactly on
    the blocks that carried a reminder. Text with no span is returned unchanged.
    """
    if not isinstance(text, str) or "<system-reminder>" not in text:
        return text, 0
    new_text, removed = _REMINDER_SPAN_RE.subn("", text)
    if removed:
        new_text = new_text.strip()
    return new_text, removed


def _contains_cache_control(value) -> bool:
    if isinstance(value, dict):
        return "cache_control" in value or any(
            _contains_cache_control(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_cache_control(child) for child in value)
    return False


_norm_local = threading.local()


def _count_trim() -> None:
    """Count a tool_result whose text changed only by whitespace trimming."""
    stats["tool_results_trimmed"] += 1
    _norm_local.trimmed = getattr(_norm_local, "trimmed", 0) + 1


def _strip_reminders_in_block(block: dict) -> tuple[int, bool]:
    """Strip reminder spans inside one content block, recursively.

    Handles top-level ``text`` blocks, ``tool_result`` blocks whose ``content``
    is a string, and ``tool_result`` blocks whose ``content`` is a list of
    blocks. Every other block type (image, tool_use, …) is left untouched.

    Returns ``(spans_removed, block_is_now_empty)``.
    """
    if not isinstance(block, dict):
        return 0, False

    btype = block.get("type")

    if btype == "text":
        new_text, removed = strip_reminder_spans(block.get("text", ""))
        if removed:
            block["text"] = new_text
            if not new_text.strip() and any(
                _contains_cache_control(value)
                for key, value in block.items() if key != "cache_control"
            ):
                block["text"] = REMINDER_PLACEHOLDER
                return removed, False
            return removed, not new_text.strip()
        return 0, False

    if btype == "tool_result":
        # CACHE-PREFIX WARNING (whitespace, 2026-10-12 mission vault-1r7n):
        # tool_result text is ALWAYS trimmed, whether or not a reminder span was
        # removed. The live CLI trims a tool output before splicing a reminder
        # into it; the JSONL keeps the untrimmed output with no reminder. If we
        # trimmed only when a span was removed, the live and the resumed/forked
        # history differed by leading spaces and every wake, resume or restart
        # re-billed the whole conversation (byte-verified: requests 598bb917
        # and e5f25ac9 on 2026-10-12). Live check: scripts/cache_regression.py ws_resume (docs/cache-proxy-safety.md).
        content = block.get("content")
        if isinstance(content, str):
            new_text, removed = strip_reminder_spans(content)
            # Live reminder-only results replay from JSONL as empty strings, and
            # whitespace-only output trims to empty: both become the same fixed
            # placeholder, preserving the result ID/error/cache fields.
            final = new_text.strip() or REMINDER_PLACEHOLDER
            if final != content:
                block["content"] = final
                if not removed:
                    _count_trim()
            return removed, False
        if isinstance(content, list):
            removed = 0
            trimmed = 0
            kept: list = []
            for sub in content:
                sub_removed, sub_empty = _strip_reminders_in_block(sub)
                removed += sub_removed
                if (not sub_empty and isinstance(sub, dict) and sub.get("type") == "text"
                        and isinstance(sub.get("text"), str)
                        and sub["text"] != REMINDER_PLACEHOLDER):
                    stripped = sub["text"].strip()
                    if stripped != sub["text"]:
                        sub["text"] = stripped
                        trimmed += 1
                        if not sub_removed:
                            _count_trim()
                        sub_empty = not stripped
                if sub_empty:
                    # CACHE-PREFIX M1: keep every nested marker on its
                    # original block, including identical and distinct TTLs.
                    if _contains_cache_control(sub):
                        sub["text"] = REMINDER_PLACEHOLDER
                    else:
                        continue
                kept.append(sub)
            if removed or trimmed or content == [] or len(kept) != len(content):
                block["content"] = kept or [
                    {"type": "text", "text": REMINDER_PLACEHOLDER}
                ]
            return removed, False
        return 0, False

    return 0, False


def strip_all_system_reminders(body: dict) -> int:
    """Rule 3: Strip every Claude Code <system-reminder> span from user messages.

    Span-level and recursive: a block that holds a reminder *and* real content
    keeps the real content, and reminders nested inside ``tool_result`` content
    are stripped too (they are the dominant remaining cache-divergence class).

    A block left with nothing but whitespace is dropped; a user message left with
    no blocks keeps a single placeholder block. User messages are never removed,
    so message indices stay aligned between a live process and a process replaying
    the same conversation from its JSONL.
    """
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return 0
    count = 0
    placeholdered = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        kept: list = []
        # CACHE-PREFIX WARNING (M1): the request must stay an
        # append-extension of the previous request of the same session, and a
        # cache breakpoint may never be dropped (or added: the API limit is 4).
        # When a reminder-only block is dropped and it carries cache_control,
        # that marker MOVES to the nearest surviving earlier block of the same
        # message (or the next surviving block / the placeholder if none is
        # earlier). An occupied destination retains both markers by keeping a
        # fixed placeholder for the removed block, never coalescing TTL objects.
        # Dropping it silently leaves the request with no message
        # breakpoint, which reads only system+tools and writes nothing.
        # Live check: scripts/cache_regression.py reminder_block. Incident:
        # 2026-10-08/09, mission vault-mief.
        pending_markers = []
        for block in content:
            removed, now_empty = _strip_reminders_in_block(block)
            count += removed
            if now_empty:
                cc = block.get("cache_control") if isinstance(block, dict) else None
                if cc is not None:
                    if kept and isinstance(kept[-1], dict):
                        if "cache_control" not in kept[-1]:
                            kept[-1]["cache_control"] = cc
                        else:
                            kept.append({"type": "text", "text": REMINDER_PLACEHOLDER,
                                         "cache_control": cc})
                    else:
                        pending_markers.append(cc)
                continue
            if pending_markers and isinstance(block, dict):
                if "cache_control" not in block:
                    block["cache_control"] = pending_markers.pop(0)
                kept.extend({"type": "text", "text": REMINDER_PLACEHOLDER,
                             "cache_control": cc} for cc in pending_markers)
                pending_markers = []
            kept.append(block)
        if pending_markers:
            if not kept:
                placeholdered += 1
            kept.extend({"type": "text", "text": REMINDER_PLACEHOLDER,
                         "cache_control": cc} for cc in pending_markers)
        if content and not kept:
            kept = [{"type": "text", "text": REMINDER_PLACEHOLDER}]
            placeholdered += 1
        msg["content"] = kept
    stats["reminders_stripped"] += count
    stats["messages_placeholdered"] += placeholdered
    if placeholdered:
        log(f"WARN: {placeholdered} user message(s) were entirely reminders — "
            f"kept with a placeholder block instead of being dropped")
    return count


def strip_dynamic_reminders(body: dict) -> int:
    """Backward-compatible alias for strip_all_system_reminders."""
    return strip_all_system_reminders(body)


def normalize_git_status(body: dict) -> int:
    """Rule 4: Normalize git status section in system prompt.

    CACHE-PREFIX WARNING: must give byte-identical output for the same conversation
    in every process (live, resume, fork). Live check: scripts/cache_regression.py.

    The gitStatus section in sys[2] contains a snapshot of the working tree
    at the time the CC process started. Sessions started at different times
    see different untracked/modified files, creating byte-level divergence
    that breaks the cache for everything after sys[2] (tools + messages).

    Replaces the gitStatus section (from "gitStatus:" to end of sys[2])
    with a fixed placeholder.
    """
    system = body.get("system")
    if not system or not isinstance(system, list) or len(system) < 3:
        return 0
    block = system[2]
    if not isinstance(block, dict) or block.get("type") != "text":
        return 0
    text = block.get("text", "")
    new_text = re.sub(r'gitStatus:.*', 'gitStatus: normalized', text, flags=re.DOTALL)
    if new_text != text:
        block["text"] = new_text
        stats["git_status_normalized"] += 1
        return 1
    return 0


def normalize_tool_order(body: dict) -> int:
    """Rule 5: Sort tool definitions alphabetically by name.

    CACHE-PREFIX WARNING: must give byte-identical output for the same conversation
    in every process (live, resume, fork). Live check: scripts/cache_regression.py.

    Tool definitions arrive in filesystem readdir order, which is
    non-deterministic across process restarts and platforms. Different
    order = different bytes = cache miss on the tools section.
    """
    tools = body.get("tools")
    if not tools or not isinstance(tools, list):
        return 0
    original = [t.get("name", "") for t in tools]
    body["tools"] = sorted(tools, key=lambda t: t.get("name", ""))
    sorted_names = [t.get("name", "") for t in body["tools"]]
    if original != sorted_names:
        stats["tools_sorted"] += 1
        return 1
    return 0


def normalize_metadata(body: dict) -> int:
    """Rule 6: Normalize session-specific IDs in metadata.

    metadata.user_id contains the session UUID which differs between
    parent and fork. Normalizing it eliminates a potential divergence
    source and produces cleaner request diffs.

    CACHE-PREFIX WARNING: this field is not only cosmetic on the GPT route.
    CLIProxyAPI derives the provider cache key from it, so this rewrite made
    the whole GPT fleet share ONE cache key until 2026-10-12;
    assign_conversation_cache_key() now replaces it per conversation. Any change
    here must keep that working. Live check: scripts/cache_regression.py
    --providers codex and scripts/cliproxy_cache_key_probe.py
    (docs/cache-proxy-safety.md).
    """
    metadata = body.get("metadata")
    if not metadata or not isinstance(metadata, dict):
        return 0
    user_id = metadata.get("user_id", "")
    normalized = re.sub(r"_session_[a-f0-9-]+$", "_session_0", user_id)
    if normalized != user_id:
        metadata["user_id"] = normalized
        stats["metadata_normalized"] += 1
        return 1
    return 0


def _fix_array_items_recursive(schema: dict) -> int:
    """Recursively add ``items: {}`` to array schemas missing it.

    OpenAI requires ``items`` on every array-typed schema node.
    Anthropic doesn't care.  This walks the full JSON Schema tree.
    """
    if not isinstance(schema, dict):
        return 0

    count = 0

    # Direct array type without items
    if schema.get("type") == "array" and "items" not in schema:
        schema["items"] = {}
        count += 1

    # Recurse into nested schema keywords
    for key in ("properties", "patternProperties"):
        props = schema.get(key)
        if isinstance(props, dict):
            for prop_schema in props.values():
                count += _fix_array_items_recursive(prop_schema)

    for key in ("items", "additionalItems", "contains", "not",
                "if", "then", "else", "additionalProperties"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            count += _fix_array_items_recursive(sub)

    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                count += _fix_array_items_recursive(variant)

    return count


def sanitize_tool_schemas_for_openai(body: dict) -> int:
    """Sanitize tool schemas for OpenAI API compatibility.

    CACHE-PREFIX WARNING: must give byte-identical output for the same conversation
    in every process (live, resume, fork). Live check: scripts/cache_regression.py --providers codex.

    OpenAI rejects array schemas without ``items``.  Anthropic accepts them.
    Walk all tool input_schemas recursively and fix missing ``items``.

    Only call this for non-Claude model requests.
    """
    tools = body.get("tools")
    if not tools or not isinstance(tools, list):
        return 0

    count = 0
    for tool in tools:
        schema = tool.get("input_schema")
        if isinstance(schema, dict):
            fixed = _fix_array_items_recursive(schema)
            if fixed:
                tool_name = tool.get("name", "?")
                log(f"SCHEMA-FIX: {tool_name} — added items to {fixed} array(s)")
                count += fixed
    if count:
        stats["schemas_sanitized"] += count
    return count


def strip_unsupported_effort(body: dict) -> int:
    """Drop harness-injected effort fields from Haiku side requests.

    CLAUDE_CODE_EXTRA_BODY is merged into every request the CLI makes,
    including its Haiku side calls (titles, WebFetch summaries), and Haiku
    rejects output_config.effort and adaptive thinking with HTTP 400.
    """
    if not str(body.get("model", "")).startswith("claude-haiku"):
        return 0
    count = 0
    output_config = body.get("output_config")
    if isinstance(output_config, dict) and "effort" in output_config:
        output_config.pop("effort")
        if not output_config:
            body.pop("output_config")
        count += 1
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
        body.pop("thinking")
        count += 1
    return count


# ── Mid-session effort pinning ───────────────────────────────────────────
#
# Anthropic caches messages per top-level effort: changing
# output_config.effort re-reads the conversation from its first thinking
# turn onward. Models with per-message effort accept a
# {"role":"system","content":[],"output_config":{"effort":X}} message that
# changes effort from the next user turn WITHOUT touching the cached prefix
# before it. So for those models the proxy keeps the top-level effort pinned
# to the conversation's first-seen level and expresses every later /effort
# change as such a message.
#
# The injected messages exist only on the wire (the CLI never sees them), so
# they must be reproduced byte-identically on every later request, including
# after a resume, a fork, or a proxy restart. The store therefore keys on
# content, not session IDs:
#   bases[hash(model, messages[0])]      -> first-seen top-level effort
#   anchors[hash(model, messages[:i+1])] -> effort message placed before i
# A fork shares its parent's prefix, so it reproduces the parent's messages up
# to the fork point. The store is persisted as JSON next to the usage log.
#
# GPT via CLIProxyAPI has no equivalent: CLIProxyAPI v7.2.49 drops a
# role:system message when translating /v1/messages to Responses (verified
# 2026-09-23), so GPT keeps the top-level effort (one cache miss per change).
MID_EFFORT_BETA = "mid-conversation-output-config-2026-07-01"
MID_EFFORT_MODEL_PREFIXES = tuple(
    p.strip() for p in os.environ.get(
        "CACHE_PROXY_MID_EFFORT_MODELS",
        "claude-opus-5,claude-fable-5-1,claude-mythos-5-1",
    ).split(",") if p.strip()
)
EFFORT_PIN_FILE = os.path.join(LOG_DIR, "effort-pins.json")
EFFORT_PIN_TTL = 30 * 86400
EFFORT_PIN_MAX = 200_000
_DEFAULT = "__default__"  # stored stand-in for "no top-level effort"


def _mid_effort_supported(model: str) -> bool:
    return any(model.startswith(p) for p in MID_EFFORT_MODEL_PREFIXES)


def _is_real_user_prompt(msg: dict) -> bool:
    """A user message that starts a turn (not a tool_result continuation).

    An effort message may not sit between a tool_use and its tool_result, and
    OBS only changes effort on an idle session, so a change is always followed
    by a real prompt. Changes seen mid tool-loop are deferred to the next one.
    """
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return True


class EffortPinStore:
    def __init__(self, path: str | None):
        import threading
        self.path = path
        self.lock = threading.Lock()
        self.bases: dict[str, list] = {}    # key -> [effort, last_used]
        self.anchors: dict[str, list] = {}  # key -> [effort, last_used]
        # key -> [{"sections": {...}, "cm": ...}, last_used]; see pin_volatile_config
        self.variants: dict[str, list] = {}
        self.texts: dict[str, str] = {}  # sha256 -> pinned section text (dedup)
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.bases = data.get("bases", {})
            self.anchors = data.get("anchors", {})
            self.variants = data.get("variants", {})
            self.texts = data.get("texts", {})
        except Exception as e:
            log(f"effort pins unreadable, starting empty: {e}")

    def _save(self):
        if not self.path:
            return
        now = time.time()
        for table, ttl in ((self.bases, EFFORT_PIN_TTL), (self.anchors, EFFORT_PIN_TTL),
                           (self.variants, VARIANT_PIN_TTL)):
            stale = [k for k, v in table.items() if now - v[1] > ttl]
            for k in stale:
                del table[k]
            if len(table) > EFFORT_PIN_MAX:
                for k, _ in sorted(table.items(), key=lambda kv: kv[1][1])[
                        : len(table) - EFFORT_PIN_MAX]:
                    del table[k]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            live_texts = {h for v in self.variants.values()
                          for h in (v[0].get("s") or {}).values() if h}
            self.texts = {h: t for h, t in self.texts.items() if h in live_texts}
            json.dump({"bases": self.bases, "anchors": self.anchors,
                       "variants": self.variants, "texts": self.texts}, f)
        os.replace(tmp, self.path)


_effort_pins: EffortPinStore | None = None


def _get_effort_pins() -> EffortPinStore:
    global _effort_pins
    if _effort_pins is None:
        _effort_pins = EffortPinStore(EFFORT_PIN_FILE)
    return _effort_pins


def _strip_cache_control(obj):
    # cache_control is a breakpoint hint the CLI moves to the newest message on
    # every request; it is not part of the cached content, so ignore it.
    if isinstance(obj, dict):
        return {k: _strip_cache_control(v) for k, v in obj.items()
                if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_cache_control(v) for v in obj]
    return obj


def _msg_bytes(msg: dict) -> bytes:
    return json.dumps(_strip_cache_control(msg), sort_keys=True,
                      separators=(",", ":")).encode()


def pin_effort(body: dict, store: EffortPinStore | None = None) -> dict:
    """Pin top-level effort; express changes as per-message effort.

    CACHE-PREFIX WARNING: a top-level effort change mid-conversation changes the
    request settings and re-bills the conversation; this pin exists to prevent
    that. Live check: scripts/cache_regression.py resume_new_proc.

    Returns {"inserted": n, "changed": bool, "beta": bool}. Mutates body.
    Must run after normalize_request so hashes cover normalized history.
    """
    import hashlib
    result = {"inserted": 0, "changed": False, "beta": False}
    model = str(body.get("model", ""))
    msgs = body.get("messages")
    if not _mid_effort_supported(model) or not isinstance(msgs, list) or not msgs:
        return result
    store = store or _get_effort_pins()
    oc = body.get("output_config")
    cur = (oc.get("effort") if isinstance(oc, dict) else None) or _DEFAULT
    if cur == "high":  # the API default; same behavior as omitting it
        cur = _DEFAULT
    now = time.time()

    running = hashlib.sha256(model.encode() + b"\0")
    prefix_keys = []
    for m in msgs:
        running.update(_msg_bytes(m) + b"\0")
        prefix_keys.append(running.copy().hexdigest())
    base_key = prefix_keys[0]

    dirty = False
    with store.lock:
        entry = store.bases.get(base_key)
        if entry is None:
            # A conversation first seen mid-way predates this proxy (or its
            # store): its history was cached with no top-level effort, which is
            # what the CLI sent for these models before effort was injected.
            base = cur if len(msgs) == 1 else _DEFAULT
            store.bases[base_key] = [base, now]
            dirty = True
        else:
            base = entry[0]
            entry[1] = now

        inserts: dict[int, str] = {}
        effective = base
        for i, m in enumerate(msgs):
            if i == 0 or not _is_real_user_prompt(m):
                continue
            a = store.anchors.get(prefix_keys[i])
            if a is not None:
                inserts[i] = a[0]
                effective = a[0]
                a[1] = now

        last = len(msgs) - 1
        if cur != effective:
            if last > 0 and _is_real_user_prompt(msgs[last]):
                store.anchors[prefix_keys[last]] = [cur, now]
                inserts[last] = cur
                result["changed"] = True
                dirty = True
            elif last == 0:
                # Identical first message to another conversation that used a
                # different effort: nothing cached to protect, send as asked.
                return result
            # else: mid tool-loop; the change applies from the next real prompt.
        if dirty:
            try:
                store._save()
            except Exception as e:
                log(f"effort pins save failed: {e}")

    if isinstance(oc, dict):
        oc.pop("effort", None)
        if base != _DEFAULT:
            oc["effort"] = base
        elif not oc:
            body.pop("output_config")
    elif base != _DEFAULT:
        body["output_config"] = {"effort": base}

    if inserts:
        out = []
        for i, m in enumerate(msgs):
            if i in inserts:
                eff = inserts[i]
                out.append({"role": "system", "content": [],
                            "output_config": {"effort": "high" if eff == _DEFAULT else eff}})
            out.append(m)
        body["messages"] = out
        result["inserted"] = len(inserts)
        result["beta"] = True
    return result


# ── Per-conversation pinning of server-driven prompt variants ────────────
#
# CACHE-PREFIX WARNING (2026-10-12, mission vault-1r7n): Claude Code receives
# feature values from Anthropic's servers. Occasionally a fleet-wide refresh
# (observed 2026-10-09 09:28Z and 2026-10-12 15:21Z; cause unknown) flips them
# between two requests of the SAME conversation:
#   * the "# Output efficiency" section of the system prompt appears/disappears;
#   * the top-level ``context_management`` field (a clear_thinking edit)
#     appears/disappears.
# Either flip changes the cached prefix and re-bills the whole conversation
# (one request re-billed 391k tokens on 2026-10-12). We cannot stop the flip,
# so each conversation is kept on the variant it was FIRST seen with. The key
# is model + first message (like the effort pins), so resumes, wakes, restarts
# and forks of that conversation share the pin. New conversations record
# whatever the server sends at that time. Only the listed section and that one
# field are ever touched.
#
# "# MCP Server Instructions" (2026-10-16, bead ops-30): the CLI rebuilds this
# section on every request from the MCP servers connected in THIS process
# (its own source marks it "MCP servers connect/disconnect between turns").
# At 04:10Z the remote exa server began returning instructions, so every live
# Claude session's next process gained the section before gitStatus and
# re-billed its whole prefix (~11 Opus sessions, ~3.7M tokens). A server that
# fails to connect for one process removes it again. Pinned like the others.
# Limitation: server instructions containing a line that starts with "# "
# end the matched section early, so such a flip is not fully pinned.
# Live check: scripts/cache_regression.py volatile_flip (docs/cache-proxy-safety.md).
# CACHE-PREFIX WARNING: if another server-flag-driven system section starts
# flipping mid-conversation (watch system-changed breaks in cache_tally.py
# --breaks-out + prefix_diff.py), add its heading here and extend volatile_flip.
# A heading added later is recorded for existing conversations on their next
# request (their current form), so adding one never re-bills anything.
VOLATILE_SYSTEM_SECTIONS = ("# Output efficiency", "# MCP Server Instructions")
# A section ends at the next top-level heading or at the CLI's trailing
# gitStatus block (normalized by then), which the MCP section precedes.
_SECTION_FOLLOWERS = ("# ", "gitStatus: ")
# A pin only matters while the conversation's cache can still be read (1 hour
# after its last request); 2 days of inactivity is ample and keeps the file small.
VARIANT_PIN_TTL = 2 * 86400
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"


def _volatile_section_re(heading: str) -> re.Pattern:
    # The section runs from its heading line up to the next top-level heading
    # (or the end of the block); the separating blank line belongs to it, so
    # removing it yields exactly the CLI's own "section absent" form.
    return re.compile(r"(?ms)^" + re.escape(heading) + r"\n.*?(?:\n\n(?=# |gitStatus: )|\Z)")


def _find_volatile_section(system: list, heading: str):
    """Return (block_index, start, end, text, following_heading) or None."""
    rx = _volatile_section_re(heading)
    for i, block in enumerate(system):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        m = rx.search(text)
        if m:
            rest = text[m.end():]
            following = rest.split("\n", 1)[0] if rest.startswith(_SECTION_FOLLOWERS) else None
            return i, m.start(), m.end(), m.group(0), following
    return None


def pin_volatile_config(body: dict, store: "EffortPinStore | None" = None) -> dict:
    """Keep server-driven prompt variants stable within one conversation.

    Returns {"recorded": bool, "applied": [...], "beta": bool}. Mutates body.
    Must run after normalize_request so the key covers normalized history.
    """
    result = {"recorded": False, "applied": [], "beta": False}
    msgs = body.get("messages")
    system = body.get("system")
    if not isinstance(msgs, list) or not msgs or not isinstance(system, list):
        return result
    if not body.get("tools"):
        return result  # tool-less side calls (titles, summaries): nothing worth pinning
    store = store or _get_effort_pins()
    model = str(body.get("model", ""))
    key = hashlib.sha256(model.encode() + b"\0" + _msg_bytes(msgs[0]) + b"\0").hexdigest()
    found = {h: _find_volatile_section(system, h) for h in VOLATILE_SYSTEM_SECTIONS}
    cm = body.get("context_management")
    # original key order is kept so a restored field is byte-identical
    cm_json = json.dumps(cm, separators=(",", ":")) if cm is not None else None
    now = time.time()
    dirty = False
    with store.lock:
        entry = store.variants.get(key)
        if entry is None:
            sections, anchors = {}, {}
            for h, f in found.items():
                if f:
                    digest = hashlib.sha256(f[3].encode()).hexdigest()
                    store.texts[digest] = f[3]
                    sections[h] = digest
                    anchors[h] = [f[0], f[4]]
                else:
                    sections[h] = None
            store.variants[key] = [{"s": sections, "a": anchors, "cm": cm_json}, now]
            result["recorded"] = True
            dirty = True
        else:
            entry[1] = now
            pinned = entry[0]
            restores = []
            for h, f in found.items():
                if h not in (pinned.get("s") or {}):
                    # heading added after this pin was made: record the
                    # conversation's current form, change nothing now
                    if f:
                        digest = hashlib.sha256(f[3].encode()).hexdigest()
                        store.texts[digest] = f[3]
                        pinned.setdefault("s", {})[h] = digest
                        pinned.setdefault("a", {})[h] = [f[0], f[4]]
                    else:
                        pinned.setdefault("s", {})[h] = None
                    dirty = True
                    continue
                want = pinned["s"][h]
                have = hashlib.sha256(f[3].encode()).hexdigest() if f else None
                if want == have:
                    continue
                # positions are re-found: another section's edit in the same
                # block shifts them
                f = _find_volatile_section(system, h)
                if f and want is None:  # section appeared: remove it
                    t = system[f[0]]["text"]
                    system[f[0]]["text"] = t[:f[1]] + t[f[2]:]
                    result["applied"].append(f"removed:{h}")
                elif f:  # different text in place: put the pinned text there
                    text = store.texts.get(want)
                    if text is not None:
                        t = system[f[0]]["text"]
                        system[f[0]]["text"] = t[:f[1]] + text + t[f[2]:]
                        result["applied"].append(f"restored:{h}")
                elif want is not None:  # section disappeared
                    restores.append(h)
            # A pinned section's anchor (the line that followed it) may itself
            # be another pinned section that is being restored, so retry until
            # no restore makes progress.
            while restores:
                progressed = False
                for h in list(restores):
                    want = pinned["s"][h]
                    text = store.texts.get(want)
                    anchor = (pinned.get("a") or {}).get(h) or [None, None]
                    i, following = anchor
                    if text is None or i is None or i >= len(system):
                        restores.remove(h)
                        continue
                    blk = system[i]
                    if not isinstance(blk, dict) or not isinstance(blk.get("text"), str):
                        restores.remove(h)
                        continue
                    t = blk["text"]
                    if following is None:
                        if t and not t.endswith("\n\n"):
                            continue
                        blk["text"] = t + text
                    else:
                        m = re.search(r"(?m)^" + re.escape(following) + r"$", t)
                        if not m:
                            continue
                        blk["text"] = t[:m.start()] + text + t[m.start():]
                    restores.remove(h)
                    progressed = True
                    result["applied"].append(f"restored:{h}")
                if not progressed:
                    break
            want_cm = pinned.get("cm", cm_json)
            if (json.loads(want_cm) if want_cm is not None else None) != cm:
                if want_cm is None:
                    body.pop("context_management", None)
                    result["applied"].append("removed:context_management")
                else:
                    thinking = body.get("thinking")
                    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"):
                        body["context_management"] = json.loads(want_cm)
                        result["applied"].append("restored:context_management")
                        result["beta"] = True
        if dirty:
            try:
                store._save()
            except Exception as e:
                log(f"variant pins save failed: {e}")
    if result["applied"]:
        stats["volatile_pins_applied"] += 1
    return result


def assign_conversation_cache_key(body: dict) -> str | None:
    """GPT route only: give each conversation its own provider cache key.

    CACHE-PREFIX WARNING (2026-10-12, mission vault-1r7n): CLIProxyAPI derives
    the Codex ``prompt_cache_key`` from the session part of metadata.user_id
    (verified by direct probe: same session -> same key, different session ->
    different key). normalize_metadata() rewrites that part to a constant, so
    before this fix EVERY GPT conversation of the fleet shared one key, and many
    byte-identical prefixes got no cache hit (the provider-side reason -- e.g.
    per-key routing overflow -- is a hypothesis, not proven). Here the session
    part becomes an id derived from model + first message, so a conversation,
    its resumes and its forks share one key while different conversations get
    different keys. Never applied to Anthropic.
    If CLIProxyAPI is upgraded this can silently stop helping. Live check:
    scripts/cliproxy_cache_key_probe.py, then scripts/cache_regression.py
    --providers codex (docs/cache-proxy-safety.md).
    """
    msgs = body.get("messages")
    md = body.get("metadata")
    if not isinstance(msgs, list) or not msgs or not isinstance(md, dict):
        return None
    uid = md.get("user_id")
    if not isinstance(uid, str) or not uid.endswith("_session_0"):
        return None
    digest = hashlib.sha256(str(body.get("model", "")).encode() + b"\0"
                            + _msg_bytes(msgs[0])).hexdigest()
    conv = f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    md["user_id"] = uid[: -len("0")] + conv
    return conv


def _add_beta(headers: dict, beta: str) -> None:
    for k in list(headers):
        if k.lower() == "anthropic-beta":
            parts = [p.strip() for p in headers[k].split(",") if p.strip()]
            if beta not in parts:
                headers[k] = ",".join(parts + [beta])
            return
    headers["anthropic-beta"] = beta


# CACHE-PREFIX WARNING (M3): the request must stay an append-extension of
# the previous request of the same session; never let the Claude Code billing
# placeholder (the "cch=" key followed by five zero characters) appear in
# message content. The CLI rewrites the FIRST occurrence of that placeholder in
# the serialized body with a random hash on every request. `messages` serialize
# before `system`, so a message that contains the placeholder gets a new random
# value each call and the whole conversation stops caching, while the system
# header keeps the placeholder. restore_cch_placeholder() is the exact inverse:
# when the system billing header still carries the placeholder and an earlier
# "cch=<5 hex>" occurrence exists, that first occurrence is put back. Everything
# else stays byte-identical. Runs on the raw bytes BEFORE normalize_request().
# Live check: scripts/cache_regression.py cch_collision. Incident: 2026-10-08/09,
# mission vault-mief. Do not write the placeholder literal in prompts, notes,
# logs or source comments that agents may read.
# Working on the proxy safely: read this file and the proxy logs only through a
# redacting filter (sed -E on the placeholder regex) and describe the placeholder by
# regex only. 2026-10-09 self-poisoning cost the mission ~30-35 weekly points.
_CCH_PLACEHOLDER = b"cch=" + b"0" * 5
_CCH_HASH_RE = re.compile(rb"cch=[0-9a-f]{5}(?![0-9a-fA-F])")
_BILLING_HEADER_MARK = b"x-anthropic-billing-header:"


def restore_cch_placeholder(raw: bytes) -> tuple[bytes, int]:
    """Undo the CLI's billing-hash mutation of an earlier message occurrence.

    The system billing header is the LAST billing-header marker in the body
    (`system` serializes after `messages`). If it still carries the placeholder,
    the CLI's single replacement landed on an earlier occurrence. Placeholders
    further down in messages (echoed in tool input/output) are irrelevant: only
    the first occurrence of the whole body was ever rewritten.
    """
    h = raw.rfind(_BILLING_HEADER_MARK)
    if h < 0 or raw.find(_CCH_PLACEHOLDER, h, h + 1000) < 0:
        return raw, 0  # header already carries a hash: nothing was displaced
    m = _CCH_HASH_RE.search(raw, 0, h)
    if m is None:
        return raw, 0
    stats["cch_restored"] += 1
    return raw[:m.start()] + _CCH_PLACEHOLDER + raw[m.end():], 1


# CACHE-PREFIX WARNING (M4): CLI global-cache rollout inserts this exact
# standalone system line between existing prompt sections. Strip only that
# scaffolding and its added separator, not user/tool text or other whitespace.
# A break is silent: no error, the API just re-bills the whole conversation.
# Live check: scripts/cache_regression.py boundary_flip boundary_marker (docs/cache-proxy-safety.md).
_DYNAMIC_BOUNDARY_LINE = re.compile(r"(?m)^__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n(?:\n)?")


def normalize_dynamic_boundary(body: dict) -> int:
    system = body.get("system")
    if not isinstance(system, list):
        return 0
    count = 0
    # CLI 2.1.59 can consume the marker itself, splitting the same prompt into
    # stable/dynamic blocks. Rejoin only its exact SDK/auto-memory shape.
    # CACHE-PREFIX WARNING (2026-10-12, mission vault-1r7n): when BOTH halves
    # carry a cache breakpoint the request is left as the CLI sent it. The
    # previous attempt (commit 09636e3) kept an empty text block holding the
    # second breakpoint, and the Anthropic API rejects that with HTTP 400
    # ("cache_control cannot be set for empty text blocks"). That shape has
    # never been seen in real CLI traffic; do not reintroduce empty carriers.
    if (len(system) == 4 and all(isinstance(b, dict) and b.get("type") == "text" for b in system)
            and str(system[0].get("text", "")).startswith("x-anthropic-billing-header:")
            and system[1].get("text") == "You are a Claude agent, built on Anthropic's Claude Agent SDK."
            and str(system[2].get("text", "")).startswith("\nYou are an interactive agent")
            and str(system[3].get("text", "")).startswith("# auto memory\n")
            and set(system[3]).issubset({"type", "text", "cache_control"})
            and not ("cache_control" in system[3] and "cache_control" in system[2])):
        system[2]["text"] += "\n\n" + system[3]["text"]
        if "cache_control" in system[3]:
            system[2]["cache_control"] = system[3]["cache_control"]
        del system[3]
        count += 1
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                block["text"], n = _DYNAMIC_BOUNDARY_LINE.subn("", text)
                count += n
    return count


def normalize_request(body: dict) -> tuple[dict, dict]:
    """Apply all normalizations to a request body in spec order.

    CACHE-PREFIX WARNING: this is the shared-history contract for forking, not
    merely a performance filter. Never allow non-JSONL runtime additions through
    to fix a transport/hook bug. A break is silent: no error, the API just
    re-bills the whole conversation. Any change requires actual
    parent/fork/resume normalized-prefix equivalence; preserving a notice alone
    is not proof. Live check: scripts/cache_regression.py (all scenarios, both
    routes) -- see docs/cache-proxy-safety.md.

    Order: billing → strings → strip all system reminders → git status →
           tool sorting → metadata

    cache_control is deliberately left untouched — it's not part of the cache
    key, and CC's native placement is sufficient.

    Returns (modified_body, info_dict) with details of what was normalized.
    """
    info = {}

    # 1. Normalize billing header (first — start of token stream)
    info["billing"] = normalize_billing_header(body)
    info["dynamic_boundary"] = normalize_dynamic_boundary(body)

    # 2. Convert bare string content to list format (before skill listing move)
    info["strings"] = normalize_user_content_structure(body)

    # 3. Strip every Claude Code system-reminder, including skill listings and
    # CLAUDE.md project-context reminders. OBS-owned project context is injected
    # into JSONL separately.
    info["skill"] = {"action": "merged_into_reminders"}
    _norm_local.trimmed = 0
    info["reminders"] = strip_all_system_reminders(body)
    # tool_result texts changed by whitespace trimming alone (no reminder)
    info["ws_trimmed"] = getattr(_norm_local, "trimmed", 0)

    # 4. Normalize git status in system prompt
    info["git_status"] = normalize_git_status(body)

    # 5. Sort tool definitions
    info["tools"] = normalize_tool_order(body)

    # 6. Normalize metadata
    info["metadata"] = normalize_metadata(body)

    # Determine overall action label
    actions_taken = [
        info["billing"], info["strings"], info["reminders"],
        info["git_status"], info["tools"], info["metadata"], info["dynamic_boundary"],
        info["ws_trimmed"],
    ]
    if any(actions_taken):
        info["action"] = "normalized"
    else:
        info["action"] = "not_found"

    return body, info


# ── Logging ──────────────────────────────────────────────────────────────


def log(msg: str):
    sys.stderr.write(f"  [cache-proxy {time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def log_usage_entry(norm_action: str, usage: dict, model: str = "", route: str = "",
                    req_id: str = "", session_id: str = "", http_status: int | None = None):
    """Append a usage entry to the structured JSONL log.

    ``model`` and ``route`` let a reader tell local turns apart from hosted
    ones. ``req_id``/``session_id``/``http_status`` (added 2026-10-08) join a
    usage line to its request-log record and its Claude session. All are
    additive keys; existing readers do keyed lookups.
    """
    entry = {
        "ts": time.time(),
        "norm_action": norm_action,
        "model": model,
        "route": route,
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_creation": usage.get("cache_creation_input_tokens", 0),
        "input_tokens": usage.get("input_tokens", 0),
    }
    entry["total"] = entry["cache_read"] + entry["cache_creation"] + entry["input_tokens"]
    entry["cache_rate"] = (entry["cache_read"] / entry["total"]
                           if entry["total"] > 0 else 0.0)
    if req_id:
        entry["req_id"] = req_id
    if session_id:
        entry["session_id"] = session_id
    if http_status is not None:
        entry["http_status"] = http_status
    try:
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    cr = entry["cache_read"]
    cc = entry["cache_creation"]
    ip = entry["input_tokens"]
    tot = entry["total"]
    rate = entry["cache_rate"]
    suffix = f" route={route}" if route else ""
    if session_id:
        suffix += f" session={session_id[:8]} req={req_id}"
    log(f"USAGE: tot={tot:,} cr={cr:,} cc={cc:,} ip={ip} ({rate:.0%} cached) "
        f"[{norm_action}]{suffix}")


# ── Durable request log ──────────────────────────────────────────────────

_SESSION_IN_USER_ID_RE = re.compile(r"_session_([0-9a-fA-F-]{8,})$")
# Header names whose values are never written (substring match, lowercase).
_SECRET_HEADER_PARTS = ("auth", "key", "cookie", "token", "secret", "session-token")


def extract_session_id(body: dict) -> str:
    """Claude session id from metadata.user_id (``..._session_<uuid>``).

    Must be read BEFORE normalize_metadata() replaces it with ``_session_0``.
    """
    try:
        user_id = (body.get("metadata") or {}).get("user_id") or ""
    except AttributeError:
        return ""
    m = _SESSION_IN_USER_ID_RE.search(user_id) if isinstance(user_id, str) else None
    return m.group(1) if m else ""


# Secret-looking values inside captured bodies/index lines. Each match becomes
# ``[REDACTED:<kind>:<sha256-12>]`` (kind names deliberately avoid the words
# key/token/secret so a placeholder never re-matches; redaction is idempotent): same value → same placeholder, so two
# captures still compare equal/unequal exactly where the originals did (the
# property scripts/prefix_diff.py relies on); offsets shift by the length
# difference. Placeholders contain no quote or backslash, so JSON stays valid.
# The generic long-run rule also replaces image base64, thinking signatures and
# Telegram/Telethon session strings (all >= 300 base64 chars) — equality is
# preserved, the payload is not. The pattern for key=value pairs keeps the key.
_REDACT_PATTERNS: list[tuple[str, "re.Pattern[bytes]", int]] = [
    ("privkey", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), 0),
    ("anthropic", re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    ("sk", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), 0),
    ("github", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"), 0),
    ("slack", re.compile(rb"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), 0),
    ("aws", re.compile(rb"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("google", re.compile(rb"\b(?:AIza[0-9A-Za-z_\-]{35}|ya29\.[0-9A-Za-z_\-]{20,})"), 0),
    ("telegram-bot", re.compile(rb"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), 0),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), 0),
    ("bearer", re.compile(rb"(?i)(\bbearer\s+)([A-Za-z0-9._\-~+/]{20,}=*)"), 2),
    ("assigned", re.compile(
        rb"(?i)((?:api_?key|access_?key|_key|token|secret[A-Za-z0-9_]*|password|passwd|api_hash|"
        rb"session_?string|string_?session)[\"']?\s*[:=]\s*[\"']?)([^\s\"'\\,;}{\]\[]{8,})"), 2),
    # Run-start lookbehind + possessive quantifier keep this linear.
    ("b64", re.compile(rb"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{300,}+={0,2}"), 0),
]


def _looks_random(value: bytes) -> bool:
    """Base64 key material mixes cases and digits densely; paths do not."""
    n = len(value)
    upper = sum(1 for c in value if 65 <= c <= 90)
    lower = sum(1 for c in value if 97 <= c <= 122)
    digit = sum(1 for c in value if 48 <= c <= 57)
    return upper > n * 0.15 and lower > n * 0.15 and digit > n * 0.03


def redact_secrets(data: bytes) -> bytes:
    """Replace secret-looking values with hashed placeholders (see above)."""
    for kind, pattern, group in _REDACT_PATTERNS:
        def _sub(m, kind=kind, group=group):
            value = m.group(group)
            if value.startswith(b"[REDACTED:"):
                return m.group(0)
            if kind == "b64" and not _looks_random(value):
                return m.group(0)  # long path/identifier, not key material
            tag = b"[REDACTED:" + kind.encode() + b":" + \
                hashlib.sha256(value).hexdigest()[:12].encode() + b"]"
            if group == 0:
                return tag
            return m.group(0)[: m.start(group) - m.start(0)] + tag + \
                m.group(0)[m.end(group) - m.start(0):]
        data = pattern.sub(_sub, data)
    return data


_OBS_ORIGIN_RE = re.compile(r"<fork_context><origin>([a-z_]+)</origin>")
_OBS_PARENT_RE = re.compile(r"<parent_session_id>([0-9a-fA-F-]{8,})</parent_session_id>")


def extract_obs_fork_context(body: dict) -> dict:
    """origin / parent_session_id from the LAST user message's obs-bootstrap.

    OBS prepends ``<obs-bootstrap>…<fork_context><origin>X</origin>…
    <parent_session_id>Y</parent_session_id>`` to the first user message of
    a new session (fresh task, fork, session_recovery). Returns {} otherwise.
    """
    try:
        msgs = body.get("messages") or []
        last = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        if not last:
            return {}
        content = last.get("content")
        texts = [content] if isinstance(content, str) else [
            b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        out = {}
        for t in texts:
            if "<obs-bootstrap" not in t:
                continue
            m = _OBS_ORIGIN_RE.search(t)
            if m:
                out["origin"] = m.group(1)
            m = _OBS_PARENT_RE.search(t)
            if m:
                out["parent_session_id"] = m.group(1)
        return out
    except Exception:
        return {}


def sanitize_headers(headers) -> dict:
    """Request headers safe to persist: credentials are dropped entirely."""
    out = {}
    for k, v in headers.items():
        lk = k.lower()
        if any(p in lk for p in _SECRET_HEADER_PARTS):
            continue
        out[lk] = v
    return out


class RequestLogger:
    """Durable per-request capture for prefix-breakage diagnosis.

    Layout (``root`` = CACHE_PROXY_REQUEST_LOG_DIR):
      root/YYYY-MM-DD/index.jsonl                 one line per completed request
      root/YYYY-MM-DD/<req_id>.<part>.json.gz     part ∈ pre | post | wire
    ``req_id`` = ``<UTC yyyymmddTHHMMSS.ffffff>Z-<pid>-<seq>-<session8>`` — unique
    across restarts (time + pid) and sortable by time. ``pre`` is the byte
    string the CLI sent; ``wire`` the bytes sent upstream; ``post`` the body
    right after normalize_request() (before route-specific effort handling),
    written only when it differs from ``wire``. All three — and every index
    line — pass through redact_secrets() first, so they are exact except that
    secret-looking values are replaced by hashed placeholders (equality kept);
    ``wire_sha256`` in the index is of the unredacted wire bytes.

    All work happens on one daemon thread fed by a bounded queue. If the queue
    is full the record is dropped and counted — traffic is never slowed or
    failed by logging. The whole tree (bodies + index) is a ring buffer:
    oldest files are evicted once ``max_bytes`` is exceeded (see _prune).
    """

    def __init__(self, root: str, max_bytes: int = REQUEST_LOG_MAX_BYTES,
                 queue_size: int = 32):
        self.root = root
        self.max_bytes = max_bytes
        self._seq = itertools.count(1)
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self.dropped = 0
        self.errors = 0
        self.written = 0
        self._bytes = None  # lazily measured on first write
        os.makedirs(root, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="request-log", daemon=True)
        self._thread.start()

    # -- request-path API (cheap, never raises) --

    def new_request_id(self, session_id: str = "") -> str:
        now = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f".{int((now % 1) * 1e6):06d}Z"
        sid = (session_id or "nosession")[:8]
        return f"{stamp}-{os.getpid()}-{next(self._seq):06d}-{sid}"

    def submit_bodies(self, req_id: str, pre: bytes, post: bytes | None, wire: bytes):
        self._put(("bodies", req_id, pre, post, wire))

    def submit_index(self, record: dict):
        self._put(("index", record))

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
                log(f"request-log queue full: {self.dropped} record(s) dropped so far")
        except Exception:
            self.errors += 1

    def flush(self, timeout: float = 10.0):
        """Block until queued items are written (tests/shutdown only)."""
        done = threading.Event()
        self._put(("barrier", done))
        done.wait(timeout)

    # -- writer thread --

    @staticmethod
    def _day_of(req_id: str) -> str:
        return f"{req_id[0:4]}-{req_id[4:6]}-{req_id[6:8]}"

    def _run(self):
        while True:
            item = self._q.get()
            try:
                kind = item[0]
                if kind == "barrier":
                    item[1].set()
                elif kind == "bodies":
                    self._write_bodies(*item[1:])
                elif kind == "index":
                    self._write_index(item[1])
            except Exception as e:  # never let the writer die
                self.errors += 1
                try:
                    log(f"request-log write error: {e}")
                except Exception:
                    pass

    def _write_bodies(self, req_id: str, pre: bytes, post: bytes | None, wire: bytes):
        day_dir = os.path.join(self.root, self._day_of(req_id))
        os.makedirs(day_dir, exist_ok=True)
        added = 0
        parts = [("pre", pre), ("wire", wire)]
        if post is not None and post != wire:   # compare BEFORE redaction
            parts.append(("post", post))
        for part, data in parts:
            data = redact_secrets(data)
            path = os.path.join(day_dir, f"{req_id}.{part}.json.gz")
            with gzip.open(path, "wb", compresslevel=6) as f:
                f.write(data)
            added += os.path.getsize(path)
        self.written += 1
        self._account(added)

    def _write_index(self, record: dict):
        req_id = record.get("req_id", "")
        day_dir = os.path.join(self.root, self._day_of(req_id))
        os.makedirs(day_dir, exist_ok=True)
        line = redact_secrets(json.dumps(record, separators=(",", ":")).encode()) + b"\n"
        with open(os.path.join(day_dir, "index.jsonl"), "ab") as f:
            f.write(line)
        self._account(len(line))

    def _measure(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
        return total

    def _account(self, added: int):
        if self._bytes is None:
            self._bytes = self._measure()
        else:
            self._bytes += added
        if self._bytes > self.max_bytes:
            self._prune()

    def _prune(self):
        """Ring-buffer eviction: oldest first, down to 90% of the cap.

        The cap covers everything under ``root`` (bodies AND index files).
        Days are visited oldest first; within a day body files go oldest first
        (names sort by time). A day whose bodies are all gone loses its
        index.jsonl too, unless it is the newest day (still being written).
        Index rows whose bodies were evicted stay readable; prefix_diff
        reports them as missing.
        """
        target = int(self.max_bytes * 0.9)
        days = sorted(d for d in os.listdir(self.root)
                      if os.path.isdir(os.path.join(self.root, d)))
        total = self._measure()
        for n, day in enumerate(days):
            if total <= target:
                break
            day_dir = os.path.join(self.root, day)
            for name in sorted(os.listdir(day_dir)):
                if total <= target:
                    break
                if not name.endswith(".json.gz"):
                    continue
                path = os.path.join(day_dir, name)
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                    total -= size
                except OSError:
                    pass
            is_newest = n == len(days) - 1
            if total > target and not is_newest:
                for name in os.listdir(day_dir):
                    path = os.path.join(day_dir, name)
                    try:
                        size = os.path.getsize(path)
                        os.remove(path)
                        total -= size
                    except OSError:
                        pass
                try:
                    os.rmdir(day_dir)
                except OSError:
                    pass
        self._bytes = total


REQUEST_LOGGER: RequestLogger | None = None


def parse_sse_usage(sse_chunks: list[bytes]) -> dict:
    """Extract usage from an SSE response, merging message_start and message_delta.

    Anthropic reports the full usage in ``message_start``. The local gate reports
    the whole prompt as ``input_tokens`` in ``message_start`` and only emits the
    cache split (``cache_read_input_tokens`` / ``cache_creation_input_tokens``)
    in ``message_delta``, so reading ``message_start`` alone logged
    ``cache_read: 0`` for every streaming local turn. Merge both, last value
    wins per key. A stream that carries only ``message_start`` yields exactly
    what it yielded before.
    """
    full = b"".join(sse_chunks).decode("utf-8", errors="replace")
    usage: dict = {}
    for line in full.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except Exception:
            continue
        etype = event.get("type")
        if etype == "message_start":
            chunk_usage = event.get("message", {}).get("usage", {})
        elif etype == "message_delta":
            chunk_usage = event.get("usage", {})
        else:
            continue
        if isinstance(chunk_usage, dict):
            usage.update(chunk_usage)
    return usage


# vLLM rejects input + max_tokens > window with this text (the local gate wraps
# it in an HTTP 500 internal_error). Claude Code only recovers from Anthropic's
# wording ("input length and `max_tokens` exceed context limit: I + M > W"):
# it retries the same request with max_tokens = max(3000, W - I - 1000). So a
# local turn that grows past window - max_tokens degrades to a shorter reply
# instead of killing the session (vault-u3b.64). Local route only.
_VLLM_OVERFLOW_RE = re.compile(
    r"maximum context length is (\d+) tokens\. However, you requested (\d+) "
    r"output tokens and your prompt contains at least (\d+) input tokens"
)


def translate_local_overflow_error(status: int, body: bytes) -> bytes | None:
    """Return an Anthropic-format 400 body for a vLLM max_tokens overflow, else None."""
    if status < 400 or not body:
        return None
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return None
    match = _VLLM_OVERFLOW_RE.search(text)
    if not match:
        return None
    window, max_tokens, input_tokens = (int(g) for g in match.groups())
    message = (
        f"input length and `max_tokens` exceed context limit: "
        f"{input_tokens} + {max_tokens} > {window}, decrease input length or "
        f"`max_tokens` and try again"
    )
    return json.dumps(
        {"type": "error", "error": {"type": "invalid_request_error", "message": message}},
        separators=(",", ":"),
    ).encode()


class LocalUsageFixer:
    """Rewrite one local-route SSE usage shape so Claude Code sums it correctly.

    The local gate (vLLM's native ``/v1/messages``) reports the whole prompt S
    as ``input_tokens`` in ``message_start`` and the real split only at the end,
    in ``message_delta`` (``input_tokens`` = uncached remainder, plus
    ``cache_read_input_tokens`` / ``cache_creation_input_tokens``). The bundled
    CLI (2.1.59) merges stream usage field by field, but only when the new value
    is ``> 0`` (its ``AEH`` merge). When the remainder is exactly 0 — the whole
    prompt was cache-read or cache-created — the CLI keeps S from
    ``message_start`` and adds ``cr + cc`` (= S) on top: the turn is recorded
    as 2S. That doubled number drives auto-compaction, the ``Context: ~N``
    note, ``context_info`` and the level context guard (seen live: 364,288
    recorded for a 182,144-token prompt; 67 of 1,395 local turns).

    Fix, applied only in that exact case: move one token from a cache bucket
    to ``input_tokens`` (``input_tokens: 1``), so the CLI overwrites S and the
    recorded total ``input + cr + cc`` equals S exactly. Every other event and
    byte passes through unchanged. Events are re-framed only on SSE event
    boundaries (blank line), and complete events are forwarded immediately.
    """

    def __init__(self):
        self._buf = b""
        self.rewrites = 0

    @staticmethod
    def _fix_event(event: bytes) -> bytes:
        if b'"message_delta"' not in event:
            return event
        lines = event.split(b"\n")
        changed = False
        for i, line in enumerate(lines):
            if not line.startswith(b"data: "):
                continue
            try:
                obj = json.loads(line[6:])
            except Exception:
                return event
            if not isinstance(obj, dict) or obj.get("type") != "message_delta":
                return event
            usage = obj.get("usage")
            if not isinstance(usage, dict):
                return event
            it = usage.get("input_tokens")
            cr = usage.get("cache_read_input_tokens") or 0
            cc = usage.get("cache_creation_input_tokens") or 0
            if it != 0 or not isinstance(cr, int) or not isinstance(cc, int) or cr + cc <= 0:
                return event
            usage["input_tokens"] = 1
            if cc > 0:
                usage["cache_creation_input_tokens"] = cc - 1
            else:
                usage["cache_read_input_tokens"] = cr - 1
            lines[i] = b"data: " + json.dumps(obj, separators=(",", ":")).encode()
            changed = True
        return b"\n".join(lines) if changed else event

    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        cut = self._buf.rfind(b"\n\n")
        if cut < 0:
            return b""
        complete, self._buf = self._buf[:cut + 2], self._buf[cut + 2:]
        out = []
        for event in complete.split(b"\n\n")[:-1]:
            fixed = self._fix_event(event)
            if fixed is not event:
                self.rewrites += 1
            out.append(fixed + b"\n\n")
        return b"".join(out)

    def flush(self) -> bytes:
        rest, self._buf = self._buf, b""
        return rest


# ── HTTP Handler ─────────────────────────────────────────────────────────


# Scratch-proxy idle exit (2026-10-16). Agents start scratch proxies for tests
# and regression runs and often never stop them; 57 leaked ones were found that
# day. CACHE_PROXY_IDLE_EXIT_SECONDS=N makes a proxy exit after N seconds with no
# request in flight and none started. Unset (the default) means never; it is
# ignored on the production port.
IDLE_EXIT_SECONDS = _env_seconds("CACHE_PROXY_IDLE_EXIT_SECONDS", 0.0)


class _Activity:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.last = time.monotonic()

    def begin(self):
        with self._lock:
            self.active += 1
            self.last = time.monotonic()

    def end(self):
        with self._lock:
            self.active -= 1
            self.last = time.monotonic()

    def idle_for(self) -> float:
        with self._lock:
            return 0.0 if self.active else time.monotonic() - self.last


_ACTIVITY = _Activity()


def _idle_exit_watch(server, limit: float):
    while True:
        time.sleep(min(30.0, max(0.5, limit / 10)))
        if _ACTIVITY.idle_for() >= limit:
            log(f"idle for {limit:.0f}s (CACHE_PROXY_IDLE_EXIT_SECONDS); exiting. "
                f"stats={json.dumps(stats)}")
            server.shutdown()
            return


class _LocalUpstreamCall:
    """One local-route upstream request, run in a worker thread.

    Puts ("headers", status, headers), then ("chunk", bytes)..., then ("end",)
    or ("error", exc) on *events*. cancel() closes the upstream connection, which
    is how the local server learns that the client is gone.
    """

    def __init__(self, url: str, body: bytes, headers: dict, events: queue.Queue,
                 stream: bool):
        self.url, self.body, self.headers, self.events = url, body, headers, events
        self.stream = stream
        self._cancelled = threading.Event()
        self._sock = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="local-upstream")

    def start(self):
        self._thread.start()

    def _trace(self, event_name: str, info: dict):
        # Keep the upstream socket so cancel() can shut it down even while this
        # thread is blocked reading (closing the client would not wake it).
        if event_name == "connection.connect_tcp.complete":
            try:
                self._sock = info["return_value"].get_extra_info("socket")
            except Exception:
                self._sock = None
            if self._cancelled.is_set():
                self._shutdown_socket()

    def _shutdown_socket(self):
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self):
        timeout = httpx.Timeout(connect=30, read=LOCAL_UPSTREAM_SILENCE_SECONDS,
                                write=30, pool=30)
        try:
            with httpx.Client(timeout=timeout) as client:
                if self._cancelled.is_set():
                    return
                with client.stream("POST", self.url, content=self.body,
                                   headers=self.headers,
                                   extensions={"trace": self._trace}) as resp:
                    self.events.put(("headers", resp.status_code, resp.headers))
                    # Streaming bytes pass through untouched, as on the hosted
                    # routes; a non-streaming body is decoded like resp.content.
                    pieces = resp.iter_raw() if self.stream else resp.iter_bytes()
                    for chunk in pieces:
                        if self._cancelled.is_set():
                            return
                        self.events.put(("chunk", chunk))
            self.events.put(("end",))
        except Exception as e:
            if not self._cancelled.is_set():
                self.events.put(("error", e))

    def read_rest(self, events: queue.Queue) -> bytes:
        """Collect the remaining body (used for error responses)."""
        parts: list[bytes] = []
        while True:
            try:
                ev = events.get(timeout=LOCAL_UPSTREAM_SILENCE_SECONDS)
            except queue.Empty:
                break
            if ev[0] == "chunk":
                parts.append(ev[1])
            else:
                break
        return b"".join(parts)

    def cancel(self):
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        if self._thread.is_alive():
            self._shutdown_socket()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def handle(self):
        _ACTIVITY.begin()
        try:
            super().handle()
        finally:
            _ACTIVITY.end()

    def _upstream_headers(self, upstream: str = ANTHROPIC_UPSTREAM) -> dict:
        h = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "accept-encoding"):
                h[k] = v
        h["host"] = urlparse(upstream).netloc
        # Don't request compression — we need to parse SSE for usage stats.
        # This adds ~10KB/response overhead but makes SSE parsing reliable.
        h["accept-encoding"] = "identity"
        # CLIProxyAPI requires sk-prefixed API key; swap Anthropic key when routing there
        if upstream == CLI_PROXY_UPSTREAM:
            h["x-api-key"] = CLI_PROXY_API_KEY
        return h

    def _handle_health(self):
        """Respond to /health with a simple status check."""
        payload = {"status": "ok", "pid": os.getpid(),
                   "loaded_at": _LOADED_AT,
                   "source_sha256": _LOADED_SOURCE_SHA256}
        if LOCAL_LIVENESS_FILE:
            doc = _local_liveness()
            payload["local_upstream"] = (
                {k: doc.get(k) for k in ("state", "reason", "since_utc", "updated_utc",
                                         "last_restart", "last_restart_result")}
                if doc else {"state": "unknown", "reason": "no fresh watchdog verdict"})
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_credential(self) -> str:
        """The caller's API credential, from Authorization: Bearer or x-api-key.

        Never logged: the value is only ever compared against LOCAL_AUTH_TOKEN.
        """
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def _forward_simple(self, method: str):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        # A non-/v1/messages request carries no model, so the upstream cannot be
        # chosen the way _forward_messages chooses it. Use the credential
        # instead: a request presenting the local gate token belongs to the gate
        # and must never be forwarded to a hosted provider.
        upstream = ANTHROPIC_UPSTREAM
        route = "anthropic"
        if LOCAL_AUTH_TOKEN and self._request_credential() == LOCAL_AUTH_TOKEN:
            if not LOCAL_UPSTREAM:
                stats["local_unconfigured"] += 1
                stats["errors"] += 1
                log(f"SIMPLE: {method} {self.path} route=local status=502 "
                    f"(local credential but OBS_LOCAL_LLM_BASE_URL is unset)")
                self.send_error(
                    502,
                    "no local upstream configured for a local-credential request "
                    "(OBS_LOCAL_LLM_BASE_URL is unset)",
                )
                return
            upstream = LOCAL_UPSTREAM
            route = "local"

        headers = self._upstream_headers(upstream)
        url = upstream + self.path

        try:
            with httpx.Client(timeout=120) as client:
                resp = client.request(method, url, content=body or None, headers=headers)
                log(f"SIMPLE: {method} {self.path} route={route} status={resp.status_code}")
                self.send_response(resp.status_code)
                for k, v in resp.headers.multi_items():
                    if k.lower() not in ("transfer-encoding", "connection", "content-encoding"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)
        except Exception as e:
            log(f"SIMPLE: {method} {self.path} route={route} upstream error: {e}")
            self.send_error(502, str(e))

    def _forward_messages(self):
        stats["requests"] += 1
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length)

        is_streaming = False
        norm_action = "error"
        upstream = ANTHROPIC_UPSTREAM  # default; overridden after model extraction
        log_model = ""
        route_label = "anthropic"
        pin_beta = False
        rlog = REQUEST_LOGGER
        # Per-request capture context. Filled best-effort; never affects forwarding.
        self._rl = {"t0": time.time(), "session_id": "", "req_id": "",
                    "status": None, "usage": None, "error": None}
        post_bytes = None
        info = {}
        try:
            # M3: undo the CLI's billing-hash mutation of an earlier message
            # (see restore_cch_placeholder). raw_body itself stays untouched so
            # the request log keeps the exact bytes the CLI sent.
            parse_bytes, cch_fixed = restore_cch_placeholder(raw_body)
            data = json.loads(parse_bytes)
            if cch_fixed:
                log("restored CLI billing-hash mutation in an earlier message")
            is_streaming = data.get("stream", False)
            try:
                self._rl["session_id"] = extract_session_id(data)
                self._rl["obs"] = extract_obs_fork_context(data)
                if rlog is not None:
                    self._rl["req_id"] = rlog.new_request_id(self._rl["session_id"])
            except Exception:
                pass

            # Determine upstream based on model
            raw_model = data.get("model") or ""
            is_local = _is_local_model(raw_model)
            upstream = _resolve_upstream(raw_model)
            if is_local and not upstream:
                # Fail local: never fall back to a hosted upstream.
                stats["local_unconfigured"] += 1
                stats["errors"] += 1
                log(f"REQ#{stats['requests']}: model={raw_model} has no local "
                    f"upstream (OBS_LOCAL_LLM_BASE_URL unset) — refusing")
                self.send_error(
                    502,
                    "no local upstream configured for local-* model "
                    "(OBS_LOCAL_LLM_BASE_URL is unset)",
                )
                return
            if is_local:
                stats["routed_local"] += 1
            elif upstream == ANTHROPIC_UPSTREAM:
                stats["routed_anthropic"] += 1
            else:
                stats["routed_cli_proxy"] += 1

            # Resolve OBS shorthand and strip context-window suffix before sending upstream.
            clean_model = _normalize_model_name(raw_model)
            if clean_model != raw_model:
                data["model"] = clean_model

            # Save pre-normalization body
            if SAVE_BODIES:
                os.makedirs(BODY_DIR, exist_ok=True)
                req_num = stats["requests"]
                pre_path = os.path.join(BODY_DIR, f"req{req_num:03d}_pre.json")
                with open(pre_path, "w") as f:
                    json.dump(data, f, indent=2)

            # CACHE-PREFIX WARNING: from here to the json.dumps() below is the
            # whole request-byte pipeline (normalize -> variant pin -> GPT cache
            # key -> route-specific schema/effort edits -> one compact
            # re-serialization). Order, the compact separators and every step
            # must be deterministic for the same conversation, or each request
            # silently re-bills the conversation. Each new step needs a
            # norm.* field in the request index and a prefix_repro scenario.
            # Live check: scripts/cache_regression.py (docs/cache-proxy-safety.md).
            data, info = normalize_request(data)
            if not is_local:
                try:
                    info["volatile_pin"] = pin_volatile_config(data)
                except Exception as e:
                    log(f"variant pin error, sending as-is: {e}")
            if upstream == CLI_PROXY_UPSTREAM:
                try:
                    info["cache_key"] = assign_conversation_cache_key(data)
                except Exception as e:
                    log(f"cache key error, sending as-is: {e}")
            if rlog is not None:
                try:
                    post_bytes = json.dumps(data, separators=(",", ":")).encode()
                except Exception:
                    post_bytes = None

            # Sanitize tool schemas for CLIProxyAPI-routed models (OpenAI compat).
            # Deliberately NOT applied to local-* upstreams: those spoke the
            # Anthropic API directly before this proxy sat in front of them, and
            # sanitizing is an untested change that does not belong in a routing fix.
            if upstream == CLI_PROXY_UPSTREAM:
                info["schema_sanitized"] = sanitize_tool_schemas_for_openai(data)
            elif upstream == ANTHROPIC_UPSTREAM:
                info["effort_stripped"] = strip_unsupported_effort(data)
                try:
                    info["effort_pin"] = pin_effort(data)
                except Exception as e:
                    log(f"effort pin error, sending as-is: {e}")

            pin_beta = bool(info.get("effort_pin", {}).get("beta"))
            body = json.dumps(data, separators=(",", ":")).encode()

            # Save post-normalization body
            if SAVE_BODIES:
                post_path = os.path.join(BODY_DIR, f"req{req_num:03d}_post.json")
                with open(post_path, "w") as f:
                    json.dump(data, f, indent=2)

            norm_action = info["action"]
            route_label = _route_label(upstream, is_local)
            log_model = clean_model
            log(f"REQ#{stats['requests']}: {norm_action} model={clean_model} route={route_label} "
                f"(billing={info.get('billing', 0)} strings={info.get('strings', 0)} "
                f"skill={info.get('skill', {}).get('action', '?')} "
                f"reminders={info.get('reminders', 0)} "
                f"git_status={info.get('git_status', 0)} "
                f"tools={info.get('tools', 0)} "
                f"meta={info.get('metadata', 0)} "
                f"schema={info.get('schema_sanitized', 0)} "
                f"effort_pin={info.get('effort_pin', {}).get('inserted', 0)})")

        except Exception as e:
            log(f"normalize error, passing through: {e}")
            body = raw_body
            stats["errors"] += 1
            is_streaming = b'"stream":true' in raw_body or b'"stream": true' in raw_body
            # The body did not parse, so the model is unknown. Hosted traffic keeps
            # its historical pass-through-to-Anthropic behaviour. But a local body
            # carries the gate credential, so sniff the raw bytes rather than
            # forwarding that credential to a third party.
            if _RAW_LOCAL_MODEL_RE.search(raw_body):
                if not LOCAL_UPSTREAM:
                    stats["local_unconfigured"] += 1
                    log("unparseable local-* body and no local upstream — refusing")
                    self.send_error(
                        502,
                        "no local upstream configured for local-* model "
                        "(OBS_LOCAL_LLM_BASE_URL is unset)",
                    )
                    return
                upstream = LOCAL_UPSTREAM
                route_label = "local"

        headers = self._upstream_headers(upstream)
        headers["content-length"] = str(len(body))
        if pin_beta:
            _add_beta(headers, MID_EFFORT_BETA)
        if (upstream == ANTHROPIC_UPSTREAM and isinstance(info, dict)
                and (info.get("volatile_pin") or {}).get("beta")):
            _add_beta(headers, CONTEXT_MANAGEMENT_BETA)
        url = upstream + self.path

        if rlog is not None:
            try:
                if not self._rl["req_id"]:
                    self._rl["req_id"] = rlog.new_request_id("")
                rlog.submit_bodies(self._rl["req_id"], raw_body, post_bytes, body)
            except Exception:
                pass

        try:
            self._forward_messages_upstream(url, body, headers, is_streaming,
                                            norm_action, log_model, route_label)
        finally:
            if rlog is not None:
                try:
                    self._submit_request_index(rlog, raw_body, post_bytes, body,
                                               headers, info, log_model,
                                               route_label, is_streaming)
                except Exception:
                    pass

    def _submit_request_index(self, rlog, raw_body, post_bytes, body, headers,
                              info, model, route, is_streaming):
        rl = self._rl
        usage = rl.get("usage") or {}
        t1 = time.time()
        effort_pin = info.get("effort_pin") if isinstance(info, dict) else None
        record = {
            "req_id": rl["req_id"],
            "ts": rl["t0"],
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(rl["t0"])) + "Z",
            "duration_ms": int((t1 - rl["t0"]) * 1000),
            "session_id": rl.get("session_id", ""),
            # OBS fork context from the newest user message's obs-bootstrap
            # block (only present on an agent's first turn in a session):
            # tells a session_recovery fork (new id + parent) from a
            # same-id restart resume (no block).
            "obs_origin": (rl.get("obs") or {}).get("origin"),
            "obs_parent_session_id": (rl.get("obs") or {}).get("parent_session_id"),
            "model": model,
            "route": route,
            "path": self.path,
            "stream": bool(is_streaming),
            "http_status": rl.get("status"),
            "error": rl.get("error"),
            "usage": {
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_creation": usage.get("cache_creation_input_tokens", 0),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_detail": usage.get("cache_creation"),
            } if usage else None,
            "norm": {k: info.get(k) for k in
                     ("action", "billing", "strings", "reminders", "git_status",
                      "tools", "metadata", "effort_stripped", "schema_sanitized",
                      "dynamic_boundary", "ws_trimmed", "volatile_pin", "cache_key")
                     if isinstance(info, dict) and k in info},
            # which proxy code produced these bytes (load-time hash, short)
            "proxy_source": _LOADED_SOURCE_SHA256[:12],
            # real CLI traffic vs synthetic probes (urllib/curl/tests): fleet
            # metrics should exclude non-CLI rows
            "client_kind": ("claude-cli" if "claude-cli" in str(self.headers.get("User-Agent", ""))
                            else "other"),
            "effort_pin": effort_pin if isinstance(effort_pin, dict) else None,
            "sizes": {"pre": len(raw_body), "wire": len(body),
                      "post": len(post_bytes) if post_bytes is not None else None},
            "post_equals_wire": (post_bytes == body) if post_bytes is not None else None,
            "wire_sha256": hashlib.sha256(body).hexdigest(),
            "client_headers": sanitize_headers(self.headers),
            "upstream_beta": headers.get("anthropic-beta"),
        }
        rlog.submit_index(record)

    def _forward_messages_upstream(self, url, body, headers, is_streaming,
                                   norm_action, log_model, route_label):
        rl = getattr(self, "_rl", None) or {}
        if route_label == "local":
            self._forward_local(url, body, headers, is_streaming, norm_action, log_model)
            return
        timeout = httpx.Timeout(connect=30, read=600, write=30, pool=30)
        try:
            with httpx.Client(timeout=timeout) as client:
                if is_streaming:
                    self._stream_upstream(client, url, body, headers, norm_action,
                                          model=log_model, route=route_label)
                else:
                    resp = client.post(url, content=body, headers=headers)
                    rl["status"] = resp.status_code
                    if resp.status_code >= 400:
                        rl["error"] = resp.content[:500].decode("utf-8", errors="replace")
                    self.send_response(resp.status_code)
                    for k, v in resp.headers.multi_items():
                        if k.lower() not in ("transfer-encoding", "connection",
                                              "content-encoding"):
                            self.send_header(k, v)
                    self.send_header("Content-Length", str(len(resp.content)))
                    self.end_headers()
                    self.wfile.write(resp.content)
                    try:
                        resp_data = resp.json()
                        usage = resp_data.get("usage", {})
                        if usage:
                            rl["usage"] = usage
                            log_usage_entry(norm_action, usage,
                                            model=log_model, route=route_label,
                                            req_id=rl.get("req_id", ""),
                                            session_id=rl.get("session_id", ""),
                                            http_status=resp.status_code)
                        else:
                            log(f"NON-STREAM: no usage in response (status={resp.status_code})")
                    except Exception as e:
                        log(f"NON-STREAM: response parse error: {e}")
        except Exception as e:
            log(f"upstream error: {e}")
            rl["status"] = rl.get("status") or 502
            rl["error"] = f"proxy upstream error: {e}"[:500]
            try:
                self.send_error(502, str(e))
            except Exception:
                pass

    def _forward_local(self, url, body, headers, is_streaming, norm_action, log_model):
        """Forward a local-route request, keeping the client connection alive.

        See LOCAL_KEEPALIVE_SECONDS for why. The upstream exchange runs in a
        worker thread that feeds a queue; this thread writes to the client and
        fills every silent LOCAL_KEEPALIVE_SECONDS with a keepalive.
        """
        rl = getattr(self, "_rl", None)
        if rl is None:
            rl = self._rl = {}
        events: queue.Queue = queue.Queue()
        upstream = _LocalUpstreamCall(url, body, headers, events, bool(is_streaming))
        committed = False      # we have sent response headers to the client
        at_boundary = True     # streaming output so far ends at an SSE event boundary
        chunks: list[bytes] = []
        fixer = LocalUsageFixer() if is_streaming else None
        status = None

        def commit(code: int, resp_headers=None):
            # Non-streaming responses are chunk-framed (HTTP/1.1, this connection
            # only) so a failure after commit can end the body WITHOUT its final
            # chunk: the CLI sees a dropped connection and retries. Under the
            # handler's HTTP/1.0 close-delimited framing any bytes we write are a
            # complete body, and an error JSON on a 200 is persisted by the CLI as
            # an empty assistant message -> OBS SilentResponseError (2026-10-18).
            nonlocal committed
            if not is_streaming:
                self.protocol_version = "HTTP/1.1"
                self.close_connection = True
            self.send_response(code)
            if resp_headers is not None:
                for k, v in resp_headers.multi_items():
                    if k.lower() not in ("transfer-encoding", "connection",
                                          "content-encoding", "content-length"):
                        self.send_header(k, v)
            else:
                self.send_header("content-type", "text/event-stream" if is_streaming
                                 else "application/json")
            if not is_streaming:
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            committed = True

        def write(data: bytes):
            nonlocal at_boundary
            if not data:
                return
            self.wfile.write(data)
            self.wfile.flush()
            at_boundary = data.endswith(b"\n\n") or data.endswith(b"\r\n\r\n")

        def keepalive():
            if is_streaming:
                if not at_boundary:
                    return
                self.wfile.write(SSE_PING)
            else:
                self.wfile.write(b"1\r\n \r\n")
            self.wfile.flush()
            stats["local_keepalives"] += 1

        def fail(message: str, *, error_type: str = "overloaded_error", code: int = 529):
            # A retryable error the CLI understands; status only if not yet committed.
            rl["status"] = rl.get("status") or code
            rl["error"] = message[:500]
            payload = json.dumps({"type": "error",
                                  "error": {"type": error_type, "message": message}})
            if not committed:
                data = payload.encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif is_streaming:
                prefix = b"" if at_boundary else b"\n\n"
                self.wfile.write(prefix + b"event: error\ndata: " + payload.encode() + b"\n\n")
            else:
                # Never a body: drop the connection before the final chunk.
                stats["local_nonstream_aborts"] += 1
                log(f"LOCAL: aborting committed non-streaming response so the "
                    f"CLI retries ({message[:200]})")
                self.wfile.flush()
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            self.wfile.flush()

        try:
            deadline = time.monotonic() + LOCAL_HEADER_WAIT_SECONDS
            doc = _local_liveness()
            if _local_upstream_bad(doc):
                stats["local_recovery_holds"] += 1
                log(f"LOCAL: upstream {doc.get('state')} ({str(doc.get('reason'))[:200]}); "
                    f"holding request until the watchdog reports it healthy")
                hold_until = time.monotonic() + LOCAL_RECOVERY_WAIT_SECONDS
                last_ka = time.monotonic()
                while _local_upstream_bad(doc):
                    now = time.monotonic()
                    if now >= hold_until:
                        fail(f"local upstream {doc.get('state')}: {doc.get('reason')}; not "
                             f"recovered after {LOCAL_RECOVERY_WAIT_SECONDS:.0f}s")
                        return
                    if not committed and now >= deadline:
                        commit(200)
                        stats["local_early_commits"] += 1
                        last_ka = now
                    elif committed and now - last_ka >= LOCAL_KEEPALIVE_SECONDS:
                        keepalive()
                        last_ka = now
                    time.sleep(LOCAL_LIVENESS_POLL_SECONDS)
                    doc = _local_liveness()
                log("LOCAL: upstream recovered; forwarding held request")
                deadline = time.monotonic() + LOCAL_HEADER_WAIT_SECONDS
            upstream.start()
            while True:
                wait = (LOCAL_KEEPALIVE_SECONDS if committed
                        else max(0.0, deadline - time.monotonic()))
                try:
                    ev = events.get(timeout=wait)
                except queue.Empty:
                    doc = _local_liveness()
                    if _local_upstream_bad(doc):
                        stats["local_wedge_aborts"] += 1
                        msg = (f"local upstream {doc.get('state')}: {doc.get('reason')} "
                               f"(watchdog recovering; retry)")
                        log(f"LOCAL: failing in-flight request: {msg[:300]}")
                        upstream.cancel()
                        fail(msg, code=529)
                        return
                    if not committed:
                        commit(200)
                        stats["local_early_commits"] += 1
                        log(f"LOCAL: no upstream response in {LOCAL_HEADER_WAIT_SECONDS:.0f}s; "
                            f"sent own 200 headers, keeping alive (stream={is_streaming})")
                    else:
                        keepalive()
                    continue
                kind = ev[0]
                if kind == "headers":
                    status, resp_headers = ev[1], ev[2]
                    rl["status"] = status
                    if status >= 400:
                        error_body = upstream.read_rest(events)
                        rl["error"] = error_body[:500].decode("utf-8", errors="replace")
                        log(f"LOCAL: upstream error status={status} "
                            f"{error_body[:300].decode('utf-8', errors='replace')}")
                        if not committed:
                            if self._send_translated_overflow(status, error_body):
                                return
                            self.send_response(status)
                            for k, v in resp_headers.multi_items():
                                if k.lower() not in ("transfer-encoding", "connection",
                                                      "content-encoding", "content-length"):
                                    self.send_header(k, v)
                            self.send_header("Content-Length", str(len(error_body)))
                            self.end_headers()
                            self.wfile.write(error_body)
                        else:
                            try:
                                err = json.loads(error_body)["error"]
                                fail(str(err.get("message", "")),
                                     error_type=str(err.get("type", "api_error")))
                            except Exception:
                                fail(f"local upstream returned {status}: "
                                     f"{error_body[:300].decode('utf-8', errors='replace')}",
                                     error_type="api_error")
                        return
                    if not committed:
                        commit(status, resp_headers)
                elif kind == "chunk":
                    chunk = ev[1]
                    if fixer is not None:
                        chunk = fixer.feed(chunk)
                    if chunk:
                        chunks.append(chunk)
                        if is_streaming:
                            write(chunk)
                elif kind == "end":
                    break
                elif kind == "error":
                    exc = ev[1]
                    if isinstance(exc, httpx.ReadTimeout):
                        stats["local_upstream_silence"] += 1
                        msg = (f"local upstream sent nothing for "
                               f"{LOCAL_UPSTREAM_SILENCE_SECONDS:.0f}s")
                        log(f"LOCAL: {msg}")
                        fail(msg)
                    else:
                        log(f"upstream error: {exc}")
                        fail(f"proxy upstream error: {exc}", error_type="api_error", code=502)
                    return

            if fixer is not None:
                tail = fixer.flush()
                if tail:
                    chunks.append(tail)
                    write(tail)
                if fixer.rewrites:
                    stats["local_usage_rewrites"] += fixer.rewrites
                usage = parse_sse_usage(chunks)
                if usage:
                    rl["usage"] = usage
                    log_usage_entry(norm_action, usage, model=log_model, route="local",
                                    req_id=rl.get("req_id", ""),
                                    session_id=rl.get("session_id", ""),
                                    http_status=status)
                else:
                    raw_preview = b"".join(chunks)[:500].decode("utf-8", errors="replace")
                    rl["error"] = raw_preview
                    log(f"STREAM: no usage in {len(chunks)} chunks. Preview: {raw_preview[:200]}")
            else:
                content = b"".join(chunks)
                if content:
                    self.wfile.write(b"%x\r\n" % len(content) + content + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                try:
                    usage = json.loads(content).get("usage", {})
                    if usage:
                        rl["usage"] = usage
                        log_usage_entry(norm_action, usage, model=log_model, route="local",
                                        req_id=rl.get("req_id", ""),
                                        session_id=rl.get("session_id", ""),
                                        http_status=status)
                    else:
                        log(f"NON-STREAM: no usage in response (status={status})")
                except Exception as e:
                    log(f"NON-STREAM: response parse error: {e}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            stats["local_client_gone"] += 1
            rl["status"] = rl.get("status") or 499
            rl["error"] = f"client disconnected: {e}"
            log(f"LOCAL: client disconnected ({e}); closing upstream request")
        finally:
            upstream.cancel()

    def _send_translated_overflow(self, status: int, error_body: bytes) -> bool:
        translated = translate_local_overflow_error(status, error_body)
        if translated is None:
            return False
        stats["local_overflow_translations"] += 1
        log(f"LOCAL OVERFLOW: upstream {status} translated to 400 for CLI "
            f"max_tokens retry: {translated.decode()}")
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("Content-Length", str(len(translated)))
        self.end_headers()
        self.wfile.write(translated)
        return True

    def _stream_upstream(self, client: httpx.Client, url: str,
                         body: bytes, headers: dict, norm_action: str,
                         model: str = "", route: str = ""):
        rl = getattr(self, "_rl", None)
        if rl is None:
            rl = self._rl = {}
        with client.stream("POST", url, content=body, headers=headers) as resp:
            rl["status"] = resp.status_code
            self.send_response(resp.status_code)
            for k, v in resp.headers.multi_items():
                if k.lower() not in ("transfer-encoding", "connection",
                                      "content-encoding", "content-length"):
                    self.send_header(k, v)
            self.end_headers()

            chunks: list[bytes] = []
            for chunk in resp.iter_raw():
                self.wfile.write(chunk)
                self.wfile.flush()
                chunks.append(chunk)

            usage = parse_sse_usage(chunks)
            if usage:
                rl["usage"] = usage
                log_usage_entry(norm_action, usage, model=model, route=route,
                                req_id=rl.get("req_id", ""),
                                session_id=rl.get("session_id", ""),
                                http_status=resp.status_code)
            else:
                # Log chunk sizes for debugging
                chunk_info = [len(c) for c in chunks]
                total_bytes = sum(chunk_info)
                # Show first 500 bytes of raw response for debugging
                raw_preview = b"".join(chunks)[:500].decode("utf-8", errors="replace")
                rl["error"] = raw_preview
                log(f"STREAM: no usage in {len(chunks)} chunks ({total_bytes} bytes). Preview: {raw_preview[:200]}")

    def do_POST(self):
        if "/v1/messages" in self.path:
            self._forward_messages()
        else:
            self._forward_simple("POST")

    def do_GET(self):
        if self.path == "/health":
            self._handle_health()
        else:
            self._forward_simple("GET")

    def do_PUT(self):
        self._forward_simple("PUT")

    def do_DELETE(self):
        self._forward_simple("DELETE")

    def do_OPTIONS(self):
        self._forward_simple("OPTIONS")

    def log_message(self, fmt, *args):
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    os.makedirs(LOG_DIR, exist_ok=True)

    server = ThreadedHTTPServer(("127.0.0.1", port), ProxyHandler)
    log(f"listening on :{port}")
    log(f"upstream (claude): {ANTHROPIC_UPSTREAM}")
    log(f"upstream (other):  {CLI_PROXY_UPSTREAM}")
    log(f"upstream (local):  {LOCAL_UPSTREAM or '<unconfigured>'}")
    log(f"logs: {LOG_DIR}")
    if port != PROD_PORT:
        log(f"WARN: port {port} is not the production port ({PROD_PORT}) but usage "
            f"and bodies go to {LOG_DIR} — set CACHE_PROXY_LOG_DIR to a private "
            f"directory so this instance does not mix into production data")
    log(f"save_bodies: {SAVE_BODIES}")
    global REQUEST_LOGGER
    if request_log_enabled(port):
        try:
            REQUEST_LOGGER = RequestLogger(REQUEST_LOG_DIR, REQUEST_LOG_MAX_BYTES)
            log(f"request_log: ON dir={REQUEST_LOG_DIR} "
                f"cap={REQUEST_LOG_MAX_BYTES / 1e9:g}GB ring buffer (pre/post/wire bodies + "
                f"index.jsonl, secrets redacted)")
        except Exception as e:
            REQUEST_LOGGER = None
            log(f"request_log: FAILED to start ({e}) — proxying without capture")
    else:
        log("request_log: OFF (non-production port or CACHE_PROXY_REQUEST_LOG=0; "
            "set CACHE_PROXY_REQUEST_LOG=1 to force)")
    log(f"normalizations: billing, string→list, strip all system reminders, "
        f"git status, tool sort, metadata (cache_control: passthrough)")
    log(f"Set ANTHROPIC_BASE_URL=http://localhost:{port}")
    if IDLE_EXIT_SECONDS:
        if port == PROD_PORT:
            log("WARN: CACHE_PROXY_IDLE_EXIT_SECONDS ignored on the production port")
        else:
            log(f"idle_exit: after {IDLE_EXIT_SECONDS:.0f}s without requests")
            threading.Thread(target=_idle_exit_watch, args=(server, IDLE_EXIT_SECONDS),
                             daemon=True, name="idle-exit").start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log(f"shutting down. stats={json.dumps(stats)}")


if __name__ == "__main__":
    main()
