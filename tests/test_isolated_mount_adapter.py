"""The canonical dry-run entry point must use complete-inventory validation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import isolated_test_runner as runner
from scripts.live_test_protocol import PreflightError


def inventory(extra: str = "") -> str:
    mounts = [
        ("/", "ro", "overlay"),
        ("/source/snapshot", "ro", "ext4"),
        ("/run/obs-live-test", "rw", "ext4"),
        ("/run/obs-live-test-secret/test-credentials", "ro", "ext4"),
        ("/run/obs-live-test-attestation/host-attestation.json", "ro", "ext4"),
    ]
    return "".join(
        f"{index} 1 8:1 / {target} {mode} - {filesystem} source rw\n"
        for index, (target, mode, filesystem) in enumerate(mounts, 1)
    ) + extra


def test_adapter_reads_one_inventory_and_preserves_protocol_schema(tmp_path, monkeypatch):
    path = tmp_path / "mountinfo"
    path.write_text(inventory())
    adapter = runner.CompleteMountObservationAdapter({}, proc_mountinfo=path)
    real_read = Path.read_text
    reads = []

    def read_text(subject, *args, **kwargs):
        reads.append(subject)
        return real_read(subject, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    records = adapter._mount_records()
    assert reads == [path]
    assert [record["name"] for record in records] == ["source", "run", "secret", "attestation"]
    assert [record["mode"] for record in records] == ["ro", "rw", "ro", "ro"]
    assert all(set(record) == {"name", "target", "mode", "symlink"} for record in records)


@pytest.mark.parametrize("extra", [
    "bad record\n",
    "40 1 8:1 / /source/snapshot/scripts rw - ext4 production rw\n",
    "40 1 8:1 / /workspace/obs rw - ext4 production rw\n",
    "40 1 8:1 / /run/obs-live-test/home rw - ext4 production rw\n",
])
def test_adapter_denies_bad_or_unexpected_records(tmp_path, extra):
    path = tmp_path / "mountinfo"
    path.write_text(inventory(extra))
    adapter = runner.CompleteMountObservationAdapter({}, proc_mountinfo=path)
    with pytest.raises(PreflightError, match="inner.mount_inventory"):
        adapter._mount_records()


def test_inner_entrypoint_selects_strict_adapter_before_evidence_write(monkeypatch):
    calls = []
    attestation = object()
    decision = SimpleNamespace(executable_path=Path("/source/snapshot/scripts/isolated_test_runner.py"))
    monkeypatch.setattr(runner, "load_host_attestation", lambda path: attestation)

    def preflight(loaded, adapter):
        assert loaded is attestation
        assert isinstance(adapter, runner.CompleteMountObservationAdapter)
        calls.append("preflight")
        return decision

    def build_manifest(**kwargs):
        assert calls == ["preflight"]
        calls.append("manifest")
        return {"status": "planned_not_run"}

    monkeypatch.setattr(runner, "inner_preflight", preflight)
    monkeypatch.setattr(runner, "build_evidence_manifest", build_manifest)
    monkeypatch.setattr(runner, "write_evidence_manifest", lambda *_: Path("/not-written"))
    args = SimpleNamespace(attestation=runner.CONTAINER_ATTESTATION_FILE, lane="unit", scenario="focused")
    assert runner._inner_dry_run(args)["status"] == "inner_preflight_allowed_factories_not_started"
    assert calls == ["preflight", "manifest"]


def test_failed_preflight_does_not_write_evidence(monkeypatch):
    monkeypatch.setattr(runner, "load_host_attestation", lambda path: object())

    def deny(*_):
        raise PreflightError("inner.mount_inventory", "denied")

    def unexpected(*args, **kwargs):
        raise AssertionError("evidence must not be written after failed preflight")

    monkeypatch.setattr(runner, "inner_preflight", deny)
    monkeypatch.setattr(runner, "build_evidence_manifest", unexpected)
    monkeypatch.setattr(runner, "write_evidence_manifest", unexpected)
    args = SimpleNamespace(attestation=runner.CONTAINER_ATTESTATION_FILE, lane="unit", scenario="focused")
    with pytest.raises(PreflightError):
        runner._inner_dry_run(args)


def test_inventory_denial_precedes_inherited_source_reads(tmp_path, monkeypatch):
    path = tmp_path / "mountinfo"
    path.write_text(inventory("40 1 8:1 / /source/snapshot/private ro - ext4 secret rw\n"))
    adapter = runner.CompleteMountObservationAdapter({}, proc_mountinfo=path)

    def forbidden_source_read(*args, **kwargs):
        raise AssertionError("source tree must not be read before mount validation")

    monkeypatch.setattr(runner.ContainerRuntimeObservationAdapter, "observe", forbidden_source_read, raising=False)
    with pytest.raises(PreflightError):
        adapter.observe(object())


def test_valid_inventory_delegates_remaining_checks_without_caching(tmp_path, monkeypatch):
    path = tmp_path / "mountinfo"
    path.write_text(inventory())
    adapter = runner.CompleteMountObservationAdapter({}, proc_mountinfo=path)
    expected_attestation = object()

    def inherited_observe(self, attestation):
        assert attestation is expected_attestation
        # A changed table must not be concealed by the first validation.
        path.write_text(inventory("40 1 8:1 / /unexpected ro - ext4 source rw\n"))
        self._mount_records()
        raise AssertionError("changed inventory should have been denied")

    monkeypatch.setattr(runner.ContainerRuntimeObservationAdapter, "observe", inherited_observe, raising=False)
    with pytest.raises(PreflightError):
        adapter.observe(expected_attestation)
