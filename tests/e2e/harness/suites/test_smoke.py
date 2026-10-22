"""obs-e2e smoke suite (E1 WP10): send/reply, /stop and a fork, on every lane.

Every wait is on an event-tap event or a Telegram message the runner reads after
the matching ``telegram_send``; timeouts only ever fail a test (R10).
"""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest

pytestmark = [pytest.mark.suite("smoke"), pytest.mark.needs(all_lanes=True)]


def _nonce() -> str:
    return uuid.uuid4().hex[:8].upper()


def _number_turn_output(event: dict) -> bool:
    head = event.get("text_head") or ""
    return "thinking" in head.lower() or bool(re.match(r"^\s*\d+(?:\s|$)", head))


def _assert_partial_number_answer(messages: list[dict], total: int = 8000) -> None:
    text = "\n".join(message.get("text") or "" for message in messages)
    numbers = [int(value) for value in re.findall(r"(?m)^\s*(\d+)\s*$", text)]
    assert len(numbers) < total // 2 and max(numbers, default=0) < total // 2, (
        f"/stop did not interrupt the numbered answer: count={len(numbers)}, last={max(numbers, default=0)}, total={total}"
    )


async def test_send_reply_then_follow_up(lane):
    topic = await lane.new_topic()
    nonce = _nonce()
    first = await lane.turn(topic, f"Reply with exactly: PONG-{nonce}")
    assert first.end.get("failed") is False, first.end
    assert f"PONG-{nonce}" in first.text, first.messages

    second = await lane.turn(topic, f"Reply with exactly: PING-{nonce}")
    assert f"PING-{nonce}" in second.text, second.messages
    # The shared negative-window API excludes the next input's receipt acknowledgement.
    lane.assert_none("telegram_send", agent=topic, after=first.end, before=second.start)
    # The follow-up resumed the same conversation.
    assert second.end.get("session_id") == first.end.get("session_id"), (first.end, second.end)


async def test_stop_interrupts_running_turn(lane):
    topic = await lane.new_topic()
    nonce = _nonce()
    sent = await lane.send(
        topic,
        "Write every integer from 1 through 8000, one number per line, with no other text. Do not abbreviate or omit numbers.",
    )
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    output = await lane.wait("telegram_send", agent=topic, match=_number_turn_output, after=start)
    stop = await lane.command(topic, "/stop")
    ack, end, cleanup = await asyncio.gather(
        lane.wait("telegram_send", agent=topic, match=lambda event: "interrupt sent" in (event.get("text_head") or "").lower(), after=output),
        lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start),
        lane.wait("stop_cleanup", agent=topic, after=start),
    )
    assert cleanup.get("survivors") == 0, cleanup
    assert ack["ts"] >= output["ts"] and end["ts"] >= output["ts"]
    replies = await lane.telegram.messages(topic, since=sent, from_bot=True)
    _assert_partial_number_answer(replies)

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
