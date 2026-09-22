from __future__ import annotations

import asyncio
import json

from obs_agent.telegram_media_bot import ALLOWLIST, MODEL_REGISTRY, SELECTABLE_MODELS, MediaBot, Settings, StateStore, _resolve_dimensions


def test_allowlist_is_exact_and_excludes_third_identity():
    assert ALLOWLIST == {227177188, 5129431382}
    assert 1350518665 not in ALLOWLIST


def test_model_registry_only_advertises_qualified_nondeprecated_paths():
    assert SELECTABLE_MODELS == ("qwen-image-2.1", "h3", "ltx")
    assert MODEL_REGISTRY["h3"]["label"] == "H3 Eros Max beta5 (checkpoint)"
    assert MODEL_REGISTRY["wan"]["deprecated"] is True
    assert MODEL_REGISTRY["ltx"]["qualified"] is True


def test_settings_keyboard_rebuilds_model_label_after_cycle(tmp_path):
    bot = MediaBot(token="", state=StateStore(tmp_path / "state.sqlite3"), api=None, result_root=tmp_path / "results")
    h3_keyboard = bot._settings_keyboard(5129431382, Settings(model="h3", media_kind="video"))
    qwen_keyboard = bot._settings_keyboard(5129431382, Settings(model="qwen-image-2.1", media_kind="image"))
    assert h3_keyboard.inline_keyboard[2][0].text == "Model: H3 Eros Max beta5 (checkpoint)"
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
    assert _resolve_dimensions(manual, (9, 16)) == (640, 480)
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
    settings = Settings(media_kind="video", model="h3", mode="t2v", width=640, height=384, steps=4, seconds=2.0)
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
