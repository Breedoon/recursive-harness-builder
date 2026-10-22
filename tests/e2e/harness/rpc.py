"""Single-userbot Telegram RPC, using JSON HTTP/1.1 over a Unix socket."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path


class RpcError(RuntimeError):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        super().__init__(f"RPC status {status}: {payload.get('error', 'request failed') if isinstance(payload, dict) else 'request failed'}")


def _encode_request(path: str, body, method: str | None) -> bytes:
    method = method or ("GET" if path == "/health" else "POST")
    data = json.dumps(body if body is not None else {}).encode("utf-8")
    return (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode("ascii") + data


def _headers(raw: bytes) -> tuple[str, dict[str, str]]:
    lines = raw.decode("iso-8859-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or name.lower() in headers:
            raise ValueError("invalid or duplicate HTTP header")
        headers[name.lower()] = value.strip()
    if "transfer-encoding" in headers:
        raise ValueError("only Content-Length framing is supported")
    length = int(headers.get("content-length", "0"))
    if length < 0:
        raise ValueError("negative Content-Length")
    return lines[0], headers


async def _read_frame(reader: asyncio.StreamReader) -> tuple[str, bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    first, headers = _headers(raw)
    data = await reader.readexactly(int(headers.get("content-length", "0")))
    return first, data


def _decode_response(first: str, data: bytes):
    status = int(first.split(" ", 2)[1])
    payload = json.loads(data)
    if status >= 400:
        raise RpcError(status, payload)
    return payload


class RpcClient:
    def __init__(self, sock_path: Path | str, *, timeout: float = 120):
        self.sock_path = str(sock_path)
        self.timeout = timeout

    def call(self, path: str, body: dict | None = None, *, method: str | None = None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.sock_path)
            sock.sendall(_encode_request(path, body, method))
            with sock.makefile("rb") as stream:
                lines = []
                while True:
                    line = stream.readline()
                    if not line:
                        raise EOFError("incomplete HTTP response headers")
                    lines.append(line)
                    if line == b"\r\n":
                        break
                first, headers = _headers(b"".join(lines))
                length = int(headers.get("content-length", "0"))
                data = stream.read(length)
                if len(data) != length:
                    raise EOFError("incomplete HTTP response body")
                return _decode_response(first, data)

    async def acall(self, path: str, body: dict | None = None, *, method: str | None = None):
        async with asyncio.timeout(self.timeout):
            reader, writer = await asyncio.open_unix_connection(self.sock_path)
            try:
                writer.write(_encode_request(path, body, method))
                await writer.drain()
                first, data = await _read_frame(reader)
                return _decode_response(first, data)
            finally:
                writer.close()
                await writer.wait_closed()


class TelegramRPCServer:
    def __init__(self, client, forums: dict, run_dir: Path, *, fresh_forum=None):
        self.client = client
        self.forums = {name: dict(row) if isinstance(row, dict) else {"chat_id": row}
                       for name, row in forums.items()}
        self.run_dir = Path(run_dir)
        self.sock_path = self.run_dir / "runner" / "telegram.sock"
        self.fresh_forum = fresh_forum
        self.server: asyncio.AbstractServer | None = None
        self._topic_chats: dict[tuple[str, int], int] = {}
        self._connections: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sock_path.exists():
            self.sock_path.unlink()
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.sock_path))
        os.chmod(self.sock_path, 0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*list(self._connections), return_exceptions=True)
        self.sock_path.unlink(missing_ok=True)

    def _log(self, path: str, lane, status: int, duration: float) -> None:
        row = {"ts": time.time(), "path": path, "lane": lane, "status": status, "duration_s": duration}
        with (self.run_dir / "runner" / "rpc.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        started, path, lane, status = time.monotonic(), "?", None, 500
        try:
            async with asyncio.timeout(120):
                first, data = await _read_frame(reader)
                method, path, version = first.split(" ", 2)
                if version != "HTTP/1.1":
                    raise RpcError(400, {"error": "HTTP/1.1 required"})
                body = json.loads(data) if data else {}
                if not isinstance(body, dict):
                    raise RpcError(400, {"error": "JSON object required"})
                lane = body.get("lane")
                payload = await self.dispatch(method, path, body)
                status = 200
        except RpcError as exc:
            status, payload = exc.status, exc.payload
        except (ValueError, KeyError, TypeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            status, payload = 400, {"error": "invalid request"}
        except Exception as exc:
            # Telethon/httpx exception strings can echo request text or token URLs.
            status, payload = 500, {"error": exc.__class__.__name__}
        except asyncio.CancelledError:
            status, payload = 503, {"error": "RPC server is closing"}
        try:
            data = json.dumps(payload, default=_json_default).encode("utf-8")
            reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "Internal Server Error")
            writer.write((f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass
            finally:
                self._log(path, lane, status, time.monotonic() - started)
                self._connections.discard(task)

    async def dispatch(self, method: str, path: str, body: dict):
        if path == "/health" and method == "GET":
            return {"ok": True, "lanes": sorted(self.forums)}
        if method != "POST":
            raise RpcError(405, {"error": "POST required"})
        if path == "/botapi" or path.startswith("/prodsend/"):
            from .profiles import load_profile
            profile = load_profile("prod-send")
            token = os.environ.get("OBS_E2E_PROD_SEND_TOKEN")
            if not token:
                raise RpcError(403, {"error": "prod-send was not enabled for this run"})
            if path == "/botapi":
                return await self.bot_api_call(body["method"], body.get("params", {}), token=token,
                                               profile=profile.name, credential_class=profile.credential_class,
                                               allowed_methods=profile.allowed_methods)
            if profile.target_forum_chat_id is None:
                raise RpcError(500, {"error": "prod-send target forum is not configured"})
            chat_id = int(profile.target_forum_chat_id)
            if path == "/prodsend/topic/new":
                return await self._new_topic(chat_id, body["title"])
            if path == "/prodsend/topic/close":
                return await self._close_topic(chat_id, int(body["topic_id"]))
            raise RpcError(404, {"error": "unknown prod-send RPC path"})
        lane = body.get("lane")
        if lane not in self.forums:
            raise RpcError(400, {"error": "unknown lane"})
        forum = self.forums[lane]
        topic_id = int(body.get("topic_id", 0))
        chat_id = self._topic_chats.get((lane, topic_id), int(forum["chat_id"]))
        if path == "/topic/new":
            result = await self._new_topic(chat_id, body["title"])
            self._topic_chats[(lane, result["topic_id"])] = chat_id
            return result
        if path == "/topic/close":
            return await self._close_topic(chat_id, topic_id)
        if path == "/send":
            message = await self.client.send_message(chat_id, body["text"], reply_to=int(body.get("reply_to") or topic_id))
            return {"message_id": message.id, "chat_id": chat_id, "date": _json_default(message.date)}
        if path == "/messages":
            limit = int(body.get("limit", 100))
            if limit <= 0:
                raise RpcError(400, {"error": "limit must be positive"})
            kwargs = {"reply_to": topic_id, "min_id": int(body.get("after_message_id") or 0), "limit": None}
            result = []
            async for message in self.client.iter_messages(chat_id, **kwargs):
                reply = getattr(message, "reply_to", None)
                top = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
                if top != topic_id and message.id != topic_id:
                    continue
                sender = getattr(message, "sender", None)
                is_bot = bool(getattr(sender, "bot", False) or str(message.sender_id) == str(forum.get("bot_id")))
                if "from_bot" in body and is_bot != bool(body["from_bot"]):
                    continue
                result.append({"message_id": message.id, "sender_id": message.sender_id, "is_bot": is_bot,
                               "text": message.text or "", "date": _json_default(message.date),
                               "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None), "topic_id": topic_id})
                if len(result) >= limit:
                    break
            return sorted(result, key=lambda row: row["message_id"])
        if path == "/forum/fresh":
            if self.fresh_forum is None:
                raise RpcError(500, {"error": "fresh forum provisioner unavailable"})
            result = self.fresh_forum(lane)
            result = await result if inspect.isawaitable(result) else result
            result = result if isinstance(result, dict) else {"chat_id": int(result)}
            self.forums[lane]["chat_id"] = int(result["chat_id"])
            return result
        raise RpcError(404, {"error": "unknown RPC path"})

    async def _new_topic(self, chat_id: int, title: str) -> dict:
        from telethon import functions
        created = await self.client(functions.messages.CreateForumTopicRequest(peer=chat_id, title=title))
        return {"chat_id": chat_id, "topic_id": _created_topic_id(created)}

    async def _close_topic(self, chat_id: int, topic_id: int) -> dict:
        from telethon import functions
        await self.client(functions.messages.EditForumTopicRequest(peer=chat_id, topic_id=topic_id, closed=True))
        readback = await self.client(functions.messages.GetForumTopicsByIDRequest(peer=chat_id, topics=[topic_id]))
        topic = next((t for t in readback.topics if t.id == topic_id), None)
        if topic is None:
            raise RpcError(500, {"error": "closed topic missing from readback"})
        return {"chat_id": chat_id, "topic_id": topic_id, "closed": bool(topic.closed)}

    def _botapi_log(self, method: str, token: str, profile: str, *, refused: bool = False) -> None:
        from .secrets import bot_id_of
        row = {"ts": time.time(), "method": method, "bot_id": bot_id_of(token), "profile": profile}
        if refused:
            row["refused"] = True
        with (self.run_dir / "runner" / "botapi-calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    async def bot_api_call(self, method: str, data: dict, *, token: str,
                           profile: str, credential_class: str | None = None,
                           allowed_methods: tuple[str, ...] | None = None, http_client=None) -> dict:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", method):
            raise RpcError(400, {"error": "invalid Bot API method"})
        is_prod = profile == "prod-send" or credential_class == "prod-send"
        # INVARIANT(credential-selection): prod-send never calls getUpdates, setWebhook, deleteWebhook, close or logOut, and calls only its allowed_methods. Breaking it consumes or disrupts production's update stream.
        # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Profiles#prod-send
        if is_prod and method.lower() in {"getupdates", "setwebhook", "deletewebhook", "close", "logout"}:
            self._botapi_log(method, token, profile, refused=True)
            raise RpcError(403, {"error": "prod-send forbids this Bot API method"})
        if is_prod:
            if allowed_methods is None:
                from .profiles import load_profile
                allowed_methods = load_profile(profile).allowed_methods
            if method not in allowed_methods:
                self._botapi_log(method, token, profile, refused=True)
                raise RpcError(403, {"error": "Bot API method is not allowed by prod-send profile"})
            self._botapi_log(method, token, profile)
        if http_client is not None:
            response = await http_client.post(f"https://api.telegram.org/bot{token}/{method}", json=data)
        else:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=data)
        if response.status_code >= 400:
            raise RpcError(502, {"error": "Bot API HTTP error", "status": response.status_code})
        payload = response.json()
        if not payload.get("ok"):
            raise RpcError(502, {"error": "Bot API rejected request", "error_code": payload.get("error_code")})
        return payload


def _json_default(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _created_topic_id(created) -> int:
    from telethon.tl.types import MessageActionTopicCreate
    for update in created.updates:
        message = getattr(update, "message", None)
        if message is not None and isinstance(getattr(message, "action", None), MessageActionTopicCreate):
            return int(message.id)
    for update in created.updates:
        if type(update).__name__ == "UpdateMessageID":
            return int(update.id)
    raise RpcError(500, {"error": "topic creation returned no topic id"})
