"""Two-stage entry point for the dedicated OBS formal-test protocol.

``host-preflight`` runs before service creation against an independently produced
host observation. ``inner`` runs inside ``obs-live-test`` and refuses all later
factories unless runtime observation matches that host attestation. This
provisional candidate implements only ``--dry-run`` decisions.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

CANONICAL_INNER_RUNNER = Path(
    "/source/snapshot/scripts/isolated_test_runner.py"
)
if Path(__file__).resolve(strict=True) == CANONICAL_INNER_RUNNER:
    sys.path.insert(0, str(CANONICAL_INNER_RUNNER.parents[1]))

from scripts.live_test_protocol import (
    CONTAINER_ATTESTATION_FILE,
    CONTAINER_RUN_ROOT,
    CONTAINER_SECRET_FILE,
    CONTAINER_SOURCE_ROOT,
    ContainerRuntimeObservationAdapter,
    EXPECTED_LIVE_MODEL,
    EXPECTED_SERVICE_NAME,
    HOST_OBSERVATION_FILE,
    HostAttestation,
    PreflightError,
    StaticHostObservationAdapter,
    build_evidence_manifest,
    host_preflight,
    inner_preflight,
    load_host_attestation,
    redact_text,
    write_evidence_manifest,
)

from scripts.mount_inventory import MountInventoryError, validated_application_mounts

CANONICAL_COMMAND = (
    "python -m scripts.isolated_test_runner --phase host-preflight "
    "--host-observation /workspace/runtime/obs-live-test-host-observation.json "
    "--compose-file deploy/obs-live-test/compose.yaml --lane unit "
    "--scenario focused --dry-run"
)
INNER_COMMAND = (
    "python -I /source/snapshot/scripts/isolated_test_runner.py --phase inner "
    "--attestation /run/obs-live-test-attestation/host-attestation.json "
    "--lane unit --scenario focused --dry-run"
)


class CompleteMountObservationAdapter(ContainerRuntimeObservationAdapter):
    """Validate every mount before adapting to the protocol's four-mount schema.

    The inherited observer still measures source, image, executable, secret,
    ownership, and environment facts. Only its lossy mount-selection step is
    replaced. Validation and selection use the same procfs read.
    """

    def observe(self, attestation: HostAttestation) -> Mapping[str, Any]:
        # Reject nested source mounts before the inherited observer hashes or
        # reads the source tree. Its later _mount_records call validates again,
        # rather than trusting a cached table across the filesystem reads.
        self._mount_records()
        return super().observe(attestation)

    def _mount_records(self) -> list[dict[str, Any]]:
        targets = {
            str(CONTAINER_SOURCE_ROOT): "source",
            str(CONTAINER_RUN_ROOT): "run",
            str(CONTAINER_SECRET_FILE): "secret",
            str(CONTAINER_ATTESTATION_FILE): "attestation",
        }
        try:
            records = validated_application_mounts(
                self.proc_mountinfo.read_text(encoding="utf-8"),
                application_targets=targets,
            )
        except (MountInventoryError, UnicodeError) as exc:
            raise PreflightError(
                "inner.mount_inventory", "complete mount inventory is invalid or outside policy"
            ) from exc
        return [
            {
                "name": targets[record.target],
                "target": record.target,
                "mode": record.mode,
                "symlink": Path(record.target).is_symlink(),
            }
            for record in records
        ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate OBS formal testing through host preflight and an independent "
            "inner preflight; never use obs-test or profile-only isolation."
        )
    )
    parser.add_argument(
        "--phase", choices=("host-preflight", "inner"), required=True
    )
    parser.add_argument("--host-observation", type=Path)
    parser.add_argument(
        "--attestation", type=Path, default=CONTAINER_ATTESTATION_FILE
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path("deploy/obs-live-test/compose.yaml"),
    )
    parser.add_argument(
        "--lane",
        choices=("unit", "integration", "restart", "stress", "live"),
        default="unit",
    )
    parser.add_argument("--scenario", default="focused")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _read_json_object(path: Path, code: str) -> Mapping[str, Any]:
    if path.resolve(strict=False) != HOST_OBSERVATION_FILE:
        raise PreflightError(
            code,
            "host observation must use the canonical supervisor-owned path",
        )
    if not path.is_file() or path.is_symlink():
        raise PreflightError(
            code, "input must be an existing non-symlink regular file"
        )
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise PreflightError(
            code,
            "host observation must be root-owned and not group/world writable",
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise PreflightError(code, "input JSON must be an object")
    return loaded


def _host_dry_run(args: argparse.Namespace) -> dict[str, Any]:
    if args.host_observation is None:
        raise PreflightError(
            "host.observation_path",
            "--host-observation is required for host-preflight",
        )
    if args.lane == "live":
        raise PreflightError(
            "host.live_lane",
            "live execution is outside this provisional dry-run authorization",
        )
    compose_file = args.compose_file.resolve(strict=False)
    if compose_file != (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "obs-live-test"
        / "compose.yaml"
    ).resolve(strict=False):
        raise PreflightError(
            "host.compose_file",
            "only deploy/obs-live-test/compose.yaml is approved",
        )
    observation = _read_json_object(
        args.host_observation, "host.observation_file"
    )
    attestation = host_preflight(StaticHostObservationAdapter(observation))
    return {
        "status": "host_preflight_allowed_service_not_created",
        "service": EXPECTED_SERVICE_NAME,
        "phase": "host-preflight",
        "lane": args.lane,
        "scenario": args.scenario,
        "attestation": attestation.as_dict(),
        "attestation_output": attestation.payload["attestation_host_path"],
        "compose_file": str(compose_file),
        "next_command": INNER_COMMAND,
        "execution": "not run; no service was created",
    }


def _inner_dry_run(args: argparse.Namespace) -> dict[str, Any]:
    if args.attestation.resolve(strict=False) != CONTAINER_ATTESTATION_FILE:
        raise PreflightError(
            "inner.attestation_path",
            "inner preflight accepts only the canonical read-only attestation mount",
        )
    attestation: HostAttestation = load_host_attestation(args.attestation)
    decision = inner_preflight(
        attestation, CompleteMountObservationAdapter(os.environ)
    )
    command = (
        "python",
        "-I",
        str(decision.executable_path),
        "--phase",
        "inner",
        "--attestation",
        str(CONTAINER_ATTESTATION_FILE),
        "--lane",
        args.lane,
        "--scenario",
        args.scenario,
        "--dry-run",
    )
    manifest = build_evidence_manifest(
        decision=decision,
        lane=args.lane,
        scenario=args.scenario,
        command=command,
    )
    evidence_path = write_evidence_manifest(decision, manifest)
    return {
        "status": "inner_preflight_allowed_factories_not_started",
        "service": EXPECTED_SERVICE_NAME,
        "model": EXPECTED_LIVE_MODEL,
        "phase": "inner",
        "lane": args.lane,
        "scenario": args.scenario,
        "evidence": str(evidence_path),
        "execution": "not run; no client, daemon, network, or poller factory was called",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error(
            "refused: this provisional candidate authorizes only the canonical --dry-run"
        )
    try:
        result = (
            _host_dry_run(args)
            if args.phase == "host-preflight"
            else _inner_dry_run(args)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(redact_text(str(exc)))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
