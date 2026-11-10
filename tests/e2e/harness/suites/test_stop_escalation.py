"""Stop-escalation survivor regression for obs-22.43 (F2), outside the smoke suite.

OBS reaches the disconnect path of `_escalate_stop` (src/obs_agent/telegram.py)
when a stopped turn is still active after OBS_STOP_KILL_GRACE_SECONDS. With the
default 30 s that happened only when Telegram flood control held the stopped turn
open (runs 20261022T190450Z-a2578d and 20261022T225255Z-ac16a7), and both times
`stop_cleanup` reported owned=1, survivors=1. The `incident-stop-grace` profile sets
the grace to 1 s, so every stop of a streaming turn takes that same path.

Run it with:
    obs-e2e run --code <worktree> --lanes 1 --suite incident-stop-escalation \
        --profile incident-stop-grace --tests tests/e2e/harness/suites/test_stop_escalation.py

On the base it is an expected failure (strict xfail through known_bugs.yaml); once
the defect is fixed it passes unexpectedly, which fails the run until the marker
is removed. A run without the short grace, or a stop that never reaches the
disconnect path, is skipped, and a skip is never green.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.e2e.harness.suites.test_smoke import _assert_partial_number_answer, _nonce

pytestmark = [pytest.mark.suite("incident-stop-escalation"), pytest.mark.needs(all_lanes=False, credential="dev")]

FIXTURE = "/workspace/runtime/git/obs-vault-active/CLAUDE.md"


@pytest.mark.known_bug("obs-22.43")
async def test_stop_escalation_reaps_owned_cli_obs_22_43(lane):
    grace = (lane.profile.lane_env or {}).get("OBS_STOP_KILL_GRACE_SECONDS")
    try:
        short = grace is not None and 0 < float(grace) <= 2
    except ValueError:
        short = False
    if not short:
        pytest.skip(f"needs a lane with OBS_STOP_KILL_GRACE_SECONDS <= 2 (profile incident-stop-grace); got {grace!r}")

    topic = await lane.new_topic()
    nonce = _nonce()
    primed = await lane.turn(topic, f"Reply with exactly: READY-{nonce}")
    assert f"READY-{nonce}" in primed.text, primed.messages
    sent = await lane.send(
        topic,
        f"First call the Read tool once on {FIXTURE}. Then write every integer from 1 through 8000, "
        "one number per line, with no other text. Do not abbreviate or omit numbers.",
    )
    start = await lane.wait("turn_start", match={"trigger_message_id": sent.message_id})
    session_id = start.get("session_id") or primed.end["session_id"]
    cursor = float(start["ts"])
    async with asyncio.timeout(120):
        while True:
            output = await lane.wait("jsonl_append", match={"session_id": session_id, "type": "assistant"}, after=cursor)
            blocks = (output["row"].get("message") or {}).get("content") or []
            if any(isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Read"
                   and (block.get("input") or {}).get("file_path") == FIXTURE for block in blocks):
                break
            cursor = output
    stop = await lane.command(topic, "/stop")
    ack, end, cleanup = await asyncio.gather(
        lane.wait("telegram_send", agent=topic, match=lambda event: "interrupt sent" in (event.get("text_head") or "").lower(), after=float(output["ts"])),
        lane.wait("turn_end", match={"trigger_message_id": sent.message_id}, after=start),
        lane.wait("stop_cleanup", agent=topic, after=start),
    )
    proof = {"run_id": lane.manifest["run_id"], "lane": lane.name, "topic_id": topic.topic_id, "grace_s": grace,
             "stop_message_id": stop.message_id, "ack": ack, "stopped_turn_end": end, "stop_cleanup": cleanup}
    path = lane.run_dir / "tests" / lane.name / "stop-escalation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, indent=2, default=str) + "\n")
    if cleanup.get("outcome") != "disconnected":
        pytest.skip(f"the stop never reached the escalation's disconnect path: {cleanup.get('outcome')!r}")

    assert ack["ts"] >= output["ts"] and end["ts"] >= output["ts"]
    replies = await lane.telegram.messages(topic, since=sent, from_bot=True)
    _assert_partial_number_answer(replies)
    # The stopped agent is idle, not dead: the next message gets a normal turn.
    after = await lane.turn(topic, f"Reply with exactly: AFTER-{nonce}")
    assert f"AFTER-{nonce}" in after.text, after.messages
    proof["after_turn_end"] = after.end
    path.write_text(json.dumps(proof, indent=2, default=str) + "\n")
    # obs-22.43: the escalation must leave no owned process alive.
    assert cleanup.get("survivors") == 0, cleanup
