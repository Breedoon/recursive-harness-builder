"""Private Telegram media-generation bot backed by the authenticated media API."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
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
        "label": "H3 Eros Max beta5 (checkpoint)",
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


def _alignment(model: str, mode: str) -> int:
    if model == "h3":
        return 32 if mode == "i2v" else 16
    if model == "ltx":
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
          chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, prompt TEXT NOT NULL,
          request_json TEXT NOT NULL, state TEXT NOT NULL, output_path TEXT,
          status_message_id INTEGER, last_status TEXT, created_at REAL NOT NULL,
          updated_at REAL NOT NULL, delivered_at REAL
        );
        CREATE INDEX IF NOT EXISTS jobs_nonterminal ON jobs(state);
        """)
        self.db.commit()

    def get_settings(self, user_id: int) -> Settings:
        row = self.db.execute("SELECT settings_json FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return Settings()
        try:
            data = json.loads(row[0])
            settings = Settings(**{k: data[k] for k in asdict(Settings()) if k in data})
            if settings.model not in SELECTABLE_MODELS:
                return Settings(media_kind="video", model="h3", mode="t2v", variant="turbo", steps=4, width=640, height=384)
            return settings
        except (TypeError, ValueError, KeyError):
            return Settings()

    def put_settings(self, user_id: int, settings: Settings) -> None:
        now = time.time()
        self.db.execute("INSERT INTO user_settings(user_id,settings_json,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json,updated_at=excluded.updated_at", (user_id, settings.to_json(), now))
        self.db.commit()

    def create_job(self, *, user_id: int, chat_id: int, message_id: int, prompt: str, request: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        self.db.execute("INSERT INTO jobs(job_id,user_id,chat_id,message_id,prompt,request_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (job_id, user_id, chat_id, message_id, prompt, json.dumps(request, separators=(",", ":")), "submitting", now, now))
        self.db.commit()
        return job_id

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

    def nonterminal(self):
        # Delivery is deliberately at-most-once: a crash after Telegram accepts the
        # message but before the delivered mark must not resend it on restart.
        return self.db.execute("SELECT * FROM jobs WHERE state NOT IN ('delivered','delivering','failed','cancelled') ORDER BY created_at").fetchall()


class MediaAPI:
    def __init__(self, base_url: str, token_file: Path) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, read=300))

    def headers(self) -> dict[str, str]:
        token = self.token_file.read_text().strip()
        if not token:
            raise RuntimeError("media API secret is empty")
        return {"Authorization": f"Bearer {token}"}

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

    async def close(self) -> None:
        await self.client.aclose()


class MediaBot:
    def __init__(self, *, token: str, state: StateStore, api: MediaAPI, result_root: Path) -> None:
        self.state = state
        self.api = api
        self.result_root = result_root
        self.result_root.mkdir(parents=True, exist_ok=True)
        self.application: Application | None = None
        self.tasks: set[asyncio.Task[Any]] = set()

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
        await update.effective_message.reply_text("Private media bot ready. Send a prompt or /settings. Use /kind image|video and /model qwen-image-2.1|h3|ltx (H3 Eros Max beta5 checkpoint or qualified LTX 2.5).")

    @staticmethod
    def _settings_keyboard(user_id: int, settings: Settings) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Kind: {settings.media_kind}", callback_data=f"s:{user_id}:kind")],
            [InlineKeyboardButton(f"Preset: {settings.preset}", callback_data=f"s:{user_id}:preset")],
            [InlineKeyboardButton(f"Model: {MODEL_REGISTRY.get(settings.model, {}).get('label', settings.model)}", callback_data=f"s:{user_id}:model")],
            [InlineKeyboardButton(f"Steps: {settings.steps}", callback_data=f"s:{user_id}:steps")],
            [InlineKeyboardButton(f"Mode: {settings.mode}", callback_data=f"s:{user_id}:mode")],
            [InlineKeyboardButton(f"LoRA: {settings.lora or 'none'}", callback_data=f"s:{user_id}:lora")],
            [InlineKeyboardButton(f"Strength: {settings.lora_strength if settings.lora_strength is not None else 'default'}", callback_data=f"s:{user_id}:strength")],
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
        strength = settings.lora_strength if settings.media_kind == 'video' and settings.lora and settings.lora_strength is not None else settings.strength
        return (f"kind={settings.media_kind} model={model_label} mode={settings.mode}\n"
                f"preset={settings.preset} {size} steps={settings.steps} seconds={settings.seconds:g}\n"
                f"variant={settings.variant} lora={settings.lora or 'none'} strength={strength:g}")

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
            data.update(steps=20 if value == "image" else 4, lora=None if value == "image" else data["lora"], lora_strength=None if value == "image" else data["lora_strength"])
        elif key == "model":
            if value not in SELECTABLE_MODELS: raise ValueError
            data.update(model=value, media_kind="image" if value == "qwen-image-2.1" else "video", mode="t2i" if value == "qwen-image-2.1" else "t2v", auto_size=True, width=None, height=None)
            data.update(variant="turbo" if value == "h3" else "uncensored", steps=20 if value == "qwen-image-2.1" else (4 if value == "h3" else 8), lora=None if value != "h3" else data["lora"], lora_strength=None if value != "h3" else data["lora_strength"])
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
                valid = {"h3": {"turbo", "int8", "w4a8"}, "ltx": {"uncensored"}, "qwen-image-2.1": set()}.get(settings.model, set())
                if value not in valid: raise ValueError
                data["variant"] = value
            else:
                if settings.model != "h3" or value not in VIDEO_LORAS | {"none"}: raise ValueError
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
        usage = "Usage: /set KEY VALUE (model, mode, preset, longest, width, height, steps, duration, variant, lora, strength, auto); /kind image|video; /model qwen-image-2.1|h3|ltx"
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
        step_values = [4, 20] if settings.model == "h3" else ([8, 20] if settings.model == "ltx" else [12, 20])
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
        if key in {"width", "height"} and settings.auto_size:
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
            await update.effective_message.reply_text(f"Job {job_id[:12]} is already delivered; no automatic resend was performed.")
            return
        backend_id = row["backend_job_id"]
        if not backend_id:
            await update.effective_message.reply_text(f"Job {job_id[:12]} has no submitted backend job to recover.")
            return
        status = await self.api.status(backend_id)
        if status.get("state") != "succeeded":
            self.state.update_job(job_id, state=str(status.get("state", "unknown")), last_status=json.dumps(status, separators=(",", ":")))
            await update.effective_message.reply_text(f"Job {job_id[:12]} is {status.get('state', 'unknown')}; try /result later.")
            return
        await update.effective_message.reply_text(f"Recovering {job_id[:12]}. If Telegram accepted the earlier send, this explicit retry can create a duplicate.")
        await self._deliver_backend_result(job_id, backend_id, int(row["chat_id"]), explicit=True)

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        message = update.effective_message
        if not message:
            return
        settings = self.state.get_settings(update.effective_user.id)
        has_image = bool(message.photo or (message.document and (message.document.mime_type or "").startswith("image/")))
        if message.document and not has_image:
            await message.reply_text("Only image documents are supported as media input.")
            return
        prompt = (message.caption or message.text or "").strip()
        if has_image and not message.caption:
            await message.reply_text("Add a caption describing the edit or image-to-video request; a photo alone is not generated.")
            return
        if not prompt:
            return
        if settings.media_kind == "image" and settings.model != "qwen-image-2.1":
            await message.reply_text("Image mode requires qwen-image-2.1."); return
        if settings.media_kind == "video" and settings.model == "qwen-image-2.1":
            await message.reply_text("Video mode requires the qualified H3 Eros Max beta5 checkpoint."); return
        if not has_image and settings.mode in {"edit", "i2v"}:
            await message.reply_text(f"Mode {settings.mode} requires an image attachment.")
            return
        if has_image:
            if settings.media_kind == "image":
                mode = "edit"
            else:
                mode = "i2v"
        else:
            mode = settings.mode
        width, height = _resolve_dimensions(settings)
        request = {"model": settings.model, "mode": mode, "prompt": prompt, "width": width, "height": height, "steps": settings.steps}
        if settings.media_kind == "video":
            request.update(seconds=settings.seconds, variant=settings.variant)
            if settings.lora: request.update(lora=settings.lora, lora_strength=settings.lora_strength)
        if settings.media_kind == "image":
            request.update(cfg=4.0, strength=settings.strength)
        job_id = self.state.create_job(user_id=update.effective_user.id, chat_id=message.chat_id, message_id=message.message_id, prompt=prompt, request=request)
        ack = await message.reply_text(f"Queued {job_id[:12]} ({settings.model}/{mode}, {settings.preset} {width}x{height})")
        self.state.update_job(job_id, status_message_id=ack.message_id)
        task = asyncio.create_task(self._submit_and_watch(job_id, update.effective_user.id, message.chat_id, message, settings, has_image))
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def _probe_image_size(self, raw: bytes) -> tuple[int, int]:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-f", "image2pipe", "-i", "pipe:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(raw)
        if process.returncode != 0 or b"x" not in output:
            raise RuntimeError(f"image dimensions unavailable: {error.decode(errors='replace')[-300:]}")
        width, height = output.decode().strip().split("x", 1)
        return int(width), int(height)

    async def _normalize_image(self, raw: bytes, settings: Settings) -> bytes:
        """Apply aspect-cover crop and model-valid resize before upload."""
        if settings.width is None or settings.height is None:
            raise RuntimeError("resolved dimensions are required before image normalization")
        vf = f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=increase,crop={settings.width}:{settings.height}"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-vf", vf, "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(raw)
        if process.returncode != 0 or not output:
            raise RuntimeError(f"image normalization failed: {error.decode(errors='replace')[-300:]}")
        return output

    async def _submit_and_watch(self, job_id: str, user_id: int, chat_id: int, message, settings: Settings, has_image: bool) -> None:
        row = self.state.get_job(job_id)
        request = json.loads(row["request_json"])
        try:
            if has_image:
                attachment = message.photo[-1] if message.photo else message.document
                telegram_file = await attachment.get_file()
                raw = bytes(await telegram_file.download_as_bytearray())
                source_size = await self._probe_image_size(raw)
                width, height = _resolve_dimensions(settings, source_size)
                effective_settings = replace(settings, width=width, height=height, auto_size=False)
                request.update(width=width, height=height)
                raw = await self._normalize_image(raw, effective_settings)
                request["image_path"] = await self.api.upload(raw, "image/png")
                self.state.update_job(job_id, request_json=json.dumps(request, separators=(",", ":")))
            submitted = await self.api.submit(request)
            backend_id = submitted["job_id"]
            self.state.update_job(job_id, backend_job_id=backend_id, state="queued")
            await self._watch(job_id, backend_id, chat_id)
        except Exception as exc:
            LOG.exception("media job failed job=%s", job_id)
            current = self.state.get_job(job_id)
            if current is not None and current["state"] == "delivering":
                self.state.update_job(job_id, last_status=f"delivery ambiguous: {type(exc).__name__}")
                try:
                    await message.reply_text(f"Job {job_id[:12]} delivery is ambiguous; use /result {job_id[:12]} for explicit recovery.")
                except Exception:
                    LOG.debug("ambiguous-delivery notice failed", exc_info=True)
                return
            self.state.update_job(job_id, state="failed", last_status=str(exc)[:500])
            await message.reply_text(f"Job {job_id[:12]} failed: {type(exc).__name__}; use /result {job_id[:12]} if delivery needs recovery")

    async def _deliver_backend_result(self, job_id: str, backend_id: str, chat_id: int, *, explicit: bool = False) -> None:
        self.state.update_job(job_id, state="delivering")
        content, content_type = await self.api.result(backend_id)
        suffix = ".png" if content_type.startswith("image/") else ".mp4"
        output = self.result_root / f"{job_id}{suffix}"
        output.write_bytes(content)
        self.state.update_job(job_id, output_path=str(output))
        if content_type.startswith("image/"):
            await self.application.bot.send_photo(chat_id=chat_id, photo=content, caption=f"Job {job_id[:12]} complete")
        else:
            await self.application.bot.send_video(chat_id=chat_id, video=content, caption=f"Job {job_id[:12]} complete", supports_streaming=True)
        self.state.update_job(job_id, state="delivered", delivered_at=time.time())

    async def _watch(self, job_id: str, backend_id: str, chat_id: int) -> None:
        row = self.state.get_job(job_id)
        status_message_id = row["status_message_id"]
        last_update = 0.0
        while True:
            status = await self.api.status(backend_id)
            state = str(status.get("state", "unknown"))
            self.state.update_job(job_id, state=state, last_status=json.dumps(status, separators=(",", ":")))
            now = time.monotonic()
            if now - last_update >= 30 and self.application:
                try:
                    await self.application.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=f"Job {job_id[:12]}: {state} ({backend_id})")
                except Exception:
                    LOG.debug("status edit failed", exc_info=True)
                last_update = now
            if state in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(5)
        if state != "succeeded":
            await self.application.bot.edit_message_text(chat_id=chat_id, message_id=status_message_id, text=f"Job {job_id[:12]}: {state}")
            return
        await self._deliver_backend_result(job_id, backend_id, chat_id)

    async def reconcile(self, application: Application) -> None:
        self.application = application
        for row in self.state.nonterminal():
            if not row["backend_job_id"]:
                self.state.update_job(row["job_id"], state="failed", last_status="bot restarted before backend submission")
                continue
            task = asyncio.create_task(self._watch(row["job_id"], row["backend_job_id"], row["chat_id"]))
            self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def configure(self, application: Application) -> None:
        await self.reconcile(application)
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

    def build(self) -> Application:
        token = os.environ.get("TELEGRAM_MEDIA_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_MEDIA_BOT_TOKEN is required")
        app = Application.builder().token(token).build()
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
    app = bot.build()
    await app.initialize(); await bot.configure(app); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop(); await app.stop(); await app.shutdown(); await api.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.ExtBot").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
