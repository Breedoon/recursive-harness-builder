from __future__ import annotations

import asyncio
import json

from obs_agent.telegram_media_bot import ALLOWLIST, MediaBot, Settings, StateStore


def test_allowlist_is_exact_and_excludes_third_identity():
    assert ALLOWLIST == {227177188, 5129431382}
    assert 1350518665 not in ALLOWLIST


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
    assert json.loads(row["request_json"])["steps"] == 4
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
