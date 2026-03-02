"""Telegram inbound media normalization and temp-file management."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Message, Update


_WHITESPACE_RE = re.compile(r"\s+")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass(frozen=True)
class DownloadedAttachment:
    kind: str
    original_filename: str
    stored_path: Path | None
    mime_type: str | None = None
    size_bytes: int | None = None
    download_error: str | None = None
    transcript_path: Path | None = None
    transcript_text: str | None = None
    transcription_error: str | None = None
    message_id: int | None = None
    media_group_id: str | None = None
    unsupported_detail: str | None = None


@dataclass(frozen=True)
class NormalizedInbound:
    agent_text: str
    attachments: list[DownloadedAttachment] = field(default_factory=list)
    user_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _AttachmentSpec:
    kind: str
    source: Any
    original_filename: str
    mime_type: str | None
    size_bytes: int | None


class TelegramInboundNormalizer:
    """Normalizes Telegram updates into one agent-facing string."""

    def __init__(self, *, temp_root: Path, transcription_script: Path) -> None:
        self._temp_root = temp_root
        self._transcription_script = transcription_script
        self._boot_id = uuid.uuid4().hex
        self._boot_root = self._temp_root / self._boot_id
        self._initialized = False

    @property
    def temp_root(self) -> Path:
        return self._temp_root

    @property
    def boot_root(self) -> Path:
        return self._boot_root

    def initialize(self) -> None:
        """Purge the temp root once and create a fresh boot-scoped workspace."""
        shutil.rmtree(self._temp_root, ignore_errors=True)
        self._boot_root.mkdir(parents=True, exist_ok=True)
        self._initialized = True

    async def normalize_update(self, update: Update) -> NormalizedInbound:
        """Normalize a single Telegram update."""
        message = update.effective_message
        if message is None:
            return NormalizedInbound(agent_text="")
        return await self._normalize_messages([message])

    async def normalize_media_group(self, updates: list[Update]) -> NormalizedInbound:
        """Normalize a media-group album into one logical agent input."""
        messages = [update.effective_message for update in updates if update.effective_message is not None]
        if not messages:
            return NormalizedInbound(agent_text="")
        return await self._normalize_messages(messages)

    async def _normalize_messages(self, messages: list[Message]) -> NormalizedInbound:
        self._ensure_initialized()
        first = messages[0]
        scope_id = str(first.media_group_id or first.message_id)
        message_dir = self._boot_root / str(first.chat_id) / scope_id
        message_dir.mkdir(parents=True, exist_ok=True)

        text_parts = self._collect_text_parts(messages)
        attachments: list[DownloadedAttachment] = []
        user_warnings: list[str] = []

        for message in messages:
            specs = self._extract_specs(message)
            if not specs and message.effective_attachment is not None:
                attachments.append(
                    DownloadedAttachment(
                        kind="unsupported",
                        original_filename="",
                        stored_path=None,
                        message_id=message.message_id,
                        media_group_id=message.media_group_id,
                        unsupported_detail=type(message.effective_attachment).__name__,
                    )
                )
                continue

            for spec in specs:
                downloaded = await self._download_spec(
                    spec,
                    message=message,
                    dest_dir=message_dir,
                )
                attachments.append(downloaded)
                if downloaded.download_error:
                    user_warnings.append(
                        "attachment download failed inside Telegram; the agent received a system note with the error"
                    )
                if downloaded.transcription_error:
                    user_warnings.append(
                        "voice transcription failed; the original file path was still sent to the agent"
                    )

        agent_parts: list[str] = []
        if text_parts:
            agent_parts.append("\n\n".join(text_parts))
        for attachment in attachments:
            note = self._attachment_note(attachment)
            if note:
                agent_parts.append(note)
            if attachment.transcript_text:
                agent_parts.append(attachment.transcript_text)

        return NormalizedInbound(
            agent_text="\n\n".join(part for part in agent_parts if part.strip()),
            attachments=attachments,
            user_warnings=user_warnings,
        )

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self._boot_root.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    def _collect_text_parts(self, messages: list[Message]) -> list[str]:
        parts: list[str] = []
        for message in messages:
            candidate = (message.text or message.caption or "").strip()
            if not candidate:
                continue
            if candidate not in parts:
                parts.append(candidate)
        return parts

    def _extract_specs(self, message: Message) -> list[_AttachmentSpec]:
        specs: list[_AttachmentSpec] = []
        if message.photo:
            photo = message.photo[-1]
            specs.append(
                _AttachmentSpec(
                    kind="photo",
                    source=photo,
                    original_filename=self._fallback_filename(
                        kind="photo",
                        file_unique_id=getattr(photo, "file_unique_id", None),
                        mime_type="image/jpeg",
                    ),
                    mime_type="image/jpeg",
                    size_bytes=getattr(photo, "file_size", None),
                )
            )
        if message.document:
            document = message.document
            specs.append(
                _AttachmentSpec(
                    kind="document",
                    source=document,
                    original_filename=document.file_name or self._fallback_filename(
                        kind="document",
                        file_unique_id=getattr(document, "file_unique_id", None),
                        mime_type=document.mime_type,
                    ),
                    mime_type=document.mime_type,
                    size_bytes=document.file_size,
                )
            )
        if message.video:
            video = message.video
            specs.append(
                _AttachmentSpec(
                    kind="video",
                    source=video,
                    original_filename=getattr(video, "file_name", None)
                    or self._fallback_filename(
                        kind="video",
                        file_unique_id=getattr(video, "file_unique_id", None),
                        mime_type=video.mime_type,
                    ),
                    mime_type=video.mime_type,
                    size_bytes=video.file_size,
                )
            )
        if message.voice:
            voice = message.voice
            specs.append(
                _AttachmentSpec(
                    kind="voice",
                    source=voice,
                    original_filename=self._fallback_filename(
                        kind="voice",
                        file_unique_id=getattr(voice, "file_unique_id", None),
                        mime_type=voice.mime_type or "audio/ogg",
                    ),
                    mime_type=voice.mime_type,
                    size_bytes=voice.file_size,
                )
            )
        if message.audio:
            audio = message.audio
            specs.append(
                _AttachmentSpec(
                    kind="audio",
                    source=audio,
                    original_filename=audio.file_name or self._fallback_filename(
                        kind="audio",
                        file_unique_id=getattr(audio, "file_unique_id", None),
                        mime_type=audio.mime_type,
                    ),
                    mime_type=audio.mime_type,
                    size_bytes=audio.file_size,
                )
            )
        if message.video_note:
            video_note = message.video_note
            specs.append(
                _AttachmentSpec(
                    kind="video_note",
                    source=video_note,
                    original_filename=self._fallback_filename(
                        kind="video_note",
                        file_unique_id=getattr(video_note, "file_unique_id", None),
                        mime_type="video/mp4",
                    ),
                    mime_type="video/mp4",
                    size_bytes=video_note.file_size,
                )
            )
        if message.animation:
            animation = message.animation
            specs.append(
                _AttachmentSpec(
                    kind="animation",
                    source=animation,
                    original_filename=getattr(animation, "file_name", None)
                    or self._fallback_filename(
                        kind="animation",
                        file_unique_id=getattr(animation, "file_unique_id", None),
                        mime_type=animation.mime_type,
                    ),
                    mime_type=animation.mime_type,
                    size_bytes=animation.file_size,
                )
            )
        if message.sticker:
            sticker = message.sticker
            specs.append(
                _AttachmentSpec(
                    kind="sticker",
                    source=sticker,
                    original_filename=self._fallback_filename(
                        kind="sticker",
                        file_unique_id=getattr(sticker, "file_unique_id", None),
                        mime_type="image/webp",
                    ),
                    mime_type="image/webp",
                    size_bytes=sticker.file_size,
                )
            )
        return specs

    async def _download_spec(
        self,
        spec: _AttachmentSpec,
        *,
        message: Message,
        dest_dir: Path,
    ) -> DownloadedAttachment:
        try:
            telegram_file = await spec.source.get_file()
            stored_path = await self._download_file(telegram_file, dest_dir, spec)
        except Exception as exc:
            detail = _normalize_whitespace(str(exc)) or type(exc).__name__
            return DownloadedAttachment(
                kind=spec.kind,
                original_filename=spec.original_filename,
                stored_path=None,
                mime_type=spec.mime_type,
                size_bytes=spec.size_bytes,
                download_error=detail,
                message_id=message.message_id,
                media_group_id=message.media_group_id,
            )

        downloaded = DownloadedAttachment(
            kind=spec.kind,
            original_filename=spec.original_filename,
            stored_path=stored_path,
            mime_type=spec.mime_type,
            size_bytes=spec.size_bytes,
            message_id=message.message_id,
            media_group_id=message.media_group_id,
        )

        if spec.kind != "voice":
            return downloaded

        transcript_path = stored_path.with_suffix(".md")
        title = transcript_path.stem
        try:
            await self._run_transcription(
                audio_file=stored_path,
                title=title,
                dest_dir=dest_dir,
            )
            transcript_text = transcript_path.read_text(encoding="utf-8", errors="replace").strip()
            return DownloadedAttachment(
                kind=downloaded.kind,
                original_filename=downloaded.original_filename,
                stored_path=downloaded.stored_path,
                mime_type=downloaded.mime_type,
                size_bytes=downloaded.size_bytes,
                transcript_path=transcript_path,
                transcript_text=transcript_text,
                message_id=downloaded.message_id,
                media_group_id=downloaded.media_group_id,
            )
        except Exception as exc:
            detail = _normalize_whitespace(str(exc)) or type(exc).__name__
            return DownloadedAttachment(
                kind=downloaded.kind,
                original_filename=downloaded.original_filename,
                stored_path=downloaded.stored_path,
                mime_type=downloaded.mime_type,
                size_bytes=downloaded.size_bytes,
                transcript_path=transcript_path,
                transcription_error=detail,
                message_id=downloaded.message_id,
                media_group_id=downloaded.media_group_id,
            )

    async def _download_file(self, telegram_file: Any, dest_dir: Path, spec: _AttachmentSpec) -> Path:
        suffix = self._guess_suffix(
            file_path=getattr(telegram_file, "file_path", None),
            mime_type=spec.mime_type,
            fallback_name=spec.original_filename,
        )
        sanitized_name = self._sanitize_filename(spec.original_filename, suffix=suffix)
        stored_path = dest_dir / sanitized_name
        await telegram_file.download_to_drive(custom_path=stored_path)
        return stored_path

    async def _run_transcription(self, *, audio_file: Path, title: str, dest_dir: Path) -> None:
        if not self._transcription_script.is_file():
            raise FileNotFoundError(
                f"transcription script not found: {self._transcription_script}"
            )
        proc = await asyncio.create_subprocess_exec(
            str(self._transcription_script),
            str(audio_file),
            title,
            str(dest_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            output = (stdout or b"").decode("utf-8", errors="replace")
            detail = _normalize_whitespace(output) or f"exit code {proc.returncode}"
            raise RuntimeError(detail)

    def _attachment_note(self, attachment: DownloadedAttachment) -> str:
        lines: list[str] = []
        if attachment.unsupported_detail:
            lines.extend([
                "<system-note>",
                "Telegram attachment was received but is not downloadable by this runtime.",
                f"Attachment type: {attachment.unsupported_detail}",
                "</system-note>",
            ])
            return "\n".join(lines)

        if attachment.download_error:
            lines.extend([
                "<system-note>",
                "Telegram attachment was received, but the bot could not download it.",
                f"Kind: {attachment.kind}",
                f"Original filename: {attachment.original_filename}",
            ])
            if attachment.mime_type:
                lines.append(f"MIME type: {attachment.mime_type}")
            if attachment.size_bytes is not None:
                lines.append(f"Size bytes: {attachment.size_bytes}")
            lines.append(f"Download error: {attachment.download_error}")
            lines.append("</system-note>")
            return "\n".join(lines)

        if attachment.kind == "voice":
            lines.extend([
                "<system-note>",
                "Telegram voice message received.",
                f"Original filename: {attachment.original_filename}",
            ])
            if attachment.stored_path is not None:
                lines.append(f"Stored path: {attachment.stored_path}")
            if attachment.mime_type:
                lines.append(f"MIME type: {attachment.mime_type}")
            if attachment.size_bytes is not None:
                lines.append(f"Size bytes: {attachment.size_bytes}")
            if attachment.transcript_path is not None:
                lines.append(f"Transcript path: {attachment.transcript_path}")
            if attachment.transcription_error:
                lines.append(
                    "Automatic transcription failed. Use the stored file path directly."
                )
                lines.append(f"Transcription error: {attachment.transcription_error}")
            else:
                lines.append(
                    "The transcript below was generated automatically from audio and is not user-typed text."
                )
            lines.append("</system-note>")
            return "\n".join(lines)

        lines.extend([
            "<system-note>",
            "Telegram attachment received.",
            f"Kind: {attachment.kind}",
            f"Original filename: {attachment.original_filename}",
        ])
        if attachment.stored_path is not None:
            lines.append(f"Stored path: {attachment.stored_path}")
        if attachment.mime_type:
            lines.append(f"MIME type: {attachment.mime_type}")
        if attachment.size_bytes is not None:
            lines.append(f"Size bytes: {attachment.size_bytes}")
        lines.append("</system-note>")
        return "\n".join(lines)

    def _fallback_filename(
        self,
        *,
        kind: str,
        file_unique_id: str | None,
        mime_type: str | None,
    ) -> str:
        suffix = self._guess_suffix(file_path=None, mime_type=mime_type, fallback_name=None)
        base = f"{kind}_{file_unique_id or uuid.uuid4().hex}"
        return base + suffix

    def _guess_suffix(
        self,
        *,
        file_path: str | None,
        mime_type: str | None,
        fallback_name: str | None,
    ) -> str:
        if fallback_name:
            suffix = Path(fallback_name).suffix
            if suffix:
                return suffix
        if file_path:
            suffix = Path(file_path).suffix
            if suffix:
                return suffix
        if mime_type:
            suffix = mimetypes.guess_extension(mime_type)
            if suffix:
                return suffix
        return ""

    def _sanitize_filename(self, name: str, *, suffix: str = "") -> str:
        cleaned = _SAFE_FILENAME_RE.sub("_", name).strip(" .")
        cleaned = _WHITESPACE_RE.sub(" ", cleaned)
        if not cleaned:
            cleaned = f"attachment_{uuid.uuid4().hex}"
        if suffix and Path(cleaned).suffix != suffix:
            cleaned += suffix
        return cleaned


def _normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()
