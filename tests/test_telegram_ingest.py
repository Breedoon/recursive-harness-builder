"""Tests for Telegram inbound normalization and temp-file handling."""

from __future__ import annotations

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
    )
    instance.initialize()
    return instance


class TestTelegramInboundNormalizer:
    def test_initialize_purges_only_temp_root(self, tmp_path: Path) -> None:
        temp_root = tmp_path / "obs-agent"
        stale = temp_root / "stale.txt"
        team_storage = tmp_path / "obs-agent-teams" / "team-alpha" / "inboxes"
        team_inbox = team_storage / "worker-a.json"
        stale.parent.mkdir(parents=True)
        team_storage.mkdir(parents=True)
        stale.write_text("old")
        team_inbox.write_text("[]")

        normalizer = TelegramInboundNormalizer(
            temp_root=temp_root,
            transcription_script=tmp_path / "transcribe.sh",
        )
        normalizer.initialize()

        assert not stale.exists()
        assert team_inbox.exists()
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

        async def _fake_transcribe(*, audio_file: Path, title: str, dest_dir: Path) -> None:
            (dest_dir / f"{title}.md").write_text("this is the transcript")

        monkeypatch.setattr(normalizer, "_run_transcription", _fake_transcribe)

        normalized = await normalizer.normalize_update(_make_update(message))

        assert "Telegram voice message received." in normalized.agent_text
        assert "Transcript path:" in normalized.agent_text
        assert "this is the transcript" in normalized.agent_text
        assert normalized.user_warnings == []

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

        async def _boom(*, audio_file: Path, title: str, dest_dir: Path) -> None:
            raise RuntimeError("decoder exploded")

        monkeypatch.setattr(normalizer, "_run_transcription", _boom)

        normalized = await normalizer.normalize_update(_make_update(message))

        assert normalized.user_warnings == [
            "voice transcription failed; the original file path was still sent to the agent"
        ]
        assert "Automatic transcription failed." in normalized.agent_text
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
