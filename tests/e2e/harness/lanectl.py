"""Supervise one lane and expose its control API over a Unix HTTP socket."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import signal
import socket
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

PYTHON = "/workspace/obs/.venv/bin/python"


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(path)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.socket_path)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


def call(sock_path: Path, method: str, path: str, body=None, timeout: float = 30) -> dict:
    connection = _UnixHTTPConnection(Path(sock_path), timeout)
    try:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        connection.request(method, path, body=payload, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status >= 400:
            raise RuntimeError(f"lanectl HTTP {response.status}: {result.get('error')}: {result.get('message')}")
        return result
    finally:
        connection.close()


def _proc_rows() -> list[dict]:
    rows = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            args = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
            rows.append({
                "pid": int(entry.name), "ppid": int(fields[1]), "pgid": int(fields[2]),
                "sid": int(fields[3]), "uid": entry.stat().st_uid, "start": int(fields[19]), "args": args,
            })
        except (OSError, ValueError, IndexError):
            continue  # Processes can exit between any two /proc reads.
    return sorted(rows, key=lambda row: row["pid"])


def _env_names() -> dict[str, list[str]]:
    result = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        result[entry.name] = sorted({
            item.partition(b"=")[0].decode("utf-8", "replace")
            for item in raw.split(b"\0") if b"=" in item
        })
    return result


def _config_value(value: str | Path) -> str:
    return str(value).replace("%", "%%")


def _write_supervisor_config(lane_dir: Path, config: dict) -> Path:
    supervisor_dir = lane_dir / "supervisor"
    logs = lane_dir / "logs"
    supervisor_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    code_dir = Path(config["code_dir"])
    harness_root = Path(config["harness_root"])
    selftest = os.environ.get("OBS_E2E_LANECTL_SELFTEST") == "1"
    command = "/bin/sleep infinity" if selftest else shlex.quote(str(harness_root / "deploy/obs-e2e/lane-obs-wrapper.sh"))
    code_pythonpath = json.dumps(str(code_dir / "src"))
    text = f"""[unix_http_server]
file={_config_value(supervisor_dir / 'supervisor.sock')}
chmod=0700

[supervisord]
nodaemon=true
logfile={_config_value(supervisor_dir / 'supervisord.log')}
pidfile={_config_value(supervisor_dir / 'supervisord.pid')}
childlogdir={_config_value(supervisor_dir)}
umask=0077

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://{_config_value(supervisor_dir / 'supervisor.sock')}

[program:obs-telegram-prod]
command={_config_value(command)}
directory={_config_value(code_dir)}
autorestart=true
startsecs={0 if selftest else 10}
stopasgroup=true
killasgroup=true
stopwaitsecs=30
environment=PYTHONPATH={_config_value(code_pythonpath)},OBS_E2E_CODE_DIR={_config_value(json.dumps(str(code_dir)))}
stdout_logfile={_config_value(logs / 'obs-telegram-prod.stdout.log')}
stderr_logfile={_config_value(logs / 'obs-telegram-prod.stderr.log')}
"""
    if config.get("needs_cliproxy") and not selftest:
        run_dir = lane_dir.parent.parent
        relay_command = shlex.join([
            PYTHON, "-m", "tests.e2e.harness.relay", "tcp-to-unix",
            "--listen", "127.0.0.1:8317", "--unix", str(run_dir / "sockets/cliproxy.sock"),
        ])
        text += f"""
[program:cliproxy-relay-client]
command={_config_value(relay_command)}
directory={_config_value(harness_root)}
autorestart=true
startsecs=0
stopasgroup=true
killasgroup=true
stopwaitsecs=30
environment=PYTHONPATH={_config_value(json.dumps(str(harness_root)))}
stdout_logfile={_config_value(logs / 'cliproxy-relay-client.stdout.log')}
stderr_logfile={_config_value(logs / 'cliproxy-relay-client.stderr.log')}
"""
    path = supervisor_dir / "supervisord.conf"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


class _LaneControl:
    def __init__(self, lane_dir: Path, config: dict, config_path: Path, supervisor: subprocess.Popen):
        self.lane_dir = lane_dir
        self.config = config
        self.config_path = config_path
        self.supervisor = supervisor
        self.started = time.monotonic()
        self.background: list[subprocess.Popen] = []
        self.background_lock = threading.Lock()
        self.stopping = False

    def reap_background(self) -> None:
        with self.background_lock:
            self.background = [process for process in self.background if process.poll() is None]

    def start_background(self, argv: list[str], env: dict[str, str], cwd: str | None) -> dict:
        with self.background_lock:
            if self.stopping:
                raise RuntimeError("lane control is shutting down")
            with tempfile.NamedTemporaryFile(dir=self.lane_dir / "supervisor", prefix=".bg-", delete=False) as log:
                temporary = Path(log.name)
                try:
                    process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=cwd)
                    self.background.append(process)
                    os.replace(temporary, self.lane_dir / "supervisor" / f"bg-{process.pid}.log")
                finally:
                    temporary.unlink(missing_ok=True)
            return {"pid": process.pid}

    def stop_background(self) -> None:
        with self.background_lock:
            self.stopping = True
            processes = self.background
            self.background = []
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def supervisor_action(self, action: str, program: str = "obs-telegram-prod", timeout: float = 60) -> dict:
        if action not in {"status", "start", "stop", "restart"}:
            raise ValueError("supervisor action must be status, start, stop, or restart")
        if not isinstance(program, str) or not program:
            raise ValueError("supervisor program must be a nonempty string")
        started = time.monotonic()
        result = subprocess.run(
            ["supervisorctl", "-c", str(self.config_path), action, program],
            capture_output=True, text=True, timeout=timeout,
        )
        return {"rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "duration_s": time.monotonic() - started}

    def ready(self) -> dict:
        alive = self.supervisor.poll() is None
        status = "UNKNOWN"
        supervised_pid = None
        if alive:
            try:
                output = self.supervisor_action("status", timeout=5)["stdout"]
                match = re.search(r"^obs-telegram-prod\s+([A-Z]+)(?:\s+pid\s+(\d+))?", output, re.MULTILINE)
                if match:
                    status = match[1]
                    supervised_pid = int(match[2]) if match[2] else None
            except (OSError, subprocess.TimeoutExpired):
                pass
        rows = _proc_rows()
        descendants = {self.supervisor.pid}
        while True:
            children = {row["pid"] for row in rows if row["ppid"] in descendants}
            new = children - descendants
            if not new:
                break
            descendants.update(new)
        obs_pid = None
        proxy_pid = None
        for row in rows:
            if row["pid"] not in descendants:
                continue
            args = row["args"]
            if len(args) >= 3 and args[1:3] == ["-m", "obs_agent.telegram_main"]:
                obs_pid = row["pid"]
            if len(args) >= 2 and Path(args[1]).name == "cache_proxy.py":
                proxy_pid = row["pid"]
        if os.environ.get("OBS_E2E_LANECTL_SELFTEST") == "1":
            obs_pid = supervised_pid
        healthy = False
        connection = http.client.HTTPConnection("127.0.0.1", int(self.config.get("proxy_port", 29925)), timeout=1)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            healthy = response.status == 200
            response.read()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        return {"supervisord": alive, "obs": status, "obs_pid": obs_pid, "proxy_healthy": healthy, "proxy_pid": proxy_pid, "uptime_s": time.monotonic() - self.started}

    def probe(self, path: str, body: dict) -> dict:
        code = """import json, os, sys
sys.path.insert(0, sys.argv[1])
try:
    from obs_agent import maintenance_restart as restart
    if sys.argv[2] == 'kill_tree':
        value = restart.kill_process_tree(json.loads(sys.argv[3]))
    else:
        value = restart.find_orphaned_agent_clis(own_pgid=os.getpgrp())
    answer = {'result': value}
except Exception as exc:
    answer = {'exception': type(exc).__name__ + ': ' + str(exc)}
print(json.dumps(answer))
"""
        action = path.rsplit("/", 1)[1]
        pid = body.get("pid")
        if action == "kill_tree" and type(pid) is not int:
            raise ValueError("pid must be an integer")
        env = dict(os.environ, PYTHONPATH=str(Path(self.config["code_dir"]) / "src"))
        # INVARIANT(process-identity): probe and signal only inside a lane PID namespace. Breaking it lets a lane probe or kill production processes.
        # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Container and Isolation
        result = subprocess.run(
            [PYTHON, "-c", code, env["PYTHONPATH"], action, json.dumps(pid)],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode:
            return {"rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        return json.loads(result.stdout)

    def dispatch(self, method: str, path: str, body: dict):
        self.reap_background()
        if method == "GET":
            if path == "/ready":
                return self.ready()
            if path == "/procs":
                return _proc_rows()
            if path == "/envnames":
                return _env_names()
        if method == "POST":
            if path == "/exec":
                argv = body.get("argv")
                if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
                    raise ValueError("argv must be a nonempty list of strings")
                extra_env = body.get("env", {})
                if not isinstance(extra_env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in extra_env.items()):
                    raise ValueError("env must map strings to strings")
                env = dict(os.environ, **extra_env)
                if body.get("background"):
                    return self.start_background(argv, env, body.get("cwd"))
                timeout = float(body.get("timeout", 30))
                if timeout <= 0:
                    raise ValueError("timeout must be positive")
                started = time.monotonic()
                result = subprocess.run(
                    argv, capture_output=True, text=True, timeout=timeout,
                    env=env, cwd=body.get("cwd"),
                )
                return {"rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "duration_s": time.monotonic() - started}
            if path == "/supervisor":
                return self.supervisor_action(body.get("action"), body.get("program", "obs-telegram-prod"))
            if path == "/signal":
                pid = body.get("pid")
                if type(pid) is not int or pid <= 0:
                    raise ValueError("pid must be a positive integer")
                name = body.get("sig")
                if not isinstance(name, str):
                    raise ValueError("sig must name a POSIX signal")
                sig = getattr(signal, name if name.startswith("SIG") else "SIG" + name, None)
                if not isinstance(sig, signal.Signals):
                    raise ValueError("unknown POSIX signal")
                os.kill(pid, sig)
                return {"pid": pid, "sig": signal.Signals(sig).name, "ok": True}
            if path in {"/probe/kill_tree", "/probe/find_orphans"}:
                return self.probe(path, body)
        raise FileNotFoundError("unknown lanectl endpoint")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args) -> None:
        pass  # Never log request bodies or process environments.

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def _handle(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative Content-Length")
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            result = self.server.control.dispatch(self.command, self.path, body)
            status = 200
        except (ValueError, TypeError, KeyError) as exc:
            status, result = 400, {"error": type(exc).__name__, "message": str(exc)}
        except (FileNotFoundError, ProcessLookupError) as exc:
            status, result = 404, {"error": type(exc).__name__, "message": str(exc)}
        except subprocess.TimeoutExpired:
            status, result = 504, {"error": "TimeoutExpired", "message": "lane command timed out"}
        except Exception as exc:
            status, result = 500, {"error": type(exc).__name__, "message": str(exc)}
        self._send_json(status, result)

    def send_error(self, code, message=None, explain=None) -> None:
        message = message or self.responses.get(code, ("HTTP error",))[0]
        self._send_json(code, {"error": "HTTPError", "message": message})

    def _send_json(self, status: int, result) -> None:
        payload = json.dumps(result).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _remove_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise ValueError(f"refusing to replace non-socket path {path}")
    path.unlink()


def serve(lane_dir: Path) -> int:
    lane_dir = Path(lane_dir).resolve()
    config = json.loads((lane_dir / "lane.json").read_text(encoding="utf-8"))
    config_path = _write_supervisor_config(lane_dir, config)
    socket_path = lane_dir / "lanectl.sock"
    _remove_socket(socket_path)
    stopping = threading.Event()
    old_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig] = signal.signal(sig, lambda signum, frame: stopping.set())
    supervisor = None
    control = None
    server = None
    server_thread = None
    unexpected_exit = False
    try:
        with (lane_dir / "supervisor/process.log").open("ab") as log:
            supervisor = subprocess.Popen(["supervisord", "-n", "-c", str(config_path)], stdout=log, stderr=subprocess.STDOUT)
        control = _LaneControl(lane_dir, config, config_path, supervisor)
        server = _Server(str(socket_path), _Handler)
        server.control = control
        socket_path.chmod(0o700)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def watch_supervisor() -> None:
            supervisor.wait()
            stopping.set()

        threading.Thread(target=watch_supervisor, daemon=True).start()
        stopping.wait()
        unexpected_exit = supervisor.poll() is not None
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=2)
        if control is not None:
            control.reap_background()
            control.stop_background()
        if supervisor is not None and supervisor.poll() is None:
            try:
                control.supervisor_action("stop", "all", timeout=40)
            except (OSError, subprocess.TimeoutExpired):
                pass
            supervisor.terminate()
            try:
                supervisor.wait(timeout=10)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)
        socket_path.unlink(missing_ok=True)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    return 1 if unexpected_exit else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    server_parser = subparsers.add_parser("serve", help="run one lane's supervisor and Unix control server")
    server_parser.add_argument("--lane-dir", type=Path, required=True)
    call_parser = subparsers.add_parser("call", help="call a running lane's Unix control API")
    call_parser.add_argument("--lane-dir", type=Path, required=True)
    call_parser.add_argument("method", choices=("GET", "POST"))
    call_parser.add_argument("path")
    call_parser.add_argument("body", nargs="?", help="optional JSON request body")
    call_parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.lane_dir)
    result = call(args.lane_dir / "lanectl.sock", args.method, args.path, None if args.body is None else json.loads(args.body), args.timeout)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
