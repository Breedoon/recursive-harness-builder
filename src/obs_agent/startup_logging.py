"""Startup progress logging helpers."""

from __future__ import annotations

import logging
import time
import sys
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any


def _ensure_startup_logging(logger: logging.Logger) -> None:
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


class StartupProfiler:
    """Log startup phase progress with elapsed timings."""

    def __init__(self, logger: logging.Logger, component: str) -> None:
        self._logger = logger
        _ensure_startup_logging(logger)
        self._component = component
        self._started_at = time.perf_counter()

    @contextmanager
    def phase(self, phase: str, **fields: Any) -> Iterator[None]:
        phase_started_at = time.perf_counter()
        self._log("phase_start", phase, 0.0, self._elapsed_ms(phase_started_at), fields)
        try:
            yield
        except Exception:
            failed_at = time.perf_counter()
            self._log(
                "phase_failed",
                phase,
                self._elapsed_ms(failed_at, since=phase_started_at),
                self._elapsed_ms(failed_at),
                fields,
                level=logging.ERROR,
            )
            raise
        finished_at = time.perf_counter()
        self._log(
            "phase_complete",
            phase,
            self._elapsed_ms(finished_at, since=phase_started_at),
            self._elapsed_ms(finished_at),
            fields,
        )

    def complete(self, **fields: Any) -> None:
        now = time.perf_counter()
        self._log("complete", "startup", 0.0, self._elapsed_ms(now), fields)

    def _elapsed_ms(self, now: float, *, since: float | None = None) -> float:
        return (now - (since if since is not None else self._started_at)) * 1000

    def _log(
        self,
        event: str,
        phase: str,
        phase_ms: float,
        total_ms: float,
        fields: dict[str, Any],
        *,
        level: int = logging.INFO,
    ) -> None:
        extra = _format_fields(fields)
        if extra:
            extra = " " + extra
        self._logger.log(
            level,
            "startup %s component=%s phase=%s phase_ms=%.1f total_ms=%.1f%s",
            event,
            self._component,
            phase,
            phase_ms,
            total_ms,
            extra,
        )


def _format_fields(fields: dict[str, Any]) -> str:
    rendered: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered.append(f"{key}={str(value).lower()}")
        elif isinstance(value, int | float):
            rendered.append(f"{key}={value}")
        else:
            text = str(value).replace("\n", " ").replace("\r", " ")[:80]
            rendered.append(f"{key}={text}")
    return " ".join(rendered)
