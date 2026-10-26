"""Private Telegram media-generation bot backed by the authenticated media API."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import resource
import secrets
import sqlite3
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

LOG = logging.getLogger("obs_agent.telegram_media_bot")
ALLOWLIST = frozenset({227177188, 5129431382})

# This registry is the bot's advertised surface, not a claim about weights on disk.
# A model remains hidden until its backend path has real qualification evidence.
MODEL_REGISTRY = {
    "qwen-image-2.1": {
        "label": "Qwen Image 2.1",
        "media_kind": "image",
        "qualified": True,
        "deprecated": False,
    },
    "h3": {
        "label": "H3 Eros Max beta6 (checkpoint)",
        "media_kind": "video",
        "qualified": True,
        "deprecated": False,
    },
    "h3-beta1": {
        "label": "H3 Eros Max beta1 (previous checkpoint)",
        "media_kind": "video",
        "qualified": True,
        "deprecated": False,
    },
    "wan": {
        "label": "Wan 2.2 (deprecated)",
        "media_kind": "video",
        "qualified": True,
        "deprecated": True,
    },
    "ltx": {
        "label": "LTX 2.5 (qualified)",
        "media_kind": "video",
        "qualified": True,
        "deprecated": False,
    },
}
SELECTABLE_MODELS = tuple(name for name, spec in MODEL_REGISTRY.items() if spec["qualified"] and not spec["deprecated"])
SELECTABLE_VIDEO_MODELS = tuple(name for name in SELECTABLE_MODELS if MODEL_REGISTRY[name]["media_kind"] == "video")
PRESET_LONGEST = {"low": 500, "medium": 700, "high": 1000}
VIDEO_LORAS = {"aftermidnight", "aftermidnight-softer", "hmnsfw", "naughtytimes"}
ABANDONED_JOB_SECONDS = 24 * 60 * 60
DIAGNOSTIC_STAGES = {"received", "preflight", "rejected", "download", "image_probe", "normalize", "upload", "submit", "queued", "status", "result", "delivery", "cleanup", "delivered", "restart"}
DIAGNOSTIC_CAUSES = {"generation_failed", "tensor_shape_mismatch", "dtype_mismatch", "device_mismatch", "model_missing", "image_decode_failed", "gpu_memory_exhausted", "gpu_resource_unavailable", "operation_timeout", "invalid_geometry", "invalid_steps", "invalid_fps", "invalid_duration", "image_required", "invalid_prompt", "invalid_image_path", "invalid_upload", "unsupported_model_mode", "unsupported_model", "invalid_request", "job_not_succeeded", "result_missing", "unsafe_output_path", "purge_failed", "unknown_job", "invalid_upload_path", "api_rate_limited", "rate_limited", "api_unreachable", "api_transport_error", "image_probe_failed", "image_normalization_failed", "telegram_transport_error", "aborted_restart", "abandoned_timeout", "cleanup_failed", "failed", "cancelled", "delivery_failed"}
DIAGNOSTIC_NUMBERS = {"width", "height", "steps", "cfg", "seed", "seconds", "fps", "length", "strength", "lora_strength", "longest", "prompt_characters", "prompt_utf8_bytes", "input_bytes", "input_width", "input_height", "normalized_bytes", "normalized_width", "normalized_height", "output_bytes", "http_status", "effective_frame_count", "requested_seconds", "effective_duration_seconds", "rotation_degrees"}


def _safe_parameters(request: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in request.items() if key in DIAGNOSTIC_NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
    choices = {"model": set(MODEL_REGISTRY), "mode": {"t2i", "edit", "i2v", "t2v"}, "variant": {"turbo", "int8", "w4a8", "uncensored", "beta1", "beta6"}, "lora": VIDEO_LORAS | {"none"}, "preset": set(PRESET_LONGEST), "media_kind": {"image", "video"}, "input_format": {"png", "jpeg", "mjpeg", "webp", "bmp", "tiff", "gif", "unknown"}, "pixel_format": {"rgb24", "rgba", "rgb48be", "rgb48le", "gray", "gray16be", "gray16le", "yuv420p", "yuvj420p", "yuvj422p", "yuvj444p", "pal8", "gbrp", "gbrap"}, "normalization": {"aspect_fit_pad", "aspect_cover"}, "checkpoint": {"beta1", "beta6", "eros-beta1", "eros-beta6"}}
    for key, allowed in choices.items():
        value = request.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    for key in ("has_image", "auto_size", "content_purged"):
        if isinstance(request.get(key), bool):
            result[key] = request[key]
    return result


def _safe_failure_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    cause = parts[-1]
    if cause not in DIAGNOSTIC_CAUSES and not re.fullmatch(r"api_http_[1-5][0-9]{2}", cause):
        return None
    return value if len(parts) == 1 or (len(parts) == 2 and parts[0] in DIAGNOSTIC_STAGES | {"generation", "comfyui_start", "graph_submit", "output_validation", "handoff_begin", "gpu_lock_acquire", "handoff_acquired", "comfyui_cleanup", "gpu_lock_release", "handoff_release", "handoff_cancel", "worker", "input_stage"}) else None


def _safe_exception(exc: Exception) -> dict[str, Any]:
    code = "generation_failed"
    details: dict[str, Any] = {}
    if isinstance(exc, httpx.HTTPStatusError):
        details["http_status"] = exc.response.status_code
        try:
            response_data = exc.response.json()
            candidate = response_data.get("error")
            code = _safe_failure_code(candidate) or f"api_http_{exc.response.status_code}"
            request_id = response_data.get("request_id")
            if isinstance(request_id, str) and re.fullmatch(r"request-[0-9a-f]{16}", request_id):
                details["api_request_id"] = request_id
        except (ValueError, AttributeError):
            code = f"api_http_{exc.response.status_code}"
    elif isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        code = "operation_timeout"
    elif isinstance(exc, httpx.TransportError):
        code = "api_transport_error"
    elif _safe_failure_code(str(exc)):
        code = str(exc).rsplit(":", 1)[-1]
    kind = type(exc).__name__
    details.update(code=code, exception_type=kind if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", kind) else "Exception")
    details["frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)[-12:] if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", Path(frame.filename).name) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,100}", frame.name)]
    return details


def _alignment(model: str, mode: str) -> int:
    if model in {"h3", "h3-beta1"}:
        return 32 if mode == "i2v" else 16
    if model == "ltx":
        return 64
    if model == "qwen-image-2.1":
        return 32
    return 16


def _default_aspect(model: str, mode: str) -> float:
    if model == "ltx" and mode == "i2v":
        return 1.0
    if model == "qwen-image-2.1":
        return 1.0
    return 5 / 3


def _aligned_dimensions(model: str, mode: str, longest: int, aspect: float) -> tuple[int, int]:
    aspect = max(0.05, aspect)
    if model == "qwen-image-2.1":
        longest = min(longest, 1000)
    if aspect >= 1:
        width, height = longest, round(longest / aspect)
    else:
        width, height = round(longest * aspect), longest
    unit = _alignment(model, mode)
    width_steps = max(1, round(width / unit))
    height_steps = max(1, round(height / unit))
    candidates = []
    for width_step in range(max(1, width_steps - 1), width_steps + 2):
        for height_step in range(max(1, height_steps - 1), height_steps + 2):
            candidate_width, candidate_height = width_step * unit, height_step * unit
            if model == "qwen-image-2.1" and max(candidate_width, candidate_height) > 1000:
                continue
            ratio_error = abs((candidate_width / candidate_height) - aspect)
            size_error = abs(max(candidate_width, candidate_height) - longest)
            candidates.append((ratio_error, size_error, candidate_width, candidate_height))
    _, _, width, height = min(candidates)
    return width, height


def _resolve_dimensions(settings: "Settings", source_size: tuple[int, int] | None = None) -> tuple[int, int]:
    aspect = (source_size[0] / source_size[1]) if source_size else _default_aspect(settings.model, settings.mode)
    if not settings.auto_size:
        if settings.width is not None and settings.height is not None:
            return _aligned_dimensions(settings.model, settings.mode, max(settings.width, settings.height), settings.width / settings.height)
        if settings.width is not None:
            return _aligned_dimensions(settings.model, settings.mode, settings.width, aspect)
        if settings.height is not None:
            return _aligned_dimensions(settings.model, settings.mode, settings.height * aspect, aspect)
    return _aligned_dimensions(settings.model, settings.mode, settings.longest, aspect)


@dataclass(frozen=True)
class Settings:
    preset: str = "low"
    media_kind: str = "image"
    model: str = "qwen-image-2.1"
    mode: str = "t2i"
    variant: str = "turbo"
    lora: str | None = None
    lora_strength: float | None = None
    strength: float = 0.75
    steps: int = 20
    seconds: float = 4.0
    width: int | None = 512
    height: int | None = 512
    longest: int = 500
    auto_size: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS user_settings (
          user_id INTEGER PRIMARY KEY, settings_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY, backend_job_id TEXT, user_id INTEGER NOT NULL,
          chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, prompt TEXT NOT NULL DEFAULT '',
          request_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL, output_path TEXT,
          status_message_id INTEGER, last_status TEXT, error_code TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, delivered_at REAL
        );
        CREATE INDEX IF NOT EXISTS jobs_nonterminal ON jobs(state);
        CREATE TABLE IF NOT EXISTS job_diagnostics (
          job_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS job_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
          stage TEXT NOT NULL, occurred_at REAL NOT NULL, details_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id,event_id);
        CREATE TABLE IF NOT EXISTS telegram_cooldowns (
          chat_id INTEGER PRIMARY KEY, retry_at REAL NOT NULL
        );
        """)
        self._ensure_column("error_code", "TEXT")
        self._scrub_legacy_content()

    def _ensure_column(self, name: str, definition: str) -> None:
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if name not in columns:
            self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            self.db.commit()

    def _scrub_legacy_content(self) -> None:
        self.db.execute("UPDATE jobs SET prompt='', request_json='{}', output_path=NULL, last_status=state")
        for row in self.db.execute("SELECT job_id,state,error_code FROM jobs").fetchall():
            code = _safe_failure_code(row["error_code"])
            if code is None and row["state"] in {"failed", "cancelled", "delivery_failed"}:
                code = row["state"]
            self.db.execute("UPDATE jobs SET error_code=? WHERE job_id=?", (code, row["job_id"]))
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("VACUUM")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()

    def get_settings(self, user_id: int) -> Settings:
        row = self.db.execute("SELECT settings_json FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return Settings()
        try:
            data = json.loads(row[0])
            settings = Settings(**{k: data[k] for k in asdict(Settings()) if k in data})
            if settings.model not in SELECTABLE_MODELS:
                return Settings(media_kind="video", model="h3", mode="t2v", variant="beta6", steps=8, width=640, height=384)
            if settings.model == "h3" and settings.variant == "beta1":
                settings = replace(settings, model="h3-beta1")
                self.put_settings(user_id, settings)
            elif settings.model == "h3" and settings.variant != "beta6":
                settings = replace(settings, variant="beta6", steps=8 if settings.steps == 4 else settings.steps, lora=None, lora_strength=None)
                self.put_settings(user_id, settings)
            elif settings.model == "h3-beta1" and settings.variant != "beta1":
                settings = replace(settings, variant="beta1", lora=None, lora_strength=None)
                self.put_settings(user_id, settings)
            return settings
        except (TypeError, ValueError, KeyError):
            return Settings()

    def put_settings(self, user_id: int, settings: Settings) -> None:
        now = time.time()
        self.db.execute("INSERT INTO user_settings(user_id,settings_json,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json,updated_at=excluded.updated_at", (user_id, settings.to_json(), now))
        self.db.commit()

    def create_job(self, *, user_id: int, chat_id: int, message_id: int, prompt: str, request: dict[str, Any]) -> str:
        metadata = {**_safe_parameters(request), "prompt_characters": len(prompt), "prompt_utf8_bytes": len(prompt.encode("utf-8")), "has_image": request.get("mode") in {"i2v", "edit"}, "lora": request.get("lora") if request.get("lora") in VIDEO_LORAS else "none"}
        job_id = uuid.uuid4().hex
        now = time.time()
        self.db.execute("INSERT INTO jobs(job_id,user_id,chat_id,message_id,prompt,request_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, user_id, chat_id, message_id, "", "{}", "submitting", now, now))
        self.db.commit()
        self.record_diagnostics(job_id, stage="received", metadata=metadata)
        return job_id

    def record_diagnostics(self, job_id: str, *, stage: str, metadata: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        if stage not in DIAGNOSTIC_STAGES:
            raise ValueError("unknown diagnostic stage")
        now = time.time()
        row = self.db.execute("SELECT metadata_json FROM job_diagnostics WHERE job_id=?", (job_id,)).fetchone()
        current = json.loads(row[0]) if row else {"schema_version": 1}
        details = _safe_parameters(metadata or {})
        current.update(details)
        if error is not None:
            details["error"] = _safe_exception(error)
            current["error"] = details["error"]
        current["stage"] = stage
        current["updated_at"] = now
        self.db.execute("INSERT INTO job_diagnostics(job_id,metadata_json,updated_at) VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET metadata_json=excluded.metadata_json,updated_at=excluded.updated_at", (job_id, json.dumps(current, separators=(",", ":"), allow_nan=False), now))
        self.db.execute("INSERT INTO job_events(job_id,stage,occurred_at,details_json) VALUES(?,?,?,?)", (job_id, stage, now, json.dumps(details, separators=(",", ":"), allow_nan=False)))
        self.db.commit()

    def get_diagnostics(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT metadata_json FROM job_diagnostics WHERE job_id=?", (job_id,)).fetchone()
        result = json.loads(row[0]) if row else {}
        result["events"] = [{"stage": event["stage"], "at": event["occurred_at"], **json.loads(event["details_json"])} for event in self.db.execute("SELECT stage,occurred_at,details_json FROM job_events WHERE job_id=? ORDER BY event_id", (job_id,))]
        return result

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        clause = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE jobs SET {clause} WHERE job_id=?", (*fields.values(), job_id))
        self.db.commit()

    def get_job(self, job_id: str):
        return self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def get_user_job(self, user_id: int, reference: str):
        row = self.db.execute("SELECT * FROM jobs WHERE job_id=? AND user_id=?", (reference, user_id)).fetchone()
        if row:
            return row
        matches = self.db.execute("SELECT * FROM jobs WHERE job_id LIKE ? AND user_id=? ORDER BY created_at DESC", (reference + "%", user_id)).fetchall()
        return matches[0] if len(matches) == 1 else None

    def telegram_retry_at(self, chat_id: int) -> float:
        row = self.db.execute("SELECT retry_at FROM telegram_cooldowns WHERE chat_id=?", (chat_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def defer_telegram(self, chat_id: int, error: RetryAfter) -> None:
        delay = error.retry_after
        seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
        retry_at = time.time() + seconds + 1
        self.db.execute("INSERT INTO telegram_cooldowns(chat_id,retry_at) VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET retry_at=MAX(retry_at,excluded.retry_at)", (chat_id, retry_at))
        self.db.commit()

    def pending_deliveries(self):
        return self.db.execute("SELECT * FROM jobs WHERE state='delivery_pending' AND backend_job_id IS NOT NULL ORDER BY created_at").fetchall()

    def nonterminal(self):
        # Delivery is deliberately at-most-once: a crash after Telegram accepts the
        # message but before the delivered mark must not resend it on restart.
        return self.db.execute("SELECT * FROM jobs WHERE state NOT IN ('delivered','delivering','delivery_pending','delivery_failed','failed','cancelled') ORDER BY created_at").fetchall()

    def cleanup_candidates(self):
        return self.db.execute("SELECT * FROM jobs WHERE state IN ('delivery_failed','cleanup_pending') AND backend_job_id IS NOT NULL ORDER BY updated_at").fetchall()


class MediaAPI:
    def __init__(self, base_url: str, token_file: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=300))

    def headers(self) -> dict[str, str]:
        token = self.token_file.read_text().strip()
        if not token:
            raise RuntimeError("media API secret is empty")
        return {"Authorization": f"Bearer {token}", "X-Telegram-Ephemeral": "1"}

    async def capabilities(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.base_url}/v1/capabilities", headers=self.headers())
        response.raise_for_status()
        return response.json()

    async def upload(self, content: bytes, content_type: str) -> str:
        payload = {"content_type": content_type, "data": base64.b64encode(content).decode("ascii")}
        response = await self.client.post(f"{self.base_url}/v1/uploads", headers=self.headers(), json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["image_path"]

    async def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(f"{self.base_url}/v1/jobs", headers=self.headers(), json=request, timeout=60)
        response.raise_for_status()
        return response.json()

    async def status(self, backend_job_id: str) -> dict[str, Any]:
        response = await self.client.get(f"{self.base_url}/v1/jobs/{backend_job_id}", headers=self.headers(), timeout=60)
        response.raise_for_status()
        return response.json()

    async def result(self, backend_job_id: str) -> tuple[bytes, str]:
        response = await self.client.get(f"{self.base_url}/v1/jobs/{backend_job_id}/result", headers=self.headers(), timeout=360)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def purge(self, backend_job_id: str) -> None:
        response = await self.client.post(f"{self.base_url}/v1/jobs/{backend_job_id}/purge", headers=self.headers(), timeout=60)
        response.raise_for_status()

    async def purge_upload(self, image_path: str) -> None:
        response = await self.client.post(f"{self.base_url}/v1/uploads/purge", headers=self.headers(), json={"image_path": image_path}, timeout=60)
        response.raise_for_status()

    async def close(self) -> None:
        await self.client.aclose()


class MediaBot:
    def __init__(self, *, token: str, state: StateStore, api: MediaAPI, result_root: Path) -> None:
        self.state = state
        self.api = api
        self.result_root = result_root
        self.application: Application | None = None
        self.tasks: set[asyncio.Task[Any]] = set()
        self.reaper_task: asyncio.Task[Any] | None = None
        self.delivery_jobs: set[str] = set()

    @staticmethod
    def authorized(update: Update) -> bool:
        user = update.effective_user
        return user is not None and int(user.id) in ALLOWLIST

    async def deny(self, update: Update) -> None:
        if update.callback_query:
            await update.callback_query.answer("Not authorized", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Not authorized.")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return await self.deny(update)
        await update.effective_message.reply_text("Private media bot ready. Send a prompt or /settings. Use /kind image|video and /model qwen-image-2.1|h3|h3-beta1|ltx (H3 Eros Max beta6 or previous beta1 checkpoint or qualified LTX 2.5).")

    @staticmethod
    def _display_strength(settings: Settings) -> float | str:
        if settings.media_kind == "video":
            return settings.lora_strength if settings.lora_strength is not None else "default"
        return settings.strength

    @staticmethod
    def _settings_keyboard(user_id: int, settings: Settings) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Kind: {settings.media_kind}", callback_data=f"s:{user_id}:kind")],
            [InlineKeyboardButton(f"Preset: {settings.preset}", callback_data=f"s:{user_id}:preset")],
            [InlineKeyboardButton(f"Model: {MODEL_REGISTRY.get(settings.model, {}).get('label', settings.model)}", callback_data=f"s:{user_id}:model")],
            [InlineKeyboardButton(f"Steps: {settings.steps}", callback_data=f"s:{user_id}:steps")],
            [InlineKeyboardButton(f"Mode: {settings.mode}", callback_data=f"s:{user_id}:mode")],
            [InlineKeyboardButton(f"LoRA: {settings.lora or 'none'}", callback_data=f"s:{user_id}:lora")],
            [InlineKeyboardButton(f"Strength: {MediaBot._display_strength(settings)}", callback_data=f"s:{user_id}:strength")],
            [InlineKeyboardButton(f"Width: {settings.width if not settings.auto_size and settings.width is not None else 'auto'}", callback_data=f"s:{user_id}:width")],
            [InlineKeyboardButton(f"Height: {settings.height if not settings.auto_size and settings.height is not None else 'auto'}", callback_data=f"s:{user_id}:height")],
            [InlineKeyboardButton(f"Longest: {settings.longest}", callback_data=f"s:{user_id}:longest")],
        ])

    async def settings_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return await self.deny(update)
        settings = self.state.get_settings(update.effective_user.id)
        await update.effective_message.reply_text(
            self._settings_text(settings),
            reply_markup=self._settings_keyboard(update.effective_user.id, settings),
        )

    @staticmethod
    def _settings_text(settings: Settings) -> str:
        model_label = MODEL_REGISTRY.get(settings.model, {}).get("label", settings.model)
        width, height = _resolve_dimensions(settings)
        size = f"auto {width}x{height} longest={settings.longest}" if settings.auto_size else f"manual {width}x{height}"
        strength = MediaBot._display_strength(settings)
        strength_text = f"{strength:g}" if isinstance(strength, float) else strength
        return (f"kind={settings.media_kind} model={model_label} mode={settings.mode}\n"
                f"preset={settings.preset} {size} steps={settings.steps} seconds={settings.seconds:g}\n"
                f"variant={settings.variant} lora={settings.lora or 'none'} strength={strength_text}")

    @staticmethod
    def _apply_setting(settings: Settings, key: str, raw: str) -> Settings:
        data = asdict(settings)
        key = key.lower().lstrip("/")
        value = raw.lower()
        if key in {"auto", "reset-auto"}:
            if value not in {"auto", "reset", "true"}: raise ValueError
            data.update(auto_size=True, width=None, height=None)
        elif key in {"kind", "media_kind"}:
            if value not in {"image", "video"}: raise ValueError
            data.update(media_kind=value, model="qwen-image-2.1" if value == "image" else "h3", mode="t2i" if value == "image" else "t2v", auto_size=True, width=None, height=None)
            data.update(steps=20 if value == "image" else 8, variant="turbo" if value == "image" else "beta6", lora=None, lora_strength=None)
        elif key == "model":
            if value not in SELECTABLE_MODELS: raise ValueError
            data.update(model=value, media_kind="image" if value == "qwen-image-2.1" else "video", mode="t2i" if value == "qwen-image-2.1" else "t2v", auto_size=True, width=None, height=None)
            data.update(variant="beta6" if value == "h3" else "beta1" if value == "h3-beta1" else "uncensored", steps=20 if value == "qwen-image-2.1" else 4 if value == "h3-beta1" else 8, lora=None, lora_strength=None)
        elif key == "mode":
            valid = {"t2i", "edit"} if settings.media_kind == "image" else {"t2v", "i2v"}
            if value not in valid: raise ValueError
            data["mode"] = value
        elif key == "preset":
            if value not in PRESET_LONGEST: raise ValueError
            data.update(preset=value, longest=PRESET_LONGEST[value], auto_size=True, width=None, height=None)
        elif key == "longest":
            longest = int(raw)
            if not 256 <= longest <= 2048: raise ValueError
            data.update(longest=longest, auto_size=True, width=None, height=None)
            data["preset"] = min(PRESET_LONGEST, key=lambda name: abs(PRESET_LONGEST[name] - longest))
        elif key in {"width", "height"}:
            if value == "auto":
                data[key] = None
                data["auto_size"] = data["width"] is None and data["height"] is None
            else:
                dimension = int(raw)
                if not 64 <= dimension <= 4096: raise ValueError
                data.update({key: dimension, "auto_size": False})
        elif key in {"steps", "duration", "seconds", "strength", "lora_strength", "variant", "lora"}:
            if key == "steps":
                data["steps"] = int(raw)
                if not 1 <= data["steps"] <= 40: raise ValueError
            elif key in {"duration", "seconds"}:
                data["seconds"] = float(raw)
                if not 1.0 <= data["seconds"] <= 15.0: raise ValueError
            elif key in {"strength", "lora_strength"}:
                target = "lora_strength" if settings.media_kind == "video" and key == "strength" else key
                data[target] = None if value == "none" else float(raw)
                if data[target] is not None and not 0.0 <= data[target] <= 2.0: raise ValueError
            elif key == "variant":
                valid = {"h3": {"beta6"}, "h3-beta1": {"beta1"}, "ltx": {"uncensored"}, "qwen-image-2.1": set()}.get(settings.model, set())
                if value not in valid: raise ValueError
                data["variant"] = value
            else:
                if settings.model not in {"h3", "h3-beta1"} or value not in VIDEO_LORAS | {"none"}: raise ValueError
                data["lora"] = None if value == "none" else value
        else:
            raise ValueError
        return Settings(**data)

    async def setting_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return await self.deny(update)
        args = context.args
        cmd = update.effective_message.text.split()[0].split("@", 1)[0].lower()
        settings = self.state.get_settings(update.effective_user.id)
        usage = "Usage: /set KEY VALUE (model, mode, preset, longest, width, height, steps, duration, variant, lora, strength, auto); /kind image|video; /model qwen-image-2.1|h3|h3-beta1|ltx"
        if cmd == "/set":
            if len(args) != 2:
                return await update.effective_message.reply_text(usage)
            key, raw = args
        elif cmd == "/auto":
            key, raw = "auto", "auto"
        else:
            if not args:
                return await update.effective_message.reply_text(usage)
            key, raw = cmd, args[0]
        try:
            new_settings = self._apply_setting(settings, key, raw)
        except (ValueError, TypeError):
            return await update.effective_message.reply_text("Invalid setting. Use /settings or /set KEY VALUE; /set auto reset restores adaptive sizing.")
        self.state.put_settings(update.effective_user.id, new_settings)
        await update.effective_message.reply_text(self._settings_text(new_settings), reply_markup=self._settings_keyboard(update.effective_user.id, new_settings))

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if user is None or int(user.id) not in ALLOWLIST:
            return await self.deny(update)
        parts = str(query.data or "").split(":")
        if len(parts) != 3 or parts[0] != "s" or parts[1] != str(user.id):
            await query.answer("This control belongs to another user", show_alert=True)
            return
        settings = self.state.get_settings(user.id)
        step_values = [8, 12, 20] if settings.model == "h3" else [4, 20] if settings.model == "h3-beta1" else [8] if settings.model == "ltx" else [12, 20]
        choices = {
            "kind": ["image", "video"],
            "preset": list(PRESET_LONGEST),
            "model": list(SELECTABLE_MODELS),
            "steps": step_values,
            "mode": ["t2i", "edit"] if settings.media_kind == "image" else ["t2v", "i2v"],
            "lora": ["none", *sorted(VIDEO_LORAS)],
            "strength": ["none", 0.25, 0.5, 0.75, 1.0, 1.5, 2.0],
            "width": ["auto", 512, 704, 992],
            "height": ["auto", 512, 704, 992],
            "longest": [500, 700, 1000],
        }
        key = parts[2]
        values = choices.get(key)
        if values is None:
            await query.answer("Unknown setting", show_alert=True); return
        if key in {"lora", "strength"} and settings.media_kind != "video":
            await query.answer("LoRA controls are available only in video mode", show_alert=True); return
        current = settings.lora if key == "lora" else (settings.lora_strength if key == "strength" and settings.media_kind == "video" else settings.strength if key == "strength" else getattr(settings, key if key != "kind" else "media_kind"))
        current_value = "none" if current is None and key in {"lora", "strength"} else current
        if key in {"width", "height"} and (settings.auto_size or getattr(settings, key) is None):
            current_value = "auto"
        value = values[(values.index(current_value) + 1) % len(values)] if current_value in values else values[0]
        try:
            new_settings = self._apply_setting(settings, key, str(value))
        except (ValueError, TypeError):
            await query.answer("Unsupported setting", show_alert=True); return
        self.state.put_settings(user.id, new_settings)
        await query.answer("Saved")
        await query.edit_message_text(self._settings_text(new_settings), reply_markup=self._settings_keyboard(user.id, new_settings))

    async def result_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return await self.deny(update)
        if not context.args or len(context.args) != 1:
            await update.effective_message.reply_text("Usage: /result JOB_ID — explicitly recover an ambiguous or pending delivery; an ambiguous retry may duplicate a Telegram message that was already accepted.")
            return
        row = self.state.get_user_job(update.effective_user.id, context.args[0].strip())
        if row is None:
            await update.effective_message.reply_text("Job not found for this user.")
            return
        job_id = row["job_id"]
        if row["state"] == "delivered":
            await update.effective_message.reply_text(f"Job {job_id[:12]} is already delivered; no local media copy is retained.")
            return
        if row["state"] in {"delivery_pending", "delivering"} and row["backend_job_id"]:
            await self._deliver_backend_result(job_id, row["backend_job_id"], row["chat_id"], explicit=True)
            return
        await update.effective_message.reply_text(f"Job {job_id[:12]} has no locally retained result. Check Telegram delivery or resubmit the original request.")

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        message = update.effective_message
        if not message:
            return
        settings = self.state.get_settings(update.effective_user.id)
        has_image = bool(message.photo or (message.document and (message.document.mime_type or "").startswith("image/")))
        prompt = (message.caption or message.text or "").strip()
        if not prompt and not has_image and not message.document:
            return
        mode = ("edit" if settings.media_kind == "image" else "i2v") if has_image else settings.mode
        effective_settings = replace(settings, mode=mode)
        width, height = _resolve_dimensions(effective_settings)
        request = {"model": "h3" if settings.model == "h3-beta1" else settings.model, "mode": mode, "prompt": prompt, "width": width, "height": height, "steps": settings.steps}
        if settings.media_kind == "video":
            request.update(seconds=settings.seconds, variant=settings.variant)
            if settings.lora:
                request.update(lora=settings.lora, lora_strength=settings.lora_strength)
        else:
            request.update(cfg=4.0, strength=settings.strength)
        job_id = self.state.create_job(user_id=update.effective_user.id, chat_id=message.chat_id, message_id=message.message_id, prompt=prompt, request=request)
        self.state.record_diagnostics(job_id, stage="preflight", metadata={**asdict(settings), "has_image": has_image})
        rejection = None
        if message.document and not has_image:
            rejection = ("invalid_upload", "Only image documents are supported as media input.")
        elif has_image and not message.caption:
            rejection = ("invalid_prompt", "Add a caption describing the edit or image-to-video request; a photo alone is not generated.")
        elif settings.media_kind == "image" and settings.model != "qwen-image-2.1":
            rejection = ("unsupported_model_mode", "Image mode requires qwen-image-2.1.")
        elif settings.media_kind == "video" and settings.model == "qwen-image-2.1":
            rejection = ("unsupported_model_mode", "Video mode requires a qualified video checkpoint.")
        elif not has_image and settings.mode in {"edit", "i2v"}:
            rejection = ("image_required", f"Mode {settings.mode} requires an image attachment.")
        if rejection:
            code, notice = rejection
            self.state.record_diagnostics(job_id, stage="rejected", error=ValueError(code))
            self.state.update_job(job_id, state="failed", error_code=f"preflight:{code}")
            await message.reply_text(f"{notice} Reference {job_id[:12]}.")
            return
        ack = None
        try:
            if self.state.telegram_retry_at(message.chat_id) <= time.time():
                ack = await message.reply_text(f"Queued {job_id[:12]} ({settings.model}/{mode}, {settings.preset} {width}x{height})")
        except RetryAfter as exc:
            self.state.defer_telegram(message.chat_id, exc)
            self.state.record_diagnostics(job_id, stage="delivery", error=exc)
        except Exception as exc:
            self.state.record_diagnostics(job_id, stage="delivery", error=exc)
            self.state.update_job(job_id, state="failed", error_code="delivery:telegram_transport_error")
            return
        self.state.update_job(job_id, status_message_id=ack.message_id if ack else None)
        task = asyncio.create_task(self._submit_and_watch(job_id, update.effective_user.id, message.chat_id, message, effective_settings, has_image, request))
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def _probe_image(self, raw: bytes) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-f", "image2pipe", "-i", "pipe:0",
            "-show_entries", "stream=width,height,codec_name,pix_fmt:stream_side_data=rotation", "-of", "json",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, _error = await process.communicate(raw)
        if process.returncode != 0:
            raise RuntimeError("image_probe_failed")
        try:
            stream = json.loads(output)["streams"][0]
            width, height = int(stream["width"]), int(stream["height"])
            rotation = next((item["rotation"] for item in stream.get("side_data_list", []) if isinstance(item.get("rotation"), (int, float))), 0)
            if int(rotation) % 180 == 90:
                width, height = height, width
            return _safe_parameters({"input_width": width, "input_height": height, "input_format": stream.get("codec_name", "unknown"), "pixel_format": stream.get("pix_fmt"), "rotation_degrees": rotation})
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("image_probe_failed") from None

    async def _normalize_image(self, raw: bytes, settings: Settings) -> bytes:
        """Apply aspect-cover crop and model-valid resize before upload."""
        if settings.width is None or settings.height is None:
            raise RuntimeError("resolved dimensions are required before image normalization")
        if settings.model == "qwen-image-2.1":
            vf = f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease,pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2"
        else:
            vf = f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase,crop={settings.width}:{settings.height}"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-vf", vf, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(raw)
        if process.returncode != 0 or not output:
            raise RuntimeError("image_normalization_failed")
        return output

    async def _submit_and_watch(self, job_id: str, user_id: int, chat_id: int, message, settings: Settings, has_image: bool, request: dict[str, Any]) -> None:
        request = dict(request)
        settings = replace(settings, mode=request["mode"])
        uploaded_path = None
        backend_id = None
        stage = "download" if has_image else "submit"
        self.state.record_diagnostics(job_id, stage=stage)
        try:
            if has_image:
                attachment = message.photo[-1] if message.photo else message.document
                telegram_file = await attachment.get_file()
                raw = bytes(await telegram_file.download_as_bytearray())
                stage = "image_probe"
                self.state.record_diagnostics(job_id, stage=stage, metadata={"input_bytes": len(raw)})
                image_metadata = await self._probe_image(raw)
                source_size = (image_metadata["input_width"], image_metadata["input_height"])
                width, height = _resolve_dimensions(settings, source_size)
                effective_settings = replace(settings, width=width, height=height, auto_size=False)
                request.update(width=width, height=height)
                stage = "normalize"
                self.state.record_diagnostics(job_id, stage=stage, metadata={**image_metadata, **request, "normalization": "aspect_fit_pad" if settings.model == "qwen-image-2.1" else "aspect_cover"})
                raw = await self._normalize_image(raw, effective_settings)
                stage = "upload"
                self.state.record_diagnostics(job_id, stage=stage, metadata={"normalized_bytes": len(raw), "normalized_width": width, "normalized_height": height})
                uploaded_path = await self.api.upload(raw, "image/png")
                request["image_path"] = uploaded_path
                del raw
            stage = "submit"
            self.state.record_diagnostics(job_id, stage=stage, metadata=request)
            submitted = await self.api.submit(request)
            backend_id = submitted["job_id"]
            self.state.update_job(job_id, backend_job_id=backend_id, state="queued")
            self.state.record_diagnostics(job_id, stage="queued", metadata=submitted)
            await self._watch(job_id, backend_id, chat_id)
        except Exception as exc:
            self.state.record_diagnostics(job_id, stage=stage, error=exc)
            error_code = f"{stage}:{_safe_exception(exc)['code']}"
            LOG.error("media job failed job=%s error_code=%s", job_id, error_code)
            if uploaded_path and not backend_id:
                try:
                    await self.api.purge_upload(uploaded_path)
                except Exception as cleanup_exc:
                    self.state.record_diagnostics(job_id, stage="cleanup", error=cleanup_exc)
                    LOG.error("uploaded media cleanup pending job=%s error_code=%s", job_id, type(cleanup_exc).__name__)
            current = self.state.get_job(job_id)
            if current is not None and current["state"] == "delivering":
                self.state.update_job(job_id, last_status="delivery_ambiguous", error_code=error_code)
                return
            self.state.update_job(job_id, state="cleanup_pending" if backend_id else "failed", last_status="failed", error_code=error_code)
            try:
                await message.reply_text(f"Job {job_id[:12]} failed: {error_code}; diagnostic metadata saved, no prompt or image retained. Resubmit from Telegram if needed.")
            except Exception as notice_exc:
                self.state.record_diagnostics(job_id, stage="delivery", error=notice_exc)

    async def _purge_backend(self, job_id: str, backend_id: str) -> bool:
        self.state.record_diagnostics(job_id, stage="cleanup")
        for attempt in range(3):
            try:
                await self.api.purge(backend_id)
                self.state.record_diagnostics(job_id, stage="cleanup", metadata={"content_purged": True})
                return True
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return True
                if attempt == 2:
                    LOG.error("media content cleanup failed job=%s error_code=http_%s", job_id, exc.response.status_code)
                else:
                    await asyncio.sleep(1)
            except Exception as exc:
                if attempt == 2:
                    LOG.error("media content cleanup failed job=%s error_code=%s", job_id, type(exc).__name__)
                else:
                    await asyncio.sleep(1)
        return False

    async def _deliver_backend_result(self, job_id: str, backend_id: str, chat_id: int, *, explicit: bool = False) -> None:
        if job_id in self.delivery_jobs:
            return
        self.delivery_jobs.add(job_id)
        try:
            await self._try_deliver_backend_result(job_id, backend_id, chat_id, explicit=explicit)
        finally:
            self.delivery_jobs.discard(job_id)

    async def _try_deliver_backend_result(self, job_id: str, backend_id: str, chat_id: int, *, explicit: bool = False) -> None:
        row = self.state.get_job(job_id)
        if row["state"] == "delivered" or (row["state"] == "delivering" and not explicit):
            return
        if self.state.telegram_retry_at(chat_id) > time.time():
            self.state.update_job(job_id, state="delivery_pending", last_status="delivery_pending", error_code="delivery:rate_limited")
            return
        self.state.record_diagnostics(job_id, stage="result")
        try:
            content, content_type = await self.api.result(backend_id)
        except Exception as exc:
            self.state.record_diagnostics(job_id, stage="result", error=exc)
            retryable = isinstance(exc, httpx.TransportError) or (isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code >= 500 or exc.response.status_code == 429))
            self.state.update_job(job_id, state="delivery_pending" if retryable else "cleanup_pending", error_code=f"result:{_safe_exception(exc)['code']}")
            if not retryable:
                await self._purge_backend(job_id, backend_id)
            return
        self.state.update_job(job_id, state="delivering", last_status="delivering")
        self.state.record_diagnostics(job_id, stage="delivery", metadata={"output_bytes": len(content)})
        try:
            if content_type.startswith("image/"):
                await self.application.bot.send_photo(chat_id=chat_id, photo=content, caption=f"Job {job_id[:12]} complete")
            else:
                await self.application.bot.send_video(chat_id=chat_id, video=content, caption=f"Job {job_id[:12]} complete", supports_streaming=True)
        except RetryAfter as exc:
            self.state.defer_telegram(chat_id, exc)
            self.state.record_diagnostics(job_id, stage="delivery", error=exc)
            self.state.update_job(job_id, state="delivery_pending", last_status="delivery_pending", error_code="delivery:rate_limited")
        except NetworkError as exc:
            self.state.record_diagnostics(job_id, stage="delivery", error=exc)
            self.state.update_job(job_id, state="delivering", last_status="delivery_ambiguous", error_code="delivery:telegram_transport_error")
        except Exception as exc:
            self.state.record_diagnostics(job_id, stage="delivery", error=exc)
            purged = await self._purge_backend(job_id, backend_id)
            self.state.update_job(job_id, state="delivery_failed", last_status="delivery_failed", error_code="delivery:telegram_transport_error" if purged else "cleanup_failed")
            if not purged:
                LOG.error("delivery cleanup pending job=%s", job_id)
        else:
            purged = await self._purge_backend(job_id, backend_id)
            self.state.update_job(job_id, state="delivered", last_status="delivered" if purged else "cleanup_pending", delivered_at=time.time(), error_code=None if purged else "cleanup_failed")
            self.state.record_diagnostics(job_id, stage="delivered", metadata={"content_purged": purged})
        finally:
            del content

    async def _watch(self, job_id: str, backend_id: str, chat_id: int) -> None:
        row = self.state.get_job(job_id)
        status_message_id = row["status_message_id"]
        last_update = 0.0
        previous_state = None
        announced_state = None
        while True:
            try:
                status = await self.api.status(backend_id)
            except Exception as exc:
                self.state.record_diagnostics(job_id, stage="status", error=exc)
                if isinstance(exc, httpx.TransportError) or (isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code >= 500 or exc.response.status_code == 429)):
                    await asyncio.sleep(5)
                    continue
                self.state.update_job(job_id, state="cleanup_pending", last_status="cleanup_pending", error_code=f"status:{_safe_exception(exc)['code']}")
                if self.application:
                    await self.application.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=f"Job {job_id[:12]}: status unavailable; resubmit from Telegram")
                return
            state = str(status.get("state", "unknown"))
            if state not in {"queued", "waiting_for_gpu", "starting_comfyui", "running", "finalizing", "succeeded", "failed", "cancelled"}:
                state = "unknown"
            if state != previous_state:
                self.state.record_diagnostics(job_id, stage="status", metadata=status)
                previous_state = state
            self.state.update_job(job_id, state=state, last_status=state)
            now = time.monotonic()
            if now - last_update >= 30 and self.application and status_message_id and state != announced_state and self.state.telegram_retry_at(chat_id) <= time.time():
                try:
                    await self.application.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=f"Job {job_id[:12]}: {state} ({backend_id})")
                    announced_state = state
                except RetryAfter as exc:
                    self.state.defer_telegram(chat_id, exc)
                    self.state.record_diagnostics(job_id, stage="status", error=exc)
                except Exception:
                    LOG.debug("status edit failed job=%s", job_id)
                last_update = now
            if state in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(5)
        if state != "succeeded":
            cause = _safe_failure_code(status.get("error")) or "generation_failed"
            failure_stage = status.get("failure_stage", "generation")
            safe_code = _safe_failure_code(f"{failure_stage}:{cause}") or f"status:{cause}"
            self.state.record_diagnostics(job_id, stage="status", error=RuntimeError(safe_code))
            self.state.update_job(job_id, state=state, last_status=state, error_code="cancelled" if state == "cancelled" else safe_code)
            try:
                await self.api.purge(backend_id)
            except Exception as exc:
                LOG.error("terminal media cleanup pending job=%s error_code=%s", job_id, type(exc).__name__)
                self.state.update_job(job_id, last_status="cleanup_pending", error_code=type(exc).__name__)
            await self.application.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=f"Job {job_id[:12]}: {state} ({safe_code}); diagnostic metadata saved.")
            return
        await self._deliver_backend_result(job_id, backend_id, chat_id)

    async def _retry_pending_deliveries(self) -> None:
        for row in self.state.pending_deliveries():
            if self.state.telegram_retry_at(row["chat_id"]) <= time.time():
                await self._deliver_backend_result(row["job_id"], row["backend_job_id"], row["chat_id"])

    async def _reaper(self) -> None:
        while True:
            await self._retry_pending_deliveries()
            for row in self.state.cleanup_candidates():
                if await self._purge_backend(row["job_id"], row["backend_job_id"]):
                    self.state.update_job(row["job_id"], last_status="cleanup_complete", error_code=None)
            await asyncio.sleep(60)

    async def reconcile(self, application: Application) -> None:
        self.application = application
        now = time.time()
        for row in self.state.cleanup_candidates():
            if await self._purge_backend(row["job_id"], row["backend_job_id"]):
                self.state.update_job(row["job_id"], last_status="cleanup_complete", error_code=None)
        for row in self.state.nonterminal():
            if not row["backend_job_id"]:
                self.state.record_diagnostics(row["job_id"], stage="restart", error=RuntimeError("aborted_restart"))
                self.state.update_job(row["job_id"], state="failed", last_status="aborted_restart", error_code="aborted_restart")
                continue
            if now - float(row["updated_at"]) > ABANDONED_JOB_SECONDS:
                try:
                    await self.api.purge(row["backend_job_id"])
                except Exception as exc:
                    LOG.error("abandoned media cleanup pending job=%s error_code=%s", row["job_id"], type(exc).__name__)
                    self.state.update_job(row["job_id"], last_status="cleanup_pending", error_code=type(exc).__name__)
                else:
                    self.state.update_job(row["job_id"], state="failed", last_status="abandoned_timeout", error_code="abandoned_timeout")
                continue
            task = asyncio.create_task(self._watch(row["job_id"], row["backend_job_id"], row["chat_id"]))
            self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def configure(self, application: Application) -> None:
        await self.reconcile(application)
        self.reaper_task = asyncio.create_task(self._reaper())
        await application.bot.set_my_commands([
            ("start", "show the private media bot welcome"),
            ("settings", "show and cycle current settings"),
            ("set", "set KEY VALUE; use auto to reset sizing"),
            ("auto", "reset width and height to adaptive sizing"),
            ("kind", "set image or video kind"),
            ("model", "select a qualified model"),
            ("mode", "select t2i, edit, t2v, or i2v"),
            ("preset", "select low, medium, or high longest side"),
            ("longest", "set adaptive longest side"),
            ("width", "set manual width or auto"),
            ("height", "set manual height or auto"),
            ("steps", "set sampling steps"),
            ("duration", "set requested video seconds"),
            ("variant", "set a supported model variant"),
            ("lora", "set the H3 LoRA"),
            ("strength", "set edit or LoRA strength"),
            ("result", "recover an explicitly requested result"),
        ])

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        del update
        LOG.error("media handler failed exception_type=%s", type(context.error).__name__)

    def build(self) -> Application:
        token = os.environ.get("TELEGRAM_MEDIA_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_MEDIA_BOT_TOKEN is required")
        app = Application.builder().token(token).build()
        app.add_error_handler(self.error_handler)
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("settings", self.settings_cmd))
        for command in ("set", "auto", "kind", "model", "mode", "preset", "longest", "width", "height", "steps", "duration", "variant", "lora", "strength"):
            app.add_handler(CommandHandler(command, self.setting_command))
        app.add_handler(CommandHandler("result", self.result_command))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r"^s:"))
        app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, self.message))
        self.application = app
        return app


async def run() -> None:
    db = Path(os.environ.get("TELEGRAM_MEDIA_STATE_DB", "/workspace/runtime/state/telegram-media-bot.sqlite3"))
    results = Path(os.environ.get("TELEGRAM_MEDIA_RESULTS", "/workspace/runtime/state/telegram-media-bot/results"))
    api = MediaAPI(os.environ.get("TELEGRAM_MEDIA_API_URL", "http://host.docker.internal:8190"), Path(os.environ.get("TELEGRAM_MEDIA_API_TOKEN_FILE", "/run/secrets/media_api_key")))
    bot = MediaBot(token=os.environ.get("TELEGRAM_MEDIA_BOT_TOKEN", ""), state=StateStore(db), api=api, result_root=results)
    LOG.warning("media bot release pid=%s source=%s sha256=%s", os.getpid(), __file__, hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    app = bot.build()
    await app.initialize(); await bot.configure(app); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        if bot.reaper_task:
            bot.reaper_task.cancel()
            await asyncio.gather(bot.reaper_task, return_exceptions=True)
        await app.updater.stop(); await app.stop(); await app.shutdown(); await api.close()


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.ExtBot").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
