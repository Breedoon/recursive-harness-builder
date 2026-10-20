"""Deterministic reproductions for the two P0 messaging reports (obs-15 lane E1).

obs-12 (/report 2026-10-19 19:57Z, topic 371524): "agent reports no route binding even
though there clearly is cuz it got a message from there". Every failing send in the
reported session (47582e7d) was a SendInboxMessage reply to ``external:glm-liveness``, the
sender label that ``obs-agentctl message`` stamps on messages from scripts. External callers
have no route, so a reply to the sender always failed with "recipient has no current route
binding".

obs-13 (/report 2026-10-18 23:43Z, topic 26042): "completion not waking agent". The route's
run loop logged TurnEnd #395 at 23:40:14 and only logged ``[flush_turn] sending`` for that
turn at 23:57:22. ``_flush_turn`` first awaits ``_flush_observability_buffer``, which sent
the buffered tool-status chunk at the lowest transport priority. The single global transport
worker serves strictly by priority, so under sustained higher-priority traffic (the night of
2026-10-18 had hundreds of timed-out sends) the inline flush starved, the run loop could not
reach the DoneEvent, the route stayed "running", and the inbox wake was only injected
17 minutes later.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from obs_agent.events import StatusEvent
from obs_agent.runner import TextEvent
from obs_agent.telegram import TelegramBot, TelegramRoute, _PRIORITY_ASSISTANT
from obs_agent.tools import create_obs_tools

_TEST_GAP = 0.05


def _send_inbox_tool(bot: TelegramBot, route: TelegramRoute, config):
    state = bot._get_state(route)
    tools = create_obs_tools(config, lambda: None, hook_state=state.hook_state, return_tools=True)
    return {tool.name: tool.handler for tool in tools}["SendInboxMessage"]


class TestObs12ReplyToExternalSender:
    async def test_reply_to_external_sender_is_delivered_to_its_outbox(self, config, tmp_path, monkeypatch):
        monkeypatch.setenv("OBS_CONTROL_DIR", str(tmp_path / "obs-control"))
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        send = _send_inbox_tool(bot, TelegramRoute(chat_id=-100123, thread_id=371524), config)

        result = await send({
            "team_name": "2026-10-03-23-39-check-rexordings",
            "recipient": "external:glm-liveness",
            "content": "live",
            "summary": "live",
            "sender": "061a2f1c5d-fix-ingestion-contention-icloud-clearing",
        })

        meta = result["tool_use_result"]
        assert meta["delivered"] is True, result
        assert meta["outcome"] == "external_reply"
        outbox = tmp_path / "obs-control" / "external-replies" / "glm-liveness.jsonl"
        rows = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["text"] == "live"
        assert rows[-1]["from"] == "061a2f1c5d-fix-ingestion-contention-icloud-clearing"
        assert rows[-1]["team_name"] == "2026-10-03-23-39-check-rexordings"
        await bot.shutdown()

    async def test_unknown_agent_recipient_is_still_underdelivered(self, config, tmp_path, monkeypatch):
        """The fix only covers ``external:`` senders; every other path is unchanged."""
        monkeypatch.setenv("OBS_CONTROL_DIR", str(tmp_path / "obs-control"))
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        send = _send_inbox_tool(bot, TelegramRoute(chat_id=-100123, thread_id=1), config)

        result = await send({
            "team_name": "some-team",
            "recipient": "no-such-agent",
            "content": "hello",
        })

        meta = result["tool_use_result"]
        assert meta["delivered"] is False
        assert meta["outcome"] == "underdelivered"
        assert not (tmp_path / "obs-control" / "external-replies").exists()
        await bot.shutdown()

    async def test_external_name_cannot_escape_the_outbox(self, config, tmp_path, monkeypatch):
        monkeypatch.setenv("OBS_CONTROL_DIR", str(tmp_path / "obs-control"))
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        send = _send_inbox_tool(bot, TelegramRoute(chat_id=-100123, thread_id=1), config)

        result = await send({
            "team_name": "t",
            "recipient": "external:../../etc/passwd",
            "content": "x",
        })

        meta = result["tool_use_result"]
        assert meta["delivered"] is True
        written = list((tmp_path / "obs-control" / "external-replies").iterdir())
        assert len(written) == 1
        assert written[0].parent == tmp_path / "obs-control" / "external-replies"
        await bot.shutdown()


class TestObs13InlineObservabilityFlushStarvation:
    async def test_turn_flush_is_not_starved_by_higher_priority_traffic(self, config):
        bot = TelegramBot(config, fragment_gap=_TEST_GAP, enable_background_poller=False)
        bot._transport_base_chat_interval_seconds = 0.0

        async def slow_send(**kwargs):
            await asyncio.sleep(0.01)
            return MagicMock(message_id=int(time.monotonic() * 1000) % 100000)

        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock(side_effect=slow_send)

        busy_route = TelegramRoute(chat_id=-200, thread_id=1)
        route = TelegramRoute(chat_id=-300, thread_id=26042)
        state = bot._get_state(route)
        state.last_bot = fake_bot

        stop = asyncio.Event()

        async def flood():
            # Other routes keep the global transport worker busy at assistant priority,
            # the way the 2026-10-18 night's timed-out sends did.
            while not stop.is_set():
                asyncio.ensure_future(bot._enqueue_send(
                    route=busy_route, text="other route", disable_notification=False,
                    parse_mode=None, reply_to_message_id=None, fallback_bot=fake_bot,
                    priority=_PRIORITY_ASSISTANT,
                ))
                await asyncio.sleep(0.004)

        flooder = asyncio.create_task(flood())
        await asyncio.sleep(0.1)  # the worker now always has assistant-priority work queued

        # A tool-status turn is buffered, then the final text turn forces the inline flush.
        await bot._flush_turn(
            state=state, bot=fake_bot,
            turn_items=[StatusEvent(type="tool_use", summary="Bash: git push")],
        )
        started = time.monotonic()
        flush = asyncio.create_task(bot._flush_turn(
            state=state, bot=fake_bot, turn_items=[TextEvent(text="Fully closed.")],
        ))
        done, _ = await asyncio.wait({flush}, timeout=1.5)
        elapsed = time.monotonic() - started
        stop.set()
        await flooder
        await flush
        await bot.shutdown()

        assert flush in done, (
            f"inline turn flush starved for {elapsed:.2f}s behind higher-priority transport traffic"
        )
