"""Host Docker transport and container isolation contract for the E2E harness.

Docker is reachable only through batch-mode SSH. Dry runs execute no commands;
mount translation uses the last live-discovered map cached under E2E_ROOT.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

E2E_ROOT = Path("/workspace/runtime/e2e")
_FORBIDDEN_SOURCES = (
    "/workspace/runtime/state",
    "/workspace/runtime/logs",
    "/home/agent/.claude",
    "/home/agent/.claude.json",
    "/workspace/runtime/git/obs-vault-active",
    "/workspace/obs/.env",
    "/workspace/runtime/cliproxy",
    "/workspace/runtime/secrets",
    "/var/run/docker.sock",
    "/run/docker.sock",
)


@dataclass(frozen=True)
class Mount:
    src: Path
    dst: str
    ro: bool = False


def _dry_run() -> bool:
    return os.environ.get("OBS_E2E_DOCKER_DRY_RUN") == "1"


def _argv(args: list[str]) -> list[str]:
    return [
        "ssh", "-o", "BatchMode=yes", "ubuntu-host",
        shlex.join(["sudo", "-n", "docker", *args]),
    ]


def _run(args: list[str], *, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    argv = _argv(args)
    if _dry_run():
        print(json.dumps(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        check=check, timeout=timeout,
    )


@lru_cache(maxsize=1)
def host_mounts() -> list[tuple[str, str]]:
    """Return obs-test's Docker Destination -> host Source bind/volume map."""
    cache = E2E_ROOT / "host-mounts.json"
    if _dry_run():
        if not cache.is_file():
            raise RuntimeError("Run host_mounts() once without dry-run to discover the host mount map")
        return [tuple(item) for item in json.loads(cache.read_text())]
    result = _run(["inspect", "obs-test", "--format", "{{json .Mounts}}"])
    mounts = [(item["Destination"], item["Source"]) for item in json.loads(result.stdout) if item.get("Source")]
    # Live runs always inspect; the disk snapshot is only for offline dry runs.
    E2E_ROOT.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".host-mounts-", suffix=".json", dir=E2E_ROOT)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(mounts, output)
            output.write("\n")
        os.replace(temporary, cache)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return mounts


def to_host_path(path: str | Path) -> str:
    local = Path(path).resolve()
    for destination, source in sorted(host_mounts(), key=lambda item: len(item[0]), reverse=True):
        destination_path = Path(destination)
        if local.is_relative_to(destination_path):
            return str(Path(source) / local.relative_to(destination_path))
    raise ValueError(f"Path is not under an obs-test Docker mount: {local}")


def _context_digest(context_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(context_dir.rglob("*"), key=lambda path: path.relative_to(context_dir).as_posix()):
        if path.is_file():
            relative = path.relative_to(context_dir).as_posix().encode("utf-8")
            data = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()


def _record_image(image: str, digest: str, context_dir: Path) -> None:
    E2E_ROOT.mkdir(parents=True, exist_ok=True)
    registry = E2E_ROOT / "images.json"
    with (E2E_ROOT / "images.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(registry.read_text()) if registry.exists() else {}
        data[image] = {
            "sha256": digest,
            "context_dir": str(context_dir),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        fd, temporary = tempfile.mkstemp(prefix=".images-", suffix=".json", dir=E2E_ROOT)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(data, output, indent=2, sort_keys=True)
                output.write("\n")
            os.replace(temporary, registry)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def ensure_image(context_dir: Path) -> str:
    context_dir = Path(context_dir).resolve()
    if not (context_dir / "Dockerfile").is_file():
        raise ValueError(f"Missing Dockerfile in build context: {context_dir}")
    digest = _context_digest(context_dir)
    image = f"obs-e2e:{digest[:12]}"
    found = _run(["image", "inspect", image], check=False)
    if _dry_run() or found.returncode != 0:
        _run(["build", "-t", image, to_host_path(context_dir)], timeout=600)
    if not _dry_run():
        _record_image(image, digest, context_dir)
    return image


def _validate_source(source: Path) -> None:
    resolved = source.resolve()
    # INVARIANT: only fixture sources may occupy production paths in a lane.
    # Check sources (including symlink targets/ancestor mounts), not destinations.
    # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Isolation
    for forbidden in _FORBIDDEN_SOURCES:
        for blocked in {Path(forbidden), Path(forbidden).resolve()}:
            if resolved.is_relative_to(blocked) or blocked.is_relative_to(resolved):
                raise ValueError(f"Production source must not be mounted into E2E: {source}")
    if resolved.name == "docker.sock":
        raise ValueError(f"Docker socket must not be mounted into E2E: {source}")


def _mount_argument(mount: Mount) -> str:
    source = Path(mount.src)
    _validate_source(source)
    destination = Path(mount.dst)
    if not destination.is_absolute():
        raise ValueError(f"Container mount destination must be absolute: {mount.dst}")
    fields = ["type=bind", f"src={to_host_path(source)}", f"dst={mount.dst}"]
    if mount.ro:
        fields.append("readonly")
    # Docker's --mount grammar is CSV; shlex alone cannot escape a path comma.
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow(fields)
    return output.getvalue()


def run_container(
    name: str,
    image: str,
    *,
    role: str,
    run_id: str,
    lane: str | None = None,
    env_file: Path,
    mounts: list[Mount],
    network: str = "bridge",
    add_host_gateway: bool = True,
    command: list[str],
    workdir: str | None = None,
) -> str:
    if not command:
        raise ValueError("An explicit container command is required")
    if network == "host":
        raise ValueError("E2E containers never use the host network namespace")
    # Validate all sources before any Docker operation, even host-path discovery.
    _validate_source(Path(env_file))
    for mount in mounts:
        _validate_source(Path(mount.src))
    args = [
        "run", "--detach", "--name", name,
        "--init", "--user", "1000:1000", "--cgroup-parent", "obs-e2e.slice",
        "--memory", "14g", "--memory-swap", "14g", "--cpus", "8", "--pids-limit", "8192",
        "--env-file", to_host_path(env_file),
        "--tmpfs", "/tmp:rw,exec,size=1g", "--security-opt", "no-new-privileges",
        "--label", f"obs-e2e.run={run_id}", "--label", f"obs-e2e.role={role}",
        "--network", network,
        # The SSH base image has /entrypoint.sh; the harness starts only its command.
        "--entrypoint", "",
    ]
    if lane is not None:
        args.extend(["--label", f"obs-e2e.lane={lane}"])
    if add_host_gateway and not network.startswith("container:"):
        args.extend(["--add-host", "host.docker.internal:host-gateway"])
    if workdir is not None:
        args.extend(["--workdir", str(workdir)])
    for mount in mounts:
        args.extend(["--mount", _mount_argument(mount)])
    result = _run([*args, image, *command])
    return f"dry-run:{name}" if _dry_run() else result.stdout.strip()


def ps(run_id: str | None = None) -> list[dict]:
    selector = "label=obs-e2e.run" if run_id is None else f"label=obs-e2e.run={run_id}"
    result = _run(["ps", "--all", "--filter", selector, "--format", "{{json .}}"])
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def rm_run(run_id: str) -> list[str]:
    containers = ps(run_id)
    ids = [item["ID"] for item in containers]
    if ids:
        _run(["rm", "--force", *ids])
    return ids


def logs(name: str, follow: bool = False) -> subprocess.Popen | str:
    args = ["logs", *( ["--follow"] if follow else [] ), name]
    if not follow:
        result = _run(args)
        return result.stdout + result.stderr
    argv = _argv(args)
    if _dry_run():
        print(json.dumps(argv))
        return ""
    return subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )


def inspect(name: str) -> dict:
    result = _run(["inspect", name])
    return {} if _dry_run() else json.loads(result.stdout)[0]


def wait_running(name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        state = inspect(name).get("State", {})
        if _dry_run() or state.get("Running"):
            return True
        if state.get("Status") in {"exited", "dead", "removing"}:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # Kernel-resource polling is explicitly permitted for harness internals.
        time.sleep(min(0.1, remaining))
