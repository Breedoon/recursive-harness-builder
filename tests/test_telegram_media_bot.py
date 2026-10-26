from __future__ import annotations

import asyncio
import json

from obs_agent.telegram_media_bot import ALLOWLIST, MODEL_REGISTRY, SELECTABLE_MODELS, MediaBot, Settings, StateStore, _resolve_dimensions


def test_allowlist_is_exact_and_excludes_third_identity():
    assert ALLOWLIST == {227177188, 5129431382}
    assert 1350518665 not in ALLOWLIST


def test_model_registry_only_advertises_qualified_nondeprecated_paths():
    assert SELECTABLE_MODELS == ("qwen-image-2.1", "h3", "h3-beta1", "ltx")
    assert MODEL_REGISTRY["h3"]["label"] == "H3 Eros Max beta6 (checkpoint)"
    assert MODEL_REGISTRY["wan"]["deprecated"] is True
    assert MODEL_REGISTRY["ltx"]["qualified"] is True


def test_settings_keyboard_rebuilds_model_label_after_cycle(tmp_path):
    bot = MediaBot(token="", state=StateStore(tmp_path / "state.sqlite3"), api=None, result_root=tmp_path / "results")
    h3_keyboard = bot._settings_keyboard(5129431382, Settings(model="h3", media_kind="video"))
    qwen_keyboard = bot._settings_keyboard(5129431382, Settings(model="qwen-image-2.1", media_kind="image"))
    assert h3_keyboard.inline_keyboard[2][0].text == "Model: H3 Eros Max beta6 (checkpoint)"
    assert qwen_keyboard.inline_keyboard[2][0].text == "Model: Qwen Image 2.1"
    assert h3_keyboard.inline_keyboard[2][0].callback_data == qwen_keyboard.inline_keyboard[2][0].callback_data
    ltx_settings = Settings(model="ltx", media_kind="video")
    ltx_keyboard = bot._settings_keyboard(5129431382, ltx_settings)
    assert ltx_keyboard.inline_keyboard[2][0].text == "Model: LTX 2.5 (qualified)"
    assert "strength=default" in bot._settings_text(ltx_settings)
    image_keyboard = bot._settings_keyboard(5129431382, Settings(model="qwen-image-2.1", media_kind="image", strength=0.5))
    assert image_keyboard.inline_keyboard[6][0].text == "Strength: 0.5"


def test_adaptive_presets_preserve_portrait_aspect_and_align_model():
    settings = Settings(media_kind="video", model="h3", mode="i2v", longest=500, auto_size=True, width=None, height=None)
    low = _resolve_dimensions(settings, (2, 3))
    medium = _resolve_dimensions(Settings(**{**settings.__dict__, "longest": 700}), (2, 3))
    high = _resolve_dimensions(Settings(**{**settings.__dict__, "longest": 1000}), (2, 3))
    assert low[0] % 32 == 0 and low[1] % 32 == 0
    assert medium[1] > low[1] and high[1] > medium[1]
    assert abs((low[0] / low[1]) - (2 / 3)) < 0.03


def test_manual_dimension_precedence_and_auto_reset():
    settings = Settings(model="ltx", media_kind="video", mode="t2v", longest=700, auto_size=True, width=None, height=None)
    manual = MediaBot._apply_setting(settings, "width", "640")
    manual = MediaBot._apply_setting(manual, "height", "480")
    assert manual.auto_size is False
    assert _resolve_dimensions(manual, (9, 16)) == (704, 512)
    reset = MediaBot._apply_setting(manual, "auto", "auto")
    assert reset.auto_size is True and reset.width is None and reset.height is None
    assert _resolve_dimensions(reset, (9, 16))[1] > _resolve_dimensions(manual, (9, 16))[1]


def test_supported_four_step_control_is_model_specific():
    h3 = MediaBot._apply_setting(Settings(model="h3", media_kind="video", mode="t2v"), "steps", "4")
    assert h3.steps == 4
    try:
        MediaBot._apply_setting(Settings(model="ltx", media_kind="video", mode="t2v"), "variant", "turbo")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported LTX variant accepted")


def test_stale_hidden_model_settings_fall_back_to_h3(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.put_settings(5129431382, Settings(media_kind="video", model="wan", mode="t2v"))
    settings = store.get_settings(5129431382)
    assert settings.model == "h3"
    assert settings.width == 640
    assert settings.height == 384


def test_settings_and_job_snapshots_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    settings = Settings(media_kind="video", model="h3", mode="t2v", variant="beta6", width=640, height=384, steps=8, seconds=2.0)
    store.put_settings(5129431382, settings)
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=7, prompt="boat", request={"model": "h3", "mode": "t2v", "steps": 4})
    store.update_job(job, backend_job_id="backend-1", state="starting_comfyui")
    store.db.close()

    reopened = StateStore(path)
    assert reopened.get_settings(5129431382) == settings
    row = reopened.get_job(job)
    assert row["backend_job_id"] == "backend-1"
    assert row["prompt"] == ""
    assert json.loads(row["request_json"]) == {}
    assert row["output_path"] is None
    assert [item["job_id"] for item in reopened.nonterminal()] == [job]


def test_image_normalization_is_aspect_cover(tmp_path):
    source = tmp_path / "source.png"
    import subprocess
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=80x40", "-frames:v", "1", str(source)],
        check=True,
    )
    bot = MediaBot(token="", state=StateStore(tmp_path / "state.sqlite3"), api=None, result_root=tmp_path / "results")
    output = asyncio.run(bot._normalize_image(source.read_bytes(), Settings(width=32, height=32)))
    normalized = tmp_path / "normalized.png"
    normalized.write_bytes(output)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(normalized)],
        check=True, capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "32,32"


def test_qwen_geometry_always_uses_32_pixel_alignment():
    settings = Settings(model="qwen-image-2.1", mode="edit", longest=500, width=None, height=None)
    for source_size in ((676, 1000), (1000, 676), (1920, 1080), (1080, 1920)):
        width, height = _resolve_dimensions(settings, source_size)
        assert width % 32 == 0 and height % 32 == 0


def test_qwen_all_presets_and_manual_sizes_fit_the_real_api_contract():
    for longest in (500, 700, 1000):
        for source_size in ((1920, 1080), (1080, 1920), (4032, 3024), (3024, 4032), (5000, 100), (100, 5000)):
            for mode in ("t2i", "edit"):
                settings = Settings(model="qwen-image-2.1", mode=mode, longest=longest, width=None, height=None)
                width, height = _resolve_dimensions(settings, source_size)
                assert 0 < width <= 1000 and 0 < height <= 1000
                assert width % 32 == 0 and height % 32 == 0
    manual = Settings(model="qwen-image-2.1", width=4096, height=3072, auto_size=False)
    assert max(_resolve_dimensions(manual)) <= 1000


def test_new_backend_causes_survive_http_status_and_startup(tmp_path):
    from obs_agent.telegram_media_bot import _safe_exception
    import httpx
    causes = ("tensor_shape_mismatch", "dtype_mismatch", "device_mismatch", "model_missing", "image_decode_failed")
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    references = []
    for cause in causes:
        response = httpx.Response(400, json={"error": cause, "request_id": "request-0123456789abcdef"}, request=httpx.Request("POST", "http://localhost/v1/jobs"))
        failure = httpx.HTTPStatusError("PRIVATE_EXCEPTION_MESSAGE", request=response.request, response=response)
        details = _safe_exception(failure)
        assert details["code"] == cause and details["api_request_id"] == "request-0123456789abcdef"
        job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="private", request={"model": "h3"})
        store.record_diagnostics(job, stage="submit", error=failure)
        store.update_job(job, state="failed", error_code=f"submit:{cause}")
        references.append(job)
    store.db.close()
    reopened = StateStore(path)
    for cause, job in zip(causes, references):
        assert reopened.get_job(job)["error_code"] == f"submit:{cause}"
        assert reopened.get_diagnostics(job)["error"]["code"] == cause


def test_failed_backend_status_keeps_specific_cause_and_metadata(tmp_path):
    class API:
        async def status(self, _backend):
            return {"state": "failed", "failure_stage": "generation", "error": "dtype_mismatch", "width": 512, "height": 288, "steps": 20}
        async def purge(self, _backend):
            pass
    class Telegram:
        async def edit_message_text(self, **_kwargs):
            pass
    from types import SimpleNamespace
    store = StateStore(tmp_path / "state.sqlite3")
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="private", request={"model": "qwen-image-2.1"})
    store.update_job(job, backend_job_id="backend-failed", status_message_id=1)
    bot = MediaBot(token="", state=store, api=API(), result_root=tmp_path / "results")
    bot.application = SimpleNamespace(bot=Telegram())
    asyncio.run(bot._watch(job, "backend-failed", 99))
    assert store.get_job(job)["error_code"] == "generation:dtype_mismatch"
    assert store.get_diagnostics(job)["steps"] == 20
    assert store.get_diagnostics(job)["error"]["code"] == "dtype_mismatch"


def test_rejected_photo_has_durable_prequeue_reference_without_download(tmp_path):
    from types import SimpleNamespace
    class Message:
        photo = [object()]
        document = None
        caption = None
        text = None
        chat_id = 99
        message_id = 1
        async def reply_text(self, text):
            assert "Reference " in text
    store = StateStore(tmp_path / "state.sqlite3")
    bot = MediaBot(token="", state=store, api=None, result_root=tmp_path / "results")
    update = SimpleNamespace(effective_user=SimpleNamespace(id=5129431382), effective_message=Message())
    asyncio.run(bot.message(update, None))
    rows = store.db.execute("SELECT * FROM jobs").fetchall()
    assert len(rows) == 1 and rows[0]["state"] == "failed"
    assert rows[0]["error_code"] == "preflight:invalid_prompt"
    assert store.get_diagnostics(rows[0]["job_id"])["events"][-1]["stage"] == "rejected"


def test_photo_inferred_h3_i2v_uses_effective_mode_alignment(tmp_path):
    class Attachment:
        async def get_file(self):
            return self

        async def download_as_bytearray(self):
            return b"synthetic image"

    class Message:
        photo = [Attachment()]
        document = None

        async def reply_text(self, _text):
            pass

    class API:
        received = None

        async def upload(self, _raw, _kind):
            return "/dev/shm/telegram-media-bot/in/synthetic.png"

        async def submit(self, request):
            self.received = request
            return {"job_id": "backend-photo"}

    store = StateStore(tmp_path / "state.sqlite3")
    settings = Settings(model="h3", media_kind="video", mode="t2v", longest=500, width=None, height=None)
    request = {"model": "h3", "mode": "i2v", "prompt": "synthetic", "width": 512, "height": 288, "steps": 4}
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=11, prompt="synthetic", request=request)
    api = API()
    bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")

    async def probe(_raw):
        return {"input_width": 676, "input_height": 1000, "input_format": "png"}

    async def normalize(raw, _settings):
        return raw

    async def watch(*_args):
        pass

    bot._probe_image, bot._normalize_image, bot._watch = probe, normalize, watch
    asyncio.run(bot._submit_and_watch(job, 5129431382, 99, Message(), settings, True, request))
    assert api.received["width"] % 32 == 0 and api.received["height"] % 32 == 0


def test_safe_failure_code_survives_startup_scrub(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=12, prompt="private", request={"model": "h3"})
    store.update_job(job, state="failed", error_code="generation:tensor_shape_mismatch")
    store.db.close()
    reopened = StateStore(path)
    assert reopened.get_job(job)["error_code"] == "generation:tensor_shape_mismatch"


def test_metadata_and_events_survive_restart_without_content(tmp_path, caplog):
    from obs_agent.telegram_media_bot import _safe_exception
    import httpx
    marker = "PRIVATE_PROMPT_DO_NOT_STORE_873fae"
    media_marker = "PRIVATE_IMAGE_FILENAME_97c4.png"
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    request = {"model": "qwen-image-2.1", "mode": "edit", "prompt": marker, "image_path": media_marker, "width": 352, "height": 512, "steps": 20, "cfg": 4.0, "seed": 71}
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=10, prompt=marker, request=request)
    store.record_diagnostics(job, stage="normalize", metadata={**request, "input_width": 676, "input_height": 1000, "input_bytes": 4123})
    response = httpx.Response(400, json={"error": marker}, request=httpx.Request("POST", "http://localhost/v1/jobs"))
    failure = httpx.HTTPStatusError(marker, request=response.request, response=response)
    store.record_diagnostics(job, stage="submit", error=failure)
    store.update_job(job, state="failed", error_code="submit:api_http_400")
    store.db.close()
    reopened = StateStore(path)
    diagnostics = reopened.get_diagnostics(job)
    assert diagnostics["width"] == 352 and diagnostics["height"] == 512
    assert diagnostics["seed"] == 71 and diagnostics["steps"] == 20
    assert diagnostics["input_width"] == 676 and diagnostics["prompt_characters"] == len(marker)
    assert [event["stage"] for event in diagnostics["events"]] == ["received", "normalize", "submit"]
    assert diagnostics["error"]["code"] == "api_http_400"
    assert marker not in json.dumps(diagnostics) and media_marker not in json.dumps(diagnostics)
    assert marker not in caplog.text and media_marker not in caplog.text
    reopened.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert marker.encode() not in path.read_bytes() and media_marker.encode() not in path.read_bytes()
    assert _safe_exception(RuntimeError(marker))["code"] == "generation_failed"


def test_checkpoints_default_to_correct_sampling_without_loras(tmp_path):
    selected = MediaBot._apply_setting(Settings(lora="hmnsfw"), "model", "h3")
    assert (selected.variant, selected.steps, selected.lora) == ("beta6", 8, None)
    previous = MediaBot._apply_setting(selected, "model", "h3-beta1")
    assert (previous.variant, previous.steps, previous.lora) == ("beta1", 4, None)
    store = StateStore(tmp_path / "state.sqlite3")
    store.put_settings(5129431382, Settings(model="h3", media_kind="video", mode="t2v", variant="turbo", steps=4, lora="hmnsfw"))
    migrated = store.get_settings(5129431382)
    assert (migrated.variant, migrated.steps, migrated.lora) == ("beta6", 8, None)
    store.put_settings(5129431382, MediaBot._apply_setting(migrated, "steps", "4"))
    assert store.get_settings(5129431382).steps == 4


def test_unknown_startup_error_strings_are_scrubbed(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=12, prompt="private", request={})
    store.update_job(job, state="failed", error_code="PRIVATE_SENTINEL_UNKNOWN_ERROR")
    store.db.close()
    reopened = StateStore(path)
    assert reopened.get_job(job)["error_code"] == "failed"
    reopened.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert b"PRIVATE_SENTINEL_UNKNOWN_ERROR" not in path.read_bytes()


def test_delivering_jobs_are_not_replayed_after_restart(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=8, prompt="x", request={"model": "qwen-image-2.1"})
    store.update_job(job, backend_job_id="backend-2", state="delivering")
    assert store.nonterminal() == []


def test_send_failure_leaves_explicitly_recoverable_delivery_state(tmp_path):
    class API:
        def __init__(self):
            self.purged = []

        async def result(self, _backend_id):
            return b"png", "image/png"

        async def purge(self, backend_id):
            self.purged.append(backend_id)

    class Bot:
        async def send_photo(self, **_kwargs):
            raise RuntimeError("synthetic send-after-accept crash")

    class Application:
        bot = Bot()

    store = StateStore(tmp_path / "state.sqlite3")
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=9, prompt="x", request={"model": "qwen-image-2.1"})
    store.update_job(job, backend_job_id="backend-3", state="succeeded")
    api = API()
    bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")
    bot.application = Application()
    asyncio.run(bot._deliver_backend_result(job, "backend-3", 99))
    row = store.get_job(job)
    assert row["state"] == "delivery_failed"
    assert row["output_path"] is None
    assert api.purged == ["backend-3"]
    assert store.nonterminal() == []
    assert not list((tmp_path / "results").glob("*"))


def test_retry_after_retains_video_and_retries_once_after_restart(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from telegram.error import RetryAfter

    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="synthetic", request={"model": "h3"})
    store.update_job(job, backend_job_id="backend-retry", state="succeeded")
    api = SimpleNamespace(result=AsyncMock(return_value=(b"synthetic-video", "video/mp4")), purge=AsyncMock())
    sender = SimpleNamespace(send_video=AsyncMock(side_effect=RetryAfter(25958)))
    bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")
    bot.application = SimpleNamespace(bot=sender)
    asyncio.run(bot._deliver_backend_result(job, "backend-retry", 99))
    assert store.get_job(job)["state"] == "delivery_pending"
    assert store.telegram_retry_at(99) > __import__("time").time() + 25950
    assert store.telegram_retry_at(99, status_edit=True) == 0
    api.purge.assert_not_awaited()
    assert store.cleanup_candidates() == []
    assert store.nonterminal() == []
    store.db.close()

    reopened = StateStore(path)
    assert reopened.get_job(job)["error_code"] == "delivery:rate_limited"
    bot = MediaBot(token="", state=reopened, api=api, result_root=tmp_path / "results")
    sender.send_video = AsyncMock()
    bot.application = SimpleNamespace(bot=sender)
    asyncio.run(bot._retry_pending_deliveries())
    sender.send_video.assert_not_awaited()
    reopened.db.execute("UPDATE telegram_delivery_cooldowns SET retry_at=0")
    reopened.db.commit()
    asyncio.run(bot._retry_pending_deliveries())
    asyncio.run(bot._retry_pending_deliveries())
    sender.send_video.assert_awaited_once()
    api.purge.assert_awaited_once_with("backend-retry")
    assert reopened.get_job(job)["state"] == "delivered"
    assert api.result.await_count == 2
    assert not (tmp_path / "results").exists()


def test_ambiguous_network_send_retains_result_without_automatic_replay(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from telegram.error import NetworkError

    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="synthetic", request={})
    store.update_job(job, backend_job_id="backend-ambiguous", state="succeeded")
    api = SimpleNamespace(result=AsyncMock(return_value=(b"synthetic", "video/mp4")), purge=AsyncMock())
    sender = SimpleNamespace(send_video=AsyncMock(side_effect=NetworkError("synthetic timeout")))
    bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")
    bot.application = SimpleNamespace(bot=sender)
    asyncio.run(bot._deliver_backend_result(job, "backend-ambiguous", 99))
    assert store.get_job(job)["state"] == "delivering"
    assert store.nonterminal() == []
    assert store.pending_deliveries() == []
    assert store.cleanup_candidates() == []
    api.purge.assert_not_awaited()
    asyncio.run(bot._deliver_backend_result(job, "backend-ambiguous", 99))
    assert sender.send_video.await_count == 1
    sender.send_video = AsyncMock()
    asyncio.run(bot._deliver_backend_result(job, "backend-ambiguous", 99, explicit=True))
    sender.send_video.assert_awaited_once()
    assert store.get_job(job)["state"] == "delivered"


def test_status_retry_after_suppresses_edits_without_blocking_video_delivery(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from telegram.error import RetryAfter
    import obs_agent.telegram_media_bot as module

    store = StateStore(tmp_path / "state.sqlite3")
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="synthetic", request={})
    store.update_job(job, backend_job_id="backend-status", state="queued", status_message_id=7)
    api = SimpleNamespace(status=AsyncMock(side_effect=[{"state":"running"}, {"state":"running"}, {"state":"succeeded"}]), result=AsyncMock(return_value=(b"synthetic-video", "video/mp4")), purge=AsyncMock())
    sender = SimpleNamespace(edit_message_text=AsyncMock(side_effect=RetryAfter(25958)), send_video=AsyncMock())
    bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")
    bot.application = SimpleNamespace(bot=sender)
    async def no_wait(_seconds):
        pass
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    asyncio.run(bot._watch(job, "backend-status", 99))
    sender.edit_message_text.assert_awaited_once()
    sender.send_video.assert_awaited_once()
    api.result.assert_awaited_once()
    api.purge.assert_awaited_once()
    assert store.telegram_retry_at(99, status_edit=True) > __import__("time").time()
    assert store.telegram_retry_at(99) == 0
    assert store.get_job(job)["state"] == "delivered"


def test_existing_status_edit_deadline_does_not_block_retained_video_after_restart(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import time

    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="synthetic", request={})
    store.update_job(job, backend_job_id="backend-held", state="delivery_pending")
    store.db.execute("INSERT INTO telegram_cooldowns(chat_id,retry_at) VALUES(?,?)", (99, time.time() + 25958))
    store.db.commit()
    store.db.close()
    reopened = StateStore(path)
    api = SimpleNamespace(result=AsyncMock(return_value=(b"synthetic-video", "video/mp4")), purge=AsyncMock())
    sender = SimpleNamespace(send_video=AsyncMock())
    bot = MediaBot(token="", state=reopened, api=api, result_root=tmp_path / "results")
    bot.application = SimpleNamespace(bot=sender)
    asyncio.run(bot._retry_pending_deliveries())
    sender.send_video.assert_awaited_once()
    assert reopened.get_job(job)["state"] == "delivered"
    assert reopened.telegram_retry_at(99, status_edit=True) > time.time()
    assert reopened.telegram_retry_at(99) == 0


def test_concurrent_pending_and_explicit_delivery_do_not_send_twice(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    store = StateStore(tmp_path / "state.sqlite3")
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="synthetic", request={})
    store.update_job(job, backend_job_id="backend-once", state="delivery_pending")
    async def result(_backend):
        await asyncio.sleep(0)
        return b"synthetic", "video/mp4"
    api = SimpleNamespace(result=result, purge=AsyncMock())
    sender = SimpleNamespace(send_video=AsyncMock())
    bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")
    bot.application = SimpleNamespace(bot=sender)
    async def run():
        await asyncio.gather(bot._retry_pending_deliveries(), bot._deliver_backend_result(job, "backend-once", 99, explicit=True))
    asyncio.run(run())
    sender.send_video.assert_awaited_once()
    api.purge.assert_awaited_once()


def test_transient_result_errors_retain_and_later_deliver(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx

    request = httpx.Request("GET", "http://media.invalid/result")
    failures = [httpx.ReadTimeout("synthetic"), httpx.HTTPStatusError("synthetic", request=request, response=httpx.Response(503, request=request))]
    for index, failure in enumerate(failures):
        store = StateStore(tmp_path / f"state-{index}.sqlite3")
        job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="synthetic", request={})
        store.update_job(job, backend_job_id="backend-transient", state="delivery_pending")
        api = SimpleNamespace(result=AsyncMock(side_effect=[failure, (b"synthetic", "video/mp4")]), purge=AsyncMock())
        sender = SimpleNamespace(send_video=AsyncMock())
        bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")
        bot.application = SimpleNamespace(bot=sender)
        asyncio.run(bot._retry_pending_deliveries())
        assert store.get_job(job)["state"] == "delivery_pending"
        api.purge.assert_not_awaited()
        sender.send_video.assert_not_awaited()
        asyncio.run(bot._retry_pending_deliveries())
        assert store.get_job(job)["state"] == "delivered"
        sender.send_video.assert_awaited_once()
        api.purge.assert_awaited_once()


def test_transient_status_errors_do_not_cleanup_running_render(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    import obs_agent.telegram_media_bot as module

    store = StateStore(tmp_path / "state.sqlite3")
    job = store.create_job(user_id=5129431382, chat_id=99, message_id=1, prompt="synthetic", request={})
    store.update_job(job, backend_job_id="backend-running", state="running", status_message_id=7)
    request = httpx.Request("GET", "http://media.invalid/status")
    failures = [httpx.ReadTimeout("synthetic"), httpx.HTTPStatusError("synthetic", request=request, response=httpx.Response(503, request=request)), {"state":"succeeded"}]
    api = SimpleNamespace(status=AsyncMock(side_effect=failures), result=AsyncMock(return_value=(b"synthetic", "video/mp4")), purge=AsyncMock())
    sender = SimpleNamespace(edit_message_text=AsyncMock(), send_video=AsyncMock())
    bot = MediaBot(token="", state=store, api=api, result_root=tmp_path / "results")
    bot.application = SimpleNamespace(bot=sender)
    async def no_wait(_seconds):
        assert store.get_job(job)["state"] == "running"
        api.purge.assert_not_awaited()
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    asyncio.run(bot._watch(job, "backend-running", 99))
    assert api.status.await_count == 3
    sender.send_video.assert_awaited_once()
    api.purge.assert_awaited_once()
