"""Validate the complete Linux mount inventory before selecting application mounts.

The system-path policy describes the dedicated Docker template, not a general
Linux host. An unknown layout is denied rather than silently dropping a mount.
This checks topology and mount options; it does not authenticate bind sources.
Host attestation and the existing source/image/secret checks are still required.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Mapping


class MountInventoryError(ValueError):
    """The observed mount table is malformed or outside the dedicated policy."""


@dataclass(frozen=True)
class MountRecord:
    mount_id: int
    parent_id: int
    device: str
    root: str
    target: str
    options: frozenset[str]
    filesystem: str
    source: str
    super_options: frozenset[str]

    @property
    def mode(self) -> str:
        return "ro" if "ro" in self.options else "rw"


_MOUNT_ESCAPE = re.compile(r"\\(040|011|012|134)")
_ESCAPE_VALUES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


def _decode_mount_path(value: str) -> str:
    """Decode procfs escaping once, without interpreting a literal backslash twice."""
    if re.search(r"\\(?!040|011|012|134)", value):
        raise MountInventoryError("mountinfo contains an unsupported path escape")
    decoded = _MOUNT_ESCAPE.sub(lambda match: _ESCAPE_VALUES[match.group(1)], value)
    if not decoded.startswith("/") or decoded.startswith("//"):
        raise MountInventoryError("mountinfo paths must be absolute and canonical")
    if str(PurePosixPath(decoded)) != decoded or ".." in PurePosixPath(decoded).parts:
        raise MountInventoryError("mountinfo paths must not contain traversal or aliases")
    return decoded


def parse_mountinfo(text: str) -> tuple[MountRecord, ...]:
    """Parse every record; malformed lines and overmounted targets are not skipped."""
    records: list[MountRecord] = []
    seen_ids: set[int] = set()
    seen_targets: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split()
        try:
            separator = fields.index("-")
            if separator < 6 or len(fields) != separator + 4:
                raise ValueError
            mount_id, parent_id = int(fields[0]), int(fields[1])
            if mount_id <= 0 or parent_id <= 0:
                raise ValueError
            if not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
                raise ValueError
            root = _decode_mount_path(fields[3])
            target = _decode_mount_path(fields[4])
            options = frozenset(fields[5].split(","))
            if not options or "" in options or len(options & {"ro", "rw"}) != 1:
                raise ValueError
            if mount_id in seen_ids or target in seen_targets:
                raise MountInventoryError("duplicate mount ID or stacked mount target")
            # Unknown optional fields between column 6 and '-' are permitted by
            # the kernel format. They must not shift the filesystem columns.
            record = MountRecord(
                mount_id=mount_id, parent_id=parent_id, device=fields[2],
                root=root, target=target, options=options,
                filesystem=fields[separator + 1], source=fields[separator + 2],
                super_options=frozenset(fields[separator + 3].split(",")),
            )
        except (ValueError, IndexError) as exc:
            # Do not include raw mount paths/sources in user-visible errors.
            raise MountInventoryError(
                f"invalid mountinfo record at line {line_number}"
            ) from None
        records.append(record)
        seen_ids.add(mount_id)
        seen_targets.add(target)
    if not records:
        raise MountInventoryError("mountinfo inventory is empty")
    return tuple(records)


@dataclass(frozen=True)
class _SystemMountPolicy:
    filesystems: frozenset[str]
    required_options: frozenset[str] = frozenset()
    required_root: str | None = None


_SYSTEM_MOUNTS: dict[str, _SystemMountPolicy] = {
    "/": _SystemMountPolicy(
        frozenset({"overlay", "fuse.overlayfs", "ext4", "xfs", "btrfs", "zfs"}),
        frozenset({"ro"}),
    ),
    "/proc": _SystemMountPolicy(frozenset({"proc"}), frozenset({"nosuid", "nodev", "noexec"})),
    "/dev": _SystemMountPolicy(frozenset({"tmpfs"}), frozenset({"nosuid"}), "/"),
    "/dev/pts": _SystemMountPolicy(frozenset({"devpts"}), frozenset({"nosuid", "noexec"}), "/"),
    "/dev/shm": _SystemMountPolicy(frozenset({"tmpfs"}), frozenset({"nosuid", "nodev", "noexec"}), "/"),
    "/dev/mqueue": _SystemMountPolicy(frozenset({"mqueue"}), frozenset({"nosuid", "nodev", "noexec"}), "/"),
    "/sys": _SystemMountPolicy(frozenset({"sysfs"}), frozenset({"ro", "nosuid", "nodev", "noexec"})),
    "/sys/fs/cgroup": _SystemMountPolicy(frozenset({"cgroup", "cgroup2"}), frozenset({"ro", "nosuid", "nodev", "noexec"})),
    "/tmp": _SystemMountPolicy(frozenset({"tmpfs"}), frozenset({"nosuid", "nodev", "noexec"}), "/"),
}
for _target in ("/etc/hostname", "/etc/hosts", "/etc/resolv.conf"):
    # Docker creates these exact three files separately from its root mount.
    # Their source identity is a host-attestation responsibility, not inferred
    # from this location allowlist.
    _SYSTEM_MOUNTS[_target] = _SystemMountPolicy(frozenset({"ext4", "xfs", "btrfs", "zfs", "tmpfs"}))
for _target in ("/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"):
    _SYSTEM_MOUNTS[_target] = _SystemMountPolicy(frozenset({"proc"}), frozenset({"ro", "nosuid", "nodev", "noexec"}))
for _target in ("/proc/acpi", "/proc/scsi", "/sys/firmware", "/sys/devices/virtual/powercap"):
    _SYSTEM_MOUNTS[_target] = _SystemMountPolicy(frozenset({"tmpfs"}), frozenset({"ro"}), "/")
for _target in ("/proc/interrupts", "/proc/kcore", "/proc/keys", "/proc/latency_stats", "/proc/timer_list", "/proc/timer_stats", "/proc/sched_debug"):
    _SYSTEM_MOUNTS[_target] = _SystemMountPolicy(frozenset({"tmpfs"}), required_root="/null")


def validated_application_mounts(
    text: str, *, application_targets: Mapping[str, str]
) -> tuple[MountRecord, ...]:
    """Validate the full inventory and return the expected application records.

    Exact system targets are exceptions, not subtree allowlists. For example,
    '/proc/sys/extra' and '/run/obs-live-test/cache' must both be rejected.
    """
    if (
        not application_targets
        or any(not isinstance(name, str) or not name for name in application_targets.values())
        or len(set(application_targets.values())) != len(application_targets)
    ):
        raise MountInventoryError("application mount names must be nonempty and unique")
    if set(application_targets) & set(_SYSTEM_MOUNTS):
        raise MountInventoryError("application targets overlap reserved system mounts")
    for target in application_targets:
        if (
            not isinstance(target, str) or not target.startswith("/")
            or target.startswith("//")
            or str(PurePosixPath(target)) != target
            or ".." in PurePosixPath(target).parts
        ):
            raise MountInventoryError("application targets must be canonical absolute paths")
    records = parse_mountinfo(text)
    application_records: dict[str, MountRecord] = {}
    root_seen = False
    for record in records:
        if record.target in application_targets:
            application_records[record.target] = record
            continue
        policy = _SYSTEM_MOUNTS.get(record.target)
        if policy is None:
            raise MountInventoryError("unexpected mount target in complete inventory")
        if record.filesystem not in policy.filesystems:
            raise MountInventoryError("unexpected filesystem at a system mount target")
        if not policy.required_options.issubset(record.options):
            raise MountInventoryError("system mount lacks required isolation options")
        if policy.required_root is not None and record.root != policy.required_root:
            raise MountInventoryError("system mount has an unexpected filesystem root")
        root_seen = root_seen or record.target == "/"
    if not root_seen:
        raise MountInventoryError("container root mount was not observed")
    if set(application_records) != set(application_targets):
        raise MountInventoryError("one or more required application mounts are missing")
    return tuple(application_records[target] for target in application_targets)
