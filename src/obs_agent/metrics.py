"""Metrics logging for OBS Agent.

Logs token usage, costs, and timing from SDK responses.
Uses Python logging (not vault) per decision D023.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("obs_agent.metrics")


def log_result(message: Any, *, label: str = "query") -> None:
    """Log metrics from an SDK message if it contains usage data.

    Extracts total_cost_usd, duration_ms, and any usage stats
    from the message object. Safe to call on any message type --
    silently skips messages without metrics fields.
    """
    try:
        parts: list[str] = [f"[{label}]"]

        cost = getattr(message, "total_cost_usd", None)
        if isinstance(cost, (int, float)):
            parts.append(f"cost=${cost:.4f}")

        duration = getattr(message, "duration_ms", None)
        if isinstance(duration, (int, float)):
            parts.append(f"duration={duration}ms")

        usage = getattr(message, "usage", None)
        if isinstance(usage, dict):
            input_t = usage.get("input_tokens")
            if isinstance(input_t, int):
                parts.append(f"input_tokens={input_t}")
            output_t = usage.get("output_tokens")
            if isinstance(output_t, int):
                parts.append(f"output_tokens={output_t}")
            cache_create = usage.get("cache_creation_input_tokens")
            if isinstance(cache_create, int):
                parts.append(f"cache_creation={cache_create}")
            cache_read = usage.get("cache_read_input_tokens")
            if isinstance(cache_read, int):
                parts.append(f"cache_read={cache_read}")

        # Only log if we extracted something beyond just the label
        if len(parts) > 1:
            logger.info(" ".join(parts))
    except Exception:
        # Never let metrics logging break the main flow
        logger.debug("Failed to log metrics", exc_info=True)
