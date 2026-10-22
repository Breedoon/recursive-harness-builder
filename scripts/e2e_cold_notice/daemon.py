"""Real TelegramBot, real pollers, real SDK/CLI; executed only inside obs-e2e."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from stub_api import CHILDREN, Stub, clean


def emit(event, **data):
    row = {"event": event, "at": time.time(), "utc": datetime.now(timezone.utc).isoformat(), **data}
    print("E2E " + clean(json.dumps(row, default=str), 2000000), flush=True)


class FakeTelegram:
    id = 9990001
    username = "ruoi_e2e_bot"

    def __init__(self, root):
        self.log = root / "telegram.jsonl"
        self.message_id = 5000
        self.thread_id = 7000

    def record(self, method, kwargs):
        row = {"t": time.time(), "method": method,
               **{k: kwargs[k] for k in ("chat_id", "message_thread_id", "message_id", "reply_to_message_id", "name") if k in kwargs}}
        if "text" in kwargs:
            row["text"] = clean(str(kwargs["text"]), 800)
        with self.log.open("a") as output:
            output.write(json.dumps(row, default=str) + "\n")

    async def create_forum_topic(self, **kwargs):
        self.record("create_forum_topic", kwargs)
        self.thread_id += 1
        return SimpleNamespace(message_thread_id=self.thread_id, name=kwargs.get("name"))

    async def send_message(self, **kwargs):
        self.record("send_message", kwargs)
        self.message_id += 1
        return SimpleNamespace(message_id=self.message_id, chat=SimpleNamespace(id=kwargs.get("chat_id")),
                               message_thread_id=kwargs.get("message_thread_id"), text=kwargs.get("text"))

    def __getattr__(self, method):
        async def call(*args, **kwargs):
            self.record(method, kwargs)
            self.message_id += 1
            return SimpleNamespace(message_id=self.message_id, message_thread_id=kwargs.get("message_thread_id"))
        return call


async def until(predicate, *, timeout=240, reason="condition"):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Harness timed out awaiting " + reason)
        await asyncio.sleep(0.05)


class Probe:
    def __init__(self, root, bot):
        self.root, self.bot = root, bot
        self.agents = {}
        self.cycles = []
        self.notices = []
        self.attached = set()

    def snapshot(self, state, agent):
        from obs_agent.cache_readiness import last_model_call
        from obs_agent.context_jsonl import find_session_jsonl
        path = find_session_jsonl(session_id=state.session_id, cwd=self.root / "vault") if state.session_id else None
        call = last_model_call(path)
        readiness = self.bot._state_cache_readiness(state=state, session_id=state.session_id)
        rows = []
        if path is not None and path.exists():
            for line in path.read_text(errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                rows.append({"type": record.get("type"), "timestamp": record.get("timestamp")})
        projection = self.bot._route_inbox_target_keys_by_route.get(state.route)
        inbox_path = None
        inbox = []
        if projection:
            inbox_path = Path.home() / ".claude/teams" / projection[0] / "inboxes" / (projection[1] + ".json")
            if inbox_path.exists():
                entries = json.loads(inbox_path.read_text())
                inbox = [{k: entry.get(k) for k in ("from", "from_team", "summary", "read", "wake", "timestamp")}
                         for entry in entries]
        now = time.time()
        return {"agent": agent, "route": str(state.route), "session_id": state.session_id,
                "busy": state.busy, "execution_active": state.hook_state.execution_active,
                "observed_at": now, "last_assistant_timestamp": call.timestamp,
                "last_assistant_utc": datetime.fromtimestamp(call.timestamp, timezone.utc).isoformat() if call.timestamp else None,
                "last_assistant_source": call.source,
                "physical_idle_seconds": now - call.timestamp if call.timestamp else None, "configured_ttl": 20,
                "readiness": dataclasses.asdict(readiness) if readiness else None,
                "jsonl": {"path": str(path) if path else None, "rows": len(rows), "row_types": [r["type"] for r in rows],
                          "row_timestamps": [r["timestamp"] for r in rows]},
                "inbox_path": str(inbox_path) if inbox_path else None, "inbox": inbox,
                "queue_depth": state.hook_state.message_queue.qsize(), "pending_messages": len(state.pending_messages)}

    def sample(self, agent):
        return self.snapshot(self.agents[agent], agent)

    def before_tool(self, role, tool, payload):
        if tool == "SendInboxMessage":
            recipient = "parent" if payload.get("recipient") == "ruoi-parent" else "child"
            if recipient == "parent":
                emit("message_boundary", recipient=recipient, snapshot=self.sample(recipient), tool_input=payload)
            else:
                emit("sender_tool_to_child", tool_input=payload)
        if tool == "AgentTaskOutput":
            emit("blocking_tool_started", recipient="parent", snapshot=self.sample("parent"), tool_input=payload)

    def notice(self, record, done):
        if not record.emit_parent_callback:
            return
        parent = self.bot._get_state(record.parent_route, create=False)
        recipient = next((name for name, state in self.agents.items() if state is parent), "parent")
        payload = self.bot._build_fork_task_callback_payload(record)
        status = None
        import re
        match = re.search(r"<status>([^<]+)</status>", payload)
        if match:
            status = match.group(1)
        row = {"recipient": recipient, "task_id": record.task_id, "child": record.agent_name,
               "terminal_status": record.status, "notice_status": status, "error": record.error,
               "notice_at": record.completed_at, "snapshot": self.snapshot(parent, recipient),
               "sampling": "real fork task done callback, before next background poll unless parent already running"}
        self.notices.append(row)
        emit("notice", **row)

    async def observe_tasks(self):
        while True:
            for task_id, record in list(self.bot._fork_tasks_by_id.items()):
                task = self.bot._fork_task_tasks.get(task_id)
                if task is not None and task not in self.attached:
                    self.attached.add(task)
                    task.add_done_callback(lambda done, record=record: self.notice(record, done))
                    emit("child_observed", task_id=task_id, child=record.agent_name, team=record.team_name,
                         timeout_ms=record.timeout_ms, route=str(record.child_route))
            await asyncio.sleep(0.025)

    def start_cycle_profile(self):
        code = self.bot._poll_background_queues_once.__func__.__code__
        seen = set()
        def profile(frame, event, arg):
            if event == "call" and frame.f_code is code and frame not in seen:
                seen.add(frame)
                now = time.time()
                delta = now - self.cycles[-1] if self.cycles else None
                self.cycles.append(now)
                emit("poller_cycle", cycle=len(self.cycles), interval_seconds=delta)
            if event == "return" and isinstance(arg, dict) and "tool_use_result" in arg and (
                frame.f_code.co_name in ("_send_inbox_message", "_read_inbox", "_task_output", "_launch_task")
                and frame.f_code.co_filename == str(self.root / "code/src/obs_agent/tools.py")
            ):
                emit("tool_handler_result", handler=frame.f_code.co_name,
                     input=frame.f_locals.get("args"), backend_result=arg["tool_use_result"],
                     distinction="native handler result; model-visible text recorded independently in stub.jsonl")
        sys.setprofile(profile)


class LogProbe(logging.Handler):
    def __init__(self, probe):
        super().__init__()
        self.probe = probe

    def emit(self, record):
        text = record.getMessage()
        if "Auto-delivering queued updates route=" not in text and "[run_and_send] preflight route=" not in text:
            return
        for name, state in list(self.probe.agents.items()):
            if str(state.route) in text:
                emit("delivery_log_boundary", recipient=name, line=clean(text), snapshot=self.probe.sample(name))


async def scenario(root, scenario_id, s1_mode):
    if Path("/run/ruoi").resolve() != root.resolve() or not os.environ.get("RUOI_E2E_CONTAINER"):
        raise RuntimeError("Daemon must be launched by run_scenario.py inside its obs-e2e container")
    interfaces = sorted(p.name for p in Path("/sys/class/net").iterdir())
    if interfaces != ["lo"]:
        raise RuntimeError(f"Expected network none, found interfaces {interfaces}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(root / "daemon.log")])
    for noisy in ("httpx", "telegram", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    import obs_agent
    from obs_agent.config import OBSConfig
    from obs_agent.telegram import TelegramBot, TelegramRoute
    config = OBSConfig(vault_path=root / "vault", model="claude-haiku-4-5-20251001",
                       telegram_allowed_user_ids=[12345], telegram_state_db_path=root / "state/telegram-state.sqlite3",
                       telegram_temp_root=root / "tg-temp", cache_proxy_enabled=False,
                       new_session_effort_level=None, telegram_typing_actions_enabled=False,
                       telegram_transport_base_chat_interval_seconds=0.01,
                       telegram_transport_max_chat_interval_seconds=0.05)
    fake = FakeTelegram(root)
    bot = TelegramBot(config, fragment_gap=0.05)
    bot._primary_bot = fake
    await bot.initialize_runtime()
    probe = Probe(root, bot)
    stub = Stub(root, before_tool=probe.before_tool)
    server = await asyncio.start_server(stub.handle, "127.0.0.1", 29181)
    logging.getLogger("obs_agent.telegram").addHandler(LogProbe(probe))
    probe.start_cycle_profile()
    monitor = asyncio.create_task(probe.observe_tasks())
    await bot._ensure_background_poller(fake)
    emit("source", obs_agent=obs_agent.__file__, scenario=scenario_id,
         background_poller_enabled=bot._enable_background_poller,
         background_poll_seconds=bot._background_poll_seconds, interfaces=interfaces,
         first_turn_entry="_run_and_send on explicitly bootstrapped root state, under route lock",
         instrumentation="read-only logging handler, fork-task done callbacks and Python call profiler")

    def root_agent(role, thread):
        state = bot._get_state(TelegramRoute(chat_id=-1009990001, thread_id=thread),
                               topic_title="RUOI " + role, apply_default_new_chat_hooks=False)
        state.last_bot = fake
        agent = "ruoi-" + role
        bot._prime_obs_bootstrap(state, lineage=("RUOI E2E", role), origin="trunk_start", is_fork=False,
                                 team_name="2026-10-22-18-00-ruoi-e2e", agent_name=agent, session_id=state.session_id)
        probe.agents[role] = state
        return state

    async def run_root(state, text):
        from obs_agent.queueing import QueuedMessage
        trigger = QueuedMessage(text=text, telegram_message_id=4100) if scenario_id == "1" and s1_mode == "stop" else None
        async with bot._get_route_lock(state.route):
            return await bot._run_and_send(state=state, user_text=text, bot=fake, trigger_message=trigger)

    async def cold(role):
        await until(lambda: (probe.sample(role)["physical_idle_seconds"] or 0) > 25.2,
                    reason=role + " physically idle beyond 25s")
        emit("cold_ready", recipient=role, snapshot=probe.sample(role))

    async def settle():
        first_cycle = len(probe.cycles)
        await until(lambda: len(probe.cycles) >= first_cycle + 3 and
                    all(not s.busy and not s.hook_state.execution_active for s in probe.agents.values()) and
                    not any(not task.done() for task in bot._background_delivery_tasks.values()),
                    reason="three real poll cycles and recipient turns idle")

    async def send(to, override):
        sender = probe.agents.get("sender") or root_agent("sender", 200)
        prompt = "RUOI-SENDER " + json.dumps({"scenario": scenario_id, "to": to, "override": override})
        await run_root(sender, prompt)
        emit("sender_turn_finished", snapshot=probe.sample("sender"))

    parent = root_agent("parent", 100)
    root_task = None
    try:
        prompt = "RUOI-PARENT " + json.dumps({"scenario": scenario_id, "s1_mode": s1_mode})
        root_task = asyncio.create_task(run_root(parent, prompt))
        await root_task
        emit("parent_first_turn_finished", snapshot=probe.sample("parent"))
        if scenario_id == "1":
            emit("terminal_mechanism", mechanism="non-retryable HTTP 400 after child's first tool round" if s1_mode == "error" else "real external-control stop of exact child generation")
            if s1_mode == "stop":
                await cold("parent")
                await until(lambda: bot._control_server is not None, reason="real control socket listener")
                record = next(iter(bot._fork_tasks_by_id.values()))
                command = [sys.executable, "-m", "obs_agent.control", "--as", "2026-10-22-18-00-ruoi-e2e", "--json", "stop",
                           "--parent-team", record.team_name, "--parent", "ruoi-parent", "--team", record.team_name,
                           "--agent", record.agent_name, "--expected-task-id", record.task_id,
                           "--expected-launch-parent-message-id", str(record.launch_parent_message_id)]
                emit("stop_control_boundary", snapshot=probe.sample("parent"), argv=command)
                process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await process.communicate()
                emit("control_result", returncode=process.returncode, stdout=clean(stdout.decode()), stderr=clean(stderr.decode()))
        if scenario_id in CHILDREN:
            if scenario_id == "8":
                await until(lambda: bool(bot._fork_tasks_by_id), reason="child launch")
                record = next(iter(bot._fork_tasks_by_id.values()))
                await send(record.agent_name, False)
                await until(lambda: record.status in ("completed", "failed", "stopped"), reason="child terminal")
                await cold("sender")
                emit("bounce_boundary", recipient="sender", snapshot=probe.sample("sender"),
                     child=record.agent_name, child_route=str(record.child_route),
                     entry="direct real _send_bounce_backs_for_dead_agent, with real unread sender row; no kill/deletion")
                await bot._send_bounce_backs_for_dead_agent(record.child_route)
            else:
                wanted = len(CHILDREN[scenario_id])
                await until(lambda: len(probe.notices) >= wanted, reason="all child terminal callbacks")
                await settle()
                if scenario_id.startswith("7-"):
                    held_at = time.time()
                    request_start = len([r for r in stub.rows if r.get("role") == "parent"])
                    first_cycle = len(probe.cycles)
                    await until(lambda: time.time() - held_at >= 30 and len(probe.cycles) - first_cycle >= 5,
                                reason="at least 30s and five actual poller cycles after held notice")
                    emit("held_interval", seconds=time.time() - held_at,
                         poller_cycles=len(probe.cycles) - first_cycle,
                         parent_requests_during=len([r for r in stub.rows if r.get("role") == "parent"]) - request_start,
                         snapshot=probe.sample("parent"))
                    if scenario_id == "7-human":
                        from telegram import Update
                        update = Update.de_json({"update_id": 991, "message": {
                            "message_id": 991, "date": int(time.time()), "message_thread_id": 100,
                            "is_topic_message": True, "chat": {"id": -1009990001, "type": "supergroup", "title": "RUOI E2E"},
                            "from": {"id": 12345, "is_bot": False, "first_name": "E2E"}, "text": "RUOI-HUMAN-WAKE read inbox twice"}}, fake)
                        emit("human_wake_boundary", recipient="parent", snapshot=probe.sample("parent"), entry="TelegramBot.handle_message with real telegram.Update and recording transport")
                        await bot.handle_message(update, SimpleNamespace(bot=fake))
                    else:
                        await send("ruoi-parent", True)
                    await until(lambda: any(r.get("role") == "parent" and r.get("kind") == "parent-read-2" for r in stub.rows),
                                reason="both real ReadInbox calls")
        elif scenario_id.startswith("5"):
            if scenario_id != "5b":
                await cold("parent")
            await send("ruoi-parent", scenario_id == "5c")
        elif scenario_id == "6":
            await cold("parent")
            await until(lambda: bot._control_server is not None, reason="real control socket listener")
            fixture = json.loads((root / "control-site.json").read_text())
            emit("control_boundary", recipient="parent", snapshot=probe.sample("parent"),
                 site=fixture["site"], argv=fixture["argv"])
            process = await asyncio.create_subprocess_exec(*fixture["argv"], stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            emit("control_result", site=fixture["site"], returncode=process.returncode,
                 stdout=clean(stdout.decode()), stderr=clean(stderr.decode()))
        await settle()
        final_agents = {name: probe.sample(name) for name in probe.agents}
        control_audit = root / "control/audit.jsonl"
        audit = [json.loads(line) for line in control_audit.read_text().splitlines()] if control_audit.exists() else []
        emit("final", agents=final_agents, poller_cycles=len(probe.cycles), poller_cycle_times=probe.cycles,
             control_audit=audit, terminal_tasks=[{"task_id": r.task_id, "agent_name": r.agent_name,
                                                 "status": r.status, "error": r.error} for r in bot._fork_tasks_by_id.values()],
             stub_request_counts={role: len([r for r in stub.rows if r.get("role") == role and r.get("kind") != "count_tokens"])
                                  for role in ("parent", "child", "sender")},
             cache_creation_tokens="synthetic zero; not billing; use request counts and byte sizes")
    finally:
        sys.setprofile(None)
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        if root_task is not None and not root_task.done():
            root_task.cancel()
        server.close()
        await server.wait_closed()
        await bot.shutdown()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--s1-mode", choices=("error", "stop"), default="error")
    args = parser.parse_args()
    try:
        async with asyncio.timeout(280):
            await scenario(args.root, args.scenario, args.s1_mode)
        return 0
    except Exception as exc:
        emit("harness_failure", error=clean(repr(exc)))
        logging.exception("Scenario harness failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
