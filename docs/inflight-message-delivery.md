# In-flight message delivery

## Failure mechanism

The old registered pipelines drained `HookState.message_queue` in both
`PreToolUse` and `PostToolUse`, before the optional user hook. Draining also
emitted `queue_delivered`. If the user hook then stopped the run or its await
was cancelled, the message was already gone. In particular, cancellation could
prevent any hook response from reaching the SDK while the transport had
already been told that the message was delivered. Concurrent tool callbacks
could make this worse: a cancelled callback could consume the only copy before
another callback completed.

The hook drain also ignored `pause_queue_delivery`, `interrupt_requested`, and
`interrupt_flag`, unlike the runner's continuation path.

These are reproducible harness-level loss paths. They do not establish that
every reported live delivery failure has the same cause.

## Delivery contract

- `PreToolUse` keeps the interrupt/native-tool/immutable-file guards and
  session/tool tracking, but does not consume queued messages.
- `PostToolUse` updates tool state, runs the user hook, and only then drains the
  queue as its final check. There is no await between draining and constructing
  the hook response. A stopped or cancelled user hook leaves messages queued.
- Paused or interrupted delivery leaves messages queued without emitting a
  delivery status. Existing runner continuation/pending-message handling stays
  unchanged for messages not handed off through a successful tool callback.
- Reply-target messages keep their metadata for later routing. Plain messages
  retain order, including repeated messages with identical text.

`queue_delivered` means the harness has placed the message into its returned
hook context, not that the model has explicitly acknowledged or acted on it.
This change does not add durable storage or an SDK/transcript acknowledgement
protocol for process crashes or failures after the callback returns.

## Deterministic regression tests

With the repository's development dependencies installed:

```sh
uv run pytest tests/test_hooks.py tests/test_inflight_message_delivery.py -q
```

The new tests exercise registered callbacks, not only the generic drain helper.
They cover pre/post timing, pause/interrupt flags, user-hook stop/deny decisions,
cancellation, concurrent callbacks with one cancelled, arrival during an
awaited hook, reply metadata, queue isolation, and tool-state tracking.

During development, the 18 new cases were executed in an isolated offline
adapter against the actual hooks, queueing, and events source. Only unavailable
SDK hook type carriers and unrelated bootstrap/config imports were substituted.
The unchanged baseline produced 13 failures and 5 passes; the fix produced 18
passes. This is not a full dependency-backed suite run or a live SDK/Telegram
validation.

## Live validation checklist

Use a disposable conversation on this branch with the normal live runtime.
Ask the agent to run a tool that takes long enough to send another message.
While that tool is running, send a correction containing a newly generated,
unpredictable token and ask the agent to include that exact token in its next
answer. Check the actual answer or resulting artifact, not just a working or
delivery status. Repeat before a tool starts, during tool execution, and near
the end of a turn. Repeat with parallel tools and with configured async user
hooks. A correction arriving after the last tool should be handled by the
runner's continuation path without requiring the user to stop the agent.

Also exercise cancellation of a waiting user hook: the message should remain
pending, with no `queue_delivered` event for that cancelled callback, and be
available to the next eligible post-tool callback or runner continuation.
The live checks above have not been run in the offline development environment.
