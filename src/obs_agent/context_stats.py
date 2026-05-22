"""Shared session/context stats formatting for Telegram and MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obs_agent.context_jsonl import load_jsonl_usage_snapshot
from obs_agent.context_probe import ContextProbe


def _as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    return 0


def _as_float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def build_context_snapshot(
    *,
    session_id: str | None,
    data: dict | None,
    context_window_estimate_tokens: int,
    cwd: Path | None = None,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Build a normalized snapshot from the latest result usage payload.

    Notes:
    - `total_tokens_billed` is a billing counter for the latest result payload.
    - `estimated_context_used_tokens` is a bounded occupancy estimate and should
      not be interpreted as billable total.
    """
    snapshot: dict[str, Any] = {
        "session_id": session_id or None,
        "num_turns": None,
        "total_cost_usd": None,
        "duration_ms": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_tokens_billed": 0,
        "sdk_input_tokens": 0,
        "sdk_output_tokens": 0,
        "sdk_cache_creation_input_tokens": 0,
        "sdk_cache_read_input_tokens": 0,
        "sdk_total_tokens_billed": 0,
        "estimated_context_window_tokens": context_window_estimate_tokens,
        "estimated_context_used_tokens": 0,
        "estimated_context_remaining_tokens": context_window_estimate_tokens,
        "estimated_context_remaining_pct": 100.0,
        "context_estimate_source": "usage_fallback",
        "jsonl_session_file": None,
        "jsonl_assistant_entries": 0,
        "jsonl_usage_entries": 0,
        "recent_peak_context_triplet_tokens": 0,
        "session_peak_context_triplet_tokens": 0,
    }

    usage = {}
    if data:
        # Prefer the latest known session_id from result data if present.
        if data.get("session_id"):
            snapshot["session_id"] = data.get("session_id")

        snapshot["num_turns"] = data.get("num_turns")
        snapshot["total_cost_usd"] = _as_float_or_none(data.get("total_cost_usd"))
        snapshot["duration_ms"] = data.get("duration_ms")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    total_billed = input_tokens + output_tokens + cache_creation + cache_read
    snapshot.update(
        {
            "sdk_input_tokens": input_tokens,
            "sdk_output_tokens": output_tokens,
            "sdk_cache_creation_input_tokens": cache_creation,
            "sdk_cache_read_input_tokens": cache_read,
            "sdk_total_tokens_billed": total_billed,
        }
    )

    resolved_session_id = snapshot.get("session_id")
    jsonl_usage = load_jsonl_usage_snapshot(
        session_id=resolved_session_id if isinstance(resolved_session_id, str) else None,
        cwd=cwd,
        projects_root=projects_root,
    )
    if jsonl_usage is not None:
        context_used = jsonl_usage.latest_context_triplet_tokens
        if context_window_estimate_tokens > 0:
            context_used = min(context_used, context_window_estimate_tokens)
        context_used = max(context_used, 0)
        context_remaining = max(0, context_window_estimate_tokens - context_used)
        pct_remaining = (
            (context_remaining / context_window_estimate_tokens) * 100
            if context_window_estimate_tokens > 0
            else 0.0
        )
        snapshot.update(
            {
                "input_tokens": jsonl_usage.latest_input_tokens,
                "output_tokens": jsonl_usage.latest_output_tokens,
                "cache_creation_input_tokens": jsonl_usage.latest_cache_creation_input_tokens,
                "cache_read_input_tokens": jsonl_usage.latest_cache_read_input_tokens,
                "total_tokens_billed": jsonl_usage.latest_total_tokens_billed,
                "estimated_context_used_tokens": context_used,
                "estimated_context_remaining_tokens": context_remaining,
                "estimated_context_remaining_pct": pct_remaining,
                "context_estimate_source": "jsonl_latest_triplet",
                "jsonl_session_file": str(jsonl_usage.source_path),
                "jsonl_assistant_entries": jsonl_usage.assistant_entries,
                "jsonl_usage_entries": jsonl_usage.usage_entries,
                "recent_peak_context_triplet_tokens": jsonl_usage.recent_peak_context_triplet_tokens,
                "session_peak_context_triplet_tokens": jsonl_usage.session_peak_context_triplet_tokens,
            }
        )
        return snapshot

    # Context occupancy estimate (bounded):
    # - Do not sum cache creation + cache read (they can represent overlapping
    #   prompt regions in one response).
    # - Use the strongest single window signal, then include non-cached input.
    # - If the cache-read signal is impossible for the configured window and we
    #   have a positive num_turns, normalize by turn count to avoid reporting
    #   cumulative multi-turn usage as active window occupancy.
    num_turns = _as_int(snapshot.get("num_turns"))
    window_signal = max(cache_read, cache_creation)
    if (
        context_window_estimate_tokens > 0
        and num_turns > 1
        and window_signal > context_window_estimate_tokens
    ):
        window_signal = int(round(window_signal / num_turns))
    context_used = max(window_signal, input_tokens + cache_creation)
    if context_window_estimate_tokens > 0:
        context_used = min(context_used, context_window_estimate_tokens)
    context_used = max(context_used, 0)
    context_remaining = max(0, context_window_estimate_tokens - context_used)
    pct_remaining = (
        (context_remaining / context_window_estimate_tokens) * 100
        if context_window_estimate_tokens > 0
        else 0.0
    )

    snapshot.update(
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "total_tokens_billed": total_billed,
            "estimated_context_used_tokens": context_used,
            "estimated_context_remaining_tokens": context_remaining,
            "estimated_context_remaining_pct": pct_remaining,
        }
    )
    return snapshot


def format_context_snapshot_lines(snapshot: dict[str, Any]) -> list[str]:
    """Render snapshot as stable line-oriented text for /context + MCP tools."""
    total_cost = snapshot.get("total_cost_usd")
    total_cost_text = f"{total_cost}" if total_cost is not None else "?"
    return [
        f"session_id: {snapshot.get('session_id') or '(none)'}",
        f"num_turns: {snapshot.get('num_turns') if snapshot.get('num_turns') is not None else '?'}",
        f"total_cost_usd: {total_cost_text}",
        f"duration_ms: {snapshot.get('duration_ms') if snapshot.get('duration_ms') is not None else '?'}",
        f"input_tokens: {snapshot.get('input_tokens', 0)}",
        f"output_tokens: {snapshot.get('output_tokens', 0)}",
        f"cache_creation_input_tokens: {snapshot.get('cache_creation_input_tokens', 0)}",
        f"cache_read_input_tokens: {snapshot.get('cache_read_input_tokens', 0)}",
        f"total_tokens_billed: {snapshot.get('total_tokens_billed', 0)}",
        f"sdk_input_tokens: {snapshot.get('sdk_input_tokens', 0)}",
        f"sdk_output_tokens: {snapshot.get('sdk_output_tokens', 0)}",
        f"sdk_cache_creation_input_tokens: {snapshot.get('sdk_cache_creation_input_tokens', 0)}",
        f"sdk_cache_read_input_tokens: {snapshot.get('sdk_cache_read_input_tokens', 0)}",
        f"sdk_total_tokens_billed: {snapshot.get('sdk_total_tokens_billed', 0)}",
        f"estimated_context_window_tokens: {snapshot.get('estimated_context_window_tokens', 0)}",
        f"estimated_context_used_tokens: {snapshot.get('estimated_context_used_tokens', 0)}",
        f"estimated_context_remaining_tokens: {snapshot.get('estimated_context_remaining_tokens', 0)}",
        f"estimated_context_remaining_pct: {float(snapshot.get('estimated_context_remaining_pct', 0.0)):.1f}%",
        f"recent_peak_context_triplet_tokens: {snapshot.get('recent_peak_context_triplet_tokens', 0)}",
        f"session_peak_context_triplet_tokens: {snapshot.get('session_peak_context_triplet_tokens', 0)}",
        f"jsonl_assistant_entries: {snapshot.get('jsonl_assistant_entries', 0)}",
        f"jsonl_usage_entries: {snapshot.get('jsonl_usage_entries', 0)}",
        f"jsonl_session_file: {snapshot.get('jsonl_session_file') or '(none)'}",
        f"context_estimate_source: {snapshot.get('context_estimate_source', 'usage_fallback')}",
    ]


def _format_compact_tokens(value: int) -> str:
    """Render token counts compactly for Telegram completion notices."""
    if value >= 1_000_000:
        return f"{int(value / 1_000_000)}m"
    if value >= 1_000:
        return f"{int(value / 1_000)}k"
    return str(value)


def format_context_snapshot_compact(snapshot: dict[str, Any]) -> str:
    """Render the minimal Telegram completion context summary."""
    used = max(0, int(snapshot.get("estimated_context_used_tokens", 0) or 0))
    if used == 0:
        return "context: context unavailable"
    return f"context: {_format_compact_tokens(used)}"


def apply_context_probe(
    snapshot: dict[str, Any],
    probe: ContextProbe | None,
) -> dict[str, Any]:
    """Overlay authoritative context estimate from Claude CLI probe, if available."""
    if probe is None:
        if not snapshot.get("context_estimate_source"):
            snapshot["context_estimate_source"] = "usage_fallback"
        return snapshot

    used = max(0, probe.used_tokens)
    window = max(1, probe.window_tokens)
    remaining = max(0, window - used)
    remaining_pct = (remaining / window) * 100
    snapshot.update(
        {
            "estimated_context_window_tokens": window,
            "estimated_context_used_tokens": min(used, window),
            "estimated_context_remaining_tokens": remaining,
            "estimated_context_remaining_pct": remaining_pct,
            "context_estimate_source": "claude_cli_context",
        }
    )
    return snapshot
