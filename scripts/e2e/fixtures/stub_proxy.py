#!/usr/bin/env python3
"""Run the repo's cache proxy with its upstreams pointed at the capture endpoint.

Usage: stub_proxy.py <port> <capture-endpoint-port>
(environment: CACHE_PROXY_LOG_DIR must be a private scratch directory)

The proxy code is imported unchanged from ``src/cache_proxy.py``; only its two
upstream base URLs are replaced, so the proxy's normalizations, pins and request
log behave exactly as in production while no request leaves the machine. The
Claude and CLIProxyAPI routes get different URL prefixes (/anthropic, /cliproxy)
because the proxy decides a request's route by comparing upstream URLs.
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def main():
    port, stub_port = int(sys.argv[1]), int(sys.argv[2])
    log_dir = os.environ.get("CACHE_PROXY_LOG_DIR", "")
    if not log_dir or "/logs/cache-proxy" in log_dir:
        sys.exit("CACHE_PROXY_LOG_DIR must be a private scratch dir, never the production log")
    os.environ.setdefault("CACHE_PROXY_REQUEST_LOG", "1")
    os.environ.setdefault("CACHE_PROXY_IDLE_EXIT_SECONDS", "1800")
    os.environ["CLI_PROXY_BASE_URL"] = f"http://127.0.0.1:{stub_port}/cliproxy"
    sys.path.insert(0, str(REPO / "src"))
    import cache_proxy as cp
    cp.ANTHROPIC_UPSTREAM = f"http://127.0.0.1:{stub_port}/anthropic"
    cp.CLI_PROXY_UPSTREAM = f"http://127.0.0.1:{stub_port}/cliproxy"
    sys.argv = [sys.argv[0], str(port)]
    cp.main()


if __name__ == "__main__":
    main()
