from __future__ import annotations

import asyncio
import json

from httpx import Request, Response

from obs_agent.telegram_media_bot import ALLOWLIST, MODEL_REGISTRY, SELECTABLE_MODELS, MediaAPI, MediaAPIError, MediaBot, Settings, StateStore, _job_failure_text, _resolve_dimensions, _safe_error_code, _validate_dimensions, _video_timing


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
    assert "seconds=4" in MediaBot._settings_text(ltx_settings)
    assert "lora=" not in MediaBot._settings_text(ltx_settings)
    assert all(not row[0].text.startswith(("Strength:", "LoRA:")) for row in ltx_keyboard.inline_keyboard)
    image_keyboard = bot._settings_keyboard(5129431382, Settings(model="qwen-image-2.1", media_kind="image", strength=0.5))
    assert all(not row[0].text.startswith(("Strength:", "Duration:", "LoRA:")) for row in image_keyboard.inline_keyboard)
    assert any(row[0].text == "Duration: 4s" for row in h3_keyboard.inline_keyboard)
    assert all("Longest" not in row[0].text for row in h3_keyboard.inline_keyboard)
    assert "longest=" not in MediaBot._settings_text(Settings(model="h3", media_kind="video"))


def test_resolution_command_setting_and_duration_choices():
    settings = Settings(model="h3", media_kind="video", mode="t2v", auto_size=True, width=None, height=None)
    manual = MediaBot._apply_setting(settings, "res", "720x432")
    assert manual.auto_size is False
    assert _resolve_dimensions(manual, (2, 3)) == (720, 432)
    for seconds in (3.0, 5.0, 10.0, 15.0):
        assert MediaBot._apply_setting(settings, "duration", str(seconds)).seconds == seconds
    ltx = MediaBot._apply_setting(settings, "model", "ltx")
    assert ltx.seconds == 4.0
    try:
        MediaBot._apply_setting(ltx, "duration", "10")
    except ValueError:
        pass
    else:
        raise AssertionError("duration outside the qualified LTX frame limit was accepted")
    for invalid in ("720", "720X432x1", "12x400", "5000x5000"):
        try:
            MediaBot._apply_setting(settings, "res", invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid resolution accepted: {invalid}")


def test_inferred_h3_i2v_geometry_uses_i2v_alignment():
    saved = Settings(model="h3", media_kind="video", mode="t2v", longest=700, auto_size=True, width=None, height=None)
    actual_i2v = Settings(**{**saved.__dict__, "mode": "i2v"})
    width, height = _resolve_dimensions(actual_i2v, (9, 16))
    assert width % 32 == 0 and height % 32 == 0


def test_api_errors_preserve_safe_codes_and_never_echo_response_text():
    secret = "private synthetic prompt with unique phrase"
    response = Response(400, json={"error": secret}, request=Request("POST", "http://media.invalid/v1/jobs"))
    try:
        MediaAPI._check(response, "submit")
    except MediaAPIError as exc:
        assert exc.code == "api_http_400"
        assert secret not in str(exc)
    else:
        raise AssertionError("HTTP error was not propagated")
    response = Response(400, json={"error": "private"}, request=Request("POST", "http://media.invalid/v1/jobs"))
    try:
        MediaAPI._check(response, "submit")
    except MediaAPIError as exc:
        assert exc.code == "api_http_400"
        assert "private" not in str(exc)
    else:
        raise AssertionError("unrecognized API error text was exposed")
    response = Response(400, json={"error": "invalid_geometry"}, request=Request("POST", "http://media.invalid/v1/jobs"))
    try:
        MediaAPI._check(response, "submit")
    except MediaAPIError as exc:
        assert exc.code == "invalid_geometry"
    else:
        raise AssertionError("safe API error code was not propagated")


def test_errors_are_actionable_without_echoing_private_text():
    secret = "private synthetic prompt with unique phrase"
    text = _job_failure_text("0123456789abcdef", {"model": "h3", "mode": "i2v", "width": 640, "height": 384, "steps": 4, "prompt": secret}, "generation", _safe_error_code(backend_error="CUDA out of memory"))
    assert "gpu_memory_exhausted" in text
    assert "h3/i2v" in text and "640x384" in text and "4 steps" in text and "stage=generation" in text
    assert secret not in text
    assert _safe_error_code(ValueError("H3 i2v width and height must be divisible by 32")) == "invalid_geometry"
    assert _safe_error_code(backend_error="LTX 2.5 uses a supported 8-step schedule") == "invalid_steps"
    assert _safe_error_code(backend_error="gpu_memory_exhausted") == "gpu_memory_exhausted"


def test_effective_mode_selects_correct_geometry_and_model_alignment():
    saved = Settings(model="h3", media_kind="video", mode="t2v", longest=700, auto_size=True, width=None, height=None)
    width, height = _resolve_dimensions(saved, (16, 9), mode="i2v")
    assert width % 32 == 0 and height % 32 == 0
    _validate_dimensions("h3", "i2v", width, height)

    qwen_width, qwen_height = _resolve_dimensions(Settings(model="qwen-image-2.1", longest=1000))
    assert qwen_width % 32 == 0 and qwen_height % 32 == 0
    _validate_dimensions("qwen-image-2.1", "t2i", qwen_width, qwen_height)

    ltx_width, ltx_height = _resolve_dimensions(Settings(model="ltx", media_kind="video", mode="t2v", longest=700))
    assert ltx_width % 64 == 0 and ltx_height % 64 == 0
    _validate_dimensions("ltx", "t2v", ltx_width, ltx_height)


def test_video_timing_and_ltx_sampling_contract():
    assert _video_timing("h3", 3.0) == (24, 73, 73 / 24)
    assert _video_timing("h3", 15.0) == (24, 362, 362 / 24)
    assert _video_timing("ltx", 3.0) == (24, 73, 73 / 24)
    assert _video_timing("ltx", 5.0) == (24, 121, 121 / 24)
    ltx = Settings(model="ltx", media_kind="video", mode="t2v", steps=8)
    try:
        MediaBot._apply_setting(ltx, "steps", "20")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported LTX sampling step count was accepted")
    assert MediaBot._apply_setting(ltx, "steps", "8").steps == 8
    for key, value in (("strength", "1.0"), ("lora", "hmnsfw")):
        try:
            MediaBot._apply_setting(ltx, key, value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsupported LTX setting was accepted: {key}")
    try:
        _video_timing("ltx", 6.8)
    except ValueError:
        pass
    else:
        raise AssertionError("duration beyond qualified LTX limit was accepted")


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


def test_stale_ltx_sampling_setting_resets_to_qualified_steps(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.put_settings(5129431382, Settings(media_kind="video", model="ltx", mode="t2v", steps=20))
    assert store.get_settings(5129431382).steps == 8


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


def test_qwen_edit_normalization_preserves_full_reference_frame(tmp_path):
    import subprocess

    source = tmp_path / "source.png"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=80x40", "-frames:v", "1", str(source)],
        check=True,
    )
    bot = MediaBot(token="", state=StateStore(tmp_path / "state.sqlite3"), api=None, result_root=tmp_path / "results")
    target = Settings(model="qwen-image-2.1", media_kind="image", mode="edit", width=32, height=32, auto_size=False)

    for crop, expected in ((False, "32,16"), (True, "32,32")):
        output = asyncio.run(bot._normalize_image(source.read_bytes(), target, cover_crop=crop))
        normalized = tmp_path / f"normalized-{crop}.png"
        normalized.write_bytes(output)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(normalized)],
            check=True, capture_output=True, text=True,
        )
        assert probe.stdout.strip() == expected


def test_content_scrub_preserves_only_safe_failure_diagnostics(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    safe_job = store.create_job(user_id=5129431382, chat_id=99, message_id=13, prompt="synthetic", request={})
    unsafe_job = store.create_job(user_id=5129431382, chat_id=99, message_id=14, prompt="synthetic", request={})
    store.update_job(safe_job, state="failed", error_code="submit:invalid_geometry")
    store.update_job(unsafe_job, state="failed", error_code="private:synthetic-prompt")
    store.db.close()

    reopened = StateStore(path)
    assert reopened.get_job(safe_job)["error_code"] == "submit:invalid_geometry"
    assert reopened.get_job(unsafe_job)["error_code"] == "failed"


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
