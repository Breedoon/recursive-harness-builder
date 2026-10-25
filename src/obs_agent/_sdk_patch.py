"""Local SDK compatibility patches."""

from __future__ import annotations

import os

from claude_agent_sdk._internal import message_parser

_PRODUCTION_BOT_ENV_KEYS = (
    "OBS_PROD_TELEGRAM_BOT_TOKEN",
    "OBS_PROD_TELEGRAM_BOT_TOKENS",
)
_BOT_ENV_PATCHED = False


def _without_production_bot_tokens(values: dict[str, str], tokens: set[str]) -> dict[str, str]:
    cleaned = {}
    for key, value in values.items():
        if key in _PRODUCTION_BOT_ENV_KEYS:
            continue
        if any(token in value for token in tokens):
            value = ",".join(part for part in value.split(",") if part.strip() not in tokens)
            for token in sorted(tokens, key=len, reverse=True):
                value = value.replace(token, "")
            if not value:
                continue
        cleaned[key] = value
    return cleaned


def ensure_production_bot_env_patch() -> None:
    """Keep production transport credentials out of SDK-spawned processes."""
    global _BOT_ENV_PATCHED
    if _BOT_ENV_PATCHED:
        return

    from claude_agent_sdk._internal.transport import subprocess_cli

    original_anyio = subprocess_cli.anyio

    class _AgentSubprocessAnyIO:
        def __getattr__(self, name):
            return getattr(original_anyio, name)

        async def open_process(self, *args, **kwargs):
            # The SDK version probe has no explicit env; the main CLI passes its
            # inherited+override merge. Sanitize a copy at both actual spawns,
            # never mutate the daemon's environment across an async await.
            parent = dict(os.environ)
            environment = kwargs.get("env")
            environment = parent if environment is None else dict(environment)
            tokens = {
                token.strip()
                for values in (parent, environment)
                for key in _PRODUCTION_BOT_ENV_KEYS
                for token in values.get(key, "").split(",")
                if token.strip()
            }
            if not tokens and parent.get("OBS_PROFILE", "prod") == "prod":
                tokens.update(
                    token.strip()
                    for key in ("OBS_TELEGRAM_BOT_TOKEN", "OBS_TELEGRAM_BOT_TOKENS")
                    for token in parent.get(key, "").split(",")
                    if token.strip()
                )
            kwargs["env"] = _without_production_bot_tokens(environment, tokens)
            return await original_anyio.open_process(*args, **kwargs)

    # Replace only this SDK module's dependency, not the shared anyio module.
    subprocess_cli.anyio = _AgentSubprocessAnyIO()
    _BOT_ENV_PATCHED = True

_PATCHED = False


def ensure_raw_uuid_patch() -> None:
    """Preserve raw JSONL UUIDs on streamed SDK message objects.

    The SDK parser currently drops ``uuid`` for most typed message variants even
    though it is present in the raw JSON payload. We attach it as ``_raw_uuid``
    so transport adapters can correlate rendered output back to JSONL entries.
    """

    global _PATCHED
    if _PATCHED:
        return

    original_parse = message_parser.parse_message

    def _parse_with_uuid(data):
        message = original_parse(data)
        if message is not None and isinstance(data, dict):
            raw_uuid = data.get("uuid")
            if isinstance(raw_uuid, str) and raw_uuid:
                setattr(message, "_raw_uuid", raw_uuid)
        return message

    message_parser.parse_message = _parse_with_uuid
    _PATCHED = True


_QUERY_CLOSE_PATCHED = False
QUERY_CLOSED_ERROR = "Claude SDK query closed while a response was pending"


def ensure_query_close_patch() -> None:
    """Make ``Query.close()`` safe when called from a task other than the starter.

    OBS starts each client in a detached task (``session.py`` runs
    ``asyncio.create_task(client.connect())``), so the SDK's anyio task group is
    entered in a task that finishes right away, and every later ``close()`` runs
    in a different task. The stock ``close()`` then:

    - raises RuntimeError from the task group's ``__aexit__`` (a cancel scope
      cannot be exited from another task), which skips ``transport.close()``;
    - leaves the cancelled scope rescheduling ``_deliver_cancellation`` on every
      event-loop iteration forever, because its finished host task stays in
      ``scope._tasks`` (one leaked spinner per disconnect; 350 of them pegged
      the prod loop at 100% CPU on 2026-10-13);
    - cancels the reader before it can send its ``end`` sentinel, so a turn
      waiting in ``receive_messages()`` never wakes. After a stop escalation
      killed a stuck CLI, that held the route lock forever (dead topics 16234
      and 83366, 2026-10-13).

    The patched ``close()`` first wakes any waiting consumer with an error item
    (the SDK raises it as an ``Exception``, which the runner treats like a CLI
    exit), cancels the group, exits it normally when it can, and otherwise waits
    briefly for the group's own child tasks to finish cancelling and then drops
    the finished host task so anyio stops re-delivering. It always closes the
    transport.
    """

    global _QUERY_CLOSE_PATCHED
    if _QUERY_CLOSE_PATCHED:
        return

    import asyncio
    from contextlib import suppress

    import anyio
    from claude_agent_sdk._internal.query import Query

    original_close = Query.close

    def _wake_receivers(query) -> None:
        send = query._message_send
        with suppress(Exception):
            send.send_nowait({"type": "error", "error": QUERY_CLOSED_ERROR})
        with suppress(Exception):
            send.close()

    async def _release_orphaned_scope(scope, host) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        while loop.time() < deadline:
            if not any(t is not host and not t.done() for t in list(scope._tasks)):
                break
            await asyncio.sleep(0.01)
        scope._tasks.discard(host)
        handle = getattr(scope, "_cancel_handle", None)
        if handle is not None and not any(not t.done() for t in scope._tasks):
            handle.cancel()
            scope._cancel_handle = None

    async def _close(self) -> None:
        self._closed = True
        _wake_receivers(self)
        tg = self._tg
        try:
            if tg is not None:
                scope = tg.cancel_scope
                scope.cancel()
                host = getattr(scope, "_host_task", None)
                if host is None or host is asyncio.current_task():
                    with suppress(anyio.get_cancelled_exc_class()):
                        await tg.__aexit__(None, None, None)
                elif host.done():
                    await _release_orphaned_scope(scope, host)
                else:
                    with suppress(anyio.get_cancelled_exc_class(), RuntimeError):
                        await tg.__aexit__(None, None, None)
        finally:
            await self.transport.close()

    _close._obs_original = original_close  # type: ignore[attr-defined]
    Query.close = _close
    _QUERY_CLOSE_PATCHED = True
