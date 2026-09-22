# In-flight message delivery

## Cache-fidelity warning

**Do not deliver queued input in native hook `additionalContext`, and do not
relax cache-proxy stripping to make it visible.** The bundled CLI sends that
context live but omits it from JSONL. Forks and resumes cannot reconstruct it.
Preserving the live-only context changes the historical prefix and defeats
fork-cache sharing. This was reproduced using actual CLI live/fork/resume
requests, not inferred from synthetic examples. See
the README's critical maintainer warning for the normalization contract.

## Failure mechanism

Earlier queue delivery drained messages in tool hooks and emitted
`queue_delivered` before their returned `additionalContext` reached the model.
Cancellation or a vetoing hook could lose that input. Moving the drain to the
last PostToolUse check reduced that race but did not fix the persistence boundary:
the cache proxy correctly stripped the live-only notification after it had been
removed from the queue. A successful hook receipt did not prove message delivery.

## Delivery contract

- PreToolUse retains interrupt/native-tool/immutable-file guards and tool/session
  tracking. PostToolUse retains tracking and user hooks. Neither consumes queued
  input or emits `queue_delivered` for it.
- ConversationRunner submits queued input through the existing ordinary
  `client.query()` path. That input is part of normal JSONL conversation history,
  so parent, fork, and resume reconstruct the same normalized historical content.
- Delivery occurs **at a turn boundary**, not mid-tool. Long running turns can
  delay corrections until they finish; `/stop` remains the interruption path.
- A queued batch becomes runner-owned before any query/preflight await and remains
  in `remaining_pending` on unaccepted query failure or cancellation. Adapters
  recover ownership before releasing the route, explicitly closing the runner if
  transport fails while it yields an event. Later arrivals follow it in order.
- After `query()` returns successfully, the batch is no longer pending and only
  then is `queue_delivered` emitted. A later response failure must not enqueue
  that already-submitted batch a second time. Repeated independent messages with
  identical text remain separate; there is no text-based deduplication.
- Pause/interrupt flags prevent automatic continuation, including a pause arriving
  during reconnect or a background-fork wait. Explicit Telegram `/stop` keeps its
  existing policy: discard canceled queued work and report the count, not silently
  resume it. Ordinary failure/shutdown cancellation retains unaccepted work.
  When the latest queued message targets a reply, the batch and its metadata stay
  pending for the adapter's existing latest-message routing policy.
- The continuation limit and background wait policy remain bounded. Remaining
  queued input is returned to the adapter for the next eligible turn; Telegram's
  route-isolated background delivery handles idle queued work without serializing
  unrelated model turns.

`queue_delivered` acknowledges successful **SDK query handoff**, not model
compliance or a durable transaction receipt. The SDK does not expose a JSONL-fsync
acknowledgement: process crashes or failures after a write may remain ambiguous.
This patch does not claim crash-proof exactly-once delivery or solve every historic
route-binding error. It does not alter cache-proxy normalization behavior.

## Regression coverage

With development dependencies installed, relevant local library/mock and
loopback-only protocol suites are:

```sh
.venv/bin/pytest -q tests/test_hooks.py tests/test_inflight_message_delivery.py tests/test_runner.py tests/test_daemon.py tests/test_cache_proxy_reminder_span.py
```

These cover registered hooks retaining messages despite cancellation, hook vetoes,
parallel tool callbacks, pause and reply metadata; initial/continuation/background
query ownership on failure/cancellation; duplicate-content input; HTTP adapter
handoff; and the actual bundled CLI's parent JSONL plus native fork **and** resume
requests. The protocol tests use a loopback fake API and verify canonical queued
input survives normalization and persists once, while transient hook context is
stripped. They do not contact a provider or measure live provider cache hits.

Production deployment/restart and live Telegram validation require a separate
coordinated activation. Source edits and local tests alone are not a live fix.
