"""Tests for Telegram inbound normalization and temp-file handling."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from obs_agent.telegram_ingest import TelegramInboundNormalizer


class _FakeTelegramFile:
    def __init__(self, *, file_path: str, payload: bytes) -> None:
        self.file_path = file_path
        self._payload = payload

    async def download_to_drive(self, custom_path: str | Path | None = None, **_: object) -> Path:
        assert custom_path is not None
        path = Path(custom_path)
        path.write_bytes(self._payload)
        return path


class _FakeAttachment:
    def __init__(
        self,
        *,
        file_path: str,
        payload: bytes,
        file_unique_id: str,
        file_name: str | None = None,
        mime_type: str | None = None,
        file_size: int | None = None,
    ) -> None:
        self._file = _FakeTelegramFile(file_path=file_path, payload=payload)
        self.file_unique_id = file_unique_id
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = file_size if file_size is not None else len(payload)

    async def get_file(self) -> _FakeTelegramFile:
        return self._file


def _make_message(
    *,
    chat_id: int = 67890,
    message_id: int = 1,
    media_group_id: str | None = None,
    text: str | None = None,
    caption: str | None = None,
    document: object | None = None,
    photo: list[object] | None = None,
    voice: object | None = None,
    video: object | None = None,
    audio: object | None = None,
    video_note: object | None = None,
    animation: object | None = None,
    sticker: object | None = None,
    effective_attachment: object | None = None,
):
    return SimpleNamespace(
        chat_id=chat_id,
        message_id=message_id,
        media_group_id=media_group_id,
        text=text,
        caption=caption,
        document=document,
        photo=photo or [],
        voice=voice,
        video=video,
        audio=audio,
        video_note=video_note,
        animation=animation,
        sticker=sticker,
        effective_attachment=effective_attachment,
    )


def _make_update(message) -> SimpleNamespace:
    return SimpleNamespace(effective_message=message)


@pytest.fixture
def normalizer(tmp_path: Path) -> TelegramInboundNormalizer:
    instance = TelegramInboundNormalizer(
        temp_root=tmp_path / "obs-agent",
        transcription_script=tmp_path / "transcribe.sh",
        gpu_ready_probe=lambda: True,
    )
    instance.initialize()
    return instance


class TestTelegramInboundNormalizer:
    def test_initialize_purges_temp_root(self, tmp_path: Path) -> None:
        temp_root = tmp_path / "obs-agent"
        stale = temp_root / "stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("old")

        normalizer = TelegramInboundNormalizer(
            temp_root=temp_root,
            transcription_script=tmp_path / "transcribe.sh",
        )
        normalizer.initialize()

        assert not stale.exists()
        assert normalizer.boot_root.exists()

    async def test_document_message_includes_caption_and_stored_path(
        self, normalizer: TelegramInboundNormalizer
    ) -> None:
        doc = _FakeAttachment(
            file_path="documents/report.pdf",
            payload=b"pdf-bytes",
            file_unique_id="doc123",
            file_name="report.pdf",
            mime_type="application/pdf",
        )
        message = _make_message(
            message_id=11,
            caption="Please read this",
            document=doc,
            effective_attachment=doc,
        )

        normalized = await normalizer.normalize_update(_make_update(message))

        assert "Please read this" in normalized.agent_text
        assert "<system-note>" in normalized.agent_text
        assert "Original filename: report.pdf" in normalized.agent_text
        assert "/obs-agent/" in normalized.agent_text
        stored_path = normalized.attachments[0].stored_path
        assert stored_path is not None
        assert stored_path.exists()

    async def test_voice_message_includes_transcript_text(
        self, normalizer: TelegramInboundNormalizer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        voice = _FakeAttachment(
            file_path="voice/clip.ogg",
            payload=b"voice-bytes",
            file_unique_id="voice123",
            mime_type="audio/ogg",
        )
        message = _make_message(
            message_id=12,
            voice=voice,
            effective_attachment=voice,
        )

        async def _fake_transcribe(
            *, audio_file: Path, title: str, dest_dir: Path, correlation_id: str
        ) -> None:
            (dest_dir / f"{title}.md").write_text("this is the transcript")

        monkeypatch.setattr(normalizer, "_run_transcription", _fake_transcribe)

        normalized = await normalizer.normalize_update(_make_update(message))

        assert "Telegram voice message received." in normalized.agent_text
        assert "Transcript path:" in normalized.agent_text
        assert "Transcription correlation ID:" in normalized.agent_text
        assert normalized.attachments[0].transcription_correlation_id
        assert "this is the transcript" in normalized.agent_text
        assert normalized.user_warnings == []

    async def test_voice_transcription_reports_immediate_gpu_waiting(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "transcribe.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "correlation_id = os.environ['OBS_TRANSCRIPTION_CORRELATION_ID']\n"
            "Path(sys.argv[3], f'{sys.argv[2]}.md').write_text(\n"
            "    'transcription_status: \\\"ok\\\"\\n'\n"
            "    f'correlation_id: \\\"{correlation_id}\\\"\\n'\n"
            "    'device: \\\"cuda\\\"\\n'\n"
            "    'compute_type: \\\"float16\\\"\\n'\n"
            "    '# Transcript\\n\\nhello world\\n',\n"
            "    encoding='utf-8',\n"
            ")\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        normalizer = TelegramInboundNormalizer(
            temp_root=tmp_path / "obs-agent-progress",
            transcription_script=script,
            gpu_ready_probe=lambda: True,
        )
        normalizer.initialize()
        voice = _FakeAttachment(
            file_path="voice/progress.ogg",
            payload=b"voice-bytes",
            file_unique_id="voice-progress",
            mime_type="audio/ogg",
        )
        message = _make_message(
            message_id=121,
            voice=voice,
            effective_attachment=voice,
        )
        progress: list[str] = []

        async def _report(text: str) -> None:
            progress.append(text)

        normalized = await normalizer.normalize_update(
            _make_update(message),
            transcription_progress=_report,
        )

        assert progress == ["waiting for GPU"]
        assert normalized.user_warnings == []
        assert "Transcription correlation ID:" in normalized.agent_text
        assert "hello world" in normalized.agent_text

    async def test_voice_transcription_failure_sets_warning(
        self, normalizer: TelegramInboundNormalizer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        voice = _FakeAttachment(
            file_path="voice/fail.ogg",
            payload=b"voice-bytes",
            file_unique_id="voice456",
            mime_type="audio/ogg",
        )
        message = _make_message(
            message_id=13,
            voice=voice,
            effective_attachment=voice,
        )

        async def _boom(
            *, audio_file: Path, title: str, dest_dir: Path, correlation_id: str
        ) -> None:
            raise RuntimeError("decoder exploded")

        monkeypatch.setattr(normalizer, "_run_transcription", _boom)

        normalized = await normalizer.normalize_update(_make_update(message))

        assert normalized.user_warnings == [
            "voice transcription failed retryably; resend the voice note later if needed"
        ]
        assert "Automatic transcription failed retryably." in normalized.agent_text
        assert "decoder exploded" in normalized.agent_text

    async def test_download_failure_surfaces_system_note_and_warning(
        self, normalizer: TelegramInboundNormalizer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc = _FakeAttachment(
            file_path="documents/big.pdf",
            payload=b"ignored",
            file_unique_id="big123",
            file_name="big.pdf",
            mime_type="application/pdf",
        )
        message = _make_message(
            message_id=14,
            document=doc,
            effective_attachment=doc,
        )

        async def _fail_download(*args, **kwargs):
            raise RuntimeError("File is too big")

        monkeypatch.setattr(normalizer, "_download_file", _fail_download)

        normalized = await normalizer.normalize_update(_make_update(message))

        assert normalized.user_warnings == [
            "attachment download failed inside Telegram; the agent received a system note with the error"
        ]
        assert "could not download it" in normalized.agent_text
        assert "File is too big" in normalized.agent_text

    async def test_media_group_aggregates_multiple_files_once(
        self, normalizer: TelegramInboundNormalizer
    ) -> None:
        first = _FakeAttachment(
            file_path="photos/a.jpg",
            payload=b"a",
            file_unique_id="photo-a",
            mime_type="image/jpeg",
        )
        second = _FakeAttachment(
            file_path="photos/b.jpg",
            payload=b"b",
            file_unique_id="photo-b",
            mime_type="image/jpeg",
        )
        msg1 = _make_message(
            message_id=21,
            media_group_id="album-1",
            caption="album caption",
            photo=[first],
            effective_attachment=[first],
        )
        msg2 = _make_message(
            message_id=22,
            media_group_id="album-1",
            photo=[second],
            effective_attachment=[second],
        )

        normalized = await normalizer.normalize_media_group(
            [_make_update(msg1), _make_update(msg2)]
        )

        assert normalized.agent_text.count("album caption") == 1
        assert normalized.agent_text.count("Telegram attachment received.") == 2
        assert len(normalized.attachments) == 2

    async def test_duplicate_filenames_land_in_separate_paths(
        self, normalizer: TelegramInboundNormalizer
    ) -> None:
        first_doc = _FakeAttachment(
            file_path="documents/duplicate.pdf",
            payload=b"first",
            file_unique_id="dup1",
            file_name="same.pdf",
            mime_type="application/pdf",
        )
        second_doc = _FakeAttachment(
            file_path="documents/duplicate.pdf",
            payload=b"second",
            file_unique_id="dup2",
            file_name="same.pdf",
            mime_type="application/pdf",
        )
        first = await normalizer.normalize_update(
            _make_update(
                _make_message(
                    message_id=31,
                    document=first_doc,
                    effective_attachment=first_doc,
                )
            )
        )
        second = await normalizer.normalize_update(
            _make_update(
                _make_message(
                    message_id=32,
                    document=second_doc,
                    effective_attachment=second_doc,
                )
            )
        )

        assert first.attachments[0].stored_path != second.attachments[0].stored_path


_FAKE_TRANSCRIBER = (
    "#!/usr/bin/env python3\n"
    "import os, sys, time\n"
    "from pathlib import Path\n"
    "marker = os.environ.get('FAKE_MARKER_DIR')\n"
    "kind = {kind!r}\n"
    "if marker:\n"
    "    Path(marker, kind + '-' + sys.argv[2]).write_text('ran')\n"
    "time.sleep({sleep})\n"
    "if {fail}:\n"
    "    print(kind + ' exploded')\n"
    "    sys.exit(75)\n"
    "cid = os.environ['OBS_TRANSCRIPTION_CORRELATION_ID']\n"
    "device = os.environ.get('OBS_TRANSCRIPTION_DEVICE', 'cuda')\n"
    "compute = os.environ.get('OBS_TRANSCRIPTION_COMPUTE_TYPE', 'float16')\n"
    "Path(sys.argv[3], sys.argv[2] + '.md').write_text(\n"
    "    'transcription_status: \"ok\"\\n'\n"
    "    f'correlation_id: \"{{cid}}\"\\n'\n"
    "    f'device: \"{{device}}\"\\n'\n"
    "    f'compute_type: \"{{compute}}\"\\n'\n"
    "    '# Transcript\\n\\n' + kind + ' words\\n', encoding='utf-8')\n"
)


def _write_fake(path: Path, *, kind: str, sleep: float = 0.0, fail: bool = False) -> Path:
    path.write_text(_FAKE_TRANSCRIBER.format(kind=kind, sleep=sleep, fail=fail), encoding="utf-8")
    path.chmod(0o700)
    return path


def _voice_message(message_id: int, unique: str):
    voice = _FakeAttachment(
        file_path=f"voice/{unique}.ogg",
        payload=b"voice",
        file_unique_id=unique,
        mime_type="audio/ogg",
    )
    return _make_message(message_id=message_id, voice=voice, effective_attachment=voice)


class TestVoiceTranscriptionFallback:
    """A busy, slow or broken GPU must never cost Daniel a voice message (obs-3, 2026-10-16)."""

    def _normalizer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        gpu_ready: bool = True,
        gpu_sleep: float = 0.0,
        gpu_fail: bool = False,
        cpu_fail: bool = False,
        budget: float = 5.0,
    ) -> TelegramInboundNormalizer:
        markers = tmp_path / "markers"
        markers.mkdir(exist_ok=True)
        monkeypatch.setenv("FAKE_MARKER_DIR", str(markers))
        monkeypatch.setenv("OBS_TRANSCRIPTION_NORMALIZER_SLOT_LOCK", str(tmp_path / "gpu-slot.lock"))
        monkeypatch.setenv("OBS_TRANSCRIPTION_NORMALIZER_CPU_SLOT_LOCK", str(tmp_path / "cpu-slot.lock"))
        instance = TelegramInboundNormalizer(
            temp_root=tmp_path / "obs-agent",
            transcription_script=_write_fake(
                tmp_path / "gpu.py", kind="gpu", sleep=gpu_sleep, fail=gpu_fail
            ),
            cpu_python=Path(sys.executable),
            cpu_script=_write_fake(tmp_path / "cpu.py", kind="cpu", fail=cpu_fail),
            gpu_ready_probe=lambda: gpu_ready,
            gpu_fallback_after_seconds=budget,
        )
        instance.initialize()
        return instance

    @staticmethod
    def _ran(tmp_path: Path) -> set[str]:
        return {p.name.split("-", 1)[0] for p in (tmp_path / "markers").iterdir()}

    async def test_gpu_free_uses_gpu_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        n = self._normalizer(tmp_path, monkeypatch)
        out = await n.normalize_update(_make_update(_voice_message(1, "a")))
        assert "gpu words" in out.agent_text
        assert out.user_warnings == []
        assert self._ran(tmp_path) == {"gpu"}

    async def test_gpu_busy_goes_straight_to_cpu(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        n = self._normalizer(tmp_path, monkeypatch, gpu_ready=False)
        progress: list[str] = []

        async def report(text: str) -> None:
            progress.append(text)

        out = await n.normalize_update(_make_update(_voice_message(2, "b")), transcription_progress=report)
        assert "cpu words" in out.agent_text
        assert 'device: "cpu"' in out.agent_text
        assert out.user_warnings == []
        assert "retry later" not in out.agent_text
        assert progress == ["GPU busy; transcribing on CPU"]
        assert self._ran(tmp_path) == {"cpu"}

    async def test_gpu_failure_falls_back_to_cpu(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        n = self._normalizer(tmp_path, monkeypatch, gpu_fail=True)
        out = await n.normalize_update(_make_update(_voice_message(3, "c")))
        assert "cpu words" in out.agent_text
        assert out.user_warnings == []
        assert self._ran(tmp_path) == {"gpu", "cpu"}

    async def test_gpu_overlong_wait_is_cut_and_cpu_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        n = self._normalizer(tmp_path, monkeypatch, gpu_sleep=30.0, budget=0.5)
        loop = asyncio.get_running_loop()
        started = loop.time()
        out = await n.normalize_update(_make_update(_voice_message(4, "d")))
        assert loop.time() - started < 15.0
        assert "cpu words" in out.agent_text
        assert out.user_warnings == []

    async def test_concurrent_voice_notes_all_transcribed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Previously the second note got "GPU transcription busy; retry later".
        n = self._normalizer(tmp_path, monkeypatch, gpu_sleep=0.5)
        results = await asyncio.gather(
            *(n.normalize_update(_make_update(_voice_message(10 + i, f"m{i}"))) for i in range(3))
        )
        for out in results:
            assert out.user_warnings == []
            assert "words" in out.agent_text
            assert "retry later" not in out.agent_text
        assert self._ran(tmp_path) == {"gpu", "cpu"}

    async def test_cpu_failure_falls_back_to_full_gpu_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        n = self._normalizer(tmp_path, monkeypatch, gpu_ready=False, cpu_fail=True)
        out = await n.normalize_update(_make_update(_voice_message(5, "e")))
        assert "gpu words" in out.agent_text
        assert out.user_warnings == []

    async def test_everything_failing_reports_every_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        n = self._normalizer(tmp_path, monkeypatch, gpu_fail=True, cpu_fail=True)
        out = await n.normalize_update(_make_update(_voice_message(6, "f")))
        error = out.attachments[0].transcription_error or ""
        assert "GPU:" in error and "CPU:" in error and "GPU (full wait):" in error
        assert out.user_warnings
