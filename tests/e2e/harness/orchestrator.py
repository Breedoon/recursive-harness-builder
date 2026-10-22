"""Runner-container orchestration: one userbot, concurrent isolated pytest workers."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .results import compute_timing, merge_junit, write_summary
from .rpc import RpcClient, TelegramRPCServer, _created_topic_id
from .userbot import UserbotLock, UserbotLockTimeout, make_client

HARNESS_ROOT = Path(__file__).resolve().parents[3]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class HarnessError(RuntimeError):
    pass


class ForumRegistry:
    def __init__(self, client, lanes: list[dict], run_dir: Path, path: Path):
        self.client = client
        self.lanes = {row["lane"]: row for row in lanes}
        self.run_dir, self.path = run_dir, path
        self.records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.forums: dict[str, dict] = {}
        self._creation_lock = asyncio.Lock()

    async def _bot(self, lane: str):
        row = self.lanes[lane]
        return await self.client.get_entity(row.get("bot_username") or int(row["bot_id"]))

    async def _probe(self, chat_id: int, bot) -> bool:
        from telethon import errors, functions
        try:
            channel = await self.client.get_entity(chat_id)
            if not getattr(channel, "forum", False) or not getattr(channel, "megagroup", False):
                return False
            part = await self.client(functions.channels.GetParticipantRequest(channel=channel, participant=bot))
            rights = getattr(part.participant, "admin_rights", None)
            if not rights or not getattr(rights, "manage_topics", False):
                return False
            permissions = await self.client.get_permissions(channel, "me")
            own_rights = getattr(permissions.participant, "admin_rights", None)
            return bool(permissions.is_creator or permissions.is_admin and getattr(own_rights, "manage_topics", False))
        except (errors.ChannelPrivateError, errors.UserNotParticipantError, ValueError):
            return False

    async def _create(self, lane: str) -> int:
        from telethon import functions, types, utils
        bot = await self._bot(lane)
        created = await self.client(functions.channels.CreateChannelRequest(
            title=f"OBS E2E {lane} {self.run_dir.name}-{uuid.uuid4().hex[:6]}",
            about="Isolated OBS E2E harness test forum", megagroup=True, forum=True))
        channel = next((chat for chat in created.chats if isinstance(chat, types.Channel)), None)
        if channel is None:
            raise HarnessError("forum creation returned no channel")
        await self.client(functions.channels.InviteToChannelRequest(channel=channel, users=[bot]))
        await self.client(functions.channels.EditAdminRequest(
            channel=channel, user_id=bot, rank="obs-e2e-bot",
            admin_rights=types.ChatAdminRights(change_info=True, delete_messages=True, ban_users=True,
                                              invite_users=True, pin_messages=True, manage_topics=True, other=True)))
        chat_id = int(utils.get_peer_id(channel))
        if not await self._probe(chat_id, bot):
            raise HarnessError("new lane forum failed membership/admin probe")
        return chat_id

    async def ensure(self) -> dict:
        # StringSession does not persist entity access hashes: populate this client's cache.
        await self.client.get_dialogs(limit=None)
        for lane, config in self.lanes.items():
            record = self.records.get(lane)
            if not (record and str(record.get("bot_id")) == str(config["bot_id"]) and
                    await self._probe(int(record["chat_id"]), await self._bot(lane))):
                record = {"chat_id": await self._create(lane), "bot_id": config["bot_id"]}
                self.records[lane] = record
                _atomic_json(self.path, self.records)
            self.forums[lane] = dict(record)
        return self.forums

    async def fresh(self, lane: str) -> dict:
        from telethon import errors, functions
        async with self._creation_lock:
            try:
                return {"chat_id": await self._create(lane)}
            except errors.FloodWaitError as exc:
                chat_id = int(self.forums[lane]["chat_id"])
                namespace = f"fresh-fallback-{self.run_dir.name}-{uuid.uuid4().hex[:8]}"
                created = await self.client(functions.messages.CreateForumTopicRequest(peer=chat_id, title=namespace))
                result = {"chat_id": chat_id, "topic_id": _created_topic_id(created),
                          "fallback": "fresh-topic-namespace", "namespace": namespace, "retry_after_s": exc.seconds}
                with (self.run_dir / "runner" / "forum-fallbacks.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"lane": lane, "ts": time.time(), **result}) + "\n")
                print(f"[{lane}] fresh forum rate-limited; using fresh topic namespace {result['topic_id']}", flush=True)
                return result


def shard_tests(tests: list[dict], lanes: list[dict], suites: str = "all") -> dict[str, list[str]]:
    names = {part.strip() for part in suites.split(",") if part.strip()}
    shards = {row["lane"]: [] for row in lanes}
    for test in tests:
        raw_markers = test.get("markers")
        markers = raw_markers if isinstance(raw_markers, dict) else test
        tags = markers.get("suite") or []
        tags = [tags] if isinstance(tags, str) else tags
        if isinstance(raw_markers, list):
            tags = [*tags, *raw_markers]
        if "all" not in names and not names.intersection(tags):
            continue
        needs = markers.get("needs") or {}
        credential = needs.get("credential")
        credentials = {credential} if isinstance(credential, str) else set(credential or [])
        eligible = [row for row in lanes if not credentials or row["credential_class"] in credentials]
        if not eligible:
            raise HarnessError(f"no eligible credential lane for collected test {test['nodeid']}")
        if needs.get("all_lanes"):
            selected = eligible
        else:
            selected = [min(eligible, key=lambda row: len(shards[row["lane"]]))]
        for row in selected:
            shards[row["lane"]].append(test["nodeid"])
    if not any(shards.values()):
        raise HarnessError("no tests selected by suite/credential filters")
    return shards


def _worker_env(run_dir: Path, **extra: str) -> dict[str, str]:
    # INVARIANT(single-userbot): worker processes cannot create their own session client.
    # Doc: vault:Projects/Personal Projects/OBS Platform/E2E Harness/Components/Runner#Single userbot client
    env = {name: value for name, value in os.environ.items()
           if name != "OBS_E2E_PROD_SEND_TOKEN" and
           not name.startswith(("OBS_TEST_TELEGRAM_", "OBS_TELEGRAM_USERBOT_", "OBS_PROD_"))}
    env.update(PYTHONPATH=str(HARNESS_ROOT), OBS_E2E_RUN_DIR=str(run_dir),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    env.update(extra)
    return env


async def _stream(process: subprocess.Popen, prefix: str, output: Path) -> int:
    assert process.stdout is not None
    with output.open("a", encoding="utf-8") as stream:
        while True:
            line = await asyncio.to_thread(process.stdout.readline)
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            stream.write(text)
            stream.flush()
            print(f"[{prefix}] {text}", end="", flush=True)
    return await asyncio.to_thread(process.wait)


async def _stop_children(children: list[subprocess.Popen]) -> None:
    # Only Popen objects created by this orchestrator are signalled, never process searches.
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            await asyncio.wait_for(asyncio.to_thread(child.wait), timeout=30)
        except asyncio.TimeoutError:
            child.kill()
            await asyncio.to_thread(child.wait)
        if child.stdout is not None:
            child.stdout.close()


async def _ready(lane: dict, run_dir: Path, timeout: float) -> None:
    path = run_dir / "lanes" / lane["lane"] / "lanectl.sock"
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HarnessError(f"lane {lane['lane']} not ready within {timeout}s (last state: {last})")
        try:
            state = await RpcClient(path, timeout=min(5, remaining)).acall("/ready", method="GET")
            last = {key: state.get(key) for key in ("supervisord", "obs", "proxy_healthy")}
            if state.get("supervisord") and state.get("obs") == "RUNNING" and state.get("proxy_healthy"):
                return
        except (OSError, TimeoutError, EOFError):
            last = "control socket unavailable"
        # The control socket represents a kernel/external resource, not a timed test assertion.
        await asyncio.sleep(min(0.25, max(0.0, deadline - asyncio.get_running_loop().time())))


async def _run(args, manifest: dict, client) -> int:
    run_dir = args.run_dir
    runner = run_dir / "runner"
    runner.mkdir(parents=True, exist_ok=True)
    lanes = manifest["lanes"]
    children: list[subprocess.Popen] = []
    server = None
    exitcodes = {}
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await client.is_user_authorized():
            raise HarnessError("runner userbot session is not authorized")
        registry_dir = Path(os.environ.get("OBS_E2E_REGISTRY_DIR") or "/workspace/runtime/e2e/registry")
        registry = ForumRegistry(client, lanes, run_dir, Path(manifest.get("forums_path", str(registry_dir / "forums.json"))))
        forums = await registry.ensure()
        server = TelegramRPCServer(client, forums, run_dir, fresh_forum=registry.fresh)
        await server.start()
        await asyncio.gather(*(_ready(lane, run_dir, args.lane_ready_timeout) for lane in lanes))
        collect_path = runner / "collect.json"
        collect_path.unlink(missing_ok=True)
        command = [sys.executable, "-m", "pytest", "--collect-only", "-q",
                   "-c", "tests/e2e/pytest.ini", "--rootdir", str(HARNESS_ROOT), str(manifest.get("tests", "tests/e2e"))]
        if manifest.get("suite", "all") != "all":
            command.extend(["--e2e-suite", manifest["suite"]])
        keyword = manifest.get("keyword")
        if keyword:
            command.extend(["-k", keyword])
        collect = subprocess.Popen(command, cwd=HARNESS_ROOT,
                                   env=_worker_env(run_dir, OBS_E2E_COLLECT_OUT=str(collect_path), OBS_E2E_ROLE="collect"),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        children.append(collect)
        if await _stream(collect, "collect", runner / "collect.out") != 0:
            raise HarnessError("pytest collection failed; see runner/collect.out")
        if not collect_path.exists():
            raise HarnessError("collection plugin did not write collect.json")
        tests = json.loads(collect_path.read_text(encoding="utf-8"))
        shards = shard_tests(tests, lanes)
        selected_suites = set()
        for test in tests:
            metadata = test["markers"] if isinstance(test.get("markers"), dict) else test
            tags = metadata.get("suite") or []
            selected_suites.update([tags] if isinstance(tags, str) else tags)
        manifest["selected_suites"] = sorted(selected_suites)
        manifest["selected_tests_count"] = len(tests)
        _atomic_json(run_dir / "manifest.json", manifest)
        _atomic_json(runner / "shards.json", shards)
        streams = []
        active = []
        for lane in lanes:
            name = lane["lane"]
            if not shards[name]:
                continue  # Never invoke an empty pytest selection: that would run the entire suite.
            active.append(name)
            command = [sys.executable, "-m", "pytest", "-c", "tests/e2e/pytest.ini",
                       "--rootdir", str(HARNESS_ROOT), "--e2e-repeat", str(manifest.get("repeat", 1)),
                       "--junitxml", str(run_dir / f"junit-{name}.xml"), *shards[name]]
            process = subprocess.Popen(command, cwd=HARNESS_ROOT,
                                       env=_worker_env(run_dir, OBS_E2E_LANE=name, OBS_E2E_ROLE="worker"),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            children.append(process)
            streams.append(_stream(process, name, runner / f"{name}.out"))
        exitcodes = dict(zip(active, await asyncio.gather(*streams)))
        paths = [run_dir / f"junit-{name}.xml" for name in active]
        if any(not path.exists() for path in paths):
            raise HarnessError("a pytest worker did not write its JUnit report")
        merged = merge_junit(paths)
        merged.write(run_dir / "junit.xml", encoding="utf-8", xml_declaration=True)
        summary = write_summary(run_dir, junit=merged, worker_exitcodes=exitcodes)
        print(summary["summary_line"], flush=True)
        return summary["exit_code"]
    except asyncio.CancelledError:
        summary = write_summary(run_dir, worker_exitcodes=exitcodes, error="runner interrupted", exit_code=2)
        print(summary["summary_line"], flush=True)
        return 2
    except Exception as exc:
        error = str(exc) if isinstance(exc, HarnessError) else exc.__class__.__name__
        summary = write_summary(run_dir, worker_exitcodes=exitcodes, error=error, exit_code=2)
        print(f"obs-e2e: {error}", file=sys.stderr, flush=True)
        print(summary["summary_line"], flush=True)
        return 2
    finally:
        await _stop_children(children)
        if server is not None:
            await server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.disconnect(), timeout=10)
        manifest["finished_at"] = _now()
        _atomic_json(run_dir / "manifest.json", manifest)
        compute_timing(run_dir)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--userbot-lock-timeout", type=float, default=1800)
    parser.add_argument("--lane-ready-timeout", type=float, default=180)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.run_dir = args.run_dir.resolve()
    manifest = json.loads((args.run_dir / "manifest.json").read_text(encoding="utf-8"))
    session = os.environ["OBS_TEST_TELEGRAM_SESSION"]
    lock = UserbotLock.for_session(session)
    try:
        lock.acquire(timeout=args.userbot_lock_timeout, owner={"run_id": manifest["run_id"], "by": "obs-e2e runner"},
                     on_wait=lambda holder: print(f"waiting for userbot session lock held by {holder}", flush=True))
        client = make_client(session, int(os.environ["OBS_TEST_TELEGRAM_API_ID"]), os.environ["OBS_TEST_TELEGRAM_API_HASH"])
        return asyncio.run(_run(args, manifest, client))
    except UserbotLockTimeout:
        manifest["finished_at"] = _now()
        _atomic_json(args.run_dir / "manifest.json", manifest)
        result = write_summary(args.run_dir, error="userbot session lock timeout", exit_code=3)
        print(result["summary_line"], flush=True)
        return 3
    except KeyboardInterrupt:
        return 2
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
