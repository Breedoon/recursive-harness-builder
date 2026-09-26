"""Behaviour of deploy/obs-telegram-prod-wrapper.sh with a fake telegram_main.

The wrapper is exercised on a copy whose paths, port and timings are rewritten
to test values. The real cache-proxy port (28925) is never touched.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "obs-telegram-prod-wrapper.sh"

FAKE_CHILD = """#!/bin/bash
# fake telegram_main: records signals; mode from $FAKE_MODE
out="$FAKE_OUT"
echo $$ > "$out/child.pid"
sleep 300 &            # a "Claude CLI" in the same process group
echo $! > "$out/cli.pid"
case "$FAKE_MODE" in
  ignore_usr1) trap 'echo usr1 >> "$out/signals"' USR1 ;;
  *) trap 'echo usr1 >> "$out/signals"; kill -TERM 0' USR1 ;;  # maintenance restart: TERM own group
esac
trap 'echo term >> "$out/signals"; exit 143' TERM
if [ "$FAKE_MODE" = "crash" ]; then sleep 0.5; exit 1; fi
while true; do sleep 0.1; done
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare(tmp_path: Path, port: int) -> Path:
    text = WRAPPER.read_text()
    assert "28925" in text
    auth = tmp_path / "auth"
    auth.write_text("token\n")
    child = tmp_path / "fake_child.sh"
    child.write_text(FAKE_CHILD)
    child.chmod(0o755)
    text = (
        text.replace("28925", str(port))
        .replace("/run/secrets/obs-local-llm-auth", str(auth))
        .replace("/workspace/obs/.venv/bin/python -m obs_agent.telegram_main --prod", str(child))
        .replace("startup_deadline=$((SECONDS + 20))", "startup_deadline=$((SECONDS + 2))")
        .replace("    sleep 5\n  done", "    sleep 0.2\n  done")
    )
    assert str(child) in text and "28925" not in text
    script = tmp_path / "wrapper.sh"
    script.write_text(text)
    script.chmod(0o755)
    return script


def _start(tmp_path: Path, script: Path, mode: str, **extra_env) -> subprocess.Popen:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        FAKE_OUT=str(out),
        FAKE_MODE=mode,
        OBS_KILLSWITCH_SENTINEL=str(tmp_path / "sentinel"),
        OBS_WRAPPER_MAINTENANCE_EXIT_WAIT_SECONDS="2",
        **extra_env,
    )
    # start_new_session: the wrapper leads its own process group, as under supervisord.
    return subprocess.Popen(["/bin/bash", str(script)], env=env, start_new_session=True)


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        if time.time() > deadline:
            raise AssertionError(f"{path} not created")
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.split(") ", 1)[1][0] != "Z"
    except OSError:
        return False


class _HealthServer:
    """/health on a spare port, in its own process.

    Its own process because the wrapper's cleanup_cache_proxy TERMs whatever
    listens on the proxy port (at start and on exit); started after the
    wrapper's start-up cleanup has run.
    """

    def __init__(self, tmp_path: Path):
        self.port = _free_port()
        self.root = tmp_path / "www"
        self.root.mkdir()
        (self.root / "health").write_text("ok")
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["python3", "-m", "http.server", str(self.port), "--bind", "127.0.0.1", "--directory", str(self.root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise AssertionError("health server did not start")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture
def health_server(tmp_path):
    server = _HealthServer(tmp_path)
    yield server
    server.stop()


def test_term_to_wrapper_is_kill_switch(tmp_path, health_server):
    proc = _start(tmp_path, _prepare(tmp_path, health_server.port), "normal")
    _wait_for(tmp_path / "out" / "cli.pid")
    health_server.start()
    time.sleep(0.3)
    os.killpg(proc.pid, signal.SIGTERM)  # what supervisord's stopasgroup sends
    assert proc.wait(timeout=10) == 143
    assert (tmp_path / "sentinel").exists()


def test_child_crash_cleans_group_without_sentinel(tmp_path, health_server):
    proc = _start(tmp_path, _prepare(tmp_path, health_server.port), "crash")
    _wait_for(tmp_path / "out" / "cli.pid")
    cli = int((tmp_path / "out" / "cli.pid").read_text())
    assert proc.wait(timeout=15) == 1
    assert not (tmp_path / "sentinel").exists()
    time.sleep(0.2)
    assert not _alive(cli), "orphaned CLI survived the crash"


def test_dead_proxy_triggers_maintenance_restart(tmp_path, health_server):
    proc = _start(tmp_path, _prepare(tmp_path, health_server.port), "normal")
    _wait_for(tmp_path / "out" / "cli.pid")
    health_server.start()
    time.sleep(0.6)  # at least one healthy check
    health_server.stop()
    assert proc.wait(timeout=20) == 143  # the child's group TERM reached the wrapper
    assert "usr1" in (tmp_path / "out" / "signals").read_text()
    assert (tmp_path / "sentinel").exists()  # maintenance marker, not crash snapshot, resumes


def test_unresponsive_daemon_gets_sigkill_after_usr1(tmp_path, health_server):
    proc = _start(tmp_path, _prepare(tmp_path, health_server.port), "ignore_usr1")
    _wait_for(tmp_path / "out" / "cli.pid")
    cli = int((tmp_path / "out" / "cli.pid").read_text())
    health_server.start()
    time.sleep(0.6)
    health_server.stop()
    assert proc.wait(timeout=30) == 137
    assert "usr1" in (tmp_path / "out" / "signals").read_text()
    assert not (tmp_path / "sentinel").exists()  # crash path: snapshot resumes
    time.sleep(0.2)
    assert not _alive(cli)
