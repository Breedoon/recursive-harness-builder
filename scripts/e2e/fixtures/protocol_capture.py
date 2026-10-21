#!/usr/bin/env python3
"""Record the real SDK <-> CLI stream-json protocol, the way OBS drives it (obs-22 E3).

Runs a fixed set of short scenarios with the official Python SDK (the same
package OBS uses) against a CLI binary wrapped in ``cli_tee.sh``, so every byte
the SDK writes to the CLI and every line the CLI writes back is kept. The
scenarios cover the surface listed in the obs-22 scope (a1-a5, a10-a12):

  tools        SDK callback hooks on all 7 OBS events, project command hooks,
               Bash, a parallel tool round, the in-process SDK MCP server
               (mcp_message), thinking
  background   run_in_background Bash -> task_notification / task_progress
  interrupt    a long Bash interrupted with the interrupt control request
  controls     mcp_status, set_model, set_permission_mode
  permission   permission_mode=default + can_use_tool callback
  subagent     the Task tool -> SubagentStart / SubagentStop hooks
  compact      the /compact command -> PreCompact hook, compact_boundary
  resume       --resume of the "tools" session in a fresh process
  decisions    hook decisions and error framing: a PreToolUse deny, a Stop
               block, a PostToolUse callback that raises (the SDK answers
               the CLI with control_response/error), rewind_files on an
               unknown message (the CLI answers with control_response/error)

Model calls go through the cache proxy given by --base-url (use a scratch
proxy with its own log dir, never 28925) on a cheap model (Haiku by default).
Output: one directory per CLI invocation under --out/raw/, plus
--out/scenarios.json mapping scenario -> invocation dirs and session ids.
Run scripts/e2e/fixtures/protocol_coverage.py afterwards for the coverage table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

TEE = HERE / "cli_tee.sh"
DEFAULT_REAL = "/workspace/runtime/reference/claude-code-src/pinned-2.1.59/claude"
HOOK_EVENTS = ["PreCompact", "PreToolUse", "PostToolUse", "Notification",
               "SubagentStart", "SubagentStop", "Stop"]  # = obs_agent.hooks create_hook_matchers events

HOOK_RECORDER = """#!/usr/bin/env python3
import json, os, sys
data = sys.stdin.read()
with open(os.environ.get("OBS_CAPTURE_HOOK_LOG", "/dev/null"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": data}) + "\\n")
sys.exit(0)
"""


def make_project(root: Path) -> Path:
    """A throwaway cwd whose .claude/settings.json mirrors the vault's hook matchers."""
    proj = root / "project"
    (proj / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    rec = proj / ".claude" / "hooks" / "record.py"
    rec.write_text(HOOK_RECORDER)
    rec.chmod(0o755)
    cmd = lambda tag: {"type": "command", "command": f"python3 {rec} {tag}"}  # noqa: E731
    settings = {
        "env": {"ENABLE_TOOL_SEARCH": "false"},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [cmd("pre-bash")]},
                {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [cmd("pre-edit")]},
                {"matcher": "mcp__obs-agent__AgentTask", "hooks": [cmd("pre-agenttask")]},
                {"matcher": "*", "hooks": [cmd("pre-any")]},
            ],
            "PostToolUse": [{"matcher": "*", "hooks": [cmd("post-any")]}],
        },
    }
    (proj / ".claude" / "settings.json").write_text(json.dumps(settings, indent=1))
    (proj / "CLAUDE.md").write_text("Protocol capture fixture project. Keep answers to one short line.\n")
    return proj


def make_mcp_server(log: list):
    @tool("ping", "Return pong and echo the given text.", {"text": str})
    async def ping(args):
        log.append({"tool": "ping", "args": args, "t": time.time()})
        return {"content": [{"type": "text", "text": f"pong: {args.get('text', '')}"}]}

    @tool("session_info", "Return a fixed session-info string.", {})
    async def session_info(args):
        log.append({"tool": "session_info", "t": time.time()})
        return {"content": [{"type": "text", "text": "session ok"}]}

    return create_sdk_mcp_server(name="obs-agent", version="1.0.0", tools=[ping, session_info])


class Run:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out)
        self.raw = self.out / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.project = make_project(self.out)
        self.hook_log = self.out / "command-hooks.jsonl"
        # re-running some scenarios into an existing capture dir keeps the others
        def _load(name, default):
            f = self.out / name
            return json.loads(f.read_text()) if f.exists() else default
        self.sdk_hook_events: list = [e for e in _load("sdk-hook-events.json", [])
                                      if e.get("scenario") not in (args.scenarios or [])]
        self.mcp_calls: list = _load("sdk-mcp-calls.json", [])
        self.scenarios: dict = _load("scenarios.json", {})

    def options(self, *, name, resume=None, permission_mode="bypassPermissions",
                can_use_tool=None, extra_env=None, model=None, hook_overrides=None):
        os.environ.pop("CLAUDECODE", None)

        def mk(event):
            async def cb(inp, tool_use_id, ctx):
                self.sdk_hook_events.append({"scenario": name, "event": event,
                                             "tool_use_id": tool_use_id,
                                             "keys": sorted(inp.keys()) if isinstance(inp, dict) else None})
                return {}
            return cb

        env = {
            "ANTHROPIC_BASE_URL": self.args.base_url,
            "OBS_CLI_TEE_DIR": str(self.raw),
            "OBS_CLI_TEE_REAL": self.args.real_cli,
            "OBS_CAPTURE_HOOK_LOG": str(self.hook_log),
            "MAX_THINKING_TOKENS": "1024",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        }
        env.update(extra_env or {})
        return ClaudeAgentOptions(
            cli_path=str(TEE),
            model=model or self.args.model,
            cwd=str(self.project),
            env=env,
            resume=resume,
            hooks={ev: [HookMatcher(matcher=None, hooks=[(hook_overrides or {}).get(ev) or mk(ev)])]
                   for ev in HOOK_EVENTS},
            mcp_servers={"obs-agent": make_mcp_server(self.mcp_calls)},
            permission_mode=permission_mode,
            can_use_tool=can_use_tool,
            setting_sources=["project"],
            settings=json.dumps({"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}}),
            system_prompt={"type": "preset", "preset": "claude_code"},
        )

    async def turn(self, client, prompt, *, interrupt_after=None):
        await client.query(prompt)
        sid = None
        t0 = time.time()
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                sid = msg.session_id
            if interrupt_after is not None and time.time() - t0 > interrupt_after:
                interrupt_after = None
                await client.interrupt()
        return sid

    def record(self, name, before: set, sid, note=""):
        after = {p.name for p in self.raw.iterdir()}
        self.scenarios[name] = {"invocations": sorted(after - before), "session_id": sid, "note": note}
        (self.out / "scenarios.json").write_text(json.dumps(self.scenarios, indent=1))
        print(f"== {name}: session={sid} invocations={sorted(after - before)}", flush=True)

    def snapshot(self):
        return {p.name for p in self.raw.iterdir()}

    async def sc_tools(self):
        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(name="tools")) as c:
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] Do these in order: (1) in ONE message, call the "
                                     "Bash tool twice in parallel, running `echo alpha` and `echo beta`; "
                                     "(2) call the mcp__obs-agent__ping tool with text 'x'; "
                                     "(3) reply with the single word: done.")
        self.record("tools", before, sid)
        return sid

    async def sc_background(self):
        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(name="background")) as c:
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] This is a protocol test of background jobs. "
                                     "Call the Bash tool exactly once with these parameters: "
                                     "command=\"sleep 4; echo bgdone\", run_in_background=true "
                                     "(the run_in_background parameter is required for this test). "
                                     "Then call the TaskOutput tool with the returned task id and block=true. "
                                     "Then reply: done.")
            # give the CLI time to emit the task notification for the finished job
            await asyncio.sleep(3)
            try:
                sid = await self.turn(c, "Reply with the single word: ok.") or sid
            except Exception as e:  # noqa: BLE001
                print("background follow-up turn failed:", e)
        self.record("background", before, sid)

    async def sc_interrupt(self):
        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(name="interrupt")) as c:
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] Run `sleep 40` with the Bash tool (foreground), "
                                     "then reply: done.", interrupt_after=8)
        self.record("interrupt", before, sid, "interrupt sent ~8s into the turn")

    async def sc_controls(self):
        before = self.snapshot()
        info = {}
        async with ClaudeSDKClient(options=self.options(name="controls")) as c:
            info["mcp_status"] = await c.get_mcp_status()
            await c.set_permission_mode("acceptEdits")
            await c.set_model(self.args.model)
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] Reply with the single word: ok.")
            info["server_info_keys"] = sorted((await c.get_server_info() or {}).keys())
        (self.out / "controls-results.json").write_text(json.dumps(info, indent=1, default=str))
        self.record("controls", before, sid)

    async def sc_permission(self):
        from claude_agent_sdk import PermissionResultAllow
        seen = []

        async def can_use(tool_name, tool_input, ctx):
            seen.append(tool_name)
            return PermissionResultAllow()

        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(name="permission", permission_mode="default",
                                                        can_use_tool=can_use)) as c:
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] Use the Write tool to create the file "
                                     "perm-test.txt containing the word perm, then reply: done.")
        self.record("permission", before, sid, f"can_use_tool calls: {seen}")

    async def sc_subagent(self):
        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(name="subagent")) as c:
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] Use the Task tool once with subagent_type "
                                     "'general-purpose' and the prompt 'Reply with the word sub.' "
                                     "Then reply: done.")
        self.record("subagent", before, sid)

    async def sc_compact(self):
        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(name="compact")) as c:
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] Reply with the single word: one.")
            sid = await self.turn(c, "/compact") or sid
            sid = await self.turn(c, "Reply with the single word: two.") or sid
        self.record("compact", before, sid)

    async def sc_decisions(self):
        state = {"stop_blocks": 0, "post_raised": 0, "denied": 0}

        def note(event, inp, tool_use_id):
            self.sdk_hook_events.append({"scenario": "decisions", "event": event, "tool_use_id": tool_use_id,
                                         "keys": sorted(inp.keys()) if isinstance(inp, dict) else None})

        async def pre(inp, tool_use_id, ctx):
            note("PreToolUse", inp, tool_use_id)
            if "denyme" in json.dumps(inp.get("tool_input") or {}):
                state["denied"] += 1
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": "protocol capture: denied"}}
            return {}

        async def post(inp, tool_use_id, ctx):
            note("PostToolUse", inp, tool_use_id)
            if not state["post_raised"]:
                state["post_raised"] += 1
                raise RuntimeError("protocol capture: deliberate hook callback error")
            return {}

        async def stop(inp, tool_use_id, ctx):
            note("Stop", inp, tool_use_id)
            if not state["stop_blocks"]:
                state["stop_blocks"] += 1
                return {"decision": "block", "reason": "Protocol capture: before stopping, reply with the word again."}
            return {}

        info = {}
        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(
                name="decisions", hook_overrides={"PreToolUse": pre, "PostToolUse": post, "Stop": stop})) as c:
            try:
                await c.rewind_files(str(uuid.uuid4()))
                info["rewind_files"] = "success"
            except Exception as e:  # noqa: BLE001 -- the error response is what is captured
                info["rewind_files"] = f"error: {type(e).__name__}"
            sid = await self.turn(c, f"[capture {uuid.uuid4()}] Do these in order: (1) run `echo denyme` with the "
                                     "Bash tool; (2) run `echo allowed` with the Bash tool; (3) reply: done.")
        info.update(state)
        (self.out / "decisions-results.json").write_text(json.dumps(info, indent=1))
        self.record("decisions", before, sid, json.dumps(info))

    async def sc_resume(self, sid):
        before = self.snapshot()
        async with ClaudeSDKClient(options=self.options(name="resume", resume=sid)) as c:
            sid2 = await self.turn(c, "Reply with the single word: resumed.")
        self.record("resume", before, sid2, f"resumed {sid}")


async def amain(args):
    run = Run(args)
    names = args.scenarios or ["tools", "decisions", "background", "interrupt", "controls", "permission",
                               "subagent", "compact", "resume"]
    tools_sid = None
    for n in names:
        try:
            if n == "resume":
                if not tools_sid:
                    tools_sid = await run.sc_tools()
                await run.sc_resume(tools_sid)
            elif n == "tools":
                tools_sid = await run.sc_tools()
            else:
                await getattr(run, f"sc_{n}")()
        except Exception as e:  # keep going; the coverage table shows the gap
            print(f"== {n}: ERROR {type(e).__name__}: {e}", flush=True)
            run.scenarios[n] = {"error": f"{type(e).__name__}: {e}"}
            (run.out / "scenarios.json").write_text(json.dumps(run.scenarios, indent=1))
    (run.out / "sdk-hook-events.json").write_text(json.dumps(run.sdk_hook_events, indent=1))
    (run.out / "sdk-mcp-calls.json").write_text(json.dumps(run.mcp_calls, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--base-url", required=True, help="scratch cache proxy, e.g. http://127.0.0.1:28947")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--real-cli", default=DEFAULT_REAL, help="binary under capture (default pinned 2.1.59)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    if "28925" in args.base_url:
        ap.error("use a scratch proxy, not production 28925")
    asyncio.run(amain(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
