#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import secrets
import subprocess


CONTAINER = "chatgpt-toolbox"
CONTAINER_ACTIVE_VAULT = "/workspace/runtime/git/obs-vault-active"


def run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-active-vault", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--container", default=CONTAINER)
    args = parser.parse_args()

    host_active_vault = args.host_active_vault.resolve()
    if not host_active_vault.is_dir():
        raise SystemExit("host active vault is unavailable")

    nonce = secrets.token_hex(16)
    filename = f".obs-chatgpt-toolbox-active-vault-probe-{nonce}"
    payload = f"obs-chatgpt-toolbox-active-vault-proof:{nonce}\n"
    expected_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    host_probe = host_active_vault / filename
    if host_probe.exists():
        raise SystemExit("active-vault probe path already exists")

    program = """
import hashlib, json, os, pathlib, stat, sys
root = pathlib.Path(os.environ.get('TOOLBOX_ACTIVE_VAULT', ''))
expected = pathlib.Path('/workspace/runtime/git/obs-vault-active')
name, payload = sys.argv[1:]
if root != expected:
    raise SystemExit('unexpected active vault environment path')
if not root.is_dir() or root.resolve() != expected:
    raise SystemExit('active vault path is unavailable or redirected')
target = root / name
target.write_text(payload, encoding='utf-8')
os.chmod(target, 0o660)
stat_result = target.stat()
print(json.dumps({
    'container_active_vault': str(root),
    'probe_path': str(target),
    'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
    'uid': stat_result.st_uid,
    'gid': stat_result.st_gid,
    'mode_octal': oct(stat.S_IMODE(stat_result.st_mode)),
}, sort_keys=True))
"""
    try:
        container_result = json.loads(
            run(
                [
                    "docker",
                    "exec",
                    args.container,
                    "python3",
                    "-c",
                    program,
                    filename,
                    payload,
                ]
            ).stdout
        )
        host_observed = {
            "exists_while_container_write": host_probe.exists(),
            "sha256": hashlib.sha256(host_probe.read_bytes()).hexdigest()
            if host_probe.exists()
            else None,
            "uid": host_probe.stat().st_uid if host_probe.exists() else None,
            "gid": host_probe.stat().st_gid if host_probe.exists() else None,
            "mode_octal": oct(host_probe.stat().st_mode & 0o777)
            if host_probe.exists()
            else None,
        }
        run(
            [
                "docker",
                "exec",
                args.container,
                "python3",
                "-c",
                "import os, pathlib, sys; root = pathlib.Path(os.environ['TOOLBOX_ACTIVE_VAULT']); target = root / sys.argv[1]; target.unlink()",
                filename,
            ]
        )
    finally:
        host_probe.unlink(missing_ok=True)

    receipt = {
        "schema": "chatgpt-toolbox-active-vault-proof-v1",
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "host_active_vault": str(host_active_vault),
        "container_active_vault": CONTAINER_ACTIVE_VAULT,
        "container_write": container_result,
        "host_observed": host_observed,
        "probe_cleanup": not host_probe.exists(),
        "checks": {
            "container_path_exact": container_result["container_active_vault"]
            == CONTAINER_ACTIVE_VAULT,
            "container_probe_path_exact": container_result["probe_path"]
            == f"{CONTAINER_ACTIVE_VAULT}/{filename}",
            "container_hash_exact": container_result["sha256"] == expected_sha256,
            "host_observed_write": host_observed["exists_while_container_write"] is True,
            "host_hash_exact": host_observed["sha256"] == expected_sha256,
            "container_non_root": container_result["uid"] == 10001,
            "shared_group": container_result["gid"] == 1000
            and host_observed["gid"] == 1000,
            "group_writable_probe": container_result["mode_octal"] == "0o660"
            and host_observed["mode_octal"] == "0o660",
            "container_cleanup": not host_probe.exists(),
        },
    }
    receipt["passed"] = all(receipt["checks"].values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": receipt["passed"], "output": str(args.output)}, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
