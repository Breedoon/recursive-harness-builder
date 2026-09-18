#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import tempfile


ACTIVE_VAULT = "/workspace/runtime/git/obs-vault-active"
ACTIVE_VAULT_GIT = f"{ACTIVE_VAULT}/.git"
FORBIDDEN_HOST_PATHS = [
    "/workspace/obs",
    "/home/agent",
    "/home/breedoon",
    "/Users/breedoon",
]
ACTIVE_VAULT_FORBIDDEN_SIBLINGS = [
    "/workspace/runtime/state",
    "/workspace/runtime/logs",
    "/workspace/runtime/cliproxy",
    "/workspace/runtime/git/obs-artifacts",
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
    f"{ACTIVE_VAULT}/.git-credentials",
    f"{ACTIVE_VAULT}/.ssh",
    f"{ACTIVE_VAULT}/.config/gh",
    f"{ACTIVE_VAULT}/.config/git/credentials",
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
    wanted = {"/", "/tmp", "/run", "/workspace", "/session", ACTIVE_VAULT, ACTIVE_VAULT_GIT}
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


def git_repo_helper_configured(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", path, "config", "--get-all", "credential.helper"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def git_repository_visible(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


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
        "forbidden_host_path_state": {
            path: path_state(path) for path in FORBIDDEN_HOST_PATHS
        },
        "active_vault": {
            "path": ACTIVE_VAULT,
            "state": path_state(ACTIVE_VAULT),
            "real_path": os.path.realpath(ACTIVE_VAULT),
            "forbidden_sibling_state": {
                path: path_state(path) for path in ACTIVE_VAULT_FORBIDDEN_SIBLINGS
            },
            "git_mount_state": path_state(ACTIVE_VAULT_GIT),
            "git_write": write_probe(f"{ACTIVE_VAULT_GIT}/toolbox-git-write-probe"),
            "git_metadata_state": {
                path: path_state(path)
                for path in (
                    f"{ACTIVE_VAULT_GIT}/HEAD",
                    f"{ACTIVE_VAULT_GIT}/config",
                    f"{ACTIVE_VAULT_GIT}/objects",
                    f"{ACTIVE_VAULT_GIT}/refs",
                )
            },
            "repository_visible": git_repository_visible(ACTIVE_VAULT),
        },
        "credential_path_state": {path: path_state(path) for path in CREDENTIAL_PATHS},
        "tool_presence": {
            "docker": shutil.which("docker") is not None,
            "gh": shutil.which("gh") is not None,
            "ssh": shutil.which("ssh") is not None,
        },
        "git_credentials": {
            "global_helper_configured": git_helper_configured("global"),
            "system_helper_configured": git_helper_configured("system"),
            "active_vault_helper_configured": git_repo_helper_configured(ACTIVE_VAULT),
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
        not receipt["write_tests"]["workspace_write"],
        receipt["write_tests"]["session_write"],
        receipt["active_vault"]["state"] == "present",
        receipt["active_vault"]["real_path"] == ACTIVE_VAULT,
        receipt["active_vault"]["git_mount_state"] == "present",
        not receipt["active_vault"]["git_write"],
        "present" not in receipt["active_vault"]["git_metadata_state"].values(),
        not receipt["active_vault"]["repository_visible"],
        "present" not in receipt["forbidden_host_path_state"].values(),
        "present" not in receipt["active_vault"]["forbidden_sibling_state"].values(),
        "present" not in receipt["credential_path_state"].values(),
        not any(receipt["tool_presence"].values()),
        not receipt["git_credentials"]["global_helper_configured"],
        not receipt["git_credentials"]["system_helper_configured"],
        not receipt["git_credentials"]["active_vault_helper_configured"],
        not sensitive_env_names,
        not receipt["docker_control_tcp"]["port_2375_reachable"],
        not receipt["docker_control_tcp"]["port_2376_reachable"],
    ]
    receipt["passed"] = all(checks)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
