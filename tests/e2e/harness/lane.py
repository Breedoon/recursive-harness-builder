"""The ``lane`` object tests use (obs-22 contract C3, Python test API).

One lane = one full OBS instance in its own container (harness-spec §1). A test
talks to it only through this object:

- Telegram as the test user, through the runner's single userbot client (RPC):
  :meth:`Lane.new_topic`, :meth:`Lane.send`, :meth:`Lane.command`, ``lane.telegram``;
- OBS's own events: :meth:`Lane.wait`, :meth:`Lane.assert_none`, :meth:`Lane.turn`;
- the lane's inside, through its control socket (``lanectl``): :meth:`Lane.procs`,
  :meth:`Lane.exec`, :meth:`Lane.control`, :meth:`Lane.restart_obs`;
- files the lane writes (read-only here): :meth:`Lane.state_db`, :meth:`Lane.reqlog`,
  and :meth:`Lane.set_rollout`, the lane's own ``rollout.json``.

INVARIANT(process-identity): tests never signal a pid themselves. Every kill,
restart or probe runs inside the lane container through ``lanectl``, whose PID
namespace holds only lane processes. Breaking it reintroduces the 2026-10-16
class (a test's pid reached production's ``kill_process_tree``).
Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Runner#Python test API
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .events import STRESS_TIMEOUT, TURN_TIMEOUT, EventLog, JsonlTail, Timing

PROJECT_SLUG = "-workspace-runtime-git-obs-vault-active"


@dataclass(frozen=True)
class Topic:
    lane: str
    chat_id: int
    topic_id: int
    title: str


@dataclass(frozen=True)
class SentMessage:
    message_id: int
    topic_id: int
    text: str
    date: str | None = None


@dataclass
class TurnResult:
    sent: SentMessage
    start: dict
    end: dict
    messages: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(m.get("text") or "" for m in self.messages if m.get("is_bot"))


class _Uds:
    """JSON over HTTP on a Unix socket (lanectl and the runner's Telegram RPC)."""

    def __init__(self, path: Path, name: str, *, default_timeout: float = 60) -> None:
        self.path = Path(path)
        self.name = name
        self.default_timeout = default_timeout
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=str(self.path)), base_url=f"http://{self.name}", timeout=None
            )
        return self._client

    async def call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> Any:
        resp = await self.client().request(method, path, json=body,
                                           timeout=self.default_timeout if timeout is None else timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.name} {method} {path} -> {resp.status_code}: {data}")
        return data

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class TelegramView:
    """Telegram content for this lane, read through the runner's single client."""

    def __init__(self, lane: "Lane") -> None:
        self._lane = lane

    async def messages(self, topic: Topic | int, since: Any = None, *, from_bot: bool | None = None,
                       limit: int = 100) -> list[dict]:
        """Messages in ``topic`` after ``since`` (a SentMessage, a message id or a telegram_send event)."""
        after = None
        if isinstance(since, SentMessage):
            after = since.message_id
        elif isinstance(since, dict):
            after = since.get("message_id")
        elif isinstance(since, int):
            after = since
        body = {"lane": self._lane.name, "topic_id": _topic_id(topic), "after_message_id": after, "limit": limit}
        if from_bot is not None:
            body["from_bot"] = from_bot
        return await self._lane._rpc.call("POST", "/messages", body)


def _topic_id(topic: Topic | int) -> int:
    return topic.topic_id if isinstance(topic, Topic) else int(topic)


class Lane:
    """One OBS lane, as seen from a pytest worker in the runner container."""

    def __init__(self, lane_dir: Path, run_dir: Path, *, timing: Timing | None = None) -> None:
        self.lane_dir = Path(lane_dir)
        self.run_dir = Path(run_dir)
        self.info: dict = json.loads((self.lane_dir / "lane.json").read_text(encoding="utf-8"))
        self.name: str = self.info["lane"]
        self.manifest: dict = json.loads((self.run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.timing = timing or Timing(self.run_dir / "runner" / f"timing-{self.name}.jsonl", self.name)
        self.events = EventLog(self.lane_dir / "events.jsonl", timing=self.timing)
        self.telegram = TelegramView(self)
        self._ctl = _Uds(self.lane_dir / "lanectl.sock", f"lanectl-{self.name}")
        # The broker bounds Telegram work at 120s; allow its response frame to arrive.
        self._rpc = _Uds(self.run_dir / "runner" / "telegram.sock", "telegram-rpc", default_timeout=125)
        self._jsonl: dict[str, JsonlTail] = {}
        self._profile = None

    # -- lifecycle -------------------------------------------------------
    @property
    def profile(self):
        if self._profile is None:
            from .profiles import load_profile

            self._profile = load_profile(self.info["profile"])
        return self._profile

    async def start(self) -> None:
        self.events.start()

    async def close(self) -> None:
        self.events.close()
        for tail in self._jsonl.values():
            tail.close()
        await self._ctl.close()
        await self._rpc.close()

    def begin_test(self, nodeid: str) -> None:
        self.timing.test = nodeid
        self.events.cursor = self.events.mark()

    # -- Telegram (as the test user) -------------------------------------
    async def new_topic(self, title: str | None = None) -> Topic:
        title = title or f"e2e {self.name} {uuid.uuid4().hex[:6]}"
        data = await self._rpc.call("POST", "/topic/new", {"lane": self.name, "title": title})
        return Topic(lane=self.name, chat_id=int(data["chat_id"]), topic_id=int(data["topic_id"]), title=title)

    async def close_topic(self, topic: Topic | int) -> dict:
        return await self._rpc.call("POST", "/topic/close", {"lane": self.name, "topic_id": _topic_id(topic)})

    async def send(self, topic: Topic | int | str, text: str, reply_to: int | None = None) -> SentMessage:
        topic_id = self._resolve_topic(topic)
        body = {"lane": self.name, "topic_id": topic_id, "text": text}
        if reply_to is not None:
            body["reply_to"] = int(reply_to)
        position, sent_at = self.events.mark(), time.time()
        data = await self._rpc.call("POST", "/send", body)
        self.events.record_input(position=position, message_id=int(data["message_id"]),
                                 chat_id=int(data["chat_id"]), topic_id=topic_id, ts=sent_at)
        return SentMessage(message_id=int(data["message_id"]), topic_id=topic_id, text=text, date=data.get("date"))

    async def command(self, topic: Topic | int | str, text: str, reply_to: int | None = None) -> SentMessage:
        """Send a slash command (``/stop``, ``/fork`` …) as the test user."""
        if not text.startswith("/"):
            raise ValueError(f"commands start with '/': {text!r}")
        return await self.send(topic, text, reply_to=reply_to)

    def _resolve_topic(self, topic: Topic | int | str) -> int:
        if isinstance(topic, (Topic, int)):
            return _topic_id(topic)
        for event in reversed(self.events.find(None, agent=str(topic))):
            if event.get("topic_id") is not None:
                return int(event["topic_id"])
        raise LookupError(f"no topic known for agent {topic!r} in lane {self.name}")

    # -- events ------------------------------------------------------------
    async def wait(self, event_type: str, agent: Any = None, match: Any = None, timeout: float | None = None,
                   after: Any = None) -> dict:
        """Wait for an event-tap event (or ``jsonl_append``); timeouts only ever fail."""
        if event_type == "jsonl_append":
            session_id = (match or {}).get("session_id") if isinstance(match, dict) else None
            if not session_id:
                raise ValueError("jsonl_append waits need match={'session_id': ...}")
            tail = self._jsonl_tail(session_id)
            rest = {k: v for k, v in match.items() if k != "session_id"}
            return await tail.log.wait("jsonl_append", match=rest or None, timeout=timeout, after=after)
        return await self.events.wait(event_type, agent=agent, match=match, timeout=timeout, after=after)

    def assert_none(self, event_type: str, agent: Any = None, match: Any = None, after: Any = None,
                    before: Any = None) -> None:
        self.events.assert_none(event_type, agent=agent, match=match, after=after, before=before)

    def find(self, event_type: str | None = None, agent: Any = None, match: Any = None, after: Any = None) -> list[dict]:
        return self.events.find(event_type, agent=agent, match=match, after=after)

    def _jsonl_tail(self, session_id: str) -> JsonlTail:
        tail = self._jsonl.get(session_id)
        if tail is None:
            path = self.lane_dir / "home" / ".claude" / "projects" / PROJECT_SLUG / f"{session_id}.jsonl"
            tail = JsonlTail(path, session_id, timing=self.timing)
            tail.start()
            self._jsonl[session_id] = tail
        return tail

    async def turn(self, topic: Topic | int | str, text: str, *, timeout: float | None = None,
                   reply_to: int | None = None) -> TurnResult:
        """Send one message and wait for OBS to finish the turn it triggered."""
        timeout = TURN_TIMEOUT if timeout is None else timeout
        sent = await self.send(topic, text, reply_to=reply_to)
        start = await self.wait("turn_start", match={"trigger_message_id": sent.message_id}, timeout=timeout)
        end = await self.wait("turn_end", match={"trigger_message_id": sent.message_id}, timeout=timeout,
                              after=start)
        messages = await self.telegram.messages(sent.topic_id, since=sent)
        return TurnResult(sent=sent, start=start, end=end, messages=messages)

    # -- lane internals (through lanectl, inside the lane namespace) ----------
    async def ready(self) -> dict:
        return await self._ctl.call("GET", "/ready", timeout=30)

    async def procs(self) -> list[dict]:
        return await self._ctl.call("GET", "/procs", timeout=30)

    async def envnames(self) -> dict:
        return await self._ctl.call("GET", "/envnames", timeout=30)

    async def exec(self, *argv: str, timeout: float = 120, env: dict | None = None, cwd: str | None = None) -> dict:
        body: dict = {"argv": list(argv), "timeout": timeout}
        if env:
            body["env"] = env
        if cwd:
            body["cwd"] = cwd
        return await self._ctl.call("POST", "/exec", body, timeout=timeout + 15)

    async def control(self, *args: str, timeout: float = 120) -> dict:
        """``obs-agentctl`` against this lane's own control socket (code under test)."""
        return await self.exec(str(self.lane_dir / "bin" / "obs-agentctl"), *args, timeout=timeout)

    async def supervisor(self, action: str = "status", program: str = "obs-telegram-prod") -> dict:
        return await self._ctl.call("POST", "/supervisor", {"action": action, "program": program}, timeout=90)

    async def restart_obs(self, mode: str = "maintenance", *, timeout: float = 180) -> dict:
        """Restart this lane's OBS only (``maintenance`` | ``plain`` | ``kill``) and wait until ready."""
        before = await self.ready()
        old_pid = before.get("obs_pid")
        if mode == "maintenance":
            result = await self.exec(
                "/workspace/obs/.venv/bin/python", "-m", "obs_agent.maintenance_restart",
                "--skip-cache-gate", "obs-e2e lane restart", timeout=60,
                env={"PYTHONPATH": f"{self.info['code_dir']}/src"}, cwd=self.info["code_dir"],
            )
        elif mode == "plain":
            result = await self.supervisor("restart")
        elif mode == "kill":
            if not old_pid:
                raise RuntimeError(f"lane {self.name}: no telegram_main pid to kill")
            result = await self._ctl.call("POST", "/signal", {"pid": int(old_pid), "sig": "KILL"})
        else:
            raise ValueError(f"unknown restart mode {mode!r}")
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = await self.ready()
            if last.get("obs") == "RUNNING" and last.get("proxy_healthy") and last.get("obs_pid") not in (None, old_pid):
                return {"restart": result, "ready": last}
            await self._ctl_wait_change()
        raise TimeoutError(f"lane {self.name} not ready {timeout:.0f}s after {mode} restart: {last}")

    async def _ctl_wait_change(self) -> None:
        # Process readiness is not an event-tap event; this harness-internal poll
        # is the one place lanectl is polled (never from a test body).
        import asyncio

        await asyncio.sleep(0.5)  # obs-e2e-lint: allow (process readiness poll inside the harness)

    # -- files the lane writes -------------------------------------------------
    def state_db(self) -> sqlite3.Connection:
        path = self.lane_dir / "state" / "telegram-state.sqlite3"
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def reqlog(self, agent: Any = None, since: Any = None) -> list[dict]:
        """The lane proxy's request-index rows (and obs-cli request-log rows when present)."""
        since_ts = since.get("ts") if isinstance(since, dict) else since
        sessions = None
        if agent is not None:
            sessions = {e.get("session_id") for e in self.events.find(None, agent=agent) if e.get("session_id")}
        rows: list[dict] = []
        roots = [self.lane_dir / "logs" / "cache-proxy" / "requests", self.lane_dir / "logs" / "obs-cli" / "requests"]
        for root in roots:
            for index in sorted(root.glob("**/index*.jsonl")):
                source = "obs-cli" if "obs-cli" in index.parts else "proxy"
                for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if sessions is not None and row.get("session_id") not in sessions:
                        continue
                    ts = row.get("ts")
                    if since_ts is not None and isinstance(ts, (int, float)) and ts <= since_ts:
                        continue
                    row["_source"] = source
                    rows.append(row)
        by_wire = {r.get("wire_sha256"): r for r in rows if r["_source"] == "proxy" and r.get("wire_sha256")}
        for row in rows:
            if row["_source"] == "obs-cli" and row.get("expected_wire_sha256") in by_wire:
                row["_proxy_row"] = by_wire[row["expected_wire_sha256"]]
        return rows

    def set_rollout(self, pct: Any = None, force: str | None = None) -> Path:
        """Write this lane's ``rollout.json`` (C9 format); never production's."""
        if isinstance(pct, (int, float)):
            pct = {"claude": pct, "gpt": pct, "local": pct}
        payload = {"pct": pct or {"claude": 0, "gpt": 0, "local": 0}, "force": force}
        path = self.lane_dir / "state" / "obs-cli" / "rollout.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".rollout.json.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def host_sentinel(self) -> int | None:
        """PID (in obs-test's namespace) of the run's host sentinel, for isolation probe (ii)."""
        pid = self.manifest.get("host_sentinel_pid")
        return int(pid) if pid else None

    @staticmethod
    def stress_timeout() -> float:
        return STRESS_TIMEOUT
