"""Static protocol contract for the dedicated OBS formal-test lane.

The host preflight consumes an independently produced host-observation record and
issues an integrity-bound attestation before service creation. The inner
preflight then compares that attestation with facts observed from the created
container before any application factory may be called.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

EXPECTED_CONTAINER_MARKER = "obs-live-test-v1"
EXPECTED_SERVICE_NAME = "obs-live-test"
PRODUCTION_CONTAINER_NAME = "obs-test"
EXPECTED_LIVE_MODEL = "gpt-5.6-luna"
ATTESTATION_SCHEMA = "obs-live-test-host-attestation-v2"
HOST_OBSERVER_KIND = "host-runtime-inspector-v1"
INNER_OBSERVER_KIND = "container-runtime-inspector-v1"

PRODUCTION_SOURCE_PATH = Path("/workspace/obs")
HOST_RUN_BASE = Path("/workspace/runtime/obs-live-test-runs")
HOST_SECRET_BASE = Path("/workspace/runtime/obs-live-test-secrets")
HOST_SOURCE_SNAPSHOT_BASE = Path("/workspace/runtime/obs-live-test-snapshots")
HOST_ATTESTATION_BASE = Path("/workspace/runtime/obs-live-test-attestations")
HOST_EVIDENCE_RETENTION_BASE = Path("/workspace/runtime/obs-live-test-evidence")
HOST_OBSERVATION_FILE = Path("/workspace/runtime/obs-live-test-host-observation.json")
ALLOWED_TEST_PORT_RANGE = range(29000, 30000)
CONTAINER_RUN_ROOT = Path("/run/obs-live-test")
CONTAINER_SOURCE_ROOT = Path("/source/snapshot")
CONTAINER_SECRET_FILE = Path("/run/obs-live-test-secret/test-credentials")
CONTAINER_ATTESTATION_FILE = Path(
    "/run/obs-live-test-attestation/host-attestation.json"
)
IMAGE_IDENTITY_FILE = Path("/opt/obs-image/identity.json")

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32,128}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
INDEPENDENT_PROVENANCE_DENY = {
    "caller",
    "compose",
    "environment",
    "self",
    "self-attested",
    "test-container",
    "unknown",
}

TOPOLOGY_LAYOUT: dict[str, tuple[str, str, str]] = {
    "home": ("home", "directory", "0700"),
    "claude_config": ("claude-config", "directory", "0700"),
    "claude_projects": ("claude-projects", "directory", "0700"),
    "claude_teams": ("claude-teams", "directory", "0700"),
    "claude_sessions": ("claude-sessions", "directory", "0700"),
    "claude_auth": ("claude-auth.json", "file", "0600"),
    "project": ("project", "directory", "0700"),
    "fixture": ("fixture", "directory", "0700"),
    "state_db": ("state.sqlite3", "file", "0600"),
    "wal": ("state.sqlite3-wal", "file", "0600"),
    "cache_root": ("cache-root", "directory", "0700"),
    "cache_data": ("cache-data", "directory", "0700"),
    "cache_log": ("cache-log", "directory", "0700"),
    "temp": ("temp", "directory", "0700"),
    "download": ("download", "directory", "0700"),
    "runtime_log": ("runtime.log", "file", "0600"),
    "telegram_log": ("telegram.log", "file", "0600"),
    "evidence": ("evidence", "directory", "0700"),
    "daemon_metadata": ("daemon-metadata", "directory", "0700"),
    "daemon_pid": ("daemon.pid", "file", "0600"),
    "daemon_lock": ("daemon.lock", "file", "0600"),
    "ownership_marker": ("ownership.json", "file", "0600"),
}
DEDICATED_PATH_NAMES = tuple(TOPOLOGY_LAYOUT)

SENSITIVE_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|cookie|session|authorization|api[_-]?key|"
    r"credential|login|email|userbot|auth[_-]?header)",
    re.IGNORECASE,
)
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{8,}\b")
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:token|secret|password|passwd|cookie|session|authorization|"
    r"api[_-]?key|credential|login|email|userbot[_-]?(?:api[_-]?id|api[_-]?hash|session))"
    r"\b\s*[:=]\s*)([^\s,;]+)"
)
COMMAND_VALUE_RE = re.compile(
    r"(?i)((?:--|\b)(?:token|secret|password|cookie|session|authorization|api-key|"
    r"credential|email)(?:=|\s+))([^\s]+)"
)
AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*:\s*)([^\r\n]+)")
QUERY_KEYS = re.compile(
    r"^(?:token|access_token|api_key|apikey|key|secret|password|passwd|session|"
    r"auth|authorization|cookie|credential|email|login)$",
    re.IGNORECASE,
)
SAFE_CREDENTIAL_EVIDENCE_FIELDS = {
    "credential_id",
    "test_bot_id",
    "fingerprint",
    "credential_class",
    "provenance",
    "expires_at",
    "expiry_status",
    "authentication_status",
}


class PreflightError(ValueError):
    """Fail-closed denial whose detail must never contain a raw secret."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class HostObservationAdapter(Protocol):
    """Trusted host boundary used before service creation."""

    def observe(self) -> Mapping[str, Any]:
        """Return host facts measured without reading them from Compose env."""


class InnerObservationAdapter(Protocol):
    """Runtime boundary used after service creation and before factories."""

    def observe(self, attestation: "HostAttestation") -> Mapping[str, Any]:
        """Return container facts measured from runtime state."""


@dataclass(frozen=True)
class StaticHostObservationAdapter:
    """Injectable record adapter for host orchestrators and authored tests."""

    record: Mapping[str, Any]

    def observe(self) -> Mapping[str, Any]:
        return json.loads(json.dumps(self.record))


@dataclass(frozen=True)
class StaticInnerObservationAdapter:
    """Injectable inner record adapter for authored adversarial tests."""

    record: Mapping[str, Any]

    def observe(self, _attestation: "HostAttestation") -> Mapping[str, Any]:
        return json.loads(json.dumps(self.record))


@dataclass(frozen=True)
class ContainerRuntimeObservationAdapter:
    """Observe container facts directly from procfs, files, and environment."""

    env: Mapping[str, str]
    proc_mountinfo: Path = Path("/proc/self/mountinfo")
    proc_net_ns: Path = Path("/proc/self/ns/net")

    def _digest_file(self, path: Path) -> str:
        _require(path.is_file() and not path.is_symlink(), "inner.file", "observed file is missing, non-regular, or a symlink")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def _digest_tree(self, root: Path) -> str:
        _require(root.is_dir() and not root.is_symlink(), "inner.source_tree", "source root is missing, non-directory, or a symlink")
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            _require(not path.is_symlink(), "inner.source_tree.symlink", "source tree contains a symlink")
            relative = path.relative_to(root).as_posix().encode("utf-8")
            if path.is_dir():
                digest.update(b"D\0" + relative + b"\0")
                continue
            _require(path.is_file(), "inner.source_tree.type", "source tree contains a non-regular entry")
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def _mount_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        targets = {
            str(CONTAINER_SOURCE_ROOT): "source",
            str(CONTAINER_RUN_ROOT): "run",
            str(CONTAINER_SECRET_FILE): "secret",
            str(CONTAINER_ATTESTATION_FILE): "attestation",
        }
        for line in self.proc_mountinfo.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if "-" not in fields or len(fields) < 6:
                continue
            target = fields[4].replace("\\040", " ")
            name = targets.get(target)
            if name is None:
                continue
            options = set(fields[5].split(","))
            records.append(
                {
                    "name": name,
                    "target": target,
                    "mode": "ro" if "ro" in options else "rw",
                    "symlink": Path(target).is_symlink(),
                }
            )
        return records

    def _topology_records(self, run_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for name, path in _topology(CONTAINER_RUN_ROOT).items():
            info = path.lstat()
            kind = "directory" if path.is_dir() else "file"
            records.append(
                {
                    "name": name,
                    "path": str(path),
                    "kind": kind,
                    "mode": f"{info.st_mode & 0o7777:04o}",
                    "symlink": path.is_symlink(),
                    "owner_run_id": run_id,
                    "owner_uid": info.st_uid,
                    "owner_gid": info.st_gid,
                }
            )
        return records

    def observe(self, attestation: "HostAttestation") -> Mapping[str, Any]:
        payload = verify_attestation(attestation)
        image_document = json.loads(IMAGE_IDENTITY_FILE.read_text(encoding="utf-8"))
        image = _require_mapping(image_document, "inner.image_identity")
        main_module = sys.modules.get("__main__")
        invoked_file = getattr(main_module, "__file__", None)
        _require(
            isinstance(invoked_file, str) and bool(invoked_file.strip()),
            "inner.executable_path",
            "the invoked runner path is unavailable from the active main module",
        )
        invoked_runner = Path(invoked_file).resolve(strict=True)
        argv_runner = Path(sys.argv[0]).resolve(strict=True)
        _require(
            invoked_runner == argv_runner,
            "inner.executable_invocation",
            "the active main module and process argument identify different runners",
        )
        runtime_executable = Path("/proc/self/exe").resolve(strict=True)
        secret_stat = CONTAINER_SECRET_FILE.lstat()
        return {
            "observer": {
                "kind": INNER_OBSERVER_KIND,
                "provenance": "container-procfs-and-filesystem",
                "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
            "attestation_integrity": attestation.integrity,
            "run_id": payload["run_id"],
            "nonce": payload["nonce"],
            "container_marker": image.get("container_marker"),
            "service_name": image.get("service_name"),
            "hostname": socket.gethostname(),
            "profile": self.env.get("OBS_PROFILE"),
            "production_mode": self.env.get("OBS_PROD_MODE") or self.env.get("OBS_MODE"),
            "process_uid": os.geteuid(),
            "process_gid": os.getegid(),
            "source_path": str(CONTAINER_SOURCE_ROOT),
            "source_tree_digest": self._digest_tree(CONTAINER_SOURCE_ROOT),
            "executable_path": str(invoked_runner),
            "executable_digest": self._digest_file(invoked_runner),
            "image_digest": image.get("image_digest"),
            "runtime_executable_path": str(runtime_executable),
            "runtime_executable_digest": self._digest_file(runtime_executable),
            "python_executable_path": str(
                Path(sys.executable).resolve(strict=True)
            ),
            "mounts": self._mount_records(),
            "topology": self._topology_records(payload["run_id"]),
            "runtime_environment": dict(self.env),
            "repository_env_present": (CONTAINER_SOURCE_ROOT / ".env").exists(),
            "production_env_keys": sorted(
                key for key, value in self.env.items() if key.startswith("OBS_PROD_") and value.strip()
            ),
            "ambient_credential_keys": sorted(
                key
                for key, value in self.env.items()
                if value.strip()
                and key not in _expected_runtime_environment(payload)
                and SENSITIVE_KEY_RE.search(key)
            ),
            "secret_regular_file": CONTAINER_SECRET_FILE.is_file(),
            "secret_symlink": CONTAINER_SECRET_FILE.is_symlink(),
            "secret_mode": f"{secret_stat.st_mode & 0o7777:04o}",
            "secret_owner_uid": secret_stat.st_uid,
            "secret_owner_gid": secret_stat.st_gid,
            "secret_fingerprint": self._digest_file(CONTAINER_SECRET_FILE),
            "network_identity": self.env.get("OBS_TEST_NETWORK_IDENTITY"),
            "network_namespace_id": os.readlink(self.proc_net_ns),
            "network_mode": self.env.get("OBS_TEST_NETWORK_MODE"),
        }


@dataclass(frozen=True)
class RuntimePolicy:
    """Decision-derived settings passed intact to every later factory."""

    decision: "PreflightDecision"
    topology_items: tuple[tuple[str, str], ...]
    environment_items: tuple[tuple[str, str], ...]
    port_items: tuple[tuple[str, int], ...]

    @property
    def topology(self) -> dict[str, Path]:
        return {name: Path(path) for name, path in self.topology_items}

    @property
    def environment(self) -> dict[str, str]:
        return dict(self.environment_items)

    @property
    def ports(self) -> dict[str, int]:
        return dict(self.port_items)


@dataclass(frozen=True)
class HostAttestation:
    """Immutable serialized host decision with a deterministic integrity field."""

    payload_json: str
    integrity: str

    @property
    def payload(self) -> dict[str, Any]:
        loaded = json.loads(self.payload_json)
        if not isinstance(loaded, dict):
            raise PreflightError(
                "attestation.payload", "attestation payload is not an object"
            )
        return loaded

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ATTESTATION_SCHEMA,
            "payload": self.payload,
            "integrity": self.integrity,
        }


@dataclass(frozen=True)
class PreflightDecision:
    """Complete secret-free launch decision consumed by every later factory."""

    run_id: str
    nonce: str
    attestation_integrity: str
    service_name: str
    container_marker: str
    host_run_root: Path
    run_root: Path
    source_host_path: Path
    source_snapshot: Path
    source_commit: str
    source_tree_digest: str
    image_digest: str
    executable_path: Path
    executable_digest: str
    topology_items: tuple[tuple[str, str], ...]
    host_topology_items: tuple[tuple[str, str], ...]
    runtime_environment_items: tuple[tuple[str, str], ...]
    port_items: tuple[tuple[str, int], ...]
    production_roots: tuple[Path, ...]
    shared_roots: tuple[Path, ...]
    test_bot_id: str
    credential_id: str
    credential_fingerprint: str
    credential_class: str
    credential_provenance: str
    credential_expires_at: str
    network_identity: str
    network_namespace_id: str
    runtime_uid: int
    runtime_gid: int
    reasons: tuple[str, ...] = ()

    @property
    def topology(self) -> dict[str, Path]:
        return {name: Path(path) for name, path in self.topology_items}

    @property
    def host_topology(self) -> dict[str, Path]:
        return {name: Path(path) for name, path in self.host_topology_items}

    @property
    def runtime_environment(self) -> dict[str, str]:
        return dict(self.runtime_environment_items)

    @property
    def ports(self) -> dict[str, int]:
        return dict(self.port_items)

    @property
    def evidence_root(self) -> Path:
        return self.topology["evidence"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "allowed",
            "run_id": self.run_id,
            "attestation_integrity": self.attestation_integrity,
            "service_name": self.service_name,
            "container_marker": self.container_marker,
            "source_commit": self.source_commit,
            "source_tree_digest": self.source_tree_digest,
            "image_digest": self.image_digest,
            "executable_path": str(self.executable_path),
            "executable_digest": self.executable_digest,
            "topology": {
                key: str(value) for key, value in self.topology.items()
            },
            "runtime_environment": self.runtime_environment,
            "ports": self.ports,
            "test_bot_id": self.test_bot_id,
            "credential_id": self.credential_id,
            "credential_fingerprint": self.credential_fingerprint,
            "credential_class": self.credential_class,
            "credential_provenance": self.credential_provenance,
            "credential_expires_at": self.credential_expires_at,
            "network_identity": self.network_identity,
            "network_namespace_id": self.network_namespace_id,
            "runtime_uid": self.runtime_uid,
            "runtime_gid": self.runtime_gid,
            "reasons": list(self.reasons),
        }


class LiveTestConfig:
    """Compatibility tombstone: environment booleans cannot prove host safety."""

    @classmethod
    def from_env(cls, *_args: Any, **_kwargs: Any) -> "LiveTestConfig":
        raise PreflightError(
            "preflight.self_attestation_removed",
            "formal testing requires host_preflight and inner_preflight; environment booleans are not evidence",
        )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _integrity(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def is_within(path: Path | str, parent: Path | str) -> bool:
    normalized_path = _resolved(path)
    normalized_parent = _resolved(parent)
    return (
        normalized_path == normalized_parent
        or normalized_parent in normalized_path.parents
    )


def _paths_overlap(left: Path | str, right: Path | str) -> bool:
    return is_within(left, right) or is_within(right, left)


def _require(condition: bool, code: str, detail: str) -> None:
    if not condition:
        raise PreflightError(code, detail)


def _require_string(value: Any, code: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()),
        code,
        "required string is missing",
    )
    return value.strip()


def _require_mapping(value: Any, code: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), code, "required object is missing")
    return value


def _require_sequence(value: Any, code: str) -> Sequence[Any]:
    _require(
        isinstance(value, Sequence) and not isinstance(value, (str, bytes)),
        code,
        "required list is missing",
    )
    return value


def _parse_time(value: Any, code: str) -> datetime:
    text = _require_string(value, code)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightError(
            code, "timestamp must be ISO-8601 with a timezone"
        ) from exc
    _require(
        parsed.tzinfo is not None, code, "timestamp must include a timezone"
    )
    return parsed.astimezone(timezone.utc)


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc)


def _validate_digest(value: Any, code: str) -> str:
    text = _require_string(value, code)
    _require(
        bool(SHA256_RE.fullmatch(text)),
        code,
        "digest must be sha256: plus 64 lowercase hexadecimal characters",
    )
    return text


def _validate_safe_id(value: Any, code: str) -> str:
    text = _require_string(value, code)
    _require(
        bool(SAFE_ID_RE.fullmatch(text)),
        code,
        "safe identifier has an invalid format",
    )
    return text


def _validate_provenance(value: Any, code: str) -> str:
    text = _require_string(value, code)
    _require(
        text.lower() not in INDEPENDENT_PROVENANCE_DENY,
        code,
        "inventory provenance is not independent",
    )
    return text


def _topology(root: Path) -> dict[str, Path]:
    return {
        name: root / relative
        for name, (relative, _kind, _mode) in TOPOLOGY_LAYOUT.items()
    }


def _record_by_name(
    records: Sequence[Any], code: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for item in records:
        record = _require_mapping(item, code)
        name = _require_string(record.get("name"), code)
        _require(
            name not in result, code, "topology contains a duplicate name"
        )
        result[name] = record
    return result


def _validate_topology_records(
    records: Sequence[Any],
    *,
    expected: Mapping[str, Path],
    run_id: str,
    owner_uid: int,
    owner_gid: int,
    code_prefix: str,
) -> tuple[tuple[str, str], ...]:
    by_name = _record_by_name(records, f"{code_prefix}.records")
    _require(
        set(by_name) == set(TOPOLOGY_LAYOUT),
        f"{code_prefix}.names",
        "topology names do not match the canonical layout",
    )
    normalized: dict[str, Path] = {}
    for name, expected_path in expected.items():
        record = by_name[name]
        actual_path = _resolved(
            _require_string(record.get("path"), f"{code_prefix}.{name}.path")
        )
        _require(
            actual_path == _resolved(expected_path),
            f"{code_prefix}.{name}.path",
            "topology path differs from the canonical layout",
        )
        expected_kind = TOPOLOGY_LAYOUT[name][1]
        expected_mode = TOPOLOGY_LAYOUT[name][2]
        _require(
            record.get("kind") == expected_kind,
            f"{code_prefix}.{name}.kind",
            "topology file type differs from the canonical layout",
        )
        _require(
            record.get("symlink") is False,
            f"{code_prefix}.{name}.symlink",
            "topology paths must not be symlinks",
        )
        _require(
            record.get("owner_run_id") == run_id,
            f"{code_prefix}.{name}.owner",
            "topology ownership is not bound to this run",
        )
        _require(
            record.get("owner_uid") == owner_uid
            and record.get("owner_gid") == owner_gid,
            f"{code_prefix}.{name}.filesystem_owner",
            "topology filesystem ownership differs from the approved runtime owner",
        )
        _require(
            str(record.get("mode")) == expected_mode,
            f"{code_prefix}.{name}.mode",
            "topology permissions differ from the canonical layout",
        )
        normalized[name] = actual_path

    names = tuple(normalized)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            left = normalized[left_name]
            right = normalized[right_name]
            _require(
                left != right,
                f"{code_prefix}.alias",
                "two topology resources resolve to the same path",
            )
            _require(
                not _paths_overlap(left, right),
                f"{code_prefix}.overlap",
                "topology resources must be pairwise disjoint after canonicalization",
            )
    return tuple((name, str(normalized[name])) for name in TOPOLOGY_LAYOUT)


def _validate_inventory(
    record: Any,
    code: str,
    *,
    current: datetime | None = None,
) -> Mapping[str, Any]:
    inventory = _require_mapping(record, code)
    _validate_provenance(
        inventory.get("provenance"), f"{code}.provenance"
    )
    observed_at = _parse_time(
        inventory.get("observed_at"), f"{code}.observed_at"
    )
    if current is not None:
        age = (current - observed_at).total_seconds()
        _require(
            0 <= age <= 300,
            f"{code}.freshness",
            "inventory is future-dated or older than five minutes",
        )
    _require(
        inventory.get("complete") is True,
        f"{code}.complete",
        "inventory completeness is not established",
    )
    return inventory


def _validate_no_unsafe_path(
    path: Path, unsafe_roots: Sequence[Path], code: str
) -> None:
    _require(
        not any(_paths_overlap(path, root) for root in unsafe_roots),
        code,
        "path overlaps a production or shared root after canonicalization",
    )


def host_preflight(
    adapter: HostObservationAdapter,
    *,
    now: datetime | None = None,
) -> HostAttestation:
    """Validate host facts and issue the pre-service attestation."""
    observation = _require_mapping(adapter.observe(), "host.observation")
    observer = _require_mapping(observation.get("observer"), "host.observer")
    _require(
        observer.get("kind") == HOST_OBSERVER_KIND,
        "host.observer.kind",
        "host facts did not come from the required observer",
    )
    _validate_provenance(
        observer.get("provenance"), "host.observer.provenance"
    )
    observer_observed_at = _parse_time(
        observer.get("observed_at"), "host.observer.observed_at"
    )

    run_id = _require_string(observation.get("run_id"), "host.run_id")
    _require(
        bool(RUN_ID_RE.fullmatch(run_id)),
        "host.run_id",
        "run ID does not match the canonical single-run format",
    )
    nonce = _require_string(observation.get("nonce"), "host.nonce")
    _require(
        bool(NONCE_RE.fullmatch(nonce)),
        "host.nonce",
        "nonce does not match the single-run format",
    )
    issued_at = _parse_time(observation.get("issued_at"), "host.issued_at")
    expires_at = _parse_time(observation.get("expires_at"), "host.expires_at")
    current = _now(now)
    observer_age = (current - observer_observed_at).total_seconds()
    _require(
        0 <= observer_age <= 300,
        "host.observer.freshness",
        "host observation is future-dated or older than five minutes",
    )
    _require(
        issued_at <= current < expires_at,
        "host.attestation_window",
        "attestation issue/expiry window is not currently valid",
    )
    _require(
        (expires_at - issued_at).total_seconds() <= 900,
        "host.attestation_window",
        "host attestation lifetime exceeds fifteen minutes",
    )
    runtime_owner = _require_mapping(
        observation.get("runtime_owner"), "host.runtime_owner"
    )
    runtime_uid = runtime_owner.get("uid")
    runtime_gid = runtime_owner.get("gid")
    _require(
        isinstance(runtime_uid, int)
        and not isinstance(runtime_uid, bool)
        and runtime_uid > 0
        and isinstance(runtime_gid, int)
        and not isinstance(runtime_gid, bool)
        and runtime_gid > 0,
        "host.runtime_owner",
        "runtime UID/GID must be independently observed non-root integers",
    )
    _validate_provenance(
        runtime_owner.get("provenance"), "host.runtime_owner.provenance"
    )

    run_root = _resolved(HOST_RUN_BASE / run_id)
    _require(
        _resolved(
            _require_string(observation.get("run_root"), "host.run_root")
        )
        == run_root,
        "host.run_root",
        "run root is not the exact dedicated host location",
    )
    attestation_host_path = _resolved(
        HOST_ATTESTATION_BASE / run_id / "host-attestation.json"
    )
    _require(
        _resolved(
            _require_string(
                observation.get("attestation_host_path"),
                "host.attestation_path",
            )
        )
        == attestation_host_path,
        "host.attestation_path",
        "attestation path is not the exact dedicated host location",
    )

    inventories = _require_mapping(
        observation.get("inventories"), "host.inventories"
    )
    production_roots_inventory = _validate_inventory(
        inventories.get("production_roots"), "inventory.production_roots", current=current
    )
    shared_roots_inventory = _validate_inventory(
        inventories.get("shared_roots"),
        "inventory.shared_roots",
        current=current,
    )
    production_roots = {
        _resolved(PRODUCTION_SOURCE_PATH),
        *(
            _resolved(
                _require_string(
                    item, "inventory.production_roots.root"
                )
            )
            for item in _require_sequence(
                production_roots_inventory.get("roots"),
                "inventory.production_roots.roots",
            )
        ),
    }
    shared_roots = {
        _resolved(
            _require_string(item, "inventory.shared_roots.root")
        )
        for item in _require_sequence(
            shared_roots_inventory.get("roots"),
            "inventory.shared_roots.roots",
        )
    }
    unsafe_roots = tuple(
        sorted(production_roots | shared_roots, key=str)
    )
    _validate_no_unsafe_path(
        run_root, unsafe_roots, "host.run_root_unsafe"
    )
    _validate_no_unsafe_path(
        attestation_host_path,
        unsafe_roots,
        "host.attestation_path_unsafe",
    )

    source = _require_mapping(observation.get("source"), "host.source")
    source_host_path = _resolved(
        _require_string(source.get("host_path"), "source.host_path")
    )
    _require(
        source_host_path == _resolved(HOST_SOURCE_SNAPSHOT_BASE / run_id),
        "source.host_path",
        "source snapshot is not the exact run-owned immutable host location",
    )
    _validate_no_unsafe_path(
        source_host_path, unsafe_roots, "source.host_path_unsafe"
    )
    _require(
        _resolved(
            _require_string(source.get("target_path"), "source.target_path")
        )
        == CONTAINER_SOURCE_ROOT,
        "source.target_path",
        "source target is not the canonical container snapshot path",
    )
    requested_commit = _require_string(
        source.get("requested_commit"), "source.requested_commit"
    )
    observed_commit = _require_string(
        source.get("observed_commit"), "source.observed_commit"
    )
    _require(
        bool(COMMIT_RE.fullmatch(requested_commit)),
        "source.requested_commit",
        "source commit must be 40 lowercase hexadecimal characters",
    )
    _require(
        bool(COMMIT_RE.fullmatch(observed_commit)),
        "source.observed_commit",
        "observed source commit must be independently measured in canonical format",
    )
    _require(
        requested_commit == observed_commit,
        "source.commit_mismatch",
        "requested and observed source commits differ",
    )
    _require(
        source.get("regular_directory") is True
        and source.get("symlink") is False,
        "source.snapshot_type",
        "source snapshot must be an observed non-symlink directory",
    )
    _require(
        source.get("writable") is False,
        "source.snapshot_mutability",
        "source snapshot must be host-observed immutable for the attestation window",
    )
    _require(
        source.get("owner_run_id") == run_id,
        "source.snapshot_owner",
        "source snapshot is not owned by this run",
    )
    _validate_provenance(
        source.get("provenance"), "source.provenance"
    )
    source_tree_digest = _validate_digest(
        source.get("tree_digest"), "source.tree_digest"
    )
    executable_path = _resolved(
        _require_string(source.get("executable_path"), "source.executable_path")
    )
    expected_executable = (
        CONTAINER_SOURCE_ROOT / "scripts" / "isolated_test_runner.py"
    )
    _require(
        executable_path == expected_executable,
        "source.executable_path",
        "executed runner is not bound to the verified snapshot",
    )
    executable_digest = _validate_digest(
        source.get("executable_digest"), "source.executable_digest"
    )
    source_executable_digest = _validate_digest(
        source.get("source_executable_digest"),
        "source.source_executable_digest",
    )
    _require(
        executable_digest == source_executable_digest,
        "source.executable_mismatch",
        "executed code digest differs from the selected source",
    )

    image = _require_mapping(observation.get("image"), "host.image")
    image_digest = _validate_digest(image.get("digest"), "image.digest")
    image_reference = _require_string(
        image.get("reference"), "image.reference"
    )
    image_runtime_executable_path = _resolved(
        _require_string(
            image.get("runtime_executable_path"),
            "image.runtime_executable_path",
        )
    )
    _require(
        image_runtime_executable_path.is_absolute(),
        "image.runtime_executable_path",
        "runtime executable path must be absolute",
    )
    image_runtime_executable_digest = _validate_digest(
        image.get("runtime_executable_digest"),
        "image.runtime_executable_digest",
    )
    _require(
        image_reference.endswith("@" + image_digest),
        "image.reference",
        "image reference is not pinned to the observed digest",
    )
    _require(
        image.get("container_marker") == EXPECTED_CONTAINER_MARKER,
        "image.container_marker",
        "host-observed immutable image marker differs from the formal-test marker",
    )
    _require(
        image.get("service_name") == EXPECTED_SERVICE_NAME,
        "image.service_name",
        "host-observed immutable image service identity differs",
    )
    _validate_provenance(image.get("provenance"), "image.provenance")

    expected_host_topology = _topology(run_root)
    host_topology_items = _validate_topology_records(
        _require_sequence(observation.get("topology"), "host.topology"),
        expected=expected_host_topology,
        run_id=run_id,
        owner_uid=runtime_uid,
        owner_gid=runtime_gid,
        code_prefix="host.topology",
    )
    for _name, path_text in host_topology_items:
        _validate_no_unsafe_path(
            Path(path_text), unsafe_roots, "host.topology.unsafe"
        )
        _require(
            not _paths_overlap(Path(path_text), source_host_path),
            "host.topology.source_overlap",
            "writable topology overlaps the source snapshot",
        )

    credential = _require_mapping(
        observation.get("credential"), "host.credential"
    )
    credential_id = _validate_safe_id(
        credential.get("credential_id"), "credential.id"
    )
    test_bot_id = _validate_safe_id(
        credential.get("test_bot_id"), "credential.bot_id"
    )
    _require(
        credential.get("credential_class") == "test",
        "credential.class",
        "credential is not classified as test-only",
    )
    credential_fingerprint = _validate_digest(
        credential.get("fingerprint"), "credential.fingerprint"
    )
    measured_fingerprint = _validate_digest(
        credential.get("measured_fingerprint"),
        "credential.measured_fingerprint",
    )
    _require(
        credential_fingerprint == measured_fingerprint,
        "credential.measurement_mismatch",
        "configured fingerprint differs from the measured test secret",
    )
    credential_provenance = _validate_provenance(
        credential.get("provenance"), "credential.provenance"
    )
    secret_host_path = _resolved(
        _require_string(credential.get("secret_path"), "credential.secret_path")
    )
    expected_secret_host_path = _resolved(
        HOST_SECRET_BASE / run_id / "test-credentials"
    )
    _require(
        secret_host_path == expected_secret_host_path,
        "credential.secret_path",
        "test secret is not in the dedicated per-run host location",
    )
    _validate_no_unsafe_path(
        secret_host_path, unsafe_roots, "credential.secret_path_unsafe"
    )
    _require(
        not _paths_overlap(secret_host_path, source_host_path),
        "credential.secret_in_source",
        "test secret overlaps the source snapshot",
    )
    _require(
        credential.get("regular_file") is True,
        "credential.file_type",
        "test secret is not an observed regular file",
    )
    _require(
        credential.get("symlink") is False,
        "credential.symlink",
        "test secret must not be a symlink",
    )
    _require(
        isinstance(credential.get("owner_uid"), int)
        and not isinstance(credential.get("owner_uid"), bool)
        and isinstance(credential.get("owner_gid"), int)
        and not isinstance(credential.get("owner_gid"), bool),
        "credential.owner",
        "test secret UID/GID were not measured as integers",
    )
    _require(
        credential.get("owner_uid") == runtime_uid
        and credential.get("owner_gid") == runtime_gid,
        "credential.owner",
        "test secret UID/GID differ from the host-attested runtime owner",
    )
    _require(
        str(credential.get("mode")) in {"0400", "0440"},
        "credential.mode",
        "test secret permissions are broader than read-only owner/group access",
    )
    _require(
        credential.get("authentication_status") == "valid",
        "credential.authentication",
        "test credential authentication is not valid",
    )
    authenticated_at = _parse_time(
        credential.get("authenticated_at"), "credential.authenticated_at"
    )
    _require(
        0 <= (current - authenticated_at).total_seconds() <= 300,
        "credential.authentication_freshness",
        "test credential authentication is future-dated or older than five minutes",
    )
    credential_expires_at = _parse_time(
        credential.get("expires_at"), "credential.expires_at"
    )
    _require(
        credential_expires_at >= expires_at,
        "credential.expired",
        "test credential does not remain valid through attestation expiry",
    )
    allowlist = {
        _validate_safe_id(item, "credential.allowlist")
        for item in _require_sequence(
            credential.get("allowlist"), "credential.allowlist"
        )
    }
    _require(
        credential_id in allowlist,
        "credential.allowlist",
        "test credential ID is not allowlisted",
    )

    production_credentials = _validate_inventory(
        inventories.get("production_credentials"),
        "inventory.production_credentials",
        current=current,
    )
    production_credential_records = _require_sequence(
        production_credentials.get("records"),
        "inventory.production_credentials.records",
    )
    _require(
        bool(production_credential_records),
        "inventory.production_credentials.empty",
        "production credential inventory must be nonempty",
    )
    production_fingerprints: list[str] = []
    for item in production_credential_records:
        record = _require_mapping(
            item, "inventory.production_credentials.record"
        )
        production_fingerprints.append(
            _validate_digest(
                record.get("fingerprint"),
                "inventory.production_credentials.fingerprint",
            )
        )
        _validate_safe_id(
            record.get("credential_id"),
            "inventory.production_credentials.credential_id",
        )
    _require(
        credential_fingerprint not in production_fingerprints,
        "credential.production_match",
        "test credential matches a production credential",
    )

    poller_inventory = _validate_inventory(
        inventories.get("production_pollers"),
        "inventory.production_pollers",
        current=current,
    )
    _require_string(
        poller_inventory.get("lease_epoch"),
        "inventory.production_pollers.lease_epoch",
    )
    selected_bot_leases: list[str] = []
    selected_bot_lease_expires_at: datetime | None = None
    poller_records = _require_sequence(
        poller_inventory.get("records"),
        "inventory.production_pollers.records",
    )
    _require(
        bool(poller_records),
        "inventory.production_pollers.empty",
        "production poller inventory must be nonempty",
    )
    for item in poller_records:
        record = _require_mapping(item, "inventory.production_pollers.record")
        bot_id = _validate_safe_id(
            record.get("bot_id"), "inventory.production_pollers.bot_id"
        )
        owner_run_id = _validate_safe_id(
            record.get("owner_run_id"),
            "inventory.production_pollers.owner_run_id",
        )
        lease_id = _validate_safe_id(
            record.get("lease_id"),
            "inventory.production_pollers.lease_id",
        )
        observed_at = _parse_time(
            record.get("observed_at"),
            "inventory.production_pollers.record_observed_at",
        )
        _require(
            0 <= (current - observed_at).total_seconds() <= 60,
            "inventory.production_pollers.record_freshness",
            "poller record is future-dated or older than one minute",
        )
        state = record.get("state")
        _require(
            state in {"active", "reserved", "stopped"},
            "inventory.production_pollers.state",
            "poller state is unknown",
        )
        lease_expires_at = _parse_time(
            record.get("lease_expires_at"),
            "inventory.production_pollers.lease_expires_at",
        )
        if state in {"active", "reserved"}:
            _require(
                lease_expires_at >= expires_at,
                "inventory.production_pollers.lease_lifetime",
                "active poller lease does not remain valid through attestation expiry",
            )
        if bot_id == test_bot_id and state in {
            "active",
            "reserved",
        }:
            _require(
                owner_run_id == run_id,
                "poller.duplicate",
                "selected test bot is owned outside this run",
            )
            selected_bot_leases.append(lease_id)
            selected_bot_lease_expires_at = lease_expires_at
    _require(
        len(selected_bot_leases) == 1
        and len(set(selected_bot_leases)) == 1,
        "poller.lease",
        "selected test bot must have exactly one run-owned active reservation",
    )
    expected_poller_lease_id = _validate_safe_id(
        credential.get("poller_lease_id"), "credential.poller_lease_id"
    )
    _require(
        selected_bot_leases[0] == expected_poller_lease_id,
        "poller.lease_binding",
        "selected poller reservation is not bound to the measured test credential",
    )
    _require(
        selected_bot_lease_expires_at is not None,
        "poller.lease_lifetime",
        "selected poller reservation expiry was not measured",
    )

    production_ports = _validate_inventory(
        inventories.get("production_ports"),
        "inventory.production_ports",
        current=current,
    )
    denied_ports: set[int] = set()
    for item in _require_sequence(
        production_ports.get("records"), "inventory.production_ports.records"
    ):
        record = _require_mapping(item, "inventory.production_ports.record")
        port = record.get("port")
        _require(
            isinstance(port, int)
            and not isinstance(port, bool)
            and 1 <= port <= 65535,
            "inventory.production_ports.port",
            "production port is outside 1..65535",
        )
        denied_ports.add(port)
    selected_ports: dict[str, int] = {}
    selected_port_reservations: dict[str, dict[str, Any]] = {}
    for item in _require_sequence(observation.get("ports"), "host.ports"):
        record = _require_mapping(item, "host.port")
        purpose = _require_string(record.get("purpose"), "host.port.purpose")
        _require(
            purpose in {"cache", "daemon"} and purpose not in selected_ports,
            "host.port.purpose",
            "ports must contain one cache and one daemon reservation",
        )
        port = record.get("port")
        _require(
            isinstance(port, int)
            and not isinstance(port, bool)
            and port in ALLOWED_TEST_PORT_RANGE,
            "host.port.range",
            "selected port is outside the dedicated 29000..29999 range",
        )
        _require(
            port not in denied_ports,
            "host.port.production",
            "selected port is reserved by production",
        )
        _require(
            record.get("available") is True,
            "host.port.occupied",
            "selected port was not observed available",
        )
        _require(
            record.get("owner_run_id") == run_id,
            "host.port.owner",
            "selected port lease is not owned by this run",
        )
        _validate_provenance(
            record.get("provenance"), "host.port.provenance"
        )
        observed_at = _parse_time(
            record.get("observed_at"), "host.port.observed_at"
        )
        lease_expires_at = _parse_time(
            record.get("lease_expires_at"), "host.port.lease_expires_at"
        )
        _require(
            0 <= (current - observed_at).total_seconds() <= 60,
            "host.port.freshness",
            "port observation is future-dated or older than one minute",
        )
        _require(
            lease_expires_at >= expires_at,
            "host.port.lease",
            "port lease does not remain valid through attestation expiry",
        )
        selected_ports[purpose] = port
        selected_port_reservations[purpose] = {
            "port": port,
            "provenance": record["provenance"],
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "lease_expires_at": lease_expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "owner_run_id": run_id,
        }
    _require(
        set(selected_ports) == {"cache", "daemon"},
        "host.ports",
        "cache and daemon reservations are both required",
    )
    _require(
        selected_ports["cache"] != selected_ports["daemon"],
        "host.ports.alias",
        "cache and daemon ports must be distinct",
    )

    production_networks = _validate_inventory(
        inventories.get("production_networks"),
        "inventory.production_networks",
        current=current,
    )
    production_network_ids = {
        _require_string(
            item, "inventory.production_networks.namespace_id"
        )
        for item in _require_sequence(
            production_networks.get("namespace_ids"),
            "inventory.production_networks.namespace_ids",
        )
    }
    _require(
        bool(production_network_ids),
        "inventory.production_networks.namespace_ids",
        "production network namespace inventory must be nonempty",
    )
    production_network_names = {
        _require_string(item, "inventory.production_networks.identity")
        for item in _require_sequence(
            production_networks.get("identities"),
            "inventory.production_networks.identities",
        )
    }
    network = _require_mapping(observation.get("network"), "host.network")
    network_identity = _validate_safe_id(
        network.get("identity"), "host.network.identity"
    )
    _require(
        network_identity == f"obs-live-test-{run_id}",
        "host.network.identity",
        "network identity is not the run-owned formal-test network",
    )
    _require(
        network.get("mode") == "private",
        "host.network.mode",
        "host or shared network mode is forbidden",
    )
    _require(
        network.get("owner_run_id") == run_id,
        "host.network.owner",
        "network lease is not owned by this run",
    )
    network_provenance = _validate_provenance(
        network.get("provenance"), "host.network.provenance"
    )
    network_observed_at = _parse_time(
        network.get("observed_at"), "host.network.observed_at"
    )
    network_lease_expires_at = _parse_time(
        network.get("lease_expires_at"), "host.network.lease_expires_at"
    )
    _require(
        0 <= (current - network_observed_at).total_seconds() <= 60,
        "host.network.freshness",
        "network observation is future-dated or older than one minute",
    )
    _require(
        network_lease_expires_at >= expires_at,
        "host.network.lease",
        "network lease does not remain valid through attestation expiry",
    )
    _require(
        network_identity not in production_network_names,
        "host.network.production",
        "selected network identity is production-owned",
    )

    mounts = _require_sequence(observation.get("mounts"), "host.mounts")
    expected_mounts = {
        "source": (source_host_path, CONTAINER_SOURCE_ROOT, "ro"),
        "run": (run_root, CONTAINER_RUN_ROOT, "rw"),
        "secret": (secret_host_path, CONTAINER_SECRET_FILE, "ro"),
        "attestation": (
            attestation_host_path,
            CONTAINER_ATTESTATION_FILE,
            "ro",
        ),
    }
    mount_names: set[str] = set()
    normalized_mounts: list[dict[str, Any]] = []
    for item in mounts:
        record = _require_mapping(item, "host.mount")
        name = _require_string(record.get("name"), "host.mount.name")
        _require(
            name in expected_mounts and name not in mount_names,
            "host.mount.name",
            "mount plan is missing, duplicated, or unknown",
        )
        source_path, target_path, mode = expected_mounts[name]
        _require(
            _resolved(
                _require_string(record.get("source"), "host.mount.source")
            )
            == _resolved(source_path),
            "host.mount.source",
            "mount source differs from the host-observed binding",
        )
        _require(
            _resolved(
                _require_string(record.get("target"), "host.mount.target")
            )
            == _resolved(target_path),
            "host.mount.target",
            "mount target differs from the canonical binding",
        )
        _require(
            record.get("mode") == mode,
            "host.mount.mode",
            "mount mode differs from the canonical binding",
        )
        _require(
            record.get("observed") is True,
            "host.mount.observed",
            "mount plan was not independently inspected",
        )
        _require(
            record.get("owner_run_id") == run_id,
            "host.mount.owner",
            "mount plan is not owned by this run",
        )
        if mode == "rw":
            _require(
                name == "run",
                "host.mount.shared_writable",
                "only the exact run root may be writable",
            )
        mount_names.add(name)
        normalized_mounts.append(
            {
                "name": name,
                "source": str(source_path),
                "target": str(target_path),
                "mode": mode,
            }
        )
    _require(
        mount_names == set(expected_mounts),
        "host.mounts",
        "the exact source/run/secret/attestation mount plan is required",
    )

    payload = {
        "run_id": run_id,
        "nonce": nonce,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "observer": dict(observer),
        "runtime_owner": {"uid": runtime_uid, "gid": runtime_gid},
        "host_run_root": str(run_root),
        "attestation_host_path": str(attestation_host_path),
        "source": {
            "host_path": str(source_host_path),
            "target_path": str(CONTAINER_SOURCE_ROOT),
            "requested_commit": requested_commit,
            "observed_commit": observed_commit,
            "tree_digest": source_tree_digest,
            "executable_path": str(executable_path),
            "executable_digest": executable_digest,
        },
        "image": {
            "reference": image_reference,
            "digest": image_digest,
            "container_marker": EXPECTED_CONTAINER_MARKER,
            "service_name": EXPECTED_SERVICE_NAME,
            "runtime_executable_path": str(image_runtime_executable_path),
            "runtime_executable_digest": image_runtime_executable_digest,
        },
        "mounts": sorted(normalized_mounts, key=lambda item: item["name"]),
        "host_topology": dict(host_topology_items),
        "container_topology": {
            name: str(path)
            for name, path in _topology(CONTAINER_RUN_ROOT).items()
        },
        "production_roots": [
            str(path) for path in sorted(production_roots, key=str)
        ],
        "shared_roots": [
            str(path) for path in sorted(shared_roots, key=str)
        ],
        "credential": {
            "credential_id": credential_id,
            "test_bot_id": test_bot_id,
            "poller_lease_id": expected_poller_lease_id,
            "credential_class": "test",
            "fingerprint": credential_fingerprint,
            "provenance": credential_provenance,
            "expires_at": credential_expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "authentication_status": "valid",
            "secret_host_path": str(secret_host_path),
            "secret_target_path": str(CONTAINER_SECRET_FILE),
            "secret_owner_uid": runtime_uid,
            "secret_owner_gid": runtime_gid,
        },
        "poller_inventory": {
            "provenance": poller_inventory["provenance"],
            "observed_at": poller_inventory["observed_at"],
            "complete": True,
            "lease_epoch": poller_inventory["lease_epoch"],
            "selected_bot_lease_id": expected_poller_lease_id,
            "selected_bot_lease_expires_at": selected_bot_lease_expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
        },
        "network": {
            "identity": network_identity,
            "mode": "private",
            "owner_run_id": run_id,
            "provenance": network_provenance,
            "observed_at": network_observed_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "lease_expires_at": network_lease_expires_at.isoformat().replace(
                "+00:00", "Z"
            ),
            "production_namespace_ids": sorted(production_network_ids),
        },
        "ports": selected_ports,
        "port_reservations": selected_port_reservations,
    }
    payload_json = _canonical_json(payload)
    return HostAttestation(
        payload_json=payload_json, integrity=_integrity(payload)
    )


def verify_attestation(
    attestation: HostAttestation, *, now: datetime | None = None
) -> dict[str, Any]:
    payload = attestation.payload
    _require(
        _integrity(payload) == attestation.integrity,
        "attestation.integrity",
        "host attestation integrity check failed",
    )
    _require(
        bool(SHA256_RE.fullmatch(attestation.integrity)),
        "attestation.integrity",
        "attestation integrity format is invalid",
    )
    current = _now(now)
    issued_at = _parse_time(
        payload.get("issued_at"), "attestation.issued_at"
    )
    expires_at = _parse_time(
        payload.get("expires_at"), "attestation.expires_at"
    )
    _require(
        issued_at <= current < expires_at,
        "attestation.expired",
        "host attestation is not currently valid",
    )
    return payload


def load_host_attestation(
    path: Path = CONTAINER_ATTESTATION_FILE,
) -> HostAttestation:
    """Load only the exact read-only attestation path; no arbitrary override."""
    _require(
        _resolved(path) == _resolved(CONTAINER_ATTESTATION_FILE),
        "attestation.path",
        "attestation must use the canonical container path",
    )
    _require(
        path.is_file() and not path.is_symlink(),
        "attestation.file",
        "attestation is missing, non-regular, or a symlink",
    )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    record = _require_mapping(loaded, "attestation.document")
    _require(
        record.get("schema") == ATTESTATION_SCHEMA,
        "attestation.schema",
        "attestation schema does not match",
    )
    payload = _require_mapping(record.get("payload"), "attestation.payload")
    integrity = _validate_digest(
        record.get("integrity"), "attestation.integrity"
    )
    return HostAttestation(
        payload_json=_canonical_json(payload), integrity=integrity
    )


def _expected_runtime_environment(
    payload: Mapping[str, Any],
) -> dict[str, str]:
    topology = _require_mapping(
        payload.get("container_topology"), "attestation.container_topology"
    )
    ports = _require_mapping(payload.get("ports"), "attestation.ports")
    return {
        "HOME": str(topology["home"]),
        "CLAUDE_CONFIG_DIR": str(topology["claude_config"]),
        "OBS_CLAUDE_PROJECTS_DIR": str(topology["claude_projects"]),
        "OBS_CLAUDE_TEAMS_DIR": str(topology["claude_teams"]),
        "OBS_CLAUDE_SESSIONS_DIR": str(topology["claude_sessions"]),
        "OBS_CLAUDE_AUTH_FILE": str(topology["claude_auth"]),
        "CLAUDE_PROJECTS_DIR": str(topology["claude_projects"]),
        "CLAUDE_TEAMS_DIR": str(topology["claude_teams"]),
        "CLAUDE_SESSIONS_DIR": str(topology["claude_sessions"]),
        "CLAUDE_AUTH_FILE": str(topology["claude_auth"]),
        "OBS_TEST_PROJECT_ROOT": str(topology["project"]),
        "OBS_VAULT_PATH": str(topology["fixture"]),
        "OBS_TELEGRAM_STATE_DB_PATH": str(topology["state_db"]),
        "OBS_TELEGRAM_STATE_WAL_PATH": str(topology["wal"]),
        "XDG_CACHE_HOME": str(topology["cache_root"]),
        "OBS_CACHE_DATA_DIR": str(topology["cache_data"]),
        "CACHE_PROXY_LOG_DIR": str(topology["cache_log"]),
        "TMPDIR": str(topology["temp"]),
        "OBS_TELEGRAM_TEMP_ROOT": str(topology["download"]),
        "OBS_RUNTIME_LOG_FILE": str(topology["runtime_log"]),
        "OBS_TELEGRAM_LOG_FILE": str(topology["telegram_log"]),
        "OBS_TEST_EVIDENCE_ROOT": str(topology["evidence"]),
        "OBS_DAEMON_METADATA_DIR": str(topology["daemon_metadata"]),
        "OBS_DAEMON_PID_FILE": str(topology["daemon_pid"]),
        "OBS_DAEMON_LOCK_FILE": str(topology["daemon_lock"]),
        "OBS_TEST_OWNERSHIP_MARKER": str(topology["ownership_marker"]),
        "OBS_TEST_SOURCE_SNAPSHOT": str(CONTAINER_SOURCE_ROOT),
        "OBS_TEST_SECRET_FILE": str(CONTAINER_SECRET_FILE),
        "OBS_TEST_ATTESTATION": str(CONTAINER_ATTESTATION_FILE),
        "OBS_CACHE_PROXY_PORT": str(ports["cache"]),
        "OBS_DAEMON_PORT": str(ports["daemon"]),
    }


def inner_preflight(
    attestation: HostAttestation,
    adapter: InnerObservationAdapter,
    *,
    now: datetime | None = None,
) -> PreflightDecision:
    """Verify actual container facts before any application factory."""
    payload = verify_attestation(attestation, now=now)
    observation = _require_mapping(
        adapter.observe(attestation), "inner.observation"
    )
    observer = _require_mapping(observation.get("observer"), "inner.observer")
    _require(
        observer.get("kind") == INNER_OBSERVER_KIND,
        "inner.observer.kind",
        "inner facts did not come from the runtime observer",
    )
    _validate_provenance(
        observer.get("provenance"), "inner.observer.provenance"
    )
    observer_observed_at = _parse_time(
        observer.get("observed_at"), "inner.observer.observed_at"
    )
    current = _now(now)
    _require(
        0 <= (current - observer_observed_at).total_seconds() <= 60,
        "inner.observer.freshness",
        "inner observation is future-dated or older than one minute",
    )
    _require(
        observation.get("attestation_integrity") == attestation.integrity,
        "inner.attestation",
        "inner observation is not bound to this attestation",
    )
    _require(
        observation.get("run_id") == payload["run_id"],
        "inner.run_id",
        "inner observation is bound to another run",
    )
    _require(
        observation.get("nonce") == payload["nonce"],
        "inner.nonce",
        "inner observation nonce differs",
    )
    _require(
        observation.get("container_marker") == EXPECTED_CONTAINER_MARKER,
        "inner.marker",
        "dedicated container marker is missing or wrong",
    )
    _require(
        observation.get("service_name") == EXPECTED_SERVICE_NAME,
        "inner.service",
        "dedicated service identity is missing or wrong",
    )
    _require(
        observation.get("hostname") == EXPECTED_SERVICE_NAME,
        "inner.hostname",
        "container hostname is not the dedicated service",
    )
    _require(
        observation.get("hostname") != PRODUCTION_CONTAINER_NAME,
        "inner.production_container",
        "production-serving obs-test is never a formal test lane",
    )
    _require(
        observation.get("profile") == "test",
        "inner.profile",
        "test profile is required only as a secondary assertion",
    )
    _require(
        observation.get("production_mode") in {None, ""},
        "inner.production_mode",
        "production mode is forbidden",
    )
    runtime_owner = _require_mapping(
        payload.get("runtime_owner"), "attestation.runtime_owner"
    )
    _require(
        observation.get("process_uid") == runtime_owner["uid"]
        and observation.get("process_gid") == runtime_owner["gid"],
        "inner.runtime_owner",
        "inner process UID/GID differs from the host-attested runtime owner",
    )

    source = _require_mapping(payload["source"], "attestation.source")
    _require(
        _resolved(
            _require_string(observation.get("source_path"), "inner.source_path")
        )
        == CONTAINER_SOURCE_ROOT,
        "inner.source_path",
        "runtime source path differs from the attested mount",
    )
    _require(
        _validate_digest(
            observation.get("source_tree_digest"),
            "inner.source_tree_digest",
        )
        == source["tree_digest"],
        "inner.source_tree_mismatch",
        "mounted source digest differs from the host snapshot",
    )
    _require(
        _resolved(
            _require_string(
                observation.get("executable_path"), "inner.executable_path"
            )
        )
        == _resolved(source["executable_path"]),
        "inner.executable_path",
        "executed code path differs from the attested snapshot path",
    )
    _require(
        _validate_digest(
            observation.get("executable_digest"),
            "inner.executable_digest",
        )
        == source["executable_digest"],
        "inner.executable_mismatch",
        "executed code digest differs from the attested source",
    )
    image = _require_mapping(payload["image"], "attestation.image")
    _require(
        _validate_digest(
            observation.get("image_digest"), "inner.image_digest"
        )
        == image["digest"],
        "inner.image_mismatch",
        "runtime image identity differs from the host-selected image",
    )
    _require(
        _resolved(
            _require_string(
                observation.get("runtime_executable_path"),
                "inner.runtime_executable_path",
            )
        )
        == _resolved(image["runtime_executable_path"]),
        "inner.runtime_executable_path",
        "runtime executable path differs from the host-observed image",
    )
    _require(
        _validate_digest(
            observation.get("runtime_executable_digest"),
            "inner.runtime_executable_digest",
        )
        == image["runtime_executable_digest"],
        "inner.runtime_executable_mismatch",
        "runtime executable bytes differ from the host-observed image",
    )
    _require(
        _resolved(
            _require_string(
                observation.get("python_executable_path"),
                "inner.python_executable_path",
            )
        )
        == _resolved(
            _require_string(
                observation.get("runtime_executable_path"),
                "inner.runtime_executable_path",
            )
        ),
        "inner.runtime_executable_binding",
        "Python reports an executable different from the measured process image",
    )

    observed_mounts = _record_by_name(
        _require_sequence(observation.get("mounts"), "inner.mounts"),
        "inner.mount",
    )
    attested_mounts = _record_by_name(
        _require_sequence(payload.get("mounts"), "attestation.mounts"),
        "attestation.mount",
    )
    _require(
        set(observed_mounts) == set(attested_mounts),
        "inner.mounts",
        "runtime mount set differs from the attested plan",
    )
    for name, attested in attested_mounts.items():
        actual = observed_mounts[name]
        _require(
            _resolved(
                _require_string(
                    actual.get("target"), f"inner.mount.{name}.target"
                )
            )
            == _resolved(attested["target"]),
            f"inner.mount.{name}.target",
            "runtime mount target differs",
        )
        _require(
            actual.get("mode") == attested["mode"],
            f"inner.mount.{name}.mode",
            "runtime mount mode differs",
        )
        _require(
            actual.get("symlink") is False,
            f"inner.mount.{name}.symlink",
            "runtime mount target is a symlink",
        )

    expected_container_topology = {
        name: Path(path)
        for name, path in _require_mapping(
            payload["container_topology"], "attestation.container_topology"
        ).items()
    }
    topology_items = _validate_topology_records(
        _require_sequence(observation.get("topology"), "inner.topology"),
        expected=expected_container_topology,
        run_id=payload["run_id"],
        owner_uid=runtime_owner["uid"],
        owner_gid=runtime_owner["gid"],
        code_prefix="inner.topology",
    )
    unsafe_container_roots = (
        CONTAINER_SOURCE_ROOT,
        CONTAINER_SECRET_FILE.parent,
        CONTAINER_ATTESTATION_FILE.parent,
    )
    for _name, path_text in topology_items:
        _require(
            not any(
                _paths_overlap(path_text, root)
                for root in unsafe_container_roots
            ),
            "inner.topology.unsafe",
            "writable topology overlaps source, secret, or attestation mounts",
        )

    expected_environment = _expected_runtime_environment(payload)
    observed_environment = _require_mapping(
        observation.get("runtime_environment"), "inner.runtime_environment"
    )
    for key, expected in expected_environment.items():
        _require(
            observed_environment.get(key) == expected,
            f"inner.environment.{key}",
            "runtime setting is not wired to the verified topology",
        )
    _require(
        observation.get("repository_env_present") is False,
        "inner.repository_env",
        "repository .env is forbidden in the formal lane",
    )
    _require(
        not observation.get("production_env_keys"),
        "inner.production_env",
        "production environment keys are populated",
    )
    _require(
        not observation.get("ambient_credential_keys"),
        "inner.ambient_credentials",
        "ambient credential variables are populated outside the mounted test secret",
    )

    credential = _require_mapping(
        payload["credential"], "attestation.credential"
    )
    _require(
        observation.get("secret_regular_file") is True,
        "inner.secret.type",
        "mounted test secret is not a regular file",
    )
    _require(
        observation.get("secret_symlink") is False,
        "inner.secret.symlink",
        "mounted test secret is a symlink",
    )
    _require(
        str(observation.get("secret_mode")) in {"0400", "0440"},
        "inner.secret.mode",
        "mounted test secret permissions are broader than approved read-only access",
    )
    _require(
        observation.get("secret_owner_uid") == credential["secret_owner_uid"]
        == runtime_owner["uid"]
        and observation.get("secret_owner_gid")
        == credential["secret_owner_gid"]
        == runtime_owner["gid"],
        "inner.secret.owner",
        "mounted test secret ownership differs from the host measurement or runtime owner",
    )
    _require(
        _validate_digest(
            observation.get("secret_fingerprint"),
            "inner.secret.fingerprint",
        )
        == credential["fingerprint"],
        "inner.secret.mismatch",
        "mounted test secret differs from the host-measured secret",
    )

    network = _require_mapping(payload["network"], "attestation.network")
    _validate_provenance(
        network.get("provenance"), "attestation.network.provenance"
    )
    attestation_expires_at = _parse_time(
        payload.get("expires_at"), "attestation.expires_at"
    )
    _require(
        _parse_time(
            network.get("lease_expires_at"),
            "attestation.network.lease_expires_at",
        )
        >= attestation_expires_at,
        "attestation.network.lease",
        "attested network lease no longer covers attestation expiry",
    )
    _require(
        observation.get("network_identity") == network["identity"],
        "inner.network.identity",
        "runtime network identity differs",
    )
    namespace_id = _require_string(
        observation.get("network_namespace_id"),
        "inner.network.namespace_id",
    )
    _require(
        namespace_id not in set(network["production_namespace_ids"]),
        "inner.network.production",
        "runtime joined a production network namespace",
    )
    _require(
        observation.get("network_mode") == "private",
        "inner.network.mode",
        "runtime network is host/shared rather than private",
    )

    host_topology = _require_mapping(
        payload["host_topology"], "attestation.host_topology"
    )
    ports = _require_mapping(payload["ports"], "attestation.ports")
    port_reservations = _require_mapping(
        payload.get("port_reservations"), "attestation.port_reservations"
    )
    _require(
        set(port_reservations) == {"cache", "daemon"},
        "attestation.port_reservations",
        "attestation does not retain both selected port reservations",
    )
    for purpose, selected_port in ports.items():
        reservation = _require_mapping(
            port_reservations.get(purpose),
            f"attestation.port_reservations.{purpose}",
        )
        _require(
            reservation.get("port") == selected_port
            and reservation.get("owner_run_id") == payload["run_id"],
            f"attestation.port_reservations.{purpose}.binding",
            "selected port is not bound to its run-owned reservation",
        )
        _validate_provenance(
            reservation.get("provenance"),
            f"attestation.port_reservations.{purpose}.provenance",
        )
        _require(
            _parse_time(
                reservation.get("lease_expires_at"),
                f"attestation.port_reservations.{purpose}.lease_expires_at",
            )
            >= attestation_expires_at,
            f"attestation.port_reservations.{purpose}.lease_lifetime",
            "selected port lease no longer covers attestation expiry",
        )
    poller = _require_mapping(
        payload.get("poller_inventory"), "attestation.poller_inventory"
    )
    _require(
        poller.get("complete") is True
        and _validate_safe_id(
            poller.get("selected_bot_lease_id"),
            "attestation.poller_inventory.selected_bot_lease_id",
        )
        == _validate_safe_id(
            credential.get("poller_lease_id"),
            "attestation.credential.poller_lease_id",
        )
        and _parse_time(
            poller.get("selected_bot_lease_expires_at"),
            "attestation.poller_inventory.selected_bot_lease_expires_at",
        )
        >= attestation_expires_at,
        "attestation.poller_inventory.binding",
        "selected poller lease is incomplete, unbound, or expires too early",
    )
    return PreflightDecision(
        run_id=payload["run_id"],
        nonce=payload["nonce"],
        attestation_integrity=attestation.integrity,
        service_name=EXPECTED_SERVICE_NAME,
        container_marker=EXPECTED_CONTAINER_MARKER,
        host_run_root=Path(payload["host_run_root"]),
        run_root=CONTAINER_RUN_ROOT,
        source_host_path=Path(source["host_path"]),
        source_snapshot=CONTAINER_SOURCE_ROOT,
        source_commit=source["observed_commit"],
        source_tree_digest=source["tree_digest"],
        image_digest=image["digest"],
        executable_path=Path(source["executable_path"]),
        executable_digest=source["executable_digest"],
        topology_items=topology_items,
        host_topology_items=tuple(
            (name, str(host_topology[name])) for name in TOPOLOGY_LAYOUT
        ),
        runtime_environment_items=tuple(sorted(expected_environment.items())),
        port_items=(
            ("cache", int(ports["cache"])),
            ("daemon", int(ports["daemon"])),
        ),
        production_roots=tuple(
            Path(item) for item in payload["production_roots"]
        ),
        shared_roots=tuple(Path(item) for item in payload["shared_roots"]),
        test_bot_id=credential["test_bot_id"],
        credential_id=credential["credential_id"],
        credential_fingerprint=credential["fingerprint"],
        credential_class=credential["credential_class"],
        credential_provenance=credential["provenance"],
        credential_expires_at=credential["expires_at"],
        network_identity=network["identity"],
        network_namespace_id=namespace_id,
        runtime_uid=int(runtime_owner["uid"]),
        runtime_gid=int(runtime_owner["gid"]),
        reasons=(
            "host attestation and independent inner observation matched",
        ),
    )


def build_runtime_policy(decision: PreflightDecision) -> RuntimePolicy:
    """Convert a complete immutable decision into the sole factory input."""
    topology = decision.topology
    _require(
        set(topology) == set(TOPOLOGY_LAYOUT),
        "runtime_policy.topology",
        "runtime policy does not contain the complete canonical topology",
    )
    expected_environment = _expected_runtime_environment(
        {
            "container_topology": {
                name: str(topology[name]) for name in TOPOLOGY_LAYOUT
            },
            "ports": decision.ports,
        }
    )
    _require(
        decision.runtime_environment == expected_environment,
        "runtime_policy.environment",
        "runtime policy environment is not derived from the decision topology",
    )
    return RuntimePolicy(
        decision=decision,
        topology_items=decision.topology_items,
        environment_items=decision.runtime_environment_items,
        port_items=decision.port_items,
    )


def guarded_launch(
    attestation: HostAttestation,
    adapter: InnerObservationAdapter,
    *,
    process_factory: Callable[[RuntimePolicy], Any],
    network_factory: Callable[[RuntimePolicy], Any],
    poller_factory: Callable[[RuntimePolicy], Any],
) -> tuple[PreflightDecision, Any, Any, Any]:
    """Pass one decision-derived policy to every post-preflight factory."""
    decision = inner_preflight(attestation, adapter)
    policy = build_runtime_policy(decision)
    apply_runtime_environment(policy)
    process = process_factory(policy)
    network = network_factory(policy)
    poller = poller_factory(policy)
    return decision, process, network, poller


def apply_runtime_environment(policy: RuntimePolicy) -> None:
    """Apply the exact verified topology before any later factory is called."""
    for key, value in policy.environment.items():
        os.environ[key] = value


def fingerprint_secret(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not parsed.netloc:
        return value
    host = parsed.hostname or "redacted-host"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    query = urlencode(
        [
            (key, "<redacted>")
            if QUERY_KEYS.match(key)
            else (key, redact_text(item))
            for key, item in parse_qsl(
                parsed.query, keep_blank_values=True
            )
        ]
    )
    path = TELEGRAM_TOKEN_RE.sub("<redacted-token>", parsed.path)
    fragment = "<redacted>" if parsed.fragment else ""
    return urlunsplit((parsed.scheme, host, path, query, fragment))


def redact_text(value: str) -> str:
    """Redact credentials from URLs, assignments, headers, commands, and errors."""
    redacted = re.sub(
        r"https?://[^\s<>\"']+",
        lambda match: _redact_url(match.group(0)),
        value,
    )
    redacted = AUTH_HEADER_RE.sub(r"\1<redacted>", redacted)
    redacted = ASSIGNMENT_RE.sub(r"\1<redacted>", redacted)
    redacted = COMMAND_VALUE_RE.sub(r"\1<redacted>", redacted)
    redacted = TELEGRAM_TOKEN_RE.sub("<redacted-token>", redacted)
    redacted = EMAIL_RE.sub("<redacted-email>", redacted)
    return redacted


def redact_value(
    value: Any, *, key: str = "", path: tuple[str, ...] = ()
) -> Any:
    current_path = (*path, key) if key else path
    in_credentials = "credentials" in current_path
    if key and SENSITIVE_KEY_RE.search(key):
        if not (
            (key == "credentials" and isinstance(value, Mapping))
            or (in_credentials and key in SAFE_CREDENTIAL_EVIDENCE_FIELDS)
        ):
            return "<redacted>"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(
                item, key=str(item_key), path=current_path
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item, path=current_path) for item in value]
    return value


def build_evidence_manifest(
    *,
    decision: PreflightDecision,
    lane: str,
    scenario: str,
    command: Sequence[str],
) -> dict[str, Any]:
    core = {
        "status": "planned_not_run",
        "run_id": decision.run_id,
        "timestamps": {
            "started_at": "not run",
            "finished_at": "not run",
        },
        "source": {
            "commit": decision.source_commit,
            "tree_digest": decision.source_tree_digest,
            "executable_digest": decision.executable_digest,
        },
        "container": {
            "service_name": decision.service_name,
            "marker": decision.container_marker,
            "image_digest": decision.image_digest,
            "container_id": "not run",
        },
        "credentials": {
            "test_bot_id": decision.test_bot_id,
            "credential_id": decision.credential_id,
            "fingerprint": decision.credential_fingerprint,
            "credential_class": decision.credential_class,
            "provenance": decision.credential_provenance,
            "expires_at": decision.credential_expires_at,
            "expiry_status": "host-observed-valid-at-attestation",
            "authentication_status": "host-observed-valid-at-attestation",
        },
        "model": EXPECTED_LIVE_MODEL,
        "lane": lane,
        "scenario": scenario,
        "command": list(command),
        "return_code": "not run",
        "preflight": decision.as_dict(),
        "production_state": {
            "before": "not run",
            "during": "not run",
            "after": "not run",
        },
        "cleanup": {
            "status": "not run",
            "remaining_resources": "not run",
        },
        "logs": "not run",
        "secret_scan": "not run",
    }
    redacted = redact_value(core)
    redacted["checksums"] = {"manifest_payload": _integrity(redacted)}
    return redacted


def _assert_no_symlink_components(path: Path, stop: Path) -> None:
    current = path
    stop_resolved = _resolved(stop)
    _require(
        is_within(_resolved(path), stop_resolved),
        "path.traversal",
        "path traversal escaped the approved root",
    )
    while True:
        _require(
            not current.is_symlink(),
            "path.symlink",
            "symlink components are forbidden",
        )
        if _resolved(current) == stop_resolved:
            return
        _require(
            current.parent != current,
            "path.escape",
            "path escaped the approved root",
        )
        current = current.parent


def _verify_ownership_marker(
    path: Path, decision: PreflightDecision
) -> None:
    container_marker = decision.topology["ownership_marker"]
    host_marker = decision.host_topology["ownership_marker"]
    retention_marker = (
        HOST_EVIDENCE_RETENTION_BASE
        / decision.run_id
        / "ownership.json"
    )
    staged_retention_marker = decision.host_topology["evidence"] / "ownership.json"
    _require(
        path
        in {
            container_marker,
            host_marker,
            staged_retention_marker,
            retention_marker,
        },
        "ownership.marker_path",
        "ownership marker is not one of the exact decision-bound paths",
    )
    _require(
        path.is_file() and not path.is_symlink(),
        "ownership.marker",
        "run ownership marker is missing or unsafe",
    )
    marker_stat = path.lstat()
    _require(
        stat.S_ISREG(marker_stat.st_mode),
        "ownership.marker_type",
        "run ownership marker is not a regular file",
    )
    _require(
        f"{marker_stat.st_mode & 0o7777:04o}" == "0600",
        "ownership.marker_mode",
        "run ownership marker permissions differ from the canonical mode",
    )
    _require(
        marker_stat.st_uid == decision.runtime_uid
        and marker_stat.st_gid == decision.runtime_gid,
        "ownership.marker_owner",
        "run ownership marker UID/GID differ from the attested runtime owner",
    )
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError(
            "ownership.marker_document",
            "run ownership marker is not readable canonical JSON",
        ) from exc
    _require(
        isinstance(marker, dict),
        "ownership.marker_document",
        "run ownership marker is not a JSON object",
    )
    _require(
        marker.get("run_id") == decision.run_id,
        "ownership.run_id",
        "ownership marker belongs to another run",
    )
    _require(
        marker.get("nonce") == decision.nonce,
        "ownership.nonce",
        "ownership marker nonce differs",
    )
    _require(
        marker.get("attestation_integrity")
        == decision.attestation_integrity,
        "ownership.attestation",
        "ownership marker is not bound to the launch decision",
    )
    expected_integrities = {
        "host_topology_integrity": _integrity(
            {
                name: str(decision.host_topology[name])
                for name in TOPOLOGY_LAYOUT
            }
        ),
        "container_topology_integrity": _integrity(
            {
                name: str(decision.topology[name])
                for name in TOPOLOGY_LAYOUT
            }
        ),
    }
    _require(
        all(
            marker.get(key) == value
            for key, value in expected_integrities.items()
        ),
        "ownership.topology",
        "ownership marker is not bound to the decision topology",
    )


def write_evidence_manifest(
    decision: PreflightDecision, manifest: Mapping[str, Any]
) -> Path:
    """Exclusively and atomically create the approved evidence manifest."""
    evidence_root = decision.evidence_root
    _require(
        _resolved(evidence_root)
        == _resolved(CONTAINER_RUN_ROOT / "evidence"),
        "evidence.root",
        "evidence root differs from the approved topology",
    )
    _require(
        evidence_root.is_dir() and not evidence_root.is_symlink(),
        "evidence.root",
        "approved evidence root is missing or a symlink",
    )
    _assert_no_symlink_components(evidence_root, CONTAINER_RUN_ROOT)
    _verify_ownership_marker(
        decision.topology["ownership_marker"], decision
    )
    for unsafe in (
        decision.source_snapshot,
        CONTAINER_SECRET_FILE.parent,
        CONTAINER_ATTESTATION_FILE.parent,
    ):
        _require(
            not _paths_overlap(evidence_root, unsafe),
            "evidence.unsafe",
            "evidence overlaps source, secret, or attestation state",
        )

    _require(
        hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"),
        "evidence.no_follow",
        "directory-descriptor no-follow support is required",
    )
    target_name = "manifest.json"
    temp_name = f".manifest-{decision.nonce}.tmp"
    target = evidence_root / target_name
    payload = (
        json.dumps(
            redact_value(manifest),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        ).encode("utf-8")
        + b"\n"
    )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    run_fd = os.open(CONTAINER_RUN_ROOT, directory_flags)
    evidence_fd = -1
    file_fd = -1
    temp_created = False
    target_created = False
    try:
        run_stat = os.fstat(run_fd)
        visible_run_stat = os.stat(
            CONTAINER_RUN_ROOT, follow_symlinks=False
        )
        _require(
            (run_stat.st_dev, run_stat.st_ino)
            == (visible_run_stat.st_dev, visible_run_stat.st_ino),
            "evidence.run_root_race",
            "run root changed while evidence directory was opened",
        )
        evidence_fd = os.open(
            "evidence", directory_flags, dir_fd=run_fd
        )
        evidence_stat = os.fstat(evidence_fd)
        visible_evidence_stat = os.stat(
            "evidence", dir_fd=run_fd, follow_symlinks=False
        )
        _require(
            (evidence_stat.st_dev, evidence_stat.st_ino)
            == (visible_evidence_stat.st_dev, visible_evidence_stat.st_ino),
            "evidence.root_race",
            "evidence root changed while its descriptor was opened",
        )
        for name, code, message in (
            (
                target_name,
                "evidence.exists",
                "existing evidence is never overwritten",
            ),
            (
                temp_name,
                "evidence.temp_exists",
                "exclusive evidence temporary path already exists",
            ),
        ):
            try:
                os.stat(name, dir_fd=evidence_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise PreflightError(code, message)

        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            file_flags |= os.O_CLOEXEC
        file_fd = os.open(
            temp_name, file_flags, 0o600, dir_fd=evidence_fd
        )
        temp_created = True
        with os.fdopen(file_fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(file_fd)
        file_fd = -1
        os.link(
            temp_name,
            target_name,
            src_dir_fd=evidence_fd,
            dst_dir_fd=evidence_fd,
            follow_symlinks=False,
        )
        target_created = True
        os.unlink(temp_name, dir_fd=evidence_fd)
        temp_created = False
        final_run_stat = os.stat(
            CONTAINER_RUN_ROOT, follow_symlinks=False
        )
        final_evidence_stat = os.stat(
            "evidence", dir_fd=run_fd, follow_symlinks=False
        )
        _require(
            (run_stat.st_dev, run_stat.st_ino)
            == (final_run_stat.st_dev, final_run_stat.st_ino)
            and (evidence_stat.st_dev, evidence_stat.st_ino)
            == (final_evidence_stat.st_dev, final_evidence_stat.st_ino),
            "evidence.path_race",
            "evidence path changed before publication completed",
        )
        os.fsync(evidence_fd)
    except Exception:
        if file_fd >= 0:
            os.close(file_fd)
        if target_created and evidence_fd >= 0:
            try:
                os.unlink(target_name, dir_fd=evidence_fd)
            except FileNotFoundError:
                pass
        if temp_created and evidence_fd >= 0:
            try:
                os.unlink(temp_name, dir_fd=evidence_fd)
            except FileNotFoundError:
                pass
        if evidence_fd >= 0:
            os.fsync(evidence_fd)
        raise
    finally:
        if evidence_fd >= 0:
            os.close(evidence_fd)
        os.close(run_fd)
    return target


def create_run_root(
    run_id: str, nonce: str, *, owner_uid: int, owner_gid: int
) -> Path:
    """Create only the canonical host topology before host observation."""
    _require(
        bool(RUN_ID_RE.fullmatch(run_id)),
        "setup.run_id",
        "run ID format is invalid",
    )
    _require(
        bool(NONCE_RE.fullmatch(nonce)),
        "setup.nonce",
        "nonce format is invalid",
    )
    _require(
        isinstance(owner_uid, int)
        and not isinstance(owner_uid, bool)
        and owner_uid > 0
        and isinstance(owner_gid, int)
        and not isinstance(owner_gid, bool)
        and owner_gid > 0,
        "setup.owner",
        "run root owner must be explicit non-root UID/GID",
    )
    run_root = HOST_RUN_BASE / run_id
    _require(
        not run_root.exists() and not run_root.is_symlink(),
        "setup.exists",
        "run root must be newly and exclusively created",
    )
    run_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chown(run_root, owner_uid, owner_gid)
    topology = _topology(run_root)
    for name, path in topology.items():
        _relative, kind, mode_text = TOPOLOGY_LAYOUT[name]
        if kind == "directory":
            path.mkdir(mode=int(mode_text, 8), exist_ok=False)
            os.chown(path, owner_uid, owner_gid)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, int(mode_text, 8))
            try:
                os.fchown(fd, owner_uid, owner_gid)
            finally:
                os.close(fd)
    topology["ownership_marker"].write_text(
        json.dumps(
            {
                "run_id": run_id,
                "nonce": nonce,
                "attestation_integrity": "pending",
                "host_topology_integrity": "pending",
                "container_topology_integrity": "pending",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_root


def bind_ownership_marker(attestation: HostAttestation) -> Path:
    """Bind the pre-created host marker before service creation."""
    payload = verify_attestation(attestation)
    host_topology = _require_mapping(
        payload.get("host_topology"), "ownership.host_topology"
    )
    run_root = Path(payload["host_run_root"])
    marker = Path(
        _require_string(
            host_topology.get("ownership_marker"), "ownership.marker_path"
        )
    )
    expected = _topology(run_root)["ownership_marker"]
    _require(
        marker == expected,
        "ownership.marker_path",
        "ownership marker is not the exact attested host path",
    )
    runtime_owner = _require_mapping(
        payload.get("runtime_owner"), "ownership.runtime_owner"
    )
    owner_uid = runtime_owner.get("uid")
    owner_gid = runtime_owner.get("gid")
    _require(
        isinstance(owner_uid, int)
        and not isinstance(owner_uid, bool)
        and owner_uid > 0
        and isinstance(owner_gid, int)
        and not isinstance(owner_gid, bool)
        and owner_gid > 0,
        "ownership.runtime_owner",
        "ownership binding requires an attested non-root integer UID/GID",
    )
    _require(
        hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"),
        "ownership.no_follow",
        "directory-descriptor no-follow support is required",
    )
    marker_name = TOPOLOGY_LAYOUT["ownership_marker"][0]
    temporary_name = marker_name + ".bind"
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    run_fd = os.open(run_root, directory_flags)
    fd = -1
    temporary_created = False
    try:
        run_stat = os.fstat(run_fd)
        visible_run_stat = os.stat(run_root, follow_symlinks=False)
        _require(
            (run_stat.st_dev, run_stat.st_ino)
            == (visible_run_stat.st_dev, visible_run_stat.st_ino),
            "ownership.run_root_race",
            "run root changed while ownership binding opened it",
        )
        existing_stat = os.stat(
            marker_name, dir_fd=run_fd, follow_symlinks=False
        )
        _require(
            stat.S_ISREG(existing_stat.st_mode)
            and f"{existing_stat.st_mode & 0o7777:04o}" == "0600"
            and existing_stat.st_uid == owner_uid
            and existing_stat.st_gid == owner_gid,
            "ownership.marker_owner",
            "pre-created ownership marker has wrong type, mode, or UID/GID",
        )
        try:
            os.stat(temporary_name, dir_fd=run_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise PreflightError(
                "ownership.bind_exists",
                "ownership bind temporary path already exists",
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(temporary_name, flags, 0o600, dir_fd=run_fd)
        temporary_created = True
        os.fchown(fd, owner_uid, owner_gid)
        payload_bytes = (
            json.dumps(
                {
                    "run_id": payload["run_id"],
                    "nonce": payload["nonce"],
                    "attestation_integrity": attestation.integrity,
                    "host_topology_integrity": _integrity(
                        {
                            name: str(host_topology[name])
                            for name in TOPOLOGY_LAYOUT
                        }
                    ),
                    "container_topology_integrity": _integrity(
                        {
                            name: str(payload["container_topology"][name])
                            for name in TOPOLOGY_LAYOUT
                        }
                    ),
                },
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        temporary_stat = os.stat(
            temporary_name, dir_fd=run_fd, follow_symlinks=False
        )
        _require(
            stat.S_ISREG(temporary_stat.st_mode)
            and f"{temporary_stat.st_mode & 0o7777:04o}" == "0600"
            and temporary_stat.st_uid == owner_uid
            and temporary_stat.st_gid == owner_gid,
            "ownership.bind_owner",
            "bound ownership marker temporary file has wrong type, mode, or UID/GID",
        )
        os.replace(
            temporary_name,
            marker_name,
            src_dir_fd=run_fd,
            dst_dir_fd=run_fd,
        )
        temporary_created = False
        bound_stat = os.stat(
            marker_name, dir_fd=run_fd, follow_symlinks=False
        )
        final_run_stat = os.stat(run_root, follow_symlinks=False)
        _require(
            stat.S_ISREG(bound_stat.st_mode)
            and f"{bound_stat.st_mode & 0o7777:04o}" == "0600"
            and bound_stat.st_uid == owner_uid
            and bound_stat.st_gid == owner_gid,
            "ownership.bind_owner",
            "bound ownership marker replacement has wrong type, mode, or UID/GID",
        )
        _require(
            (run_stat.st_dev, run_stat.st_ino)
            == (final_run_stat.st_dev, final_run_stat.st_ino),
            "ownership.run_root_race",
            "run root changed before ownership binding completed",
        )
        os.fsync(run_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=run_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(run_fd)
    return marker


def _verify_owned_tree(
    path: Path,
    run_root: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    _require(
        is_within(path, run_root),
        "cleanup.escape",
        "enumerated cleanup resource escaped the run root",
    )
    if not path.exists() and not path.is_symlink():
        return
    _require(
        not path.is_symlink(),
        "cleanup.symlink",
        "cleanup refuses symlinked resources",
    )
    item_stat = path.lstat()
    _require(
        item_stat.st_uid == owner_uid and item_stat.st_gid == owner_gid,
        "cleanup.owner",
        "cleanup refuses resources not owned by the attested runtime UID/GID",
    )
    if path.is_dir():
        for child in path.iterdir():
            _verify_owned_tree(
                child,
                run_root,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )


def _remove_owned_tree(
    path: Path,
    run_root: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    _require(
        is_within(path, run_root),
        "cleanup.escape",
        "enumerated cleanup resource escaped the run root",
    )
    if not path.exists() and not path.is_symlink():
        return
    _require(
        not path.is_symlink(),
        "cleanup.symlink",
        "cleanup refuses symlinked resources",
    )
    item_stat = path.lstat()
    _require(
        item_stat.st_uid == owner_uid and item_stat.st_gid == owner_gid,
        "cleanup.owner",
        "cleanup refuses resources not owned by the attested runtime UID/GID",
    )
    if path.is_dir():
        for child in path.iterdir():
            _remove_owned_tree(
                child,
                run_root,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        path.rmdir()
    else:
        path.unlink()


def _verify_evidence_bundle(
    path: Path, decision: PreflightDecision
) -> None:
    _require(
        path.is_dir() and not path.is_symlink(),
        "cleanup.evidence",
        "evidence bundle is missing, not a directory, or a symlink",
    )
    bundle_stat = path.lstat()
    _require(
        f"{bundle_stat.st_mode & 0o7777:04o}" == "0700"
        and bundle_stat.st_uid == decision.runtime_uid
        and bundle_stat.st_gid == decision.runtime_gid,
        "cleanup.evidence_owner",
        "evidence bundle mode or UID/GID differs from the attested owner",
    )
    allowed_names = {"manifest.json", "ownership.json"}
    manifest_path = path / "manifest.json"
    _require(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "cleanup.evidence_manifest",
        "evidence bundle does not contain the required regular manifest",
    )
    for child in path.iterdir():
        _require(
            child.name in allowed_names,
            "cleanup.evidence_unknown",
            "evidence bundle contains an unrecognized resource",
        )
        _require(
            child.is_file() and not child.is_symlink(),
            "cleanup.evidence_type",
            "evidence bundle entries must be regular non-symlink files",
        )
        child_stat = child.lstat()
        _require(
            f"{child_stat.st_mode & 0o7777:04o}" == "0600"
            and child_stat.st_uid == decision.runtime_uid
            and child_stat.st_gid == decision.runtime_gid,
            "cleanup.evidence_owner",
            "evidence entry mode or UID/GID differs from the attested owner",
        )


def cleanup_run(decision: PreflightDecision) -> dict[str, Any]:
    """Preserve evidence and delete only decision-enumerated resources."""
    run_root = decision.host_run_root
    expected_root = HOST_RUN_BASE / decision.run_id
    expected_topology = _topology(expected_root)
    _require(
        run_root == expected_root
        and decision.host_topology == expected_topology,
        "cleanup.run_root",
        "cleanup target and topology are not the exact decision-owned paths",
    )
    retention = HOST_EVIDENCE_RETENTION_BASE / decision.run_id
    retention_marker = retention / "ownership.json"
    _require(
        retention.parent == HOST_EVIDENCE_RETENTION_BASE,
        "cleanup.retention_parent",
        "evidence retention parent differs from the fixed dedicated base",
    )
    unsafe = (
        *decision.production_roots,
        *decision.shared_roots,
        decision.source_host_path,
        HOST_SECRET_BASE,
        HOST_ATTESTATION_BASE,
    )
    _validate_no_unsafe_path(
        run_root,
        (*unsafe, HOST_EVIDENCE_RETENTION_BASE),
        "cleanup.unsafe",
    )
    _validate_no_unsafe_path(
        retention,
        (*unsafe, HOST_RUN_BASE),
        "cleanup.retention_unsafe",
    )
    _assert_no_symlink_components(run_root, Path("/"))
    _assert_no_symlink_components(retention.parent, Path("/"))
    _require(
        retention.parent.is_dir() and not retention.parent.is_symlink(),
        "cleanup.retention_parent",
        "evidence retention base is missing, not a directory, or a symlink",
    )
    retention_base_stat = retention.parent.lstat()
    _require(
        f"{retention_base_stat.st_mode & 0o7777:04o}" == "0700"
        and retention_base_stat.st_uid == decision.runtime_uid
        and retention_base_stat.st_gid == decision.runtime_gid,
        "cleanup.retention_owner",
        "evidence retention base mode or UID/GID differs from the attested owner",
    )

    run_present = run_root.exists() or run_root.is_symlink()
    retention_present = retention.exists() or retention.is_symlink()
    if not run_present:
        if retention_present:
            _assert_no_symlink_components(retention, Path("/"))
            _verify_evidence_bundle(retention, decision)
            _verify_ownership_marker(retention_marker, decision)
            return {
                "status": "already_absent_evidence_retained",
                "run_id": decision.run_id,
                "evidence": str(retention),
                "remaining": [str(retention)],
            }
        return {
            "status": "already_absent",
            "run_id": decision.run_id,
            "remaining": [],
        }
    _require(
        run_root.is_dir() and not run_root.is_symlink(),
        "cleanup.symlink",
        "cleanup refuses a missing, non-directory, or symlinked run root",
    )
    run_stat = run_root.lstat()
    _require(
        f"{run_stat.st_mode & 0o7777:04o}" == "0700"
        and run_stat.st_uid == decision.runtime_uid
        and run_stat.st_gid == decision.runtime_gid,
        "cleanup.owner",
        "run root mode or UID/GID differs from the attested owner",
    )
    known_top_level = {path.name for path in expected_topology.values()}
    _require(
        all(child.name in known_top_level for child in run_root.iterdir()),
        "cleanup.unowned_remaining",
        "unrecognized resources remain; refusing mutation",
    )

    evidence = expected_topology["evidence"]
    host_marker = expected_topology["ownership_marker"]
    if retention_present:
        _assert_no_symlink_components(retention, Path("/"))
        _verify_evidence_bundle(retention, decision)
        _verify_ownership_marker(retention_marker, decision)
        _require(
            not evidence.exists() and not evidence.is_symlink(),
            "cleanup.evidence_duplicate",
            "both transient and retained evidence exist",
        )
    else:
        _verify_ownership_marker(host_marker, decision)
        _verify_evidence_bundle(evidence, decision)
        staged_marker = evidence / "ownership.json"
        if staged_marker.exists() or staged_marker.is_symlink():
            _verify_ownership_marker(staged_marker, decision)
            source_stat = host_marker.lstat()
            staged_stat = staged_marker.lstat()
            _require(
                (source_stat.st_dev, source_stat.st_ino)
                == (staged_stat.st_dev, staged_stat.st_ino),
                "cleanup.retention_marker_link",
                "staged retention marker is not hard-linked to the run marker",
            )
        else:
            os.link(host_marker, staged_marker, follow_symlinks=False)
            _verify_ownership_marker(staged_marker, decision)
        os.replace(evidence, retention)
        _assert_no_symlink_components(retention, Path("/"))
        _verify_evidence_bundle(retention, decision)
        _verify_ownership_marker(retention_marker, decision)

    top_level_names = tuple(
        name
        for name in TOPOLOGY_LAYOUT
        if name not in {"evidence", "ownership_marker"}
    )
    for name in top_level_names:
        _verify_owned_tree(
            expected_topology[name],
            run_root,
            owner_uid=decision.runtime_uid,
            owner_gid=decision.runtime_gid,
        )
    _verify_owned_tree(
        host_marker,
        run_root,
        owner_uid=decision.runtime_uid,
        owner_gid=decision.runtime_gid,
    )
    for name in top_level_names:
        _remove_owned_tree(
            expected_topology[name],
            run_root,
            owner_uid=decision.runtime_uid,
            owner_gid=decision.runtime_gid,
        )
    _remove_owned_tree(
        host_marker,
        run_root,
        owner_uid=decision.runtime_uid,
        owner_gid=decision.runtime_gid,
    )
    _require(
        not any(run_root.iterdir()),
        "cleanup.unowned_remaining",
        "unrecognized resources remain; refusing broad recursive deletion",
    )
    run_root.rmdir()
    return {
        "status": "transients_removed_evidence_retained",
        "run_id": decision.run_id,
        "evidence": str(retention),
        "remaining": [str(retention)],
    }
