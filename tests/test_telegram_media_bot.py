from __future__ import annotations

import json

from obs_agent.telegram_media_bot import ALLOWLIST, Settings, StateStore


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
