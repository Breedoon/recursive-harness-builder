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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

LOG = logging.getLogger("obs_agent.telegram_media_bot")
ALLOWLIST = frozenset({227177188, 5129431382})


@dataclass(frozen=True)
class Settings:
    preset: str = "low"
    media_kind: str = "image"
    model: str = "qwen-image-2.1"
    mode: str = "t2i"
    variant: str = "turbo"
    lora: str | None = None
    lora_strength: float | None = None
    steps: int = 20
    seconds: float = 4.0
    width: int = 512
    height: int = 512

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
            return Settings(**{k: data[k] for k in asdict(Settings()) if k in data})
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

    def nonterminal(self):
        return self.db.execute("SELECT * FROM jobs WHERE state NOT IN ('delivered','failed','cancelled') ORDER BY created_at").fetchall()


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
        await update.effective_message.reply_text("Private media bot ready. Send a prompt or /settings. Use /kind image|video and /model qwen-image-2.1|h3.")

    async def settings_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return await self.deny(update)
        settings = self.state.get_settings(update.effective_user.id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Kind: {settings.media_kind}", callback_data=f"s:{update.effective_user.id}:kind")],
            [InlineKeyboardButton(f"Preset: {settings.preset}", callback_data=f"s:{update.effective_user.id}:preset")],
            [InlineKeyboardButton(f"Model: {settings.model}", callback_data=f"s:{update.effective_user.id}:model")],
            [InlineKeyboardButton(f"Steps: {settings.steps}", callback_data=f"s:{update.effective_user.id}:steps")],
        ])
        await update.effective_message.reply_text(self._settings_text(settings), reply_markup=keyboard)

    @staticmethod
    def _settings_text(settings: Settings) -> str:
        return (f"kind={settings.media_kind} model={settings.model} mode={settings.mode}\n"
                f"preset={settings.preset} {settings.width}x{settings.height} steps={settings.steps} seconds={settings.seconds:g}\n"
                f"variant={settings.variant} lora={settings.lora or 'none'} strength={settings.lora_strength or 'default'}")

    async def setting_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return await self.deny(update)
        args = context.args
        cmd = update.effective_message.text.split()[0].split("@", 1)[0].lower()
        settings = self.state.get_settings(update.effective_user.id)
        if not args:
            return await update.effective_message.reply_text("Usage: /kind image|video, /model qwen-image-2.1|h3, /preset low|high, /steps N, /duration SECONDS, /variant turbo|int8|w4a8")
        value = args[0].lower()
        data = asdict(settings)
        try:
            if cmd == "/kind":
                if value not in {"image", "video"}: raise ValueError
                data.update(media_kind=value, model="qwen-image-2.1" if value == "image" else "h3", mode="t2i" if value == "image" else "t2v")
                if value == "video": data.update(width=640, height=384)
            elif cmd == "/model":
                if value not in {"qwen-image-2.1", "h3", "wan", "ltx"}: raise ValueError
                data["model"] = value
                data["media_kind"] = "image" if value == "qwen-image-2.1" else "video"
                data["mode"] = "t2i" if value == "qwen-image-2.1" else "t2v"
                if value != "qwen-image-2.1": data.update(width=640, height=384)
            elif cmd == "/preset":
                if value not in {"low", "high"}: raise ValueError
                if settings.media_kind == "video":
                    data.update(preset=value, width=640, height=384)
                else:
                    data.update(preset=value, width=512 if value == "low" else 640, height=512 if value == "low" else 640)
            elif cmd == "/steps":
                data["steps"] = max(1, min(40, int(args[0])))
            elif cmd == "/duration":
                data["seconds"] = max(1.0, min(10.0, float(args[0])))
            elif cmd == "/variant":
                if value not in {"turbo", "int8", "w4a8"}: raise ValueError
                data["variant"] = value
            else: raise ValueError
        except (ValueError, TypeError):
            return await update.effective_message.reply_text("Invalid setting. Use /settings for current values and supported choices.")
        new_settings = Settings(**data)
        self.state.put_settings(update.effective_user.id, new_settings)
        await update.effective_message.reply_text(self._settings_text(new_settings))

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
        data = asdict(settings)
        choices = {
            "kind": ("media_kind", ["image", "video"]),
            "preset": ("preset", ["low", "high"]),
            "model": ("model", ["qwen-image-2.1", "h3"]),
            "steps": ("steps", [12, 20]),
        }
        key = parts[2]
        field, values = choices.get(key, (None, []))
        if field is None:
            await query.answer("Unknown setting", show_alert=True); return
        current = data[field]
        value = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
        data[field] = value
        if field == "media_kind":
            data.update(model="qwen-image-2.1" if value == "image" else "h3", mode="t2i" if value == "image" else "t2v")
            if value == "video": data.update(width=640, height=384)
        if field == "model":
            data.update(media_kind="image" if value == "qwen-image-2.1" else "video", mode="t2i" if value == "qwen-image-2.1" else "t2v")
            if value != "qwen-image-2.1": data.update(width=640, height=384)
        new_settings = Settings(**data)
        self.state.put_settings(user.id, new_settings)
        await query.answer("Saved")
        await query.edit_message_text(self._settings_text(new_settings), reply_markup=query.message.reply_markup)

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        message = update.effective_message
        if not message:
            return
        settings = self.state.get_settings(update.effective_user.id)
        has_image = bool(message.photo or (message.document and (message.document.mime_type or "").startswith("image/")))
        prompt = (message.caption or message.text or "").strip()
        if has_image and not message.caption:
            await message.reply_text("Add a caption describing the edit or image-to-video request; a photo alone is not generated.")
            return
        if not prompt:
            return
        if settings.media_kind == "image" and settings.model != "qwen-image-2.1":
            await message.reply_text("Image mode requires qwen-image-2.1."); return
        if settings.media_kind == "video" and settings.model == "qwen-image-2.1":
            await message.reply_text("Video mode requires a video model such as h3 or wan."); return
        if has_image:
            if settings.media_kind == "image":
                mode = "edit"
            else:
                mode = "i2v"
        else:
            mode = "t2i" if settings.media_kind == "image" else "t2v"
        request = {"model": settings.model, "mode": mode, "prompt": prompt, "width": settings.width, "height": settings.height, "steps": settings.steps}
        if settings.media_kind == "video":
            request.update(seconds=settings.seconds, variant=settings.variant)
            if settings.lora: request.update(lora=settings.lora, lora_strength=settings.lora_strength)
        if settings.media_kind == "image":
            request.update(cfg=4.0, strength=0.75)
        job_id = self.state.create_job(user_id=update.effective_user.id, chat_id=message.chat_id, message_id=message.message_id, prompt=prompt, request=request)
        ack = await message.reply_text(f"Queued {job_id[:12]} ({settings.model}/{mode}, {settings.preset} {settings.width}x{settings.height})")
        self.state.update_job(job_id, status_message_id=ack.message_id)
        task = asyncio.create_task(self._submit_and_watch(job_id, update.effective_user.id, message.chat_id, message, settings, has_image))
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def _submit_and_watch(self, job_id: str, user_id: int, chat_id: int, message, settings: Settings, has_image: bool) -> None:
        row = self.state.get_job(job_id)
        request = json.loads(row["request_json"])
        try:
            if has_image:
                attachment = message.photo[-1] if message.photo else message.document
                telegram_file = await attachment.get_file()
                raw = bytes(await telegram_file.download_as_bytearray())
                mime = "image/jpeg" if message.photo else (message.document.mime_type or "image/jpeg")
                request["image_path"] = await self.api.upload(raw, mime)
                self.state.update_job(job_id, request_json=json.dumps(request, separators=(",", ":")))
            submitted = await self.api.submit(request)
            backend_id = submitted["job_id"]
            self.state.update_job(job_id, backend_job_id=backend_id, state="queued")
            await self._watch(job_id, backend_id, chat_id)
        except Exception as exc:
            LOG.exception("media job failed job=%s", job_id)
            self.state.update_job(job_id, state="failed", last_status=str(exc)[:500])
            await message.reply_text(f"Job {job_id[:12]} failed: {type(exc).__name__}")

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
        content, content_type = await self.api.result(backend_id)
        suffix = ".png" if content_type.startswith("image/") else ".mp4"
        output = self.result_root / f"{job_id}{suffix}"
        output.write_bytes(content)
        self.state.update_job(job_id, state="delivering", output_path=str(output))
        if content_type.startswith("image/"):
            await self.application.bot.send_photo(chat_id=chat_id, photo=content, caption=f"Job {job_id[:12]} complete")
        else:
            await self.application.bot.send_video(chat_id=chat_id, video=content, caption=f"Job {job_id[:12]} complete", supports_streaming=True)
        self.state.update_job(job_id, state="delivered", delivered_at=time.time())

    async def reconcile(self, application: Application) -> None:
        self.application = application
        for row in self.state.nonterminal():
            if not row["backend_job_id"]:
                self.state.update_job(row["job_id"], state="failed", last_status="bot restarted before backend submission")
                continue
            task = asyncio.create_task(self._watch(row["job_id"], row["backend_job_id"], row["chat_id"]))
            self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    def build(self) -> Application:
        token = os.environ.get("TELEGRAM_MEDIA_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_MEDIA_BOT_TOKEN is required")
        app = Application.builder().token(token).post_init(self.reconcile).build()
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("settings", self.settings_cmd))
        for command in ("kind", "model", "preset", "steps", "duration", "variant"):
            app.add_handler(CommandHandler(command, self.setting_command))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r"^s:"))
        app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.IMAGE, self.message))
        self.application = app
        return app


async def run() -> None:
    db = Path(os.environ.get("TELEGRAM_MEDIA_STATE_DB", "/workspace/runtime/state/telegram-media-bot.sqlite3"))
    results = Path(os.environ.get("TELEGRAM_MEDIA_RESULTS", "/workspace/runtime/state/telegram-media-bot/results"))
    api = MediaAPI(os.environ.get("TELEGRAM_MEDIA_API_URL", "http://host.docker.internal:8190"), Path(os.environ.get("TELEGRAM_MEDIA_API_TOKEN_FILE", "/run/secrets/media_api_key")))
    bot = MediaBot(token=os.environ.get("TELEGRAM_MEDIA_BOT_TOKEN", ""), state=StateStore(db), api=api, result_root=results)
    app = bot.build()
    await app.initialize(); await bot.reconcile(app); await app.start(); await app.updater.start_polling(drop_pending_updates=True)
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
