#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import secrets
import subprocess
import sys
from typing import Any


CONTAINER = "chatgpt-toolbox"
IMAGE = "local/chatgpt-toolbox:0.1.0"
EXPECTED_NETWORK = "chatgpt-toolbox-net"
ACTIVE_VAULT_HOST_PATH = "/data/scratch/obs-test/runtime/git/obs-vault-active"
ACTIVE_VAULT_CONTAINER_PATH = "/workspace/runtime/git/obs-vault-active"
ACTIVE_VAULT_GIT_PATH = f"{ACTIVE_VAULT_CONTAINER_PATH}/.git"
EXPECTED_MOUNTS = {
    "/session": ("volume", "chatgpt-toolbox-session", True),
    ACTIVE_VAULT_CONTAINER_PATH: ("bind", ACTIVE_VAULT_HOST_PATH, True),
}
EXPECTED_ENV_NAMES = {
    "DESKTOP_COMMANDER_DISABLE_TELEMETRY",
    "HOME",
    "MCP_SERVER_URL",
    "NPM_CONFIG_CACHE",
    "TOOLBOX_ACTIVE_VAULT",
    "TOOLBOX_SESSION",
    "TOOLBOX_WORKSPACE",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
}
SOURCE_FILES = [
    ".dockerignore",
    "DEPENDENCY-ADVISORY.md",
    "Dockerfile",
    "compose.yaml",
    "package.json",
    "package-lock.json",
    "bin/entrypoint.sh",
    "bin/host_pairing_broker.py",
    "bin/self_test.py",
    "bin/boundary_probe.py",
    "tests/probe_mcp_stdio.py",
    "tests/prove_active_vault.py",
    "tests/prove_runtime.py",
    "tests/verify_golden.py",
]


def run(
    args: list[str],
    *,
    check: bool = True,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def compose(source_dir: pathlib.Path, *args: str, check: bool = True, timeout: int = 300):
    return run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(source_dir),
            "-f",
            str(source_dir / "compose.yaml"),
            *args,
        ],
        check=check,
        timeout=timeout,
    )


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_json_command(args: list[str], *, timeout: int = 120) -> Any:
    result = run(args, timeout=timeout)
    return json.loads(result.stdout)


def active_vault_proof(source_dir: pathlib.Path, output: pathlib.Path) -> dict[str, Any]:
    result = run(
        [
            sys.executable,
            str(source_dir / "tests/prove_active_vault.py"),
            "--host-active-vault",
            ACTIVE_VAULT_HOST_PATH,
            "--output",
            str(output),
            "--container",
            CONTAINER,
        ],
        timeout=120,
    )
    return json.loads(output.read_text(encoding="utf-8"))


def existing_container() -> bool:
    result = run(
        ["docker", "container", "inspect", CONTAINER],
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def validate_source(source_dir: pathlib.Path, config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = [name for name in SOURCE_FILES if not (source_dir / name).is_file()]
    if missing:
        errors.append(f"missing source files: {missing}")

    dockerfile = (source_dir / "Dockerfile").read_text(encoding="utf-8")
    if "node:22-bookworm-slim@sha256:4d676821dff059fd00d277ee4261ef34ea712317fed0737c03941481b5760c96" not in dockerfile:
        errors.append("base image is not pinned to the resolved amd64 digest")
    if "@wonderwhy-er/desktop-commander" not in (source_dir / "package-lock.json").read_text(encoding="utf-8"):
        errors.append("Desktop Commander is absent from package lock")
    advisory = (source_dir / "DEPENDENCY-ADVISORY.md").read_text(encoding="utf-8")
    if (
        "## Materiality" not in advisory
        or "## Candidate-specific disposition" not in advisory
        or "0.2.23" not in advisory
        or "write_file" not in advisory
    ):
        errors.append("dependency advisory disposition is incomplete")

    service = config.get("services", {}).get("toolbox", {})
    if service.get("user") != "10001:1000":
        errors.append("Compose service user is not 10001:1000")
    if service.get("read_only") is not True:
        errors.append("read_only is not true")
    if service.get("privileged") is True:
        errors.append("privileged is true")
    if set(service.get("cap_drop", [])) != {"ALL"}:
        errors.append("cap_drop is not exactly ALL")
    if "no-new-privileges:true" not in service.get("security_opt", []):
        errors.append("no-new-privileges is absent")
    if service.get("network_mode") == "host":
        errors.append("host networking is configured")
    if service.get("ports") or service.get("expose"):
        errors.append("ports or expose are configured")
    if service.get("pids_limit") != 128:
        errors.append("pids_limit is not 128")
    if int(service.get("mem_limit", 0)) != 1073741824:
        errors.append("mem_limit is not 1 GiB")
    if int(service.get("memswap_limit", 0)) != 1073741824:
        errors.append("memswap_limit is not 1 GiB")
    if service.get("cpus") != 1:
        errors.append("cpus is not 1.0")

    environment = service.get("environment", {})
    if set(environment) != EXPECTED_ENV_NAMES:
        errors.append(f"unexpected Compose environment names: {sorted(set(environment) ^ EXPECTED_ENV_NAMES)}")
    if environment.get("MCP_SERVER_URL") != "https://mcp.desktopcommander.app":
        errors.append("Desktop Commander device server base is not the evidenced endpoint")
    if environment.get("TOOLBOX_ACTIVE_VAULT") != ACTIVE_VAULT_CONTAINER_PATH:
        errors.append("active vault container path is not exact")
    if environment.get("TOOLBOX_WORKSPACE") != ACTIVE_VAULT_CONTAINER_PATH:
        errors.append("toolbox workspace is not the active vault")
    labels = service.get("labels", {})
    if labels.get("io.obs.chatgpt-toolbox.connector.endpoint") != "https://mcp.desktopcommander.app/mcp":
        errors.append("ChatGPT connector endpoint label is not the evidenced endpoint")
    if labels.get("io.obs.chatgpt-toolbox.remote.session-persistence") != "disabled":
        errors.append("remote session persistence is not explicitly disabled")

    mounts = service.get("volumes", [])
    parsed_mounts = {
        mount.get("target"): (
            mount.get("type"),
            mount.get("source"),
            not mount.get("read_only", False),
        )
        for mount in mounts
    }
    expected_mounts = {
        "/session": ("volume", "session", True),
        ACTIVE_VAULT_CONTAINER_PATH: ("bind", ACTIVE_VAULT_HOST_PATH, True),
    }
    if parsed_mounts != expected_mounts:
        errors.append(f"unexpected Compose mounts: {parsed_mounts}")
    tmpfs = service.get("tmpfs", [])
    git_tmpfs = [item for item in tmpfs if item.startswith(f"{ACTIVE_VAULT_GIT_PATH}:")]
    if len(git_tmpfs) != 1 or "mode=0555" not in git_tmpfs[0]:
        errors.append("active-vault .git is not hidden by a non-writable tmpfs")

    if set(service.get("networks", {})) != {"egress"}:
        errors.append("service network is not exactly the dedicated egress bridge")

    network = config.get("networks", {}).get("egress", {})
    if network.get("name") != EXPECTED_NETWORK or network.get("driver") != "bridge":
        errors.append("dedicated bridge name or driver is wrong")

    logging = service.get("logging", {})
    if logging.get("driver") != "none":
        errors.append("logging driver must be none so pairing codes are not retained by Docker")

    return errors


def inspect_runtime() -> tuple[dict[str, Any], dict[str, Any]]:
    container = parse_json_command(["docker", "container", "inspect", CONTAINER])[0]
    image = parse_json_command(["docker", "image", "inspect", IMAGE])[0]
    return container, image


def summarize_runtime(container: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
    host = container["HostConfig"]
    config = container["Config"]
    network_settings = container["NetworkSettings"]
    mounts = [
        {
            "type": mount["Type"],
            "name": mount.get("Name"),
            "source": mount.get("Source"),
            "destination": mount["Destination"],
            "rw": mount["RW"],
        }
        for mount in container.get("Mounts", [])
    ]
    ulimits = {
        item["Name"]: {"soft": item["Soft"], "hard": item["Hard"]}
        for item in host.get("Ulimits") or []
    }
    return {
        "running": container["State"]["Running"],
        "user": config.get("User"),
        "readonly_rootfs": host.get("ReadonlyRootfs"),
        "privileged": host.get("Privileged"),
        "cap_drop": host.get("CapDrop") or [],
        "security_opt": host.get("SecurityOpt") or [],
        "network_mode": host.get("NetworkMode"),
        "attached_networks": sorted(network_settings.get("Networks", {})),
        "published_ports": network_settings.get("Ports") or {},
        "port_bindings": host.get("PortBindings") or {},
        "mounts": sorted(mounts, key=lambda item: item["destination"]),
        "hostconfig_bind_entries": host.get("Binds") or [],
        "tmpfs": host.get("Tmpfs") or {},
        "bind_mounts": [item for item in mounts if item["type"] == "bind"],
        "devices": host.get("Devices") or [],
        "device_requests": host.get("DeviceRequests") or [],
        "extra_hosts": host.get("ExtraHosts") or [],
        "memory_bytes": host.get("Memory"),
        "memory_swap_bytes": host.get("MemorySwap"),
        "nano_cpus": host.get("NanoCpus"),
        "pids_limit": host.get("PidsLimit"),
        "ulimits": ulimits,
        "environment_names": sorted(item.split("=", 1)[0] for item in config.get("Env", [])),
        "image_id": image.get("Id"),
        "image_labels": {
            key: value
            for key, value in (image.get("Config", {}).get("Labels") or {}).items()
            if key.startswith("io.obs.chatgpt-toolbox")
            or key in {
                "org.opencontainers.image.base.digest",
                "org.opencontainers.image.version",
            }
        },
    }


def validate_runtime(
    summary: dict[str, Any],
    boundary: dict[str, Any],
    self_test: dict[str, Any],
    active_vault_proof: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    checks = {
        "container running": summary["running"] is True,
        "non-root shared-vault identity": (
            summary["user"] == "10001:1000"
            and self_test["uid"] == 10001
            and self_test["gid"] == 1000
        ),
        "read-only root": summary["readonly_rootfs"] is True,
        "not privileged": summary["privileged"] is False,
        "cap_drop ALL": set(summary["cap_drop"]) == {"ALL"},
        "no-new-privileges": "no-new-privileges:true" in summary["security_opt"],
        "dedicated network": summary["attached_networks"] == [EXPECTED_NETWORK],
        "not host network": summary["network_mode"] != "host",
        "no published ports": not summary["published_ports"] and not summary["port_bindings"],
        "only active-vault bind mount": summary["bind_mounts"] == [
            {
                "type": "bind",
                "name": None,
                "source": ACTIVE_VAULT_HOST_PATH,
                "destination": ACTIVE_VAULT_CONTAINER_PATH,
                "rw": True,
            }
        ],
        "active-vault git tmpfs": (
            ACTIVE_VAULT_GIT_PATH in summary["tmpfs"]
            and "mode=0555" in summary["tmpfs"][ACTIVE_VAULT_GIT_PATH]
        ),
        "no devices": not summary["devices"] and not summary["device_requests"],
        "no extra hosts": not summary["extra_hosts"],
        "memory bounded": summary["memory_bytes"] == 1073741824,
        "swap bounded": summary["memory_swap_bytes"] == 1073741824,
        "cpu bounded": summary["nano_cpus"] == 1_000_000_000,
        "pids bounded": summary["pids_limit"] == 128,
        "nofile bounded": summary["ulimits"].get("nofile") == {"soft": 1024, "hard": 2048},
        "nproc bounded": summary["ulimits"].get("nproc") == {"soft": 128, "hard": 128},
        "exact workspace/session/active-vault mounts": {
            item["destination"]: (
                item["type"],
                item["source"] if item["type"] == "bind" else item["name"],
                item["rw"],
            )
            for item in summary["mounts"]
        }
        == EXPECTED_MOUNTS,
        "boundary probe": boundary.get("passed") is True,
        "workspace positive": self_test.get("workspace_round_trip") is True,
        "active vault positive": (
            self_test.get("active_vault", {}).get("path") == ACTIVE_VAULT_CONTAINER_PATH
            and self_test.get("active_vault", {}).get("round_trip") is True
            and self_test.get("active_vault", {}).get("cleanup") is True
        ),
        "active vault host/container proof": active_vault_proof.get("passed") is True,
        "runtime HTTPS": self_test.get("runtime_https", {}).get("status") == 200,
        "Desktop Commander version": self_test.get("tools", {}).get("desktop_commander") == "0.2.50",
    }
    for name, passed in checks.items():
        if not passed:
            errors.append(name)
    return errors


def image_credential_scan() -> dict[str, bool]:
    paths = [
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/root/.docker/config.json",
        "/root/.ssh",
        "/root/.git-credentials",
        "/root/.config/git/credentials",
        "/root/.config/gh",
        "/home/toolbox/.ssh",
        "/home/toolbox/.git-credentials",
        "/home/toolbox/.config/gh",
    ]
    program = (
        "import json,os; "
        f"paths={paths!r}; "
        "print(json.dumps({p: os.path.lexists(p) for p in paths}, sort_keys=True))"
    )
    result = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python3",
            IMAGE,
            "-c",
            program,
        ],
        timeout=60,
    )
    return json.loads(result.stdout)


def npm_audit_counts() -> dict[str, Any]:
    result = run(
        [
            "docker",
            "exec",
            CONTAINER,
            "npm",
            "audit",
            "--omit=dev",
            "--package-lock-only",
            "--json",
            "--prefix",
            "/opt/toolbox",
        ],
        check=False,
        timeout=180,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"available": False, "exit_code": result.returncode}
    return {
        "available": True,
        "exit_code": result.returncode,
        "vulnerability_counts": payload.get("metadata", {}).get("vulnerabilities", {}),
        "dependency_counts": payload.get("metadata", {}).get("dependencies", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--negative-output", type=pathlib.Path)
    parser.add_argument(
        "--canonical-source-dir",
        default="",
        help="canonical repository path when the Docker control plane sees the same bind mount under another prefix",
    )
    parser.add_argument(
        "--allow-recreate",
        action="store_true",
        help="permit recreation of an existing mission-owned toolbox container",
    )
    args = parser.parse_args()
    source_dir = args.source_dir.resolve()

    config = json.loads(compose(source_dir, "config", "--format", "json").stdout)
    source_errors = validate_source(source_dir, config)
    if source_errors:
        raise SystemExit("source validation failed: " + "; ".join(source_errors))

    existed = existing_container()
    if existed and not args.allow_recreate:
        raise SystemExit(
            "chatgpt-toolbox already exists; rerun without recreation after inspection, "
            "or pass --allow-recreate only for the mission-owned container"
        )

    compose(source_dir, "build", "--pull", timeout=900)
    compose(
        source_dir,
        "up",
        "-d",
        "--no-build",
        *( ["--force-recreate"] if existed else [] ),
        timeout=180,
    )

    nonce = f"b1-{secrets.token_hex(12)}"
    self_test = json.loads(
        run(
            [
                "docker",
                "exec",
                CONTAINER,
                "/usr/local/bin/toolbox-entrypoint",
                "self-test",
                "--nonce",
                nonce,
                "--network",
            ],
            timeout=120,
        ).stdout
    )
    remote_help = run(
        [
            "docker",
            "exec",
            CONTAINER,
            "/usr/local/bin/toolbox-entrypoint",
            "remote",
            "--help",
        ],
        timeout=120,
    ).stdout
    boundary = json.loads(
        run(
            [
                "docker",
                "exec",
                CONTAINER,
                "/usr/local/bin/toolbox-entrypoint",
                "boundary-probe",
            ],
            timeout=120,
        ).stdout
    )
    mcp_stdio = json.loads(
        run(
            [
                sys.executable,
                str(source_dir / "tests/probe_mcp_stdio.py"),
                "--container",
                CONTAINER,
            ],
            timeout=120,
        ).stdout
    )
    active_vault_proof_output = args.output.with_name("active-vault-proof.json")
    active_vault = active_vault_proof(source_dir, active_vault_proof_output)
    container, image = inspect_runtime()
    runtime_summary = summarize_runtime(container, image)
    image_credentials = image_credential_scan()
    runtime_errors = validate_runtime(runtime_summary, boundary, self_test, active_vault)
    if any(image_credentials.values()):
        runtime_errors.append("credential/control paths are present in the immutable image")
    if not mcp_stdio.get("all_required_tools_present"):
        runtime_errors.append("Desktop Commander MCP stdio proof lacks required tools")
    if "--no-persist-session" not in remote_help:
        runtime_errors.append("pinned package remote help lacks --no-persist-session")

    compose(source_dir, "restart", "toolbox", timeout=180)
    remote_clean = run(
        ["docker", "exec", CONTAINER, "/usr/local/bin/toolbox-entrypoint", "remote-clean"],
        timeout=30,
    )
    recovery_nonce = f"recovery-{secrets.token_hex(12)}"
    recovery_self_test = json.loads(
        run(
            [
                "docker",
                "exec",
                CONTAINER,
                "/usr/local/bin/toolbox-entrypoint",
                "self-test",
                "--nonce",
                recovery_nonce,
                "--network",
            ],
            timeout=120,
        ).stdout
    )
    recovery_boundary = json.loads(
        run(
            [
                "docker",
                "exec",
                CONTAINER,
                "/usr/local/bin/toolbox-entrypoint",
                "boundary-probe",
            ],
            timeout=120,
        ).stdout
    )
    recovery_active_vault_output = args.output.with_name("active-vault-recovery-proof.json")
    recovery_active_vault = active_vault_proof(source_dir, recovery_active_vault_output)
    recovery_container, recovery_image = inspect_runtime()
    recovery_summary = summarize_runtime(recovery_container, recovery_image)
    recovery_errors = validate_runtime(
        recovery_summary,
        recovery_boundary,
        recovery_self_test,
        recovery_active_vault,
    )
    if recovery_errors:
        runtime_errors.extend(f"recovery: {error}" for error in recovery_errors)

    receipt = {
        "schema": "chatgpt-toolbox-runtime-proof-v1",
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_dir": args.canonical_source_dir or str(source_dir),
        "control_plane_source_dir": str(source_dir),
        "source_path_relationship": (
            "same bind-mounted repository files; not a build copy"
            if args.canonical_source_dir
            else "single path"
        ),
        "source_sha256": {
            name: sha256(source_dir / name)
            for name in SOURCE_FILES
        },
        "source_validation": {
            "passed": not source_errors,
            "errors": source_errors,
        },
        "container_existed_before": existed,
        "self_test": self_test,
        "active_vault_proof": active_vault,
        "boundary_probe": boundary,
        "immutable_image_credential_path_presence": image_credentials,
        "runtime_inspection": runtime_summary,
        "desktop_commander_mcp_stdio": mcp_stdio,
        "desktop_commander_remote_help": {
            "version": self_test["tools"]["desktop_commander"],
            "supports_no_persist_session": "--no-persist-session" in remote_help,
            "pairing_started": False,
        },
        "npm_audit": npm_audit_counts(),
        "account_side_github_connector_authorization": {
            "status": "not-checked-by-lane-b",
            "required_evidence_owner": "Lane A authenticated web and integration owner",
        },
        "connector_transport": {
            "status": "evidenced-desktop-commander-remote-mcp-adapter-ready",
            "device_server_base": "https://mcp.desktopcommander.app",
            "chatgpt_connector_endpoint": "https://mcp.desktopcommander.app/mcp",
            "session_persistence": "disabled",
            "adapter_active": False,
        },
        "runtime_validation": {
            "passed": not runtime_errors,
            "errors": runtime_errors,
        },
        "recovery": {
            "compose_restart": "passed",
            "remote_clean_exit_code": remote_clean.returncode,
            "self_test": recovery_self_test,
            "active_vault_proof": recovery_active_vault,
            "boundary_probe": recovery_boundary,
            "runtime_inspection": recovery_summary,
            "passed": not recovery_errors,
            "errors": recovery_errors,
        },
        "passed": not source_errors and not runtime_errors,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.negative_output:
        negative_receipt = {
            "schema": "chatgpt-toolbox-negative-isolation-proof-v1",
            "observed_at": receipt["observed_at"],
            "canonical_source_dir": receipt["source_dir"],
            "runtime_proof_path": str(args.output),
            "active_vault_proof": receipt["active_vault_proof"],
            "boundary_probe": receipt["boundary_probe"],
            "immutable_image_credential_path_presence": receipt[
                "immutable_image_credential_path_presence"
            ],
            "runtime_inspection": receipt["runtime_inspection"],
            "github_boundary": {
                "container_authenticated_github_material": "absent",
                "account_side_state": "outside-lane-b; consume Lane A evidence separately",
            },
            "application_allowlists_used_as_boundary_proof": False,
            "passed": receipt["passed"],
        }
        args.negative_output.parent.mkdir(parents=True, exist_ok=True)
        args.negative_output.write_text(
            json.dumps(negative_receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "passed": receipt["passed"],
        "output": str(args.output),
        "negative_output": str(args.negative_output) if args.negative_output else None,
        "container": CONTAINER,
        "image": IMAGE,
        "nonce": nonce,
        "runtime_errors": runtime_errors,
        "npm_vulnerabilities": receipt["npm_audit"].get("vulnerability_counts"),
    }, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
