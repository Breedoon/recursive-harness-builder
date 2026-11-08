"""obs-52: a content refusal must not drive an endless session-recovery loop.

On 2026-10-25 a Usage-Policy refusal made run-start recovery fork the
proc-work trunk nine times in 18 minutes; every fork re-sent the same context
and was refused within seconds, and every one copied the 4.8 MB JSONL. The row
shapes below are the CLI's own (stub, refusal and API-error rows copied from
sessions 9f50dccc and 2af23bcc with their content replaced).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from obs_agent.jsonl_health import (
    _refusal_streak_path,
    analyze_jsonl_path,
    content_refusal_after,
    futile_refusal_streak,
    session_has_own_answer,
)
from obs_agent.queueing import QueuedMessage
from obs_agent.telegram import ContentRefusalLoopError, TelegramBot, TelegramRoute

_GAP = 0.05
USAGE_POLICY = (
    "API Error: Claude Code is unable to respond to this request, which appears to violate our "
    "Usage Policy (https://www.anthropic.com/legal/aup). Try rephrasing the request or attempting "
    "a different approach. If you are seeing this refusal repeatedly, try running /model "
    "claude-sonnet-4-20250514 to switch models."
)
CYBER_FLAG = (
    'API Error: 400 {"type":"error","error":{"type":"invalid_request","message":"This content was '
    "flagged for possible cybersecurity risk. If this seems wrong, try rephrasing your request.\"}}"
)


class _Rows:
    """Writes CLI-shaped rows of one session file, chaining parentUuid."""

    def __init__(self, path, session_id, parent=None):
        self.path, self.sid, self.parent, self.n = path, session_id, parent, 0

    def _write(self, obj):
        self.n += 1
        obj.setdefault("uuid", f"{self.sid[:6]}-{self.n}")
        obj["parentUuid"] = self.parent
        obj["sessionId"] = self.sid
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(obj) + "\n")
        self.parent = obj["uuid"]
        return obj["uuid"]

    def user(self, text):
        return self._write({"type": "user", "message": {"role": "user", "content": text}})

    def answer(self, text):
        return self._write({"type": "assistant", "message": {
            "model": "claude-opus-5-5", "id": f"msg_{self.sid[:6]}{self.n}", "type": "message",
            "role": "assistant", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}})

    def stub(self):
        return self._write({"type": "assistant", "isApiErrorMessage": False, "message": {
            "model": "<synthetic>", "role": "assistant", "stop_reason": "stop_sequence", "type": "message",
            "content": [{"type": "text", "text": "No response requested."}]}})

    def refusal(self, text=USAGE_POLICY, with_refusal_row=True):
        if with_refusal_row:
            self._write({"type": "assistant", "requestId": f"req_{self.n}", "message": {
                "model": "claude-opus-5-5", "type": "message", "role": "assistant",
                "content": [{"type": "thinking", "thinking": "", "signature": "sig"}],
                "stop_reason": "refusal"}})
        return self._write({"type": "assistant", "isApiErrorMessage": True, "error": "invalid_request",
                            "message": {"model": "<synthetic>", "role": "assistant",
                                        "stop_reason": "stop_sequence", "type": "message",
                                        "content": [{"type": "text", "text": text}]}})


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = tmp_path / ".claude" / "projects" / "-fixture"
    path.mkdir(parents=True)
    return path


def _bot(config, sid):
    bot = TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)
    state = bot._get_state(TelegramRoute(chat_id=67890, thread_id=None))
    state.session_manager.set_session_id(sid)
    bot._bind_state_session(state)
    return bot, state


def _trunk(project_dir, text=USAGE_POLICY):
    rows = _Rows(project_dir / "sid-trunk.jsonl", "sid-trunk")
    rows.user("start")
    rows.answer("working on it")
    rows.user("next wake")
    rows.refusal(text)
    return rows


def _refused_retry(project_dir, sid, text=USAGE_POLICY, with_refusal_row=True):
    """What the recovered CLI writes: its stub, the new wake, a refusal."""
    path = project_dir / f"{sid}.jsonl"
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["uuid"]
    rows = _Rows(path, sid, parent=last)
    rows.stub()
    rows.user("another wake")
    rows.refusal(text, with_refusal_row=with_refusal_row)


def test_refusal_kinds_and_own_answers(project_dir):
    rows = _trunk(project_dir)
    health = analyze_jsonl_path(path=rows.path, session_id="sid-trunk")
    assert health.needs_recovery
    assert content_refusal_after(health, health.safe_recovery_uuid) == "usage_policy"
    assert session_has_own_answer(health) is True
    # the API-error row alone (no refusal row before it), cybersecurity flag
    other = _Rows(project_dir / "sid-cyber.jsonl", "sid-cyber")
    other.user("start")
    other.answer("ok")
    other.user("scan")
    other.refusal(CYBER_FLAG, with_refusal_row=False)
    health = analyze_jsonl_path(path=other.path, session_id="sid-cyber")
    assert content_refusal_after(health, health.safe_recovery_uuid) == "cybersecurity_flag"
    # an ordinary API error is not a content refusal
    plain = _Rows(project_dir / "sid-plain.jsonl", "sid-plain")
    plain.user("start")
    plain.answer("ok")
    plain.user("slow")
    plain.refusal("Request timed out", with_refusal_row=False)
    health = analyze_jsonl_path(path=plain.path, session_id="sid-plain")
    assert content_refusal_after(health, health.safe_recovery_uuid) is None


async def test_refusal_loop_stops_after_two_futile_retries(config, project_dir):
    _trunk(project_dir)
    bot, state = _bot(config, "sid-trunk")
    # the refused session answered before: a normal recovery
    first = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert first and state.session_id == first
    assert session_has_own_answer(analyze_jsonl_path(path=project_dir / f"{first}.jsonl",
                                                     session_id=first)) is False
    _refused_retry(project_dir, first)
    second = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert second and second != first
    _refused_retry(project_dir, second)
    files_before = sorted(p.name for p in project_dir.iterdir())
    with pytest.raises(ContentRefusalLoopError) as stopped:
        await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert stopped.value.kind == "usage_policy" and stopped.value.futile == 2
    # nothing forked, the route keeps its session; every later wake stops the same way
    assert state.session_id == second
    assert sorted(p.name for p in project_dir.iterdir()) == files_before
    with pytest.raises(ContentRefusalLoopError) as again:
        await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert str(again.value) == str(stopped.value)


async def test_an_answer_in_between_resets_the_count(config, project_dir):
    _trunk(project_dir, CYBER_FLAG)
    bot, state = _bot(config, "sid-trunk")
    first = await bot._recover_route_session_if_needed(state=state, source="run_start")
    _refused_retry(project_dir, first, CYBER_FLAG)
    second = await bot._recover_route_session_if_needed(state=state, source="run_start")
    # this retry got going: it answered, then was flagged again later
    path = project_dir / f"{second}.jsonl"
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["uuid"]
    rows = _Rows(path, second, parent=last)
    rows.user("wake")
    rows.answer("progress")
    rows.user("wake again")
    rows.refusal(CYBER_FLAG, with_refusal_row=False)
    third = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert third and third != second
    _refused_retry(project_dir, third, CYBER_FLAG)
    fourth = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert fourth and fourth != third  # one futile retry so far, not two


async def test_a_healthy_run_in_between_gives_a_new_budget(config, project_dir):
    _trunk(project_dir)
    bot, state = _bot(config, "sid-trunk")
    first = await bot._recover_route_session_if_needed(state=state, source="run_start")
    _refused_retry(project_dir, first)
    second = await bot._recover_route_session_if_needed(state=state, source="run_start")
    path = project_dir / f"{second}.jsonl"
    last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["uuid"]
    rows = _Rows(path, second, parent=last)
    rows.user("wake")
    rows.answer("fine now")
    assert await bot._recover_route_session_if_needed(state=state, source="run_start") is None
    # refused again later: two more retries before the stop, as for any new refusal
    rows.user("later wake")
    rows.refusal()
    third = await bot._recover_route_session_if_needed(state=state, source="run_start")
    _refused_retry(project_dir, third)
    fourth = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert third and fourth and fourth != third
    _refused_retry(project_dir, fourth)
    with pytest.raises(ContentRefusalLoopError):
        await bot._recover_route_session_if_needed(state=state, source="run_start")


async def _stopped(config, project_dir):
    _trunk(project_dir)
    bot, state = _bot(config, "sid-trunk")
    first = await bot._recover_route_session_if_needed(state=state, source="run_start")
    _refused_retry(project_dir, first)
    second = await bot._recover_route_session_if_needed(state=state, source="run_start")
    _refused_retry(project_dir, second)
    with pytest.raises(ContentRefusalLoopError):
        await bot._recover_route_session_if_needed(state=state, source="run_start")
    return bot, state


async def test_the_stop_survives_an_obs_restart(config, project_dir):
    _, state = await _stopped(config, project_dir)
    # fresh daemon objects over the same stored session: the count comes from
    # the session file and the streak recorded at its recovery, not from memory
    # (V1 finding)
    again, restored = _bot(config, state.session_id)
    with pytest.raises(ContentRefusalLoopError):
        await again._recover_route_session_if_needed(state=restored, source="run_start")
    assert restored.session_id == state.session_id


async def test_a_fresh_context_gets_its_own_budget(config, project_dir):
    bot, state = await _stopped(config, project_dir)
    await bot._reset_route_state(state)  # what /clear does
    assert await bot._recover_route_session_if_needed(state=state, source="run_start") is None
    fresh = _Rows(project_dir / "sid-unrelated.jsonl", "sid-unrelated")
    fresh.user("an unrelated fresh task")
    fresh.refusal()
    state.session_manager.set_session_id("sid-unrelated")
    bot._bind_state_session(state)
    retry = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert retry and retry != "sid-unrelated"


@pytest.mark.parametrize("point", ["first row", "last user row before the refusal"])
async def test_a_reply_that_rolls_back_past_the_refusals_gets_its_own_budget(config, project_dir, point):
    # V1 counterexample at 15452b2: a reply to the FIRST row of the stopped
    # session forks from before the refused part; its first refusal must not
    # inherit the discarded attempts (it was stopped at once, streak 3). The
    # same holds for a reply to the stopped retry's own last user row.
    bot, state = await _stopped(config, project_dir)
    old = state.session_id
    rows = [json.loads(x) for x in (project_dir / f"{old}.jsonl").read_text(encoding="utf-8").splitlines()]
    if point == "first row":
        target = rows[0]
    else:
        target = [r for r in rows if r.get("type") == "user" and r.get("sessionId") == old][-1]
    bot._record_message_binding(route=state.route, message_id=11, jsonl_uuid=target["uuid"],
                                session_id=old, role="user")
    bot._send_system_message = AsyncMock()
    proceed, _ = await bot._resolve_session_for_trigger(
        state=state, bot=MagicMock(),
        trigger_message=QueuedMessage(text="a different task from here", telegram_message_id=21,
                                      reply_to_message_id=11))
    assert proceed and state.session_id != old
    assert await bot._recover_route_session_if_needed(state=state, source="run_start") is None
    _refused_retry(project_dir, state.session_id)
    assert await bot._recover_route_session_if_needed(state=state, source="run_start")


async def test_later_work_in_the_source_session_does_not_change_the_count(config, project_dir):
    # V1 counterexample at 15452b2: the count was re-read from the source
    # session's current file, so an answer appended there later (never copied
    # into the child) unblocked the unchanged refused child.
    bot, state = await _stopped(config, project_dir)
    child = project_dir / f"{state.session_id}.jsonl"
    before = child.read_bytes()
    source_sid = json.loads(_refusal_streak_path(state.session_id).read_text())["source_session_id"]
    source = project_dir / f"{source_sid}.jsonl"
    last = json.loads(source.read_text(encoding="utf-8").splitlines()[-1])["uuid"]
    rows = _Rows(source, source_sid, parent=last)
    rows.n = 1000
    rows.user("independent later work in the source session")
    rows.answer("progress the child never saw")
    assert child.read_bytes() == before
    with pytest.raises(ContentRefusalLoopError):
        await bot._recover_route_session_if_needed(state=state, source="run_start")


async def test_only_content_refusal_recoveries_record_a_streak(config, project_dir):
    rows = _Rows(project_dir / "sid-trunk.jsonl", "sid-trunk")
    rows.user("start")
    rows.answer("ok")
    rows.user("slow")
    rows.refusal("Request timed out", with_refusal_row=False)
    bot, state = _bot(config, "sid-trunk")
    plain = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert plain and not _refusal_streak_path(plain).exists()
    # refused after an interrupted attempt: the interruption ended the streak
    _refused_retry(project_dir, plain)
    first = await bot._recover_route_session_if_needed(state=state, source="run_start")
    assert json.loads(_refusal_streak_path(first).read_text())["futile_before"] == 1
    _refused_retry(project_dir, first)
    with pytest.raises(ContentRefusalLoopError):
        await bot._recover_route_session_if_needed(state=state, source="run_start")


def test_an_unreadable_streak_record_counts_as_none(project_dir):
    a = _Rows(project_dir / "sid-a.jsonl", "sid-a")
    a.user("start")
    a.refusal()
    health = analyze_jsonl_path(path=a.path, session_id="sid-a")
    record = _refusal_streak_path("sid-a")
    record.parent.mkdir(parents=True, exist_ok=True)
    for text, expect in (("", 1), ("{", 1), ("[3]", 1), ('{"futile_before": -2}', 1),
                         ('{"futile_before": true}', 1), ('{"futile_before": "3"}', 1),
                         ('{"futile_before": 0}', 1), ('{"futile_before": 4}', 5)):
        record.write_text(text)
        assert futile_refusal_streak(health, health.safe_recovery_uuid) == ("usage_policy", expect), text
    record.unlink()
    assert futile_refusal_streak(health, health.safe_recovery_uuid) == ("usage_policy", 1)


async def test_an_inbox_wake_keeps_queued_work_when_the_loop_stops(config, project_dir):
    bot, state = await _stopped(config, project_dir)
    state.last_bot = MagicMock()
    state.hook_state.message_queue.put_nowait(QueuedMessage(text="queued-before-inbox-wake"))
    bot._send_system_message = AsyncMock()
    bot._send_system_html_message = AsyncMock(return_value=[])
    with pytest.raises(ContentRefusalLoopError):
        await bot._start_idle_route_inbox_wake(
            state=state, team_name="fixture-team", agent_name="fixture-agent",
            sender="fixture-sender", summary="wake", content="new inbox message")
    remaining = [m.text for m in state.pending_messages]
    while not state.hook_state.message_queue.empty():
        remaining.append(state.hook_state.message_queue.get_nowait().text)
    assert "queued-before-inbox-wake" in remaining


async def test_other_api_errors_keep_recovering(config, project_dir):
    rows = _Rows(project_dir / "sid-trunk.jsonl", "sid-trunk")
    rows.user("start")
    rows.answer("ok")
    rows.user("slow")
    rows.refusal("Request timed out", with_refusal_row=False)
    bot, state = _bot(config, "sid-trunk")
    sid = state.session_id
    for _ in range(4):
        new = await bot._recover_route_session_if_needed(state=state, source="run_start")
        assert new and new != sid
        _refused_retry(project_dir, new, "Request timed out", with_refusal_row=False)
        sid = new


async def test_run_start_posts_the_refusal_notice_once(config, project_dir):
    _trunk(project_dir)
    bot, state = _bot(config, "sid-trunk")
    first = await bot._recover_route_session_if_needed(state=state, source="run_start")
    _refused_retry(project_dir, first)
    second = await bot._recover_route_session_if_needed(state=state, source="run_start")
    _refused_retry(project_dir, second)
    bot._resolve_session_for_trigger = AsyncMock(return_value=(True, None))
    bot._send_system_message = AsyncMock()
    state.pending_messages = [QueuedMessage(text="inbox wake")]
    for _ in range(2):
        with pytest.raises(ContentRefusalLoopError):
            await bot._run_and_send(state=state, user_text="wake", bot=MagicMock(),
                                    trigger_message=QueuedMessage(text="wake", telegram_message_id=7))
    assert bot._send_system_message.await_count == 1
    text = bot._send_system_message.await_args.kwargs["text"]
    assert "Usage Policy" in text and "/clear" in text and "reply to an earlier message" in text
    # queued work stays with the route
    assert [m.text for m in state.pending_messages] == ["inbox wake"]
