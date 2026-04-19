"""Cache proxy subprocess lifecycle management.

Launches the cache-normalizing proxy as a subprocess and verifies it's
healthy before allowing sessions to route through it.
"""

from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger("obs_agent.cache_proxy")

_CACHE_PROXY_SCRIPT = Path(__file__).resolve().parents[1] / "cache_proxy.py"
_HEALTH_CHECK_TIMEOUT = 3.0  # seconds to wait for proxy to become healthy
_HEALTH_CHECK_INTERVAL = 0.1  # seconds between health check polls

# Module-level state for the proxy subprocess
_proxy_proc: subprocess.Popen | None = None
_proxy_healthy: bool = False


def start_cache_proxy(port: int) -> subprocess.Popen | None:
    """Launch the cache proxy subprocess and wait for it to become healthy.

    Returns the Popen object if the proxy started and passed health check,
    None if it failed to start or health check timed out.
    """
    global _proxy_proc, _proxy_healthy

    if not _CACHE_PROXY_SCRIPT.exists():
        logger.warning(
            "Cache proxy script not found at %s — skipping proxy", _CACHE_PROXY_SCRIPT
        )
        return None

    logger.info("Starting cache proxy on port %d ...", port)

    try:
        proc = subprocess.Popen(
            [sys.executable, str(_CACHE_PROXY_SCRIPT), str(port)],
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,  # proxy logs to stderr
        )
    except Exception:
        logger.exception("Failed to launch cache proxy subprocess")
        return None

    # Wait for health check
    health_url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + _HEALTH_CHECK_TIMEOUT
    while time.monotonic() < deadline:
        # Check if process died
        if proc.poll() is not None:
            logger.warning(
                "Cache proxy exited immediately with code %d", proc.returncode
            )
            return None
        try:
            resp = httpx.get(health_url, timeout=0.5)
            if resp.status_code == 200:
                logger.info("Cache proxy healthy on port %d (pid %d)", port, proc.pid)
                _proxy_proc = proc
                _proxy_healthy = True
                atexit.register(_cleanup_proxy)
                return proc
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(_HEALTH_CHECK_INTERVAL)

    logger.warning(
        "Cache proxy health check timed out after %.1fs — proxy will not be used",
        _HEALTH_CHECK_TIMEOUT,
    )
    # Kill the process since it's not healthy
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return None


def _cleanup_proxy() -> None:
    """Terminate the proxy subprocess on interpreter exit."""
    global _proxy_proc
    if _proxy_proc is not None:
        try:
            _proxy_proc.terminate()
            _proxy_proc.wait(timeout=3)
        except Exception:
            try:
                _proxy_proc.kill()
            except Exception:
                pass
        _proxy_proc = None


def should_attempt_proxy_start(*, cache_proxy_enabled: bool) -> bool:
    """Determine whether to attempt launching the cache proxy subprocess.

    Checks only config and env — NOT _proxy_healthy, since the proxy
    hasn't started yet at this point. Used by entrypoints (daemon.py,
    telegram_main.py) to decide whether to call start_cache_proxy().
    """
    if os.environ.get("OBS_SKIP_CACHE_PROXY", "").strip() == "1":
        logger.info("Cache proxy skipped (OBS_SKIP_CACHE_PROXY=1)")
        return False
    if not cache_proxy_enabled:
        logger.info("Cache proxy disabled in config")
        return False
    return True


def should_use_proxy(*, cache_proxy_enabled: bool) -> bool:
    """Determine whether sessions should route through the cache proxy.

    Checks config flag, the OBS_SKIP_CACHE_PROXY env var, AND whether
    the proxy subprocess actually started and passed its health check.
    Used by session.py to decide whether to set ANTHROPIC_BASE_URL.
    """
    if not should_attempt_proxy_start(cache_proxy_enabled=cache_proxy_enabled):
        return False
    if not _proxy_healthy:
        logger.info("Cache proxy not healthy — routing directly to Anthropic API")
        return False
    return True
