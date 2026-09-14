"""Complete procfs inventory validation, including unexpected nested mounts."""

from __future__ import annotations

import pytest

from scripts.mount_inventory import MountInventoryError, parse_mountinfo, validated_application_mounts


TARGETS = {
    "/source/snapshot": "source",
    "/run/obs-live-test": "run",
    "/run/obs-live-test-secret/test-credentials": "secret",
    "/run/obs-live-test-attestation/host-attestation.json": "attestation",
}


def mount_line(number, target, *, root="/", options="ro", filesystem="ext4", extra=""):
    optional = f" {extra}" if extra else ""
    return f"{number} 1 8:1 {root} {target} {options}{optional} - {filesystem} source rw\n"


def valid_inventory():
    text = mount_line(1, "/", filesystem="overlay")
    for number, target in enumerate(TARGETS, 2):
        text += mount_line(number, target, options="rw" if target == "/run/obs-live-test" else "ro")
    return text


def test_application_records_preserve_expected_modes_and_order():
    records = validated_application_mounts(valid_inventory(), application_targets=TARGETS)
    assert [record.target for record in records] == list(TARGETS)
    assert [record.mode for record in records] == ["ro", "rw", "ro", "ro"]


@pytest.mark.parametrize("target", [
    "/source/snapshot/vendor", "/source/snapshot/scripts", "/run/obs-live-test/cache",
    "/run/obs-live-test/home/.claude", "/run/obs-live-test-secret/extra",
    "/var/run/docker.sock", "/workspace/obs", "/proc/sys/extra", "/dev/extra",
    "/etc/shadow", "/etc/hosts/extra", "/tmp/extra",
])
def test_unrecognized_mounts_are_not_filtered_out(target):
    text = valid_inventory() + mount_line(20, target)
    with pytest.raises(MountInventoryError, match="unexpected mount target"):
        validated_application_mounts(text, application_targets=TARGETS)


@pytest.mark.parametrize("target", list(TARGETS) + ["/"])
def test_stacked_mount_targets_are_denied(target):
    with pytest.raises(MountInventoryError):
        validated_application_mounts(valid_inventory() + mount_line(20, target), application_targets=TARGETS)


def test_duplicate_mount_ids_are_denied():
    with pytest.raises(MountInventoryError):
        parse_mountinfo(mount_line(2, "/one") + mount_line(2, "/two"))


@pytest.mark.parametrize("line", [
    "bad line\n", "1 2 8:1 / / ro ext4 source rw\n",
    "1 2 bad / / ro - ext4 source rw\n", "0 2 8:1 / / ro - ext4 source rw\n",
    "1 0 8:1 / / ro - ext4 source rw\n", "1 2 8:1 / / ro,rw - ext4 source rw\n",
    "1 2 8:1 / / nosuid - ext4 source rw\n", "1 2 8:1 / / ro - ext4 source\n",
    "1 2 8:1 / / ro - ext4 source rw extra\n", "1 2 8:1 / /bad\\999 ro - ext4 source rw\n",
    "1 2 8:1 / /bad/../alias ro - ext4 source rw\n", "1 2 8:1 / //alias ro - ext4 source rw\n",
])
def test_malformed_record_cannot_disappear_from_inventory(line):
    with pytest.raises(MountInventoryError, match="invalid mountinfo record"):
        parse_mountinfo(valid_inventory() + line)


def test_optional_fields_do_not_shift_filesystem_columns():
    record, = parse_mountinfo(mount_line(1, "/", filesystem="overlay", extra="shared:9 master:2 future-tag:42"))
    assert record.filesystem == "overlay"
    assert record.options == frozenset({"ro"})


@pytest.mark.parametrize("encoded,decoded", [
    (r"/a\040b", "/a b"), (r"/a\011b", "/a\tb"), (r"/a\012b", "/a\nb"),
    (r"/a\134b", "/a\\b"), (r"/a\134040b", r"/a\040b"),
])
def test_procfs_path_escaping_is_decoded_exactly_once(encoded, decoded):
    record, = parse_mountinfo(mount_line(1, encoded))
    assert record.target == decoded


@pytest.mark.parametrize("target", list(TARGETS))
def test_missing_application_mount_is_denied(target):
    text = "".join(line + "\n" for line in valid_inventory().splitlines() if line.split()[4] != target)
    with pytest.raises(MountInventoryError, match="required application mounts"):
        validated_application_mounts(text, application_targets=TARGETS)


def test_missing_or_writable_root_is_denied():
    text = valid_inventory()
    with pytest.raises(MountInventoryError, match="root mount"):
        validated_application_mounts("\n".join(text.splitlines()[1:]), application_targets=TARGETS)
    with pytest.raises(MountInventoryError, match="isolation options"):
        validated_application_mounts(text.replace("/ / ro", "/ / rw", 1), application_targets=TARGETS)


@pytest.mark.parametrize("target,filesystem,root,options", [
    ("/proc", "proc", "/", "rw,nosuid,nodev,noexec"),
    ("/proc/sys", "proc", "/sys", "ro,nosuid,nodev,noexec"),
    ("/proc/kcore", "tmpfs", "/null", "rw,nosuid"),
    ("/dev", "tmpfs", "/", "rw,nosuid"),
    ("/sys", "sysfs", "/", "ro,nosuid,nodev,noexec"),
    ("/dev/shm", "tmpfs", "/", "rw,nosuid,nodev,noexec"),
    ("/tmp", "tmpfs", "/", "rw,nosuid,nodev,noexec"),
    ("/etc/hosts", "ext4", "/docker/containers/123/hosts", "rw"),
])
def test_explicit_system_mounts_are_supported(target, filesystem, root, options):
    text = valid_inventory() + mount_line(20, target, root=root, options=options, filesystem=filesystem)
    assert len(validated_application_mounts(text, application_targets=TARGETS)) == 4


@pytest.mark.parametrize("target,filesystem,root,options", [
    ("/proc", "ext4", "/", "rw,nosuid,nodev,noexec"),
    ("/proc/sys", "proc", "/sys", "rw,nosuid,nodev,noexec"),
    ("/proc/kcore", "tmpfs", "/production", "rw,nosuid"),
    ("/tmp", "tmpfs", "/", "rw"),
    ("/sys", "sysfs", "/", "rw,nosuid,nodev,noexec"),
])
def test_known_target_does_not_bypass_system_mount_policy(target, filesystem, root, options):
    text = valid_inventory() + mount_line(20, target, root=root, options=options, filesystem=filesystem)
    with pytest.raises(MountInventoryError):
        validated_application_mounts(text, application_targets=TARGETS)


def test_errors_do_not_expose_raw_mount_source_paths():
    with pytest.raises(MountInventoryError) as caught:
        parse_mountinfo("bad /host/secret-credentials\n")
    assert "secret-credentials" not in str(caught.value)


@pytest.mark.parametrize("targets", [{}, {"/": "source"}, {"relative": "source"}, {"/a": "same", "/b": "same"}])
def test_invalid_expected_mount_policy_is_denied(targets):
    with pytest.raises(MountInventoryError):
        validated_application_mounts(valid_inventory(), application_targets=targets)
