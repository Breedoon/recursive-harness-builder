"""obs-e2e smoke suite (E1 WP10): send/reply, /stop and a fork, on every lane.

Every wait is on an event-tap event or a Telegram message the runner reads after
the matching ``telegram_send``; timeouts only ever fail a test (R10).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.suite("smoke"), pytest.mark.needs(all_lanes=True)]


def _nonce() -> str:
    return uuid.uuid4().hex[:8].upper()


async def test_send_reply_then_follow_up(lane):
    topic = await lane.new_topic()
    nonce = _nonce()
    first = await lane.turn(topic, f"Reply with exactly: PONG-{nonce}")
    assert first.end.get("failed") is False, first.end
    assert f"PONG-{nonce}" in first.text, first.messages

    second = await lane.turn(topic, f"Reply with exactly: PING-{nonce}")
    assert f"PING-{nonce}" in second.text, second.messages
    # Negative assertion (design-v3 §2.3): after the first turn ended, nothing more
    # was sent in this topic before the next turn started.
    lane.assert_none("telegram_send", agent=topic, after=first.end, before=second.start)
    # The follow-up resumed the same conversation.
    assert second.end.get("session_id") == first.end.get("session_id"), (first.end, second.end)


async def test_stop_interrupts_running_turn(lane):
    topic = await lane.new_topic()
    nonce = _nonce()
    sent = await lane.send(
        topic,
        "Write the whole numbered list from 1 to 3000, one number per line, with no other text.",
    )
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    stop = await lane.command(topic, "/stop")
    end = await lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start)
    cleanup = await lane.wait("stop_cleanup", agent=topic, after=start)
    assert cleanup.get("survivors") == 0, cleanup
    replies = await lane.telegram.messages(topic, since=sent, from_bot=True)
    assert not any("3000" in (m.get("text") or "") for m in replies), "the turn ran to completion; /stop did not interrupt it"
    assert end["ts"] >= start["ts"]

    # The stopped agent is idle, not dead: the next message gets a normal turn.
    after = await lane.turn(topic, f"Reply with exactly: AFTER-{nonce}")
    assert f"AFTER-{nonce}" in after.text, after.messages
    assert stop.message_id > sent.message_id


async def test_fork_inherits_context(lane):
    topic = await lane.new_topic()
    code = "K" + _nonce()
    first = await lane.turn(topic, f"Remember this codeword for later: {code}. Reply with exactly: NOTED")
    assert "NOTED" in first.text, first.messages

    mark = lane.events.mark()
    await lane.command(topic, "/fork")
    child_send = await lane.wait(
        "telegram_send",
        match=lambda e: e.get("chat_id") == topic.chat_id and e.get("topic_id") not in (None, topic.topic_id),
        after=mark,
    )
    child_topic = int(child_send["topic_id"])
    answer = await lane.turn(child_topic, "What is the codeword I asked you to remember? Reply with only the codeword.")
    assert code in answer.text, answer.messages
    assert answer.end.get("session_id") not in (None, first.end.get("session_id")), (first.end, answer.end)
