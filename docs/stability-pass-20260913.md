# Stability pass — 13 September 2026

## Published scope

This pass builds on `3b5e49ab07ded201c1bd79b89a1e9fea3885e8fa`.
It strengthens the **canonical isolated-test runner's mount observation** and
adds **fork-prefix regression coverage**. It does not claim that the complete
WIP branch is stable, release-ready, or verified on the owner's host.

The canonical runner now uses `CompleteMountObservationAdapter`. It validates
all records from `/proc/self/mountinfo` before selecting the four application
mounts expected by the existing protocol. Unknown and nested mounts, stacked
targets, duplicate mount IDs, malformed records, and noncanonical paths are
denied. Procfs path escapes are decoded once. Unknown optional fields remain
compatible with the kernel's mountinfo format.

Validation happens before the inherited observer reads/hashes the source tree,
and is repeated when the inherited observer collects its mount records. There
is no cached mount inventory trusted across those source reads. A validation
failure is reported as `inner.mount_inventory` and cannot produce an allowed
preflight manifest through the canonical entry point.

## Policy and limits

`scripts/mount_inventory.py` contains an explicit system-mount policy for the
intended dedicated Docker template. These are exact path exceptions, not prefix
exceptions: allowing `/proc/sys` does not allow `/proc/sys/extra`. The root must
be read-only; known procfs, sysfs, device, cgroup and temporary mounts have
filesystem/option restrictions. Unknown host/container layouts fail closed.

The policy has **not** been exercised in a real container. Rootless Docker,
alternate storage drivers, different kernel masking defaults, or other runtime
layouts may need separately reviewed additions. Do not loosen the policy merely
to make a failed live preflight green.

This is a topology check, **not bind-source authentication**. For example,
allowlisting Docker's `/etc/hosts` location does not prove the origin of that
file. The host/image/source/secret/ownership/network checks remain necessary.
The original `ContainerRuntimeObservationAdapter` in `live_test_protocol.py`
is unchanged; direct library users who instantiate it do not gain the stricter
inventory check. The documented canonical runner is the hardened path. Static
observation adapters are not a substitute for trusted runtime observations.

The runner still authorizes only its existing dry-run flow. No disposable-service
lifecycle, provider smoke test, deployment or credential handling was enabled.

## Fork invariant

This pass does **not** modify `jsonl_fork.py`, `context_jsonl.py`, `jsonl_health.py`,
`session.py`, or the Telegram fork implementation. Analytics are not reused as a
fork parser, and no tolerant decoder is introduced into the copy path.

`tests/test_fork_prefix_integrity.py` checks exact output bytes, not equivalent
parsed JSON. Fixtures preserve unusual whitespace, key order, escaped Unicode,
unknown fields, thinking signatures, redacted-thinking data, tool-use/result
pairs, selected ancestry, and a fork-of-fork prefix. The tests exercise preferred
and fallback session lookup, explicit paths, and analytics before copying. The
parent bytes remain unchanged, including when an invalid chain is rejected.

These are **LF-terminated fixtures**, not a claim about every possible input
format. A separate diagnostic reproduced existing behavior in the unchanged
copier: CRLF is normalized to LF, and a missing final newline is added. That
behavior predates this pass and is not fixed here. It prevents a blanket claim
of byte-identical copying for arbitrary line endings. No real provider cache
hit or signed-thinking acceptance was tested.

## Verification performed

All checks ran offline under Python 3.13.5 in a selected-source workspace:

- 60 direct mount-parser/policy cases passed.
- 9 canonical-runner adapter/wiring cases passed. The unavailable full protocol
  import was explicitly substituted in the offline test harness; inherited
  attestation and host/inner preflight logic were **not** thereby verified.
- 18 direct fork-prefix cases passed against the unchanged real copy and lookup
  modules. No SDK stand-in was used for their execution.
- The preceding review's 136 focused checks were rerun successfully, retaining
  their documented SDK/import-boundary substitutions for search.

The complete repository suite, the historical 23 Telegram unit failures, actual
SDK integration, and the host-managed `obs-live-test` lane were not run. No
Telegram poller, daemon, provider, Docker service, or credentialed call was made.

## Remaining high-priority work

The scoped-stop launch race is **not fixed** by this commit. An isolated admission
registry candidate passed deterministic interleaving tests, but it is not wired
into fork creation, resume, inbox wake, task publication, and pre-run setup.
Those integrations need to share a stop fence; two immediate snapshots do not
provide that fence. A helper's green tests are not evidence of end-to-end safety.

The Telegram delayed-flush and tree-renderer repairs were also developed as
source-bound candidates. Their selected-method regressions pass, but the actual
`telegram.py` in this commit is unchanged. The full startup, task ownership,
transport cancellation, partial-send behavior, and the combined
`search -> AgentTaskOutput -> Stop -> search` path remain acceptance work.

Reference for the procfs record format: Linux kernel documentation,
`Documentation/filesystems/proc.rst`, section `/proc/<pid>/mountinfo`.
