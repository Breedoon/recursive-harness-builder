#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys


CONTAINER = "chatgpt-toolbox"
NONCE_PATTERN = re.compile(r"^CHATGPT-WEB-GOLDEN-[0-9]{8}T[0-9]{6}Z-[0-9A-F]{8}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_file(container: str, path: str) -> dict:
    program = """
import hashlib, json, os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
data = path.read_bytes()
metadata = path.lstat()
print(json.dumps({
    "actual_path": str(path),
    "real_path": str(path.resolve()),
    "is_regular_file": stat.S_ISREG(metadata.st_mode),
    "is_symlink": stat.S_ISLNK(metadata.st_mode),
    "size_bytes": len(data),
    "sha256": hashlib.sha256(data).hexdigest(),
    "uid": metadata.st_uid,
    "gid": metadata.st_gid,
    "mode_octal": oct(stat.S_IMODE(metadata.st_mode)),
    "process_uid": os.getuid(),
    "process_gid": os.getgid(),
    "pwd": os.getcwd(),
}, sort_keys=True))
"""
    result = subprocess.run(
        ["docker", "exec", container, "python3", "-c", program, path],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-nonce", required=True)
    parser.add_argument("--device-identity-sha256", required=True)
    parser.add_argument("--source-runtime-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--negative-isolation-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--container", default=CONTAINER)
    args = parser.parse_args()

    if not NONCE_PATTERN.fullmatch(args.base_nonce):
        raise SystemExit("base nonce does not match the golden contract")
    if not SHA256_PATTERN.fullmatch(args.device_identity_sha256):
        raise SystemExit("device identity must be a lowercase SHA-256 value")

    stage_nonce = args.base_nonce + "-P1"
    expected_path = f"/workspace/golden/{stage_nonce}.txt"
    expected_bytes = (stage_nonce + "\n").encode("utf-8")
    expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()

    source_runtime = json.loads(args.source_runtime_receipt.read_text(encoding="utf-8"))
    negative_isolation = json.loads(
        args.negative_isolation_receipt.read_text(encoding="utf-8")
    )
    observed = inspect_file(args.container, expected_path)

    checks = {
        "source_runtime_passed": source_runtime.get("passed") is True,
        "negative_isolation_passed": negative_isolation.get("passed") is True,
        "path_exact": observed.get("actual_path") == expected_path,
        "real_path_contained": observed.get("real_path") == expected_path,
        "regular_not_symlink": (
            observed.get("is_regular_file") is True
            and observed.get("is_symlink") is False
        ),
        "size_exact": observed.get("size_bytes") == len(expected_bytes),
        "hash_exact": observed.get("sha256") == expected_sha256,
        "file_owned_by_toolbox": (
            observed.get("uid") == 10001 and observed.get("gid") == 10001
        ),
        "inspection_non_root": (
            observed.get("process_uid") == 10001
            and observed.get("process_gid") == 10001
        ),
        "pwd_exact": observed.get("pwd") == "/workspace",
    }

    receipt = {
        "schema": "chatgpt-toolbox-golden-execution-proof-v1",
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "container": args.container,
        "base_nonce": args.base_nonce,
        "stage_nonce": stage_nonce,
        "nonce_sha256": hashlib.sha256(args.base_nonce.encode("utf-8")).hexdigest(),
        "expected_path": expected_path,
        "expected_content_sha256": expected_sha256,
        "observed_file": observed,
        "device_identity_sha256": args.device_identity_sha256,
        "source_runtime_receipt": {
            "path": str(args.source_runtime_receipt),
            "sha256": sha256_file(args.source_runtime_receipt),
        },
        "negative_isolation_receipt": {
            "path": str(args.negative_isolation_receipt),
            "sha256": sha256_file(args.negative_isolation_receipt),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "passed": receipt["passed"],
        "output": str(args.output),
        "file_sha256": observed.get("sha256"),
        "expected_sha256": expected_sha256,
        "uid": observed.get("process_uid"),
        "pwd": observed.get("pwd"),
    }, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
