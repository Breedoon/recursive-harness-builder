"""Model-free ingestion through real normalization, buffers and route dispatch."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from obs_agent.telegram import TelegramBot, TelegramRoute


class Attachment:
    file_unique_id = "synthetic"
    file_name = "synthetic.dat"
    mime_type = "application/octet-stream"
    file_size = 4

    def __init__(self, entered=None, release=None):
        self.entered = entered
        self.release = release

    async def get_file(self):
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        return self

    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(b"test")
        return Path(custom_path)


def update(mid, *, text=None, kind=None, attachment=None, album=None):
    message = SimpleNamespace(
        message_id=mid, chat_id=67890, message_thread_id=None,
        chat=SimpleNamespace(title="Synthetic"), text=text, caption=None,
        reply_to_message=None, media_group_id=album, document=None, photo=[],
        voice=None, audio=None, video=None, video_note=None, animation=None,
        sticker=None, effective_attachment=attachment,
    )
    if kind == "photo":
        message.photo = [attachment]
    elif kind:
        setattr(message, kind, attachment)
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=12345, full_name="Synthetic User", username=None),
    )


@pytest.fixture
def ingress(config, monkeypatch):
    bot = TelegramBot(config, fragment_gap=0.001, enable_background_poller=False)
    bot._media_group_buffer._gap = 0.001
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    monkeypatch.setattr(bot, "_send_received_marker", AsyncMock(return_value=[900]))
    monkeypatch.setattr(bot, "_maybe_finalize_joined_allowed_user", AsyncMock())
    delivered = []

    async def dispatch(**kwargs):
        delivered.append((kwargs["trigger_message"].telegram_message_id, kwargs["user_text"]))

    monkeypatch.setattr(bot, "_run_and_send", dispatch)
    return bot, context, delivered


async def wait(event):
    await asyncio.wait_for(event.wait(), timeout=2)


@pytest.mark.parametrize(
    "kind", ["voice", "document", "photo", "audio", "video", "video_note", "animation", "sticker"],
)
async def test_delayed_normalization_survives_newer_text(ingress, monkeypatch, kind):
    bot, context, delivered = ingress
    entered, release = asyncio.Event(), asyncio.Event()
    attachment = Attachment(entered, release)

    async def transcribe(*, title, dest_dir, **kwargs):
        (dest_dir / f"{title}.md").write_text("synthetic transcript")

    monkeypatch.setattr(bot._normalizer, "_run_transcription", transcribe)
    older = update(101, kind=kind, attachment=attachment)
    task = asyncio.create_task(bot.handle_message(older, context))
    try:
        await wait(entered)
        await bot.handle_message(update(102, text="newer text"), context)
        assert [mid for mid, _ in delivered] == [102]
        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert [mid for mid, _ in delivered] == [102, 101]
        assert bot._last_inbound_message_id_by_route[TelegramRoute(67890)] == 102
        await bot.handle_message(older, context)
        assert [mid for mid, _ in delivered] == [102, 101]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_two_voices_reverse_transcription_completion(ingress, monkeypatch):
    bot, context, delivered = ingress
    entered = {201: asyncio.Event(), 202: asyncio.Event()}
    release = {201: asyncio.Event(), 202: asyncio.Event()}
    calls = []

    async def transcribe(*, audio_file, title, dest_dir, **kwargs):
        mid = int(audio_file.parent.name)
        calls.append(mid)
        entered[mid].set()
        await release[mid].wait()
        (dest_dir / f"{title}.md").write_text(f"synthetic transcript {mid}")

    monkeypatch.setattr(bot._normalizer, "_run_transcription", transcribe)
    older = update(201, kind="voice", attachment=Attachment())
    newer = update(202, kind="voice", attachment=Attachment())
    tasks = [asyncio.create_task(bot.handle_message(older, context))]
    try:
        await wait(entered[201])
        tasks.append(asyncio.create_task(bot.handle_message(newer, context)))
        await wait(entered[202])
        release[202].set()
        await asyncio.wait_for(tasks[1], timeout=2)
        release[201].set()
        await asyncio.wait_for(tasks[0], timeout=2)
        assert [mid for mid, _ in delivered] == [202, 201]
        assert calls == [201, 202]
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_duplicate_voice_is_refused_before_second_transcription(ingress, monkeypatch):
    bot, context, delivered = ingress
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def transcribe(*, title, dest_dir, **kwargs):
        calls.append(title)
        entered.set()
        await release.wait()
        (dest_dir / f"{title}.md").write_text("synthetic directive")

    monkeypatch.setattr(bot._normalizer, "_run_transcription", transcribe)
    voice = update(251, kind="voice", attachment=Attachment())
    task = asyncio.create_task(bot.handle_message(voice, context))
    try:
        await wait(entered)
        await asyncio.wait_for(bot.handle_message(voice, context), timeout=0.2)
        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert len(calls) == 1
        assert [mid for mid, _ in delivered] == [251]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_album_normalization_survives_newer_text_and_deduplicates_items(ingress):
    bot, context, delivered = ingress
    entered, release = asyncio.Event(), asyncio.Event()
    first = update(301, kind="document", attachment=Attachment(entered, release), album="album")
    second = update(302, kind="document", attachment=Attachment(), album="album")
    task = asyncio.create_task(bot.handle_message(first, context))
    try:
        for _ in range(20):
            if bot._media_group_buffer._pending:
                break
            await asyncio.sleep(0)
        assert bot._media_group_buffer._pending
        await bot.handle_message(second, context)
        await bot.handle_message(second, context)
        await wait(entered)
        await bot.handle_message(update(303, text="newer text"), context)
        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert [mid for mid, _ in delivered] == [303, 301]
        assert delivered[1][1].count("Original filename:") == 2
        await bot.handle_message(first, context)
        await bot.handle_message(second, context)
        assert [mid for mid, _ in delivered] == [303, 301]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_text_fragments_preserve_concat_and_reject_exact_replays(ingress):
    bot, context, delivered = ingress
    first = update(401, text="a" * 4096)
    second = update(402, text="tail")
    task = asyncio.create_task(bot.handle_message(first, context))
    try:
        # Wait for buffer insertion, not a wall-clock normalization delay.
        for _ in range(20):
            if bot._fragment_buffer._pending:
                break
            await asyncio.sleep(0)
        assert bot._fragment_buffer._pending
        await bot.handle_message(second, context)
        await bot.handle_message(second, context)
        await asyncio.wait_for(task, timeout=2)
        assert len(delivered) == 1
        assert delivered[0][1].endswith("a" * 4096 + "tail")
        await bot.handle_message(first, context)
        await bot.handle_message(second, context)
        assert len(delivered) == 1
        assert bot._last_inbound_message_id_by_route[TelegramRoute(67890)] == 402
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("fragmented", [False, True])
async def test_restart_keeps_completed_high_watermark_replay_policy(ingress, monkeypatch, fragmented):
    bot, context, delivered = ingress
    if fragmented:
        task = asyncio.create_task(bot.handle_message(update(500, text="a" * 4096), context))
        for _ in range(20):
            if bot._fragment_buffer._pending:
                break
            await asyncio.sleep(0)
        assert bot._fragment_buffer._pending
        await bot.handle_message(update(501, text="tail"), context)
        await asyncio.wait_for(task, timeout=2)
    else:
        await bot.handle_message(update(501, text="completed"), context)
    await bot.shutdown()
    restored = TelegramBot(bot._config, fragment_gap=0.001, enable_background_poller=False)
    await restored.initialize_runtime()
    monkeypatch.setattr(restored, "_send_received_marker", AsyncMock(return_value=[900]))
    monkeypatch.setattr(restored, "_maybe_finalize_joined_allowed_user", AsyncMock())
    run = AsyncMock()
    monkeypatch.setattr(restored, "_run_and_send", run)
    try:
        await restored.handle_message(update(500, text="stale"), context)
        await restored.handle_message(update(501, text="duplicate"), context)
        run.assert_not_awaited()
        await restored.handle_message(update(502, text="fresh"), context)
        run.assert_awaited_once()
    finally:
        await restored.shutdown()


async def test_same_text_distinct_ids_are_not_duplicates(ingress):
    bot, context, delivered = ingress
    await bot.handle_message(update(551, text="same directive"), context)
    await bot.handle_message(update(552, text="same directive"), context)
    assert [mid for mid, _ in delivered] == [551, 552]


async def test_delayed_voice_enters_real_turn_delivery_pipeline(ingress, monkeypatch):
    from obs_agent.runner import DoneEvent, TextEvent, TurnEndEvent

    bot, context, _ = ingress
    monkeypatch.setattr(bot, "_run_and_send", TelegramBot._run_and_send.__get__(bot))
    prompts = []

    class ModelFreeRunner:
        def __init__(self, *args, **kwargs):
            self.remaining_pending = []

        async def run(self, text):
            prompts.append(text)
            yield TextEvent(text="synthetic answer")
            yield TurnEndEvent()
            yield DoneEvent()

    monkeypatch.setattr("obs_agent.telegram.ConversationRunner", ModelFreeRunner)
    entered, release = asyncio.Event(), asyncio.Event()

    async def transcribe(*, title, dest_dir, **kwargs):
        entered.set()
        await release.wait()
        (dest_dir / f"{title}.md").write_text("earlier synthetic directive")

    monkeypatch.setattr(bot._normalizer, "_run_transcription", transcribe)
    older = update(601, kind="voice", attachment=Attachment())
    task = asyncio.create_task(bot.handle_message(older, context))
    try:
        await wait(entered)
        await bot.handle_message(update(602, text="later synthetic directive"), context)
        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert len(prompts) == 2
        assert "later synthetic directive" in prompts[0]
        assert "earlier synthetic directive" in prompts[1]
        await bot.handle_message(older, context)
        assert len(prompts) == 2
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await bot.shutdown()


async def test_delayed_voice_is_queued_once_when_newer_turn_is_busy(ingress, monkeypatch):
    bot, context, delivered = ingress
    entered, release = asyncio.Event(), asyncio.Event()
    older = update(701, kind="document", attachment=Attachment(entered, release))
    task = asyncio.create_task(bot.handle_message(older, context))
    try:
        await wait(entered)
        await bot.handle_message(update(702, text="newer"), context)
        state = bot._get_state(TelegramRoute(67890))
        state.busy = True
        release.set()
        await asyncio.wait_for(task, timeout=2)
        await bot.handle_message(older, context)
        assert [mid for mid, _ in delivered] == [702]
        queued = state.hook_state.message_queue.get_nowait()
        assert queued.telegram_message_id == 701
        assert state.hook_state.message_queue.empty()
        assert bot._last_inbound_message_id_by_route[TelegramRoute(67890)] == 702
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_voice_normalization_can_be_retried(ingress, monkeypatch):
    bot, context, delivered = ingress
    entered, release = asyncio.Event(), asyncio.Event()

    async def transcribe(*, title, dest_dir, **kwargs):
        entered.set()
        await release.wait()
        (dest_dir / f"{title}.md").write_text("synthetic retry")

    monkeypatch.setattr(bot._normalizer, "_run_transcription", transcribe)
    voice = update(801, kind="voice", attachment=Attachment())
    task = asyncio.create_task(bot.handle_message(voice, context))
    try:
        await wait(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await bot.handle_message(voice, context)
        assert [mid for mid, _ in delivered] == [801]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
