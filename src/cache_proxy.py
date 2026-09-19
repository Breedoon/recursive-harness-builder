from __future__ import annotations

"""Cache-normalizing proxy for Claude Code API requests.

Intercepts POST /v1/messages requests and applies idempotent normalization
rules to maximize prompt cache hits across session restarts and forks.
All other requests are forwarded unmodified. CC's native cache_control
placement is left untouched — cache_control is not part of the cache key
(it's a breakpoint hint only), so normalizing it is unnecessary.

Normalizations (applied in order):
1. Billing header: replaced with fixed value
2. String→list: bare-string user content converted to list format
3. Claude Code system-reminders: stripped from all user messages
4. Git status: normalized in system prompt to fixed placeholder
5. Tool sorting: tools[] sorted alphabetically by name
6. Metadata: session-specific IDs normalized
7. Tool schema sanitization: array types get ``items`` added (non-Claude only)

Usage:
    python cache_proxy.py [port]  # default: 18923

Client:
    ANTHROPIC_BASE_URL=http://localhost:18923

"""
import json
import os
import re
import sys
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
DEFAULT_PORT = 18923
_CODEBASE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.environ.get(
    "CACHE_PROXY_LOG_DIR",
    os.path.join(_CODEBASE_ROOT, ".obs-agent", "cache-proxy-log"),
)
BODY_DIR = os.path.join(LOG_DIR, "bodies")
USAGE_LOG = os.path.join(LOG_DIR, "usage.jsonl")

SKILL_MARKER = "The following skills are available for use with the Skill tool:"
CLAUDEMD_MARKER = "As you answer the user's questions, you can use the following context:"

SAVE_BODIES = os.environ.get("CACHE_PROXY_SAVE_BODIES", "").lower() in ("1", "true")

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
CC_VERSION = os.environ.get("CACHE_PROXY_CC_VERSION", "2.1.251.a37")
FIXED_BILLING_HEADER = (
    f"x-anthropic-billing-header: cc_version={CC_VERSION}; cc_entrypoint=sdk-py; cch=0;"
)

# Running stats
stats = {
    "requests": 0,
    "skill_stripped": 0, "skill_missing": 0,
    "billing_normalized": 0, "strings_converted": 0,
    "reminders_stripped": 0, "git_status_normalized": 0,
    "tools_sorted": 0, "metadata_normalized": 0,
    "errors": 0,
    "routed_anthropic": 0, "routed_cli_proxy": 0, "routed_local": 0,
    "local_unconfigured": 0,
    "schemas_sanitized": 0,
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


def strip_all_system_reminders(body: dict) -> int:
    """Rule 3: Strip every Claude Code <system-reminder> from user messages."""
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return 0
    count = 0
    kept_messages: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            kept_messages.append(msg)
            continue
        if msg.get("role") != "user":
            kept_messages.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            kept_messages.append(msg)
            continue
        original_len = len(content)
        filtered = [
            block for block in content if not _is_strippable_system_reminder(block)
        ]
        stripped = original_len - len(filtered)
        count += stripped
        if original_len > 0 and stripped == original_len:
            continue
        msg["content"] = filtered
        kept_messages.append(msg)
    body["messages"] = kept_messages
    stats["reminders_stripped"] += count
    return count


def strip_dynamic_reminders(body: dict) -> int:
    """Backward-compatible alias for strip_all_system_reminders."""
    return strip_all_system_reminders(body)


def normalize_git_status(body: dict) -> int:
    """Rule 4: Normalize git status section in system prompt.

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


def normalize_request(body: dict) -> tuple[dict, dict]:
    """Apply all normalizations to a request body in spec order.

    Order: billing → strings → strip all system reminders → git status →
           tool sorting → metadata

    cache_control is deliberately left untouched — it's not part of the cache
    key, and CC's native placement is sufficient.

    Returns (modified_body, info_dict) with details of what was normalized.
    """
    info = {}

    # 1. Normalize billing header (first — start of token stream)
    info["billing"] = normalize_billing_header(body)

    # 2. Convert bare string content to list format (before skill listing move)
    info["strings"] = normalize_user_content_structure(body)

    # 3. Strip every Claude Code system-reminder, including skill listings and
    # CLAUDE.md project-context reminders. OBS-owned project context is injected
    # into JSONL separately.
    info["skill"] = {"action": "merged_into_reminders"}
    info["reminders"] = strip_all_system_reminders(body)

    # 4. Normalize git status in system prompt
    info["git_status"] = normalize_git_status(body)

    # 5. Sort tool definitions
    info["tools"] = normalize_tool_order(body)

    # 6. Normalize metadata
    info["metadata"] = normalize_metadata(body)

    # Determine overall action label
    actions_taken = [
        info["billing"], info["strings"], info["reminders"],
        info["git_status"], info["tools"], info["metadata"],
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


def log_usage_entry(norm_action: str, usage: dict, model: str = "", route: str = ""):
    """Append a usage entry to the structured JSONL log.

    ``model`` and ``route`` let a reader tell local turns apart from hosted
    ones. Both are additive keys; existing readers do keyed lookups.
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
    log(f"USAGE: tot={tot:,} cr={cr:,} cc={cc:,} ip={ip} ({rate:.0%} cached) "
        f"[{norm_action}]{suffix}")


def parse_sse_usage(sse_chunks: list[bytes]) -> dict:
    """Extract usage from the message_start SSE event."""
    full = b"".join(sse_chunks).decode("utf-8", errors="replace")
    for line in full.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
            if event.get("type") == "message_start":
                return event.get("message", {}).get("usage", {})
        except Exception:
            continue
    return {}


# ── HTTP Handler ─────────────────────────────────────────────────────────


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

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
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward_simple(self, method: str):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        headers = self._upstream_headers(ANTHROPIC_UPSTREAM)
        url = ANTHROPIC_UPSTREAM + self.path

        try:
            with httpx.Client(timeout=120) as client:
                resp = client.request(method, url, content=body or None, headers=headers)
                self.send_response(resp.status_code)
                for k, v in resp.headers.multi_items():
                    if k.lower() not in ("transfer-encoding", "connection", "content-encoding"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)
        except Exception as e:
            log(f"upstream error ({method} {self.path}): {e}")
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
        try:
            data = json.loads(raw_body)
            is_streaming = data.get("stream", False)

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

            # Apply all normalizations
            data, info = normalize_request(data)

            # Sanitize tool schemas for CLIProxyAPI-routed models (OpenAI compat).
            # Deliberately NOT applied to local-* upstreams: those spoke the
            # Anthropic API directly before this proxy sat in front of them, and
            # sanitizing is an untested change that does not belong in a routing fix.
            if upstream == CLI_PROXY_UPSTREAM:
                info["schema_sanitized"] = sanitize_tool_schemas_for_openai(data)

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
                f"schema={info.get('schema_sanitized', 0)})")

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
        url = upstream + self.path

        timeout = httpx.Timeout(connect=30, read=600, write=30, pool=30)
        try:
            with httpx.Client(timeout=timeout) as client:
                if is_streaming:
                    self._stream_upstream(client, url, body, headers, norm_action,
                                          model=log_model, route=route_label)
                else:
                    resp = client.post(url, content=body, headers=headers)
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
                            log_usage_entry(norm_action, usage,
                                            model=log_model, route=route_label)
                        else:
                            log(f"NON-STREAM: no usage in response (status={resp.status_code})")
                    except Exception as e:
                        log(f"NON-STREAM: response parse error: {e}")
        except Exception as e:
            log(f"upstream error: {e}")
            try:
                self.send_error(502, str(e))
            except Exception:
                pass

    def _stream_upstream(self, client: httpx.Client, url: str,
                         body: bytes, headers: dict, norm_action: str,
                         model: str = "", route: str = ""):
        with client.stream("POST", url, content=body, headers=headers) as resp:
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
                log_usage_entry(norm_action, usage, model=model, route=route)
            else:
                # Log chunk sizes for debugging
                chunk_info = [len(c) for c in chunks]
                total_bytes = sum(chunk_info)
                # Show first 500 bytes of raw response for debugging
                raw_preview = b"".join(chunks)[:500].decode("utf-8", errors="replace")
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
    log(f"save_bodies: {SAVE_BODIES}")
    log(f"normalizations: billing, string→list, strip all system reminders, "
        f"git status, tool sort, metadata (cache_control: passthrough)")
    log(f"Set ANTHROPIC_BASE_URL=http://localhost:{port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log(f"shutting down. stats={json.dumps(stats)}")


if __name__ == "__main__":
    main()
