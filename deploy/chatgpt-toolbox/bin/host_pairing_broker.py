#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import select
import signal
import subprocess
import sys
import time
from typing import Any


CONTAINER = "chatgpt-toolbox"
ENTRYPOINT = "/usr/local/bin/toolbox-entrypoint"
CODE_PATTERN = re.compile(r"^[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})*$")
URL_PATTERN = re.compile(r"^https://")
PAIRING_TTL_SECONDS = 15 * 60


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def default_runtime_dir() -> pathlib.Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return pathlib.Path(base) / "chatgpt-toolbox-pairing"


def atomic_json(path: pathlib.Path, payload: dict[str, Any], mode: int) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def container_running() -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def ensure_runtime_dir(runtime_dir: pathlib.Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_dir, 0o700)


def sanitized_status(runtime_dir: pathlib.Path) -> dict[str, Any]:
    status = read_json(runtime_dir / "status.json")
    pid = int(read_json(runtime_dir / "broker.json").get("pid", 0) or 0)
    return {
        "schema": "chatgpt-toolbox-pairing-status-v1",
        "state": status.get("state", "stopped"),
        "updated_at": status.get("updated_at"),
        "broker_running": pid_running(pid),
        "pairing_available": (runtime_dir / "pairing.json").exists(),
        "device_identity_sha256": status.get("device_identity_sha256"),
        "runtime_dir": str(runtime_dir),
    }


def remove_pairing(runtime_dir: pathlib.Path) -> None:
    for name in ("pairing.json", "pairing.json.tmp"):
        try:
            (runtime_dir / name).unlink()
        except FileNotFoundError:
            pass


def remote_clean() -> None:
    subprocess.run(
        ["docker", "exec", CONTAINER, ENTRYPOINT, "remote-clean"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )


def purge_remote_config() -> None:
    subprocess.run(
        ["docker", "exec", CONTAINER, ENTRYPOINT, "remote-purge-config"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )


def stop(runtime_dir: pathlib.Path) -> dict[str, Any]:
    broker = read_json(runtime_dir / "broker.json")
    pid = int(broker.get("pid", 0) or 0)
    remote_clean()
    if pid_running(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while pid_running(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if pid_running(pid):
            os.kill(pid, signal.SIGKILL)
    remove_pairing(runtime_dir)
    prior_status = read_json(runtime_dir / "status.json")
    atomic_json(
        runtime_dir / "status.json",
        {
            "state": "stopped",
            "updated_at": utc_now(),
            "device_identity_sha256": prior_status.get("device_identity_sha256"),
        },
        0o600,
    )
    try:
        (runtime_dir / "broker.json").unlink()
    except FileNotFoundError:
        pass
    return sanitized_status(runtime_dir)


def worker(runtime_dir: pathlib.Path) -> int:
    ensure_runtime_dir(runtime_dir)
    pairing_path = runtime_dir / "pairing.json"
    status_path = runtime_dir / "status.json"
    remove_pairing(runtime_dir)
    atomic_json(status_path, {"state": "starting", "updated_at": utc_now()}, 0o600)

    process = subprocess.Popen(
        ["docker", "exec", "-i", CONTAINER, ENTRYPOINT, "remote"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        remote_clean()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    verification_uri = ""
    user_code = ""
    device_identity_sha256 = ""
    expect_url = False
    expect_code = False
    deadline = time.monotonic() + PAIRING_TTL_SECONDS

    try:
        assert process.stdout is not None
        while True:
            if stopping:
                break
            if time.monotonic() >= deadline and not user_code:
                atomic_json(
                    status_path,
                    {"state": "pairing-timeout", "updated_at": utc_now()},
                    0o600,
                )
                remote_clean()
                break
            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            stripped = line.strip()

            if "Open this URL in your browser" in stripped:
                expect_url = True
                continue
            if expect_url and URL_PATTERN.match(stripped):
                verification_uri = stripped
                expect_url = False
                continue
            if "Enter this code when prompted" in stripped:
                expect_code = True
                continue
            if expect_code and CODE_PATTERN.fullmatch(stripped):
                user_code = stripped
                expect_code = False
                if verification_uri:
                    created_at = utc_now()
                    atomic_json(
                        pairing_path,
                        {
                            "schema": "chatgpt-toolbox-volatile-pairing-v1",
                            "verification_uri": verification_uri,
                            "user_code": user_code,
                            "created_at": created_at,
                            "expires_in_seconds": PAIRING_TTL_SECONDS,
                        },
                        0o600,
                    )
                    atomic_json(
                        status_path,
                        {"state": "pairing-ready", "updated_at": created_at},
                        0o600,
                    )
                continue
            for marker in (
                "Device ID assigned:",
                "Device ID authenticated:",
                "Device ID changed:",
            ):
                if marker in stripped:
                    raw_identity = stripped.split(marker, 1)[1].strip()
                    if "→" in raw_identity:
                        raw_identity = raw_identity.rsplit("→", 1)[1].strip()
                    if raw_identity:
                        device_identity_sha256 = hashlib.sha256(
                            raw_identity.encode("utf-8")
                        ).hexdigest()
                    break
            if "Device ready:" in stripped:
                remove_pairing(runtime_dir)
                purge_remote_config()
                atomic_json(
                    status_path,
                    {
                        "state": (
                            "connected" if device_identity_sha256
                            else "connected-identity-missing"
                        ),
                        "updated_at": utc_now(),
                        "device_identity_sha256": device_identity_sha256 or None,
                    },
                    0o600,
                )
                continue
            if "Authorization timeout" in stripped:
                remove_pairing(runtime_dir)
                atomic_json(
                    status_path,
                    {"state": "authorization-timeout", "updated_at": utc_now()},
                    0o600,
                )
                continue
            if "Device startup failed" in stripped:
                remove_pairing(runtime_dir)
                atomic_json(
                    status_path,
                    {"state": "device-startup-failed", "updated_at": utc_now()},
                    0o600,
                )
                continue

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if stopping:
            final_state = "stopped"
        else:
            current_state = read_json(status_path).get("state")
            final_state = current_state if current_state in {
                "authorization-timeout",
                "device-startup-failed",
                "pairing-timeout",
            } else "exited"
        remove_pairing(runtime_dir)
        atomic_json(
            status_path,
            {
                "state": final_state,
                "updated_at": utc_now(),
                "device_identity_sha256": device_identity_sha256 or None,
            },
            0o600,
        )
        return 0
    finally:
        remote_clean()
        remove_pairing(runtime_dir)
        try:
            (runtime_dir / "broker.json").unlink()
        except FileNotFoundError:
            pass


def start(runtime_dir: pathlib.Path) -> dict[str, Any]:
    ensure_runtime_dir(runtime_dir)
    if not container_running():
        raise RuntimeError(f"{CONTAINER} is not running")
    existing_pid = int(read_json(runtime_dir / "broker.json").get("pid", 0) or 0)
    if pid_running(existing_pid):
        return sanitized_status(runtime_dir)
    remove_pairing(runtime_dir)
    process = subprocess.Popen(
        [
            sys.executable,
            str(pathlib.Path(__file__).resolve()),
            "_worker",
            "--runtime-dir",
            str(runtime_dir),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    atomic_json(
        runtime_dir / "broker.json",
        {"pid": process.pid, "started_at": utc_now()},
        0o600,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = sanitized_status(runtime_dir)
        if status["state"] in {
            "pairing-ready",
            "connected",
            "connected-identity-missing",
            "authorization-timeout",
            "device-startup-failed",
            "pairing-timeout",
            "exited",
        }:
            return status
        time.sleep(0.25)
    return sanitized_status(runtime_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop", "_worker"])
    parser.add_argument("--runtime-dir", type=pathlib.Path, default=default_runtime_dir())
    args = parser.parse_args()
    runtime_dir = args.runtime_dir

    if args.action == "_worker":
        return worker(runtime_dir)
    if args.action == "start":
        result = start(runtime_dir)
    elif args.action == "stop":
        result = stop(runtime_dir)
    else:
        ensure_runtime_dir(runtime_dir)
        result = sanitized_status(runtime_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
