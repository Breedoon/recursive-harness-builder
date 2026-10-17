"""vault-wlh: schedules accept a prompt_file that is read at every fire.

Spec (bead vault-wlh, Daniel 2026-10-17): CronCreate takes ``prompt``, ``prompt_file`` or both.
When both are given the inline prompt goes first and the file content follows. The file is read
at fire time (so a memory file can BE the wake prompt), persisted with the schedule, shown in
CronList, and an unreadable file fails that fire visibly without resetting the session.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute, _RunOutcome, _TopicScheduleRecord

_GAP = 0.05


@pytest.fixture(autouse=True)
def _no_real_kills(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError(f"test reached a real kill path: {args!r}")

    monkeypatch.setattr(os, "kill", _refuse)
    monkeypatch.setattr(os, "killpg", _refuse)
    monkeypatch.setattr("obs_agent.maintenance_restart.kill_process_tree", _refuse)


def _bot(config, thread_id):
    bot = TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)
    route = TelegramRoute(chat_id=67890, thread_id=thread_id)
    state = bot._get_state(route)
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    state.last_bot = fake_bot
    return bot, route


def _record(route, *, prompt="", prompt_file=None, reset_session=False, schedule_id="sched-pf"):
    return _TopicScheduleRecord(
        schedule_id=schedule_id,
        route=route,
        description="MemoryWake",
        schedule_mode="interval",
        cron_expr=None,
        trigger_kind="interval",
        interval_seconds=600,
        prompt=prompt,
        prompt_file=prompt_file,
        reset_session=reset_session,
        max_runs=5,
        next_run_at=0.0,
    )


async def _fire(bot, record):
    run_mock = AsyncMock(return_value=_RunOutcome(assistant_text="ok"))
    with patch.object(bot, "_run_and_send", new=run_mock), patch.object(
        bot, "_send_system_message", new=AsyncMock()
    ) as send_mock:
        ran = await bot._execute_topic_schedule(record=record, trigger_kind="interval")
    texts = [str(call.kwargs.get("text", "")) for call in send_mock.await_args_list]
    return ran, run_mock, texts


def _sent_prompt(run_mock) -> str:
    return str(run_mock.await_args.kwargs["user_text"])


class TestPromptFileFire:
    async def test_file_only_is_read_at_fire_time(self, config, tmp_path):
        memory = tmp_path / "memory.md"
        memory.write_text("version one", encoding="utf-8")
        bot, route = _bot(config, 9101)
        record = _record(route, prompt_file=str(memory))
        bot._register_topic_schedule(record)

        ran, run_mock, _ = await _fire(bot, record)
        assert ran is True
        assert "version one" in _sent_prompt(run_mock)

        memory.write_text("version two", encoding="utf-8")
        ran, run_mock, _ = await _fire(bot, record)
        sent = _sent_prompt(run_mock)
        assert "version two" in sent and "version one" not in sent

    async def test_inline_prompt_goes_first_then_file(self, config, tmp_path):
        memory = tmp_path / "memory.md"
        memory.write_text("FILE BODY", encoding="utf-8")
        bot, route = _bot(config, 9102)
        record = _record(route, prompt="INLINE HEAD", prompt_file=str(memory))
        bot._register_topic_schedule(record)

        _, run_mock, _ = await _fire(bot, record)
        sent = _sent_prompt(run_mock)
        assert sent.index("INLINE HEAD") < sent.index("FILE BODY")
        assert f'<schedule_prompt_file path="{memory}">' in sent

    async def test_vault_relative_path_resolves_against_vault(self, config):
        rel = "Drafts/_schedule_prompt_file_test.md"
        target = config.vault_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("from the vault", encoding="utf-8")
        try:
            bot, route = _bot(config, 9103)
            record = _record(route, prompt_file=rel)
            bot._register_topic_schedule(record)
            _, run_mock, _ = await _fire(bot, record)
            assert "from the vault" in _sent_prompt(run_mock)
        finally:
            target.unlink()

    async def test_missing_file_fails_visibly_and_keeps_session(self, config, tmp_path):
        bot, route = _bot(config, 9104)
        record = _record(route, prompt_file=str(tmp_path / "gone.md"), reset_session=True)
        bot._register_topic_schedule(record)
        with patch.object(bot, "_reset_route_state", new=AsyncMock()) as reset_mock:
            ran, run_mock, texts = await _fire(bot, record)
        assert ran is False
        run_mock.assert_not_awaited()
        reset_mock.assert_not_awaited()
        assert "prompt_file unreadable" in (record.last_error or "")
        assert any(text.startswith("schedule failed") for text in texts)

    async def test_inline_only_unchanged(self, config):
        bot, route = _bot(config, 9105)
        record = _record(route, prompt="plain wake")
        bot._register_topic_schedule(record)
        _, run_mock, _ = await _fire(bot, record)
        assert _sent_prompt(run_mock) == "(System: scheduled execution.)\n\nplain wake"


class TestPromptFilePersistence:
    async def test_prompt_file_survives_restart_and_shows_in_payload(self, config, tmp_path):
        memory = tmp_path / "memory.md"
        memory.write_text("x", encoding="utf-8")
        bot, route = _bot(config, 9201)
        bot._state_store.initialize()
        record = _record(route, prompt_file=str(memory))
        bot._register_topic_schedule(record)
        await _fire(bot, record)  # a fire persists the record, as in production
        ids = [e.schedule_id for e in bot._state_store.load_snapshot().topic_schedules]
        assert "sched-pf" in ids, ids

        restored = TelegramBot(config, fragment_gap=_GAP, enable_background_poller=False)
        restored._state_store.initialize()
        restored._restore_state_from_store()
        again = restored._topic_schedules_by_id["sched-pf"]
        assert again.prompt_file == str(memory)
        assert restored._schedule_summary_payload(again)["prompt_file"] == str(memory)


class TestCronCreate:
    async def test_create_with_prompt_file_only(self, config, tmp_path):
        memory = tmp_path / "memory.md"
        memory.write_text("x", encoding="utf-8")
        bot, route = _bot(config, 9301)
        with patch.object(bot, "_send_system_message", new=AsyncMock()):
            result = await bot._cron_create(
                route=route,
                args={"schedule_mode": "interval", "interval_seconds": 60, "prompt_file": str(memory),
                      "description": "[ops-test] prompt-file fixture"},
            )
        assert not result.get("is_error"), result
        created = [r for r in bot._topic_schedules_by_id.values() if r.route == route]
        assert len(created) == 1 and created[0].prompt_file == str(memory) and created[0].prompt == ""

    async def test_create_rejects_unreadable_file_and_neither(self, config, tmp_path):
        bot, route = _bot(config, 9302)
        with patch.object(bot, "_send_system_message", new=AsyncMock()):
            bad = await bot._cron_create(
                route=route,
                args={"schedule_mode": "interval", "interval_seconds": 60, "prompt_file": str(tmp_path / "no.md")},
            )
            neither = await bot._cron_create(
                route=route, args={"schedule_mode": "interval", "interval_seconds": 60}
            )
        assert bad.get("is_error") and "cannot read prompt_file" in str(bad)
        assert neither.get("is_error") and "prompt is required unless prompt_file is given" in str(neither)
