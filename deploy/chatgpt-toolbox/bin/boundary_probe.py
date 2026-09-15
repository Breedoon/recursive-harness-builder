#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile


HOST_PATHS = [
    "/workspace/runtime",
    "/workspace/obs",
    "/home/agent",
    "/home/breedoon",
    "/Users/breedoon",
]

CREDENTIAL_PATHS = [
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/secrets",
    "/root/.ssh",
    "/root/.git-credentials",
    "/root/.config/gh",
    "/session/home/.ssh",
    "/session/home/.git-credentials",
    "/session/home/.config/gh",
    "/session/home/.config/git/credentials",
    "/workspace/.git",
    "/workspace/.git-credentials",
    "/session/home/.desktop-commander-device/device.json",
]

SENSITIVE_ENV_EXACT = {
    "DOCKER_CONFIG",
    "DOCKER_HOST",
    "GIT_ASKPASS",
    "SSH_AGENT_PID",
    "SSH_AUTH_SOCK",
}
SENSITIVE_ENV_FRAGMENTS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "ACCESS_KEY")
SENSITIVE_ENV_PREFIXES = ("GH_", "GITHUB_")


def status_fields() -> dict[str, str]:
    wanted = {"CapEff", "CapBnd", "NoNewPrivs", "Seccomp"}
    result = {}
    for line in pathlib.Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in wanted:
            result[key] = value.strip()
    return result


def relevant_mounts() -> list[dict[str, str]]:
    wanted = {"/", "/tmp", "/run", "/workspace", "/session"}
    mounts = []
    for line in pathlib.Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        mount_point = fields[4]
        if mount_point not in wanted:
            continue
        fs_fields = after.split()
        mounts.append(
            {
                "mount_point": mount_point,
                "mount_options": fields[5],
                "fs_type": fs_fields[0],
            }
        )
    return sorted(mounts, key=lambda item: item["mount_point"])


def path_state(path: str) -> str:
    try:
        os.lstat(path)
        return "present"
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "inaccessible"


def write_probe(path: str) -> bool:
    target = pathlib.Path(path)
    try:
        target.write_text("probe", encoding="utf-8")
        target.unlink()
        return True
    except OSError:
        return False


def git_helper_configured(scope: str) -> bool:
    result = subprocess.run(
        ["git", "config", f"--{scope}", "--get-all", "credential.helper"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def default_gateway() -> str | None:
    for line in pathlib.Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    return None


def tcp_reachable(host: str | None, port: int) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> int:
    env_names = sorted(os.environ)
    sensitive_env_names = sorted(
        name
        for name in env_names
        if name in SENSITIVE_ENV_EXACT
        or name.startswith(SENSITIVE_ENV_PREFIXES)
        or any(fragment in name for fragment in SENSITIVE_ENV_FRAGMENTS)
    )
    status = status_fields()
    gateway = default_gateway()

    receipt = {
        "schema": "chatgpt-toolbox-boundary-probe-v1",
        "identity": {
            "uid": os.getuid(),
            "gid": os.getgid(),
            "groups": os.getgroups(),
            "hostname": socket.gethostname(),
            "cwd": os.getcwd(),
        },
        "kernel_boundary": {
            "cap_eff_hex": status.get("CapEff"),
            "cap_bnd_hex": status.get("CapBnd"),
            "no_new_privs": status.get("NoNewPrivs"),
            "seccomp": status.get("Seccomp"),
        },
        "mounts": relevant_mounts(),
        "write_tests": {
            "root_filesystem_write": write_probe("/rootfs-write-probe"),
            "tmp_write": write_probe("/tmp/toolbox-write-probe"),
            "run_write": write_probe("/run/toolbox-write-probe"),
            "workspace_write": write_probe("/workspace/toolbox-write-probe"),
            "session_write": write_probe("/session/toolbox-write-probe"),
        },
        "environment_names": env_names,
        "sensitive_environment_names": sensitive_env_names,
        "host_path_state": {path: path_state(path) for path in HOST_PATHS},
        "credential_path_state": {path: path_state(path) for path in CREDENTIAL_PATHS},
        "tool_presence": {
            "docker": shutil.which("docker") is not None,
            "gh": shutil.which("gh") is not None,
            "ssh": shutil.which("ssh") is not None,
        },
        "git_credentials": {
            "global_helper_configured": git_helper_configured("global"),
            "system_helper_configured": git_helper_configured("system"),
        },
        "docker_control_tcp": {
            "gateway_present": gateway is not None,
            "port_2375_reachable": tcp_reachable(gateway, 2375),
            "port_2376_reachable": tcp_reachable(gateway, 2376),
        },
    }

    checks = [
        os.getuid() != 0,
        status.get("CapEff") == "0000000000000000",
        status.get("CapBnd") == "0000000000000000",
        status.get("NoNewPrivs") == "1",
        not receipt["write_tests"]["root_filesystem_write"],
        receipt["write_tests"]["tmp_write"],
        not receipt["write_tests"]["run_write"],
        receipt["write_tests"]["workspace_write"],
        receipt["write_tests"]["session_write"],
        "present" not in receipt["host_path_state"].values(),
        "present" not in receipt["credential_path_state"].values(),
        not any(receipt["tool_presence"].values()),
        not receipt["git_credentials"]["global_helper_configured"],
        not receipt["git_credentials"]["system_helper_configured"],
        not sensitive_env_names,
        not receipt["docker_control_tcp"]["port_2375_reachable"],
        not receipt["docker_control_tcp"]["port_2376_reachable"],
    ]
    receipt["passed"] = all(checks)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
