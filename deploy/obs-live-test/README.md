# `obs-live-test` service-creation template

> **UNVERIFIED / UNRESOLVED.** This repository slice did not parse Compose,
> build or inspect an image, create/start a service, authenticate, read host
> runtime state, open a network, or run a test.

`compose.yaml` is inert source-controlled configuration for a later authorized
Ubuntu host executor. It may be used only after the host phase in
[`docs/testing.md`](../../docs/testing.md) validates a trusted independent host
observation and creates the integrity-bound attestation. Compose/environment
variables are launch inputs, never proof.

## Fixed boundary

- service/container/hostname: `obs-live-test`;
- marker: `obs-live-test-v1`, host-observed in immutable image identity rather
  than supplied as a Compose environment assertion;
- current `obs-test`: production-serving and forbidden;
- image: exact `repository@sha256:<64 lowercase hex>` selected and independently
  observed by the host;
- source: immutable measured snapshot, read-only at `/source/snapshot`, never
  `/workspace/obs`;
- writable state: exactly one run-owned host root mounted at
  `/run/obs-live-test`;
- secret: exact measured test-only regular file mounted read-only at
  `/run/obs-live-test-secret/test-credentials`;
- attestation: exact host-preflight file mounted read-only at
  `/run/obs-live-test-attestation/host-attestation.json`;
- network: private run-owned bridge named `obs-live-test-${OBS_LIVE_TEST_RUN_ID}`;
- restart: disabled; the only command is the provisional inner `--dry-run`.

No other mounts are permitted. Only the exact run mount is writable. Host/shared
network mode, production/shared mounts, writable source/secret/attestation, and
unknown mounts are denied by host and inner observation.

## Exact Compose inputs

Populate these only from the accepted host attestation:

- `OBS_LIVE_TEST_IMAGE_REFERENCE` — immutable image reference pinned by digest;
- `OBS_LIVE_TEST_UID`, `OBS_LIVE_TEST_GID` — observed non-production owner;
- `OBS_LIVE_TEST_RUN_ID` — canonical single-run ID;
- `OBS_LIVE_TEST_SOURCE_SNAPSHOT` — exact immutable source snapshot;
- `OBS_LIVE_TEST_RUN_ROOT` — exact run-owned root;
- `OBS_LIVE_TEST_SECRET_FILE` — exact measured test-secret file;
- `OBS_LIVE_TEST_ATTESTATION_FILE` — exact host attestation file;
- `OBS_LIVE_TEST_CACHE_PORT`, `OBS_LIVE_TEST_DAEMON_PORT` — distinct
  host-observed, run-owned, available, non-production ports.

The host observation also carries complete production/shared root, credential,
poller, port, and network inventories; source commit/tree/actual-runner digests;
image digest/provenance; mount ownership; secret fingerprint/provenance/UID/GID
and expiry through the attestation window; selected-bot poller lease; selected
port reservation provenance/expiry; and the exact pairwise-disjoint topology.
These facts are not recreated as Compose booleans.

## Ordering

1. Run the one host command from `docs/testing.md`; it performs no service
   creation and prints the accepted attestation.
2. A later approved host executor exclusively writes that document to the exact
   attested host path and binds the run ownership marker.
3. Populate only the exact values above from that attestation.
4. Create `obs-live-test` from the host, never from `obs-test`.
5. The immutable container command measures the active `__main__.__file__` and
   `sys.argv[0]` runner, procfs/files/mounts/network/source/image/secret/topology
   facts, and runs inner preflight.
6. Any mismatch exits before client, daemon, network, model, test, or poller
   factories. A match yields one complete decision-derived `RuntimePolicy` for
   every later factory and produces only a redacted `planned_not_run` manifest.

The exact topology and evidence/cleanup semantics are normative in
`docs/testing.md` and implemented in `scripts/live_test_protocol.py`. The
historical `--test`, `--test-instance`, `--profile test`, and parallel live-smoke
launchers are disabled and do not provide a compatibility bypass.
