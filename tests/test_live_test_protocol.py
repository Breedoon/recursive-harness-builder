"""Authored adversarial coverage for the formal-test protocol; never run here."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import isolated_test_runner
from scripts import live_test_protocol as protocol

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
RUNTIME_UID = 1001
RUNTIME_GID = 1001
COMMIT = "d" * 40
RUN_ID = "run-20260812T120000Z-deadbeef"
NONCE = "e" * 32
NOW = datetime(2026, 8, 12, 12, 5, tzinfo=timezone.utc)
OBSERVED_AT = "2026-08-12T12:04:00Z"
ISSUED_AT = "2026-08-12T12:00:00Z"
EXPIRES_AT = "2026-08-12T12:10:00Z"
CREDENTIAL_EXPIRES_AT = "2026-08-13T12:00:00Z"


def topology_records(root: Path, run_id: str = RUN_ID) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "path": str(root / relative),
            "kind": kind,
            "mode": mode,
            "symlink": False,
            "owner_run_id": run_id,
            "owner_uid": RUNTIME_UID,
            "owner_gid": RUNTIME_GID,
        }
        for name, (relative, kind, mode) in protocol.TOPOLOGY_LAYOUT.items()
    ]


def inventory(**values: Any) -> dict[str, Any]:
    return {
        "provenance": "host-supervisor-snapshot",
        "observed_at": OBSERVED_AT,
        "complete": True,
        **values,
    }


@pytest.fixture
def host_record() -> dict[str, Any]:
    run_root = protocol.HOST_RUN_BASE / RUN_ID
    source = Path("/workspace/runtime/obs-live-test-snapshots") / RUN_ID
    secret = protocol.HOST_SECRET_BASE / RUN_ID / "test-credentials"
    attestation = (
        protocol.HOST_ATTESTATION_BASE / RUN_ID / "host-attestation.json"
    )
    return {
        "observer": {
            "kind": protocol.HOST_OBSERVER_KIND,
            "provenance": "host-supervisor-snapshot",
            "observed_at": OBSERVED_AT,
        },
        "run_id": RUN_ID,
        "nonce": NONCE,
        "issued_at": ISSUED_AT,
        "expires_at": EXPIRES_AT,
        "runtime_owner": {
            "uid": RUNTIME_UID,
            "gid": RUNTIME_GID,
            "provenance": "host-runtime-account-inspect",
        },
        "run_root": str(run_root),
        "attestation_host_path": str(attestation),
        "source": {
            "host_path": str(source),
            "target_path": str(protocol.CONTAINER_SOURCE_ROOT),
            "requested_commit": COMMIT,
            "observed_commit": COMMIT,
            "regular_directory": True,
            "symlink": False,
            "writable": False,
            "owner_run_id": RUN_ID,
            "provenance": "host-source-snapshot-inspect",
            "tree_digest": DIGEST_A,
            "executable_path": str(
                protocol.CONTAINER_SOURCE_ROOT
                / "scripts"
                / "isolated_test_runner.py"
            ),
            "executable_digest": DIGEST_B,
            "source_executable_digest": DIGEST_B,
        },
        "image": {
            "reference": "registry.invalid/obs-live-test@" + DIGEST_C,
            "digest": DIGEST_C,
            "container_marker": protocol.EXPECTED_CONTAINER_MARKER,
            "service_name": protocol.EXPECTED_SERVICE_NAME,
            "runtime_executable_path": "/usr/bin/python3.12",
            "runtime_executable_digest": DIGEST_B,
            "provenance": "host-image-runtime-inspect",
        },
        "topology": topology_records(run_root),
        "credential": {
            "credential_id": "test-credential-v2",
            "test_bot_id": "test-bot-v2",
            "poller_lease_id": "test-run-lease-v1",
            "credential_class": "test",
            "fingerprint": DIGEST_A,
            "measured_fingerprint": DIGEST_A,
            "provenance": "host-secret-file-sha256",
            "secret_path": str(secret),
            "regular_file": True,
            "symlink": False,
            "owner_uid": RUNTIME_UID,
            "owner_gid": RUNTIME_GID,
            "mode": "0400",
            "authentication_status": "valid",
            "authenticated_at": OBSERVED_AT,
            "expires_at": CREDENTIAL_EXPIRES_AT,
            "allowlist": ["test-credential-v2"],
        },
        "inventories": {
            "production_roots": inventory(
                roots=["/workspace/obs", "/srv/obs-production"]
            ),
            "shared_roots": inventory(roots=["/workspace/shared"]),
            "production_credentials": inventory(
                records=[
                    {
                        "credential_id": "production-credential-v1",
                        "fingerprint": DIGEST_C,
                    }
                ]
            ),
            "production_pollers": inventory(
                lease_epoch="poller-epoch-v1",
                records=[
                    {
                        "bot_id": "production-bot-v1",
                        "owner_run_id": "production-runtime-v1",
                        "lease_id": "production-lease-v1",
                        "state": "active",
                        "observed_at": OBSERVED_AT,
                        "lease_expires_at": CREDENTIAL_EXPIRES_AT,
                    },
                    {
                        "bot_id": "test-bot-v2",
                        "owner_run_id": RUN_ID,
                        "lease_id": "test-run-lease-v1",
                        "state": "reserved",
                        "observed_at": OBSERVED_AT,
                        "lease_expires_at": CREDENTIAL_EXPIRES_AT,
                    },
                ],
            ),
            "production_ports": inventory(
                records=[{"port": 18923}, {"port": 7832}]
            ),
            "production_networks": inventory(
                namespace_ids=["net:[100]"],
                identities=["obs-production-network"],
            ),
        },
        "ports": [
            {
                "purpose": "cache",
                "port": 29001,
                "available": True,
                "owner_run_id": RUN_ID,
                "provenance": "host-socket-inventory",
                "observed_at": OBSERVED_AT,
                "lease_expires_at": EXPIRES_AT,
            },
            {
                "purpose": "daemon",
                "port": 29002,
                "available": True,
                "owner_run_id": RUN_ID,
                "provenance": "host-socket-inventory",
                "observed_at": OBSERVED_AT,
                "lease_expires_at": EXPIRES_AT,
            },
        ],
        "network": {
            "identity": f"obs-live-test-{RUN_ID}",
            "mode": "private",
            "owner_run_id": RUN_ID,
            "provenance": "host-network-namespace-inventory",
            "observed_at": OBSERVED_AT,
            "lease_expires_at": EXPIRES_AT,
        },
        "mounts": [
            {
                "name": "source",
                "source": str(source),
                "target": str(protocol.CONTAINER_SOURCE_ROOT),
                "mode": "ro",
                "observed": True,
                "owner_run_id": RUN_ID,
            },
            {
                "name": "run",
                "source": str(run_root),
                "target": str(protocol.CONTAINER_RUN_ROOT),
                "mode": "rw",
                "observed": True,
                "owner_run_id": RUN_ID,
            },
            {
                "name": "secret",
                "source": str(secret),
                "target": str(protocol.CONTAINER_SECRET_FILE),
                "mode": "ro",
                "observed": True,
                "owner_run_id": RUN_ID,
            },
            {
                "name": "attestation",
                "source": str(attestation),
                "target": str(protocol.CONTAINER_ATTESTATION_FILE),
                "mode": "ro",
                "observed": True,
                "owner_run_id": RUN_ID,
            },
        ],
    }


@pytest.fixture
def attestation(host_record: dict[str, Any]) -> protocol.HostAttestation:
    return protocol.host_preflight(
        protocol.StaticHostObservationAdapter(host_record), now=NOW
    )


@pytest.fixture
def inner_record(
    attestation: protocol.HostAttestation,
) -> dict[str, Any]:
    payload = attestation.payload
    return {
        "observer": {
            "kind": protocol.INNER_OBSERVER_KIND,
            "provenance": "container-procfs-and-filesystem",
            "observed_at": OBSERVED_AT,
        },
        "attestation_integrity": attestation.integrity,
        "run_id": RUN_ID,
        "nonce": NONCE,
        "container_marker": protocol.EXPECTED_CONTAINER_MARKER,
        "service_name": protocol.EXPECTED_SERVICE_NAME,
        "hostname": protocol.EXPECTED_SERVICE_NAME,
        "profile": "test",
        "production_mode": None,
        "process_uid": RUNTIME_UID,
        "process_gid": RUNTIME_GID,
        "source_path": str(protocol.CONTAINER_SOURCE_ROOT),
        "source_tree_digest": DIGEST_A,
        "executable_path": payload["source"]["executable_path"],
        "executable_digest": DIGEST_B,
        "image_digest": DIGEST_C,
        "runtime_executable_path": "/usr/bin/python3.12",
        "runtime_executable_digest": DIGEST_B,
        "python_executable_path": "/usr/bin/python3.12",
        "mounts": [
            {
                "name": item["name"],
                "target": item["target"],
                "mode": item["mode"],
                "symlink": False,
            }
            for item in payload["mounts"]
        ],
        "topology": topology_records(protocol.CONTAINER_RUN_ROOT),
        "runtime_environment": protocol._expected_runtime_environment(payload),
        "repository_env_present": False,
        "production_env_keys": [],
        "ambient_credential_keys": [],
        "secret_regular_file": True,
        "secret_symlink": False,
        "secret_mode": "0400",
        "secret_owner_uid": RUNTIME_UID,
        "secret_owner_gid": RUNTIME_GID,
        "secret_fingerprint": DIGEST_A,
        "network_identity": f"obs-live-test-{RUN_ID}",
        "network_namespace_id": "net:[200]",
        "network_mode": "private",
    }


def host_attestation(
    record: dict[str, Any], now: datetime = NOW
) -> protocol.HostAttestation:
    return protocol.host_preflight(
        protocol.StaticHostObservationAdapter(record), now=now
    )


def inner_decision(
    attestation: protocol.HostAttestation, record: dict[str, Any]
) -> protocol.PreflightDecision:
    return protocol.inner_preflight(
        attestation,
        protocol.StaticInnerObservationAdapter(record),
        now=NOW,
    )


def test_host_preflight_is_a_required_pre_service_stage(
    host_record: dict[str, Any],
) -> None:
    events: list[str] = []

    def host_observer() -> dict[str, Any]:
        events.append("host-observation")
        return host_record

    attested = protocol.host_preflight(
        protocol.StaticHostObservationAdapter(host_observer()), now=NOW
    )
    events.append("service-create-eligible")
    assert attested.integrity.startswith("sha256:")
    assert events == ["host-observation", "service-create-eligible"]


def test_self_attested_environment_booleans_are_not_an_api() -> None:
    with pytest.raises(protocol.PreflightError, match="self_attestation_removed"):
        protocol.LiveTestConfig.from_env(
            {"OBS_TEST_SOURCE_IDENTITY_VERIFIED": "true"}
        )


def test_host_preflight_rejects_every_independent_observation_boundary(
    host_record: dict[str, Any],
) -> None:
    cases: dict[str, dict[str, Any]] = {}
    for name in (
        "production_credentials",
        "production_pollers",
        "production_ports",
        "production_networks",
        "production_roots",
        "shared_roots",
    ):
        candidate = deepcopy(host_record)
        candidate["inventories"][name]["provenance"] = "environment"
        cases[f"inventory-{name}"] = candidate
    for label, mutate in {
        "bad-observer": lambda item: item["observer"].update(kind="caller"),
        "expired-attestation": lambda item: item.update(expires_at=ISSUED_AT),
        "source-production": lambda item: item["source"].update(
            host_path="/workspace/obs"
        ),
        "source-commit": lambda item: item["source"].update(
            observed_commit="f" * 40
        ),
        "source-digest": lambda item: item["source"].update(
            tree_digest="sha256:short"
        ),
        "source-symlink": lambda item: item["source"].update(symlink=True),
        "source-writable": lambda item: item["source"].update(writable=True),
        "source-owner": lambda item: item["source"].update(
            owner_run_id="other-run"
        ),
        "executed-code": lambda item: item["source"].update(
            source_executable_digest=DIGEST_C
        ),
        "image-unpinned": lambda item: item["image"].update(
            reference="registry.invalid/obs-live-test:latest"
        ),
        "image-marker": lambda item: item["image"].update(
            container_marker="obs-test-v1"
        ),
        "image-service": lambda item: item["image"].update(
            service_name="obs-test"
        ),
        "credential-measurement": lambda item: item["credential"].update(
            measured_fingerprint=DIGEST_B
        ),
        "credential-symlink": lambda item: item["credential"].update(
            symlink=True
        ),
        "credential-owner": lambda item: item["credential"].update(
            owner_uid=2002
        ),
        "credential-group": lambda item: item["credential"].update(
            owner_gid=2002
        ),
        "credential-mode": lambda item: item["credential"].update(
            mode="0644"
        ),
        "credential-auth-stale": lambda item: item["credential"].update(
            authenticated_at="2026-08-12T11:59:59Z"
        ),
        "credential-expired": lambda item: item["credential"].update(
            expires_at=ISSUED_AT
        ),
        "credential-expires-before-attestation": lambda item: item[
            "credential"
        ].update(expires_at="2026-08-12T12:09:59Z"),
        "credential-production-match": lambda item: item["inventories"][
            "production_credentials"
        ]["records"][0].update(fingerprint=DIGEST_A),
        "credential-inventory-empty": lambda item: item["inventories"][
            "production_credentials"
        ].update(records=[]),
        "poller-inventory-empty": lambda item: item["inventories"][
            "production_pollers"
        ].update(records=[]),
        "poller-owner": lambda item: item["inventories"][
            "production_pollers"
        ]["records"].append(
            {
                "bot_id": "test-bot-v2",
                "owner_run_id": "other-run",
                "lease_id": "other-lease",
                "state": "active",
                "observed_at": OBSERVED_AT,
                "lease_expires_at": CREDENTIAL_EXPIRES_AT,
            }
        ),
        "poller-missing-run-lease": lambda item: item["inventories"][
            "production_pollers"
        ].update(
            records=[
                record
                for record in item["inventories"]["production_pollers"]["records"]
                if record["bot_id"] != "test-bot-v2"
            ]
        ),
        "poller-wrong-credential-lease": lambda item: item[
            "credential"
        ].update(poller_lease_id="different-test-lease"),
        "poller-record-stale": lambda item: item["inventories"][
            "production_pollers"
        ]["records"][1].update(observed_at=ISSUED_AT),
        "poller-lease-too-short": lambda item: item["inventories"][
            "production_pollers"
        ]["records"][1].update(
            lease_expires_at="2026-08-12T12:09:59Z"
        ),
        "runtime-owner-root": lambda item: item["runtime_owner"].update(uid=0),
        "runtime-owner-boolean": lambda item: item["runtime_owner"].update(uid=True),
        "production-port-boolean": lambda item: item["inventories"][
            "production_ports"
        ]["records"][0].update(port=True),
        "selected-port-boolean": lambda item: item["ports"][0].update(port=True),
        "topology-owner": lambda item: item["topology"][0].update(
            owner_uid=2002
        ),
        "path-alias": lambda item: item["topology"][1].update(
            path=item["topology"][0]["path"]
        ),
        "path-ancestor": lambda item: item["topology"][7].update(
            path=item["topology"][6]["path"] + "/fixture"
        ),
        "shared-writable-mount": lambda item: item["mounts"].append(
            {
                "name": "extra",
                "source": "/workspace/shared",
                "target": "/shared",
                "mode": "rw",
                "observed": True,
                "owner_run_id": RUN_ID,
            }
        ),
        "production-port": lambda item: item["ports"][0].update(port=18923),
        "occupied-port": lambda item: item["ports"][0].update(
            available=False
        ),
        "port-lease-too-short": lambda item: item["ports"][0].update(
            lease_expires_at="2026-08-12T12:09:59Z"
        ),
        "shared-network": lambda item: item["network"].update(mode="host"),
        "network-unproven": lambda item: item["network"].update(
            provenance="environment"
        ),
        "network-stale": lambda item: item["network"].update(
            observed_at=ISSUED_AT
        ),
        "network-lease-too-short": lambda item: item["network"].update(
            lease_expires_at="2026-08-12T12:09:59Z"
        ),
    }.items():
        candidate = deepcopy(host_record)
        mutate(candidate)
        cases[label] = candidate
    for label, candidate in cases.items():
        try:
            host_attestation(candidate)
        except protocol.PreflightError:
            continue
        pytest.fail(f"mutation {label!r} did not raise PreflightError")


def test_create_run_root_rejects_boolean_owner_before_filesystem_mutation() -> None:
    with pytest.raises(protocol.PreflightError, match="setup.owner"):
        protocol.create_run_root(
            RUN_ID,
            NONCE,
            owner_uid=True,
            owner_gid=RUNTIME_GID,
        )


def test_host_attestation_retains_owner_and_full_window_reservations(
    attestation: protocol.HostAttestation,
) -> None:
    payload = attestation.payload
    assert payload["credential"]["secret_owner_uid"] == RUNTIME_UID
    assert payload["credential"]["secret_owner_gid"] == RUNTIME_GID
    assert payload["credential"]["poller_lease_id"] == "test-run-lease-v1"
    assert payload["poller_inventory"]["selected_bot_lease_id"] == "test-run-lease-v1"
    assert (
        payload["poller_inventory"]["selected_bot_lease_expires_at"]
        == CREDENTIAL_EXPIRES_AT
    )
    assert payload["network"]["provenance"] == "host-network-namespace-inventory"
    assert payload["network"]["lease_expires_at"] == EXPIRES_AT
    assert set(payload["port_reservations"]) == {"cache", "daemon"}
    for purpose, reservation in payload["port_reservations"].items():
        assert reservation["port"] == payload["ports"][purpose]
        assert reservation["owner_run_id"] == RUN_ID
        assert reservation["lease_expires_at"] == EXPIRES_AT


def test_inner_preflight_binds_source_image_executable_mounts_and_secret(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
) -> None:
    cases = {
        "source": ("source_tree_digest", DIGEST_B),
        "image": ("image_digest", DIGEST_A),
        "image-runtime": ("runtime_executable_digest", DIGEST_C),
        "python-runtime": ("python_executable_path", "/usr/bin/other-python"),
        "executable": ("executable_digest", DIGEST_C),
        "secret": ("secret_fingerprint", DIGEST_B),
        "secret-mode": ("secret_mode", "0644"),
        "secret-owner": ("secret_owner_uid", 2002),
        "runtime-owner": ("process_uid", 2002),
        "marker": ("container_marker", "obs-test-v1"),
        "service": ("service_name", "obs-test"),
        "hostname": ("hostname", "obs-test"),
        "network": ("network_namespace_id", "net:[100]"),
    }
    for _label, (key, value) in cases.items():
        candidate = deepcopy(inner_record)
        candidate[key] = value
        with pytest.raises(protocol.PreflightError):
            inner_decision(attestation, candidate)


def test_concrete_inner_observer_binds_active_main_and_argv_runner() -> None:
    source = Path(protocol.__file__).read_text(encoding="utf-8")
    assert "sys.modules.get(\"__main__\")" in source
    assert "getattr(main_module, \"__file__\", None)" in source
    assert "Path(sys.argv[0]).resolve(strict=True)" in source
    assert "invoked_runner == argv_runner" in source
    assert '"executable_digest": self._digest_file(invoked_runner)' in source
    assert 'self._digest_file(Path(source["executable_path"]))' not in source


def test_inner_preflight_enforces_every_runtime_setting(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
) -> None:
    for key in inner_record["runtime_environment"]:
        candidate = deepcopy(inner_record)
        candidate["runtime_environment"][key] = "/wrong"
        with pytest.raises(protocol.PreflightError, match="inner.environment"):
            inner_decision(attestation, candidate)
    candidate = deepcopy(inner_record)
    candidate["ambient_credential_keys"] = ["OBS_TELEGRAM_BOT_TOKEN"]
    with pytest.raises(protocol.PreflightError, match="ambient_credentials"):
        inner_decision(attestation, candidate)


def test_runtime_policy_is_complete_and_decision_derived(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
) -> None:
    decision = inner_decision(attestation, inner_record)
    policy = protocol.build_runtime_policy(decision)
    assert policy.decision is decision
    assert policy.topology == decision.topology
    assert policy.environment == decision.runtime_environment
    assert policy.ports == decision.ports
    assert set(policy.topology) == set(protocol.TOPOLOGY_LAYOUT)
    assert policy.topology["wal"] == protocol.CONTAINER_RUN_ROOT / "state.sqlite3-wal"
    assert policy.topology["wal"] == Path(f"{policy.topology['state_db']}-wal")
    for key in (
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_PROJECTS_DIR",
        "CLAUDE_TEAMS_DIR",
        "CLAUDE_SESSIONS_DIR",
        "CLAUDE_AUTH_FILE",
        "OBS_TEST_PROJECT_ROOT",
        "OBS_VAULT_PATH",
        "OBS_TELEGRAM_STATE_DB_PATH",
        "OBS_TELEGRAM_STATE_WAL_PATH",
        "XDG_CACHE_HOME",
        "OBS_CACHE_DATA_DIR",
        "CACHE_PROXY_LOG_DIR",
        "TMPDIR",
        "OBS_TELEGRAM_TEMP_ROOT",
        "OBS_RUNTIME_LOG_FILE",
        "OBS_TELEGRAM_LOG_FILE",
        "OBS_TEST_EVIDENCE_ROOT",
        "OBS_DAEMON_METADATA_DIR",
        "OBS_DAEMON_PID_FILE",
        "OBS_DAEMON_LOCK_FILE",
    ):
        assert key in policy.environment


def test_guarded_launch_passes_one_policy_to_every_factory(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[protocol.RuntimePolicy] = []
    received: list[protocol.RuntimePolicy] = []
    monkeypatch.setattr(
        protocol,
        "_now",
        lambda value=None: value if value is not None else NOW,
    )
    monkeypatch.setattr(
        protocol,
        "apply_runtime_environment",
        lambda policy: applied.append(policy),
    )
    decision, _process, _network, _poller = protocol.guarded_launch(
        attestation,
        protocol.StaticInnerObservationAdapter(inner_record),
        process_factory=lambda policy: received.append(policy) or "process",
        network_factory=lambda policy: received.append(policy) or "network",
        poller_factory=lambda policy: received.append(policy) or "poller",
    )
    assert len(applied) == 1
    assert received == [applied[0], applied[0], applied[0]]
    assert applied[0].decision is decision


def test_guarded_launch_calls_no_factory_before_inner_preflight(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
) -> None:
    calls: list[str] = []
    denied = deepcopy(inner_record)
    denied["attestation_integrity"] = DIGEST_C
    with pytest.raises(protocol.PreflightError):
        protocol.guarded_launch(
            attestation,
            protocol.StaticInnerObservationAdapter(denied),
            process_factory=lambda decision: calls.append("process"),
            network_factory=lambda decision: calls.append("network"),
            poller_factory=lambda decision: calls.append("poller"),
        )
    assert calls == []


def test_attestation_tampering_and_staleness_are_denied(
    attestation: protocol.HostAttestation,
) -> None:
    tampered = replace(attestation, integrity=DIGEST_A)
    with pytest.raises(protocol.PreflightError, match="integrity"):
        protocol.verify_attestation(tampered, now=NOW)
    with pytest.raises(protocol.PreflightError, match="expired"):
        protocol.verify_attestation(
            attestation,
            now=datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc),
        )


def test_inner_observation_must_be_fresh(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
) -> None:
    candidate = deepcopy(inner_record)
    candidate["observer"]["observed_at"] = ISSUED_AT
    with pytest.raises(protocol.PreflightError, match="freshness"):
        inner_decision(attestation, candidate)


def test_evidence_writer_rejects_symlink_overwrite_and_arbitrary_path(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
    tmp_path: Path,
) -> None:
    decision = inner_decision(attestation, inner_record)
    forged = replace(
        decision,
        topology_items=tuple(
            (name, str(tmp_path / "outside" if name == "evidence" else path))
            for name, path in decision.topology_items
        ),
    )
    with pytest.raises(protocol.PreflightError, match="evidence.root"):
        protocol.write_evidence_manifest(forged, {"status": "not run"})
    source = Path(protocol.__file__).read_text(encoding="utf-8")
    assert "path traversal escaped the approved root" in source
    assert "CONTAINER_SECRET_FILE.parent" in source
    assert "O_NOFOLLOW" in source
    assert "O_DIRECTORY" in source
    assert "O_EXCL" in source
    assert "src_dir_fd=evidence_fd" in source
    assert "dst_dir_fd=evidence_fd" in source
    assert "os.unlink(temp_name, dir_fd=evidence_fd)" in source
    assert "evidence path changed before publication completed" in source
    assert "existing evidence is never overwritten" in source


def test_cleanup_requires_decision_ownership_and_preserves_evidence(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
) -> None:
    decision = inner_decision(attestation, inner_record)
    with pytest.raises(protocol.PreflightError, match="cleanup.run_root"):
        protocol.cleanup_run(
            replace(decision, host_run_root=Path("/workspace/obs"))
        )
    text = Path(protocol.__file__).read_text(encoding="utf-8")
    assert "HOST_EVIDENCE_RETENTION_BASE" in text
    assert "unrecognized resources remain" in text
    assert "decision.host_topology" in text
    assert "HOST_SECRET_BASE" in text
    assert "cleanup.retention_unsafe" in text
    assert "ownership marker is not bound to the decision topology" in text
    assert "name not in {\"evidence\", \"ownership_marker\"}" in text
    assert "decision.host_topology == expected_topology" in text
    assert "unrecognized resources remain; refusing mutation" in text


def test_cleanup_interruption_retry_retains_decision_bound_marker() -> None:
    source = Path(protocol.__file__).read_text(encoding="utf-8")
    assert 'staged_marker = evidence / "ownership.json"' in source
    assert "os.link(host_marker, staged_marker, follow_symlinks=False)" in source
    assert "cleanup.retention_marker_link" in source
    assert "(source_stat.st_dev, source_stat.st_ino)" in source
    assert "(staged_stat.st_dev, staged_stat.st_ino)" in source
    assert "if retention_present:" in source
    assert "_verify_evidence_bundle(retention, decision)" in source
    assert "_verify_ownership_marker(retention_marker, decision)" in source


def test_cleanup_rejects_unknown_or_wrong_owner_before_deletion() -> None:
    source = Path(protocol.__file__).read_text(encoding="utf-8")
    cleanup_source = source.split("def cleanup_run(", maxsplit=1)[1]
    pre_delete, deletion = cleanup_source.split(
        "for name in top_level_names:", maxsplit=1
    )
    assert "unrecognized resources remain; refusing mutation" in pre_delete
    assert "cleanup.owner" in source
    assert "cleanup.evidence_owner" in source
    assert "cleanup.evidence_unknown" in source
    assert "cleanup.evidence_manifest" in source
    assert "item_stat.st_uid == owner_uid" in source
    assert "item_stat.st_gid == owner_gid" in source
    assert "_verify_owned_tree(" in deletion
    verify_position = deletion.index("_verify_owned_tree(")
    remove_position = deletion.index("_remove_owned_tree(")
    assert verify_position < remove_position
    assert "shutil.rmtree" not in source


def test_cleanup_source_secret_ancestor_symlink_partial_and_idempotent_guards_are_authored() -> None:
    source = Path(protocol.__file__).read_text(encoding="utf-8")
    for sentinel in (
        "cleanup.unsafe",
        "cleanup.retention_unsafe",
        "cleanup.symlink",
        "cleanup.escape",
        "cleanup.unowned_remaining",
        "already_absent",
        "already_absent_evidence_retained",
        "transients_removed_evidence_retained",
        "not path.exists()",
    ):
        assert sentinel in source
    assert "shutil.rmtree" not in source
    assert "decision.source_host_path" in source
    assert "HOST_SECRET_BASE" in source
    assert "HOST_ATTESTATION_BASE" in source
    assert "retention_marker = retention / \"ownership.json\"" in source
    assert "if staged_marker.exists() or staged_marker.is_symlink()" in source
    assert "staged retention marker is not hard-linked to the run marker" in source
    assert "os.link(host_marker, staged_marker, follow_symlinks=False)" in source
    assert "_verify_evidence_bundle(retention, decision)" in source
    assert "_verify_ownership_marker(retention_marker, decision)" in source


def test_ownership_binding_is_descriptor_relative_and_preserves_uid_gid() -> None:
    source = Path(protocol.__file__).read_text(encoding="utf-8")
    assert "run_fd = os.open(run_root, directory_flags)" in source
    assert 'os.open(temporary_name, flags, 0o600, dir_fd=run_fd)' in source
    assert "os.fchown(fd, owner_uid, owner_gid)" in source
    assert "src_dir_fd=run_fd" in source
    assert "dst_dir_fd=run_fd" in source
    assert "bound ownership marker replacement has wrong type, mode, or UID/GID" in source
    assert "marker_stat.st_uid == decision.runtime_uid" in source
    assert "marker_stat.st_gid == decision.runtime_gid" in source


def test_recursive_redaction_covers_structured_and_free_text() -> None:
    raw = {
        "environment": {
            "OBS_TELEGRAM_BOT_TOKEN": "123456789:FAKE_SECRET_TOKEN",
            "SAFE": "shown",
        },
        "exception": (
            "Authorization: Bearer raw password=hunter2 "
            "https://example.invalid/send?access_token=raw&safe=shown "
            "person@example.invalid --api-key raw-key"
        ),
        "nested": [
            {"cookie": "cookie-value", "userbot_session": "session-value"}
        ],
        "credentials": {
            "credential_id": "safe-id",
            "fingerprint": DIGEST_A,
            "raw_secret": "must-not-survive",
        },
    }
    redacted = protocol.redact_value(raw)
    serialized = json.dumps(redacted)
    for forbidden in (
        "FAKE_SECRET_TOKEN",
        "hunter2",
        "access_token=raw",
        "person@example.invalid",
        "raw-key",
        "cookie-value",
        "session-value",
        "must-not-survive",
    ):
        assert forbidden not in serialized
    assert redacted["environment"]["SAFE"] == "shown"
    assert isinstance(redacted["credentials"], dict)
    assert redacted["credentials"]["credential_id"] == "safe-id"
    assert redacted["credentials"]["fingerprint"] == DIGEST_A
    assert redacted["credentials"]["raw_secret"] == "<redacted>"


def test_canonical_dry_run_is_single_and_internally_consistent() -> None:
    command = isolated_test_runner.CANONICAL_COMMAND
    assert "--phase host-preflight" in command
    assert "--host-observation" in command
    assert "--compose-file deploy/obs-live-test/compose.yaml" in command
    assert "--lane unit --scenario focused --dry-run" in command
    assert "--dry-run" in isolated_test_runner.INNER_COMMAND
    runner_source = Path(isolated_test_runner.__file__).read_text(encoding="utf-8")
    assert "CANONICAL_COMMAND = (" in runner_source
    assert 'parser.add_argument("--host-observation", type=Path)' in runner_source
    assert 'parser.add_argument("--dry-run", action="store_true")' in runner_source
    assert '"--source"' not in runner_source
    assert '"--evidence-dir"' not in runner_source
    assert "--phase host-preflight" in command
    assert command.endswith("--lane unit --scenario focused --dry-run")


def test_compose_has_no_self_attested_verification_booleans() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "obs-live-test" / "compose.yaml").read_text(
        encoding="utf-8"
    )
    assert "_VERIFIED" not in compose
    assert "_CHECKED" not in compose
    assert "OBS_TEST_SOURCE_COMMIT_OBSERVED" not in compose
    assert "OBS_CONTAINER_MARKER:" not in compose
    assert "OBS_SERVICE_NAME:" not in compose
    assert "PYTHONPATH:" not in compose
    runner_source = (root / "scripts" / "isolated_test_runner.py").read_text(
        encoding="utf-8"
    )
    assert "Path(__file__).resolve(strict=True) == CANONICAL_INNER_RUNNER" in runner_source
    assert "sys.path.insert(0, str(CANONICAL_INNER_RUNNER.parents[1]))" in runner_source
    assert "except ModuleNotFoundError" not in runner_source
    assert "from live_test_protocol import" not in runner_source
    assert "@sha256" in compose
    assert "internal: true" in compose
    assert "host-attestation.json" in compose
    assert "/workspace/obs" not in compose
    for name, (relative, _kind, _mode) in protocol.TOPOLOGY_LAYOUT.items():
        assert str(protocol.CONTAINER_RUN_ROOT / relative) in compose or name in {
            "cache_root",
            "project",
        }


def test_manifest_remains_explicitly_planned_not_run(
    attestation: protocol.HostAttestation,
    inner_record: dict[str, Any],
) -> None:
    decision = inner_decision(attestation, inner_record)
    manifest = protocol.build_evidence_manifest(
        decision=decision,
        lane="live",
        scenario="stop-tree",
        command=(isolated_test_runner.INNER_COMMAND,),
    )
    assert manifest["status"] == "planned_not_run"
    assert manifest["return_code"] == "not run"
    assert manifest["production_state"]["before"] == "not run"
    assert manifest["model"] == protocol.EXPECTED_LIVE_MODEL
    assert manifest["credentials"] == {
        "test_bot_id": "test-bot-v2",
        "credential_id": "test-credential-v2",
        "fingerprint": DIGEST_A,
        "credential_class": "test",
        "provenance": "host-secret-file-sha256",
        "expires_at": CREDENTIAL_EXPIRES_AT,
        "expiry_status": "host-observed-valid-at-attestation",
        "authentication_status": "host-observed-valid-at-attestation",
    }
    assert protocol.SHA256_RE.fullmatch(
        manifest["checksums"]["manifest_payload"]
    )

