"""Scratch-proxy idle exit (CACHE_PROXY_IDLE_EXIT_SECONDS, 2026-10-16).

Starts real proxy subprocesses on spare ports with private log dirs.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp_path, port: int, idle: str | None) -> subprocess.Popen:
    env = dict(os.environ, CACHE_PROXY_LOG_DIR=str(tmp_path / "log"),
               CACHE_PROXY_REQUEST_LOG="0", PYTHONPATH=str(REPO / "src"))
    env.pop("CACHE_PROXY_IDLE_EXIT_SECONDS", None)
    if idle is not None:
        env["CACHE_PROXY_IDLE_EXIT_SECONDS"] = idle
    proc = subprocess.Popen([sys.executable, str(REPO / "src" / "cache_proxy.py"), str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("proxy did not start")


def test_scratch_proxy_exits_when_idle(tmp_path):
    proc = _start(tmp_path, _free_port(), "2")
    try:
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_requests_keep_it_alive(tmp_path):
    port = _free_port()
    proc = _start(tmp_path, port, "2")
    try:
        end = time.monotonic() + 4
        while time.monotonic() < end:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            time.sleep(0.5)
        assert proc.poll() is None
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_unset_means_never(tmp_path):
    proc = _start(tmp_path, _free_port(), None)
    try:
        time.sleep(3)
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()
