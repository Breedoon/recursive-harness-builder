"""Live Telegram media integration tests using a real bot and real Telethon auth."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from tests.evals.platform_telegram import TelegramPlatform


_REQUIRED_ENV = [
    "OBS_TEST_TELEGRAM_API_ID",
    "OBS_TEST_TELEGRAM_API_HASH",
    "OBS_TEST_TELEGRAM_SESSION",
    "OBS_TEST_TELEGRAM_BOT_USERNAME",
    "OBS_TEST_TELEGRAM_BOT_TOKEN",
]


def _has_telegram_credentials() -> bool:
    return all(os.environ.get(name) for name in _REQUIRED_ENV)


def _require_file(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"Required test file not found: {path}")
    return path


def _find_latest_boot_root(temp_root: Path) -> Path:
    roots = [path for path in temp_root.iterdir() if path.is_dir()]
    assert roots, f"No boot root created under {temp_root}"
    return max(roots, key=lambda path: path.stat().st_mtime)


def _read_log_tail(log_file: Path) -> str:
    if not log_file.exists():
        return ""
    text = log_file.read_text(errors="replace")
    return text[-4000:]


def _start_bot(vault_path: Path, temp_root: Path) -> tuple[subprocess.Popen, Path]:
    env = os.environ.copy()
    env["OBS_VAULT_PATH"] = str(vault_path)
    env["OBS_TELEGRAM_BOT_TOKEN"] = os.environ["OBS_TEST_TELEGRAM_BOT_TOKEN"]
    env["OBS_TELEGRAM_ALLOWED_USERS"] = os.environ.get(
        "OBS_TEST_TELEGRAM_ALLOWED_USERS",
        "5129431382",
    )
    env["OBS_TELEGRAM_TEMP_ROOT"] = str(temp_root)

    log_file = Path(tempfile.mktemp(prefix="obs_tg_media_", suffix=".log"))
    log_fh = open(log_file, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "obs_agent.telegram_main", "--test"],
        env=env,
        stdout=log_fh,
        stderr=log_fh,
    )
    time.sleep(5)
    if proc.poll() is not None:
        log_fh.close()
        raise RuntimeError(
            f"Telegram bot exited during startup (rc={proc.returncode}).\n{_read_log_tail(log_file)}"
        )
    return proc, log_file


def _stop_bot(proc: subprocess.Popen, log_file: Path) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def _warm_platform(platform: TelegramPlatform) -> None:
    for _ in range(6):
        reply = await platform.send_control("/new", timeout=20.0)
        if "session cleared" in reply.lower():
            await platform.rebaseline()
            return
        await asyncio.sleep(1.0)
    raise AssertionError("Telegram bot did not respond to /new during warmup")


async def _reset(platform: TelegramPlatform) -> None:
    reply = await platform.send_control("/new", timeout=20.0)
    assert "session cleared" in reply.lower()
    await platform.rebaseline()
    await asyncio.sleep(1.0)


@dataclass
class _LiveTelegramHarness:
    platform: TelegramPlatform
    temp_root: Path
    log_file: Path


@pytest_asyncio.fixture
async def live_tg_media(eval_vault: Path, tmp_path: Path) -> _LiveTelegramHarness:
    if not _has_telegram_credentials():
        pytest.skip("Telegram credentials not configured in environment")

    temp_root = tmp_path / "obs-agent-temp"
    proc, log_file = _start_bot(eval_vault, temp_root)
    platform = TelegramPlatform(
        timeout=240,
        done_timeout=240,
        idle_quiescence_timeout=60,
    )
    await platform.connect()
    try:
        await _warm_platform(platform)
        yield _LiveTelegramHarness(platform=platform, temp_root=temp_root, log_file=log_file)
    finally:
        await platform.close()
        _stop_bot(proc, log_file)


@pytest.mark.integration
@pytest.mark.telegram
class TestTelegramLiveMedia:
    async def test_live_document_downloads_and_agent_sees_filename(
        self, live_tg_media: _LiveTelegramHarness, tmp_path: Path
    ) -> None:
        await _reset(live_tg_media.platform)
        doc_path = tmp_path / "live-doc.pdf"
        doc_path.write_bytes(b"%PDF-1.4\n% live integration\n")

        response = await live_tg_media.platform.send_file(
            doc_path,
            caption="Reply with only the exact original filename of the attachment I just sent.",
        )

        assert "live-doc.pdf" in response
        boot_root = _find_latest_boot_root(live_tg_media.temp_root)
        stored = list(boot_root.rglob("live-doc.pdf"))
        assert stored, f"Expected downloaded file in {boot_root}"

    async def test_live_duplicate_filenames_do_not_collide(
        self, live_tg_media: _LiveTelegramHarness, tmp_path: Path
    ) -> None:
        await _reset(live_tg_media.platform)
        first_dir = tmp_path / "one"
        second_dir = tmp_path / "two"
        first_dir.mkdir()
        second_dir.mkdir()
        first = first_dir / "same.pdf"
        second = second_dir / "same.pdf"
        first.write_bytes(b"%PDF-1.4\n% one\n")
        second.write_bytes(b"%PDF-1.4\n% two\n")

        first_response = await live_tg_media.platform.send_file(
            first,
            caption="Say ok.",
        )
        second_response = await live_tg_media.platform.send_file(
            second,
            caption="Say ok again.",
        )

        assert "ok" in first_response.lower()
        assert "ok" in second_response.lower()
        boot_root = _find_latest_boot_root(live_tg_media.temp_root)
        stored = list(boot_root.rglob("same.pdf"))
        assert len(stored) == 2
        assert stored[0].parent != stored[1].parent

    async def test_live_photo_album_is_aggregated(
        self, live_tg_media: _LiveTelegramHarness
    ) -> None:
        await _reset(live_tg_media.platform)
        image_a = _require_file(Path("/path/to/test-media/Play Button Png.png"))
        image_b = _require_file(Path("/path/to/test-media/IMG_1108.JPG"))

        first_response = await live_tg_media.platform.send_files([image_a, image_b])
        followup = await live_tg_media.platform.send(
            "How many attachments were in my previous message? Reply with only the number."
        )

        assert "timeout" not in first_response.lower()
        assert "2" in followup
        boot_root = _find_latest_boot_root(live_tg_media.temp_root)
        jpgs = [path for path in boot_root.rglob("*") if path.suffix.lower() in {".png", ".jpg", ".jpeg"}]
        assert len(jpgs) >= 2

    async def test_live_native_video_is_downloaded(
        self, live_tg_media: _LiveTelegramHarness
    ) -> None:
        await _reset(live_tg_media.platform)
        video_path = _require_file(Path("/path/to/test-media/IMG_0254.MOV"))

        first_response = await live_tg_media.platform.send_file(video_path)
        followup = await live_tg_media.platform.send(
            "Did I send a video in my previous message? Reply with only yes or no."
        )

        assert "timeout" not in first_response.lower()
        assert "yes" in followup.lower()
        boot_root = _find_latest_boot_root(live_tg_media.temp_root)
        videos = [
            path
            for path in boot_root.rglob("*")
            if path.suffix.lower() in {".mov", ".mp4"}
        ]
        assert videos

    async def test_live_short_voice_note_transcribes_and_remains_in_context(
        self, live_tg_media: _LiveTelegramHarness
    ) -> None:
        await _reset(live_tg_media.platform)
        voice_path = _require_file(Path("/path/to/test-media/audio_2026-02-27_23-52-38.ogg"))
        expected_transcript = _require_file(Path("/path/to/test-media/audio_2026-02-27_23-52-38.txt"))

        response = await live_tg_media.platform.send_file(
            voice_path,
            voice_note=True,
            timeout=240,
        )
        followup = await live_tg_media.platform.send(
            "What were the first four words of my previous voice memo? Reply with only those words."
        )

        assert "timeout" not in response.lower()
        assert "Okay, so can you" in followup
        boot_root = _find_latest_boot_root(live_tg_media.temp_root)
        transcripts = list(boot_root.rglob("*.md"))
        assert transcripts, f"Expected transcript markdown under {boot_root}"
        latest = max(transcripts, key=lambda path: path.stat().st_mtime)
        transcript_text = latest.read_text(encoding="utf-8", errors="replace")
        expected_text = expected_transcript.read_text(encoding="utf-8", errors="replace")
        assert "okay, so can you help me get started" in transcript_text.lower()
        assert "last session" in transcript_text.lower()
        assert len(transcript_text) >= len(expected_text) // 2

    async def test_live_long_voice_note_over_chat_limit_still_reaches_agent(
        self, live_tg_media: _LiveTelegramHarness
    ) -> None:
        await _reset(live_tg_media.platform)
        voice_path = _require_file(Path("/path/to/test-media/audio_2026-02-27_23-53-30.ogg"))
        expected_transcript = _require_file(Path("/path/to/test-media/audio_2026-02-27_23-53-30.txt"))

        response = await live_tg_media.platform.send_file(
            voice_path,
            voice_note=True,
            timeout=420,
        )
        followup = await live_tg_media.platform.send(
            "In my previous voice memo, what short replacement name did I want for library? Reply with only that short name."
        )

        assert "timeout" not in response.lower()
        assert "kb" in followup.lower()
        boot_root = _find_latest_boot_root(live_tg_media.temp_root)
        transcripts = list(boot_root.rglob("*.md"))
        latest = max(transcripts, key=lambda path: path.stat().st_mtime)
        transcript_text = latest.read_text(encoding="utf-8", errors="replace")
        assert len(transcript_text) > 4000
        assert "rename it because i don't like library" in transcript_text.lower()
        assert "kb" in transcript_text.lower()
        assert len(transcript_text) >= len(expected_transcript.read_text(encoding="utf-8", errors="replace")) // 2
