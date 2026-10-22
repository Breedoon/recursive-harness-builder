"""Preflight /stop regression for obs-22.24, outside the streaming smoke contract."""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e.harness.suites.test_smoke import _assert_partial_number_answer

pytestmark = [pytest.mark.suite("incident"), pytest.mark.needs(all_lanes=False, credential="dev")]


@pytest.mark.known_bug("obs-22.24")
async def test_preflight_stop_obs_22_24(lane):
    topic = await lane.new_topic()
    sent = await lane.send(topic, "Write the whole numbered list from 1 to 3000, one number per line, with no other text.")
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    await lane.command(topic, "/stop")
    ack, end, cleanup = await asyncio.gather(
        lane.wait("telegram_send", agent=topic, match=lambda event: "interrupt sent" in (event.get("text_head") or "").lower(), after=start),
        lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start),
        lane.wait("stop_cleanup", agent=topic, after=start),
    )
    assert cleanup.get("survivors") == 0, cleanup
    assert ack["ts"] >= start["ts"] and end["ts"] >= start["ts"]
    replies = await lane.telegram.messages(topic, since=sent, from_bot=True)
    _assert_partial_number_answer(replies, total=3000)
