#!/usr/bin/env python3
"""Live prompt-cache prefix reproduction drivers (one scenario per miss class).

PREFIX-STABILITY: these drivers exist because unit tests "have utterly failed at
preventing these issues" (Daniel, 2026-10-09 00:21Z). Each scenario drives a REAL
bundled Claude Code CLI (cheap Haiku model) through the PRODUCTION cache proxy
(default http://127.0.0.1:28925), then reads the proxy's own request log
(cache_proxy.RequestLogger index + bodies) for that scenario's session ids and
asserts, for every adjacent request pair of the scenario's main conversation:

  * prefix_diff classification == "append" (earlier request is a byte prefix of
    the later one, ignoring only cache_control), and
  * the later request has a message-level cache_control breakpoint, and
  * cache_read >= THRESHOLD (default 0.95) of the earlier request's prompt total.

Before the 2026-10-09 fix, scenarios marked "expect FAIL on unfixed proxy" are
expected to fail — that is the reproduction. After E4's fix all must pass.

Scenarios (mission cache-proxy-prefix-fix, bead vault-mief.3; E4 promotes these
into the live regression harness):
  baseline         3 plain turns, same process (control; must pass everywhere)
  reminder_block   class A: a PreToolUse deny makes the CLI append a reminder-only
                   text block carrying the only message cache_control; the proxy
                   strips the block and the marker with it.
  queued_midturn   notification splice: a second query() is submitted while the
                   first turn is mid-tool (OBS's queued-message delivery path).
  cch_collision    a tool result containing the literal billing placeholder
                   string; the CLI rewrites the FIRST occurrence in the body with
                   a per-request hash, so that history message changes each call.
  boundary_flip    class B flip-flop: resume the same session in a new process
                   with CLAUDE_CODE_FORCE_GLOBAL_CACHE flipped, which toggles the
                   __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__ marker in system[2]
                   (same effect as the GrowthBook flag
                   tengu_system_prompt_global_cache changing between processes).
  resume_new_proc  wake path: same session resumed by a new CLI process after an
                   idle gap (--gap seconds; use >300 to cover the >5 min case).
  recovery         real OBS recovery path: the CLI is killed while a tool_use is
                   in flight; obs_agent.jsonl_health picks the safe target and
                   jsonl_fork.fork_session_jsonl copies the chain to a NEW
                   session id (exactly what telegram.py
                   _recover_route_session_if_needed does), then the new session
                   is resumed. Compares the first recovered request against the
                   last pre-kill request (cross-session pair).

Usage:
  prefix_repro.py --list
  prefix_repro.py SCENARIO [SCENARIO ...] [--model M] [--base-url URL]
                  [--log-dir DIR] [--gap SECONDS] [--threshold 0.95] [--json]
  prefix_repro.py all

Exit status: 0 if every selected scenario passed, 1 otherwise.
Cost: Haiku, ~20-40K cached prompt per call, a few calls per scenario.
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import prefix_diff as pd  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    ToolUseBlock,
)

DEFAULT_BASE_URL = "http://127.0.0.1:28925"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
WORK_ROOT = Path(os.environ.get("PREFIX_REPRO_ROOT", "/workspace/runtime/prefix-repro"))
# Built at runtime so this file never contains the literal placeholder itself
# (the CLI would rewrite it if an agent ever reads this file into context).
CCH_PLACEHOLDER = "cch=" + "0" * 5


# ── driving the CLI ─────────────────────────────────────────────────────

def _options(args, *, session_id=None, resume=None, env_extra=None, hooks=None, system_append=None):
    # The driver may itself run inside an agent's CLI; the nested-session guard
    # keys on CLAUDECODE in the inherited environment.
    os.environ.pop("CLAUDECODE", None)
    cwd = WORK_ROOT / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    if resume:
        from obs_agent.jsonl_replay import prepare_session_replay
        prepare_session_replay(session_id=resume, cwd=cwd)
    env = {
        "ANTHROPIC_BASE_URL": args.base_url,
        "CLAUDE_CODE_EAGER_FLUSH": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ENABLE_TOOL_SEARCH": "false",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    }
    if args.provider == "codex":
        for key in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
            env[key] = args.model
    # Keep the GrowthBook-driven system marker deterministic unless a scenario
    # flips it on purpose (see boundary_flip).
    env["CLAUDE_CODE_FORCE_GLOBAL_CACHE"] = "0"
    env.update(env_extra or {})
    extra = {}
    if session_id:
        extra["session-id"] = session_id
    return ClaudeAgentOptions(
        model=args.model,
        cwd=str(cwd),
        env=env,
        resume=resume,
        extra_args=extra,
        setting_sources=[],
        # the real Claude Code system prompt (what OBS agents run with); its
        # system[2] is where the GrowthBook-driven boundary marker lands
        system_prompt={"type": "preset", "preset": "claude_code",
                       **({"append": system_append} if system_append is not None else {})},
        permission_mode="bypassPermissions",
        allowed_tools=["Bash", "Read"],
        hooks=hooks,
    )


async def _turn(client, prompt, *, stop_on_tool_use=False):
    from obs_agent._sdk_patch import ensure_raw_uuid_patch
    ensure_raw_uuid_patch()
    await client.query(prompt)
    last_assistant_uuid = None
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            last_assistant_uuid = getattr(msg, "_raw_uuid", None) or last_assistant_uuid
        if stop_on_tool_use and isinstance(msg, AssistantMessage):
            if any(isinstance(b, ToolUseBlock) for b in msg.content):
                return "tool_use_seen"
        if isinstance(msg, ResultMessage):
            if msg.subtype == "success" and not msg.is_error:
                from obs_agent.jsonl_replay import record_terminal_completion
                record_terminal_completion(session_id=msg.session_id, cwd=WORK_ROOT / "cwd",
                                           target_uuid=last_assistant_uuid)
            return "done"
    return "eof"


async def _session(args, prompts, **kw):
    async with ClaudeSDKClient(options=_options(args, **kw)) as client:
        for p in prompts:
            await _turn(client, p)
        # let the CLI flush its JSONL before the process is torn down
        await asyncio.sleep(float(os.environ.get("PREFIX_REPRO_SETTLE", "0")))


# ── reading the proxy log ───────────────────────────────────────────────

def _rows(log_dir, session_ids, since):
    want = set(session_ids)
    out = [
        r for r in pd.read_index(log_dir)
        if r.get("session_id") in want
        and r.get("ts", 0) >= since
        and str(r.get("path", "")).startswith("/v1/messages")
        and "count_tokens" not in str(r.get("path", ""))
        and r.get("http_status") == 200
    ]
    out.sort(key=lambda r: r["ts"])
    # drop CLI side calls (title/topic helpers: no tools, tiny body) — they are
    # separate cache lines, not part of the conversation prefix
    out = [r for r in out if ((r.get("sizes") or {}).get("pre") or 0) >= 20000
           and pd.load_request(r["req_id"], "wire", log_dir).get("tools")]
    # keep the scenario's main model only
    by = {}
    for r in out:
        by.setdefault(r["model"], []).append(r)
    if not by:
        return []
    main = max(by, key=lambda m: len(by[m]))
    return by[main]


def _prompt_total(u):
    u = u or {}
    return (u.get("cache_read") or 0) + (u.get("cache_creation") or 0) + (u.get("input_tokens") or 0)


def evaluate(log_dir, session_ids, since, threshold, *, settle=3.0, pairs=None):
    """Return list of per-pair verdicts for the given sessions (chronological)."""
    time.sleep(settle)  # RequestLogger writes from a background thread
    rows = _rows(log_dir, session_ids, since)
    verdicts = []
    seq = pairs if pairs is not None else list(zip(rows, rows[1:]))
    for a, b in seq:
        A = pd.load_request(a["req_id"], "wire", log_dir)
        B = pd.load_request(b["req_id"], "wire", log_dir)
        res = pd.diff_requests(A, B, pd._beta(a), pd._beta(b))
        text = pd.format_result(res)
        cls = text.split()[0]
        no_bp = "NO MESSAGE BREAKPOINT" in text
        prior = _prompt_total(a.get("usage"))
        cr = (b.get("usage") or {}).get("cache_read") or 0
        ratio = cr / prior if prior else 0.0
        def tool_blocks(request):
            blocks = {}
            for message in request.get("messages", []):
                content = message.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                        key = (block["type"], block.get("id", block.get("tool_use_id")))
                        blocks.setdefault(key, []).append(pd.canon(block))
            return blocks
        old_tools, new_tools = tool_blocks(A), tool_blocks(B)
        tool_fidelity = all(new_tools.get(key) == value for key, value in old_tools.items())
        ok = cls in ("append", "tail-block-append", "identical") and not no_bp and ratio >= threshold and tool_fidelity
        verdicts.append({
            "a": a["req_id"], "b": b["req_id"], "class": cls, "no_breakpoint": no_bp,
            "cache_read": cr, "prior_prompt": prior, "ratio": round(ratio, 4),
            "original_tool_blocks_exactly_once": tool_fidelity,
            "ok": ok, "diff": text if not ok else "",
        })
    return rows, verdicts


# ── scenarios ───────────────────────────────────────────────────────────

async def sc_baseline(args):
    sid = str(uuid.uuid4())
    await _session(args, ["Reply with the single word: one.",
                          "Reply with the single word: two.",
                          "Reply with the single word: three."], session_id=sid)
    return [sid], None


async def sc_reminder_block(args):
    sid = str(uuid.uuid4())

    async def deny(input_data, tool_use_id, context):
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "prefix_repro: Bash is denied in this scenario.",
        }}

    hooks = {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[deny])]}
    await _session(args, [
        "Reply with the single word: ready.",
        "Run the Bash command `echo hi`. If it is denied, reply with the single word: denied.",
        "Reply with the single word: after.",
    ], session_id=sid, hooks=hooks)
    return [sid], None


async def sc_queued_midturn(args):
    sid = str(uuid.uuid4())
    async with ClaudeSDKClient(options=_options(args, session_id=sid)) as client:
        await _turn(client, "Reply with the single word: ready.")
        await client.query("Run the Bash command `sleep 8 && echo slept`, then reply with the single word: done.")
        await asyncio.sleep(3)  # mid-tool: OBS delivers queued notifications exactly like this
        await client.query("[Queued message from user]: (System notification: New teammate messages "
                           "arrived while you were still running.) Reply 'ack' when convenient.")

        async def drain():
            async for msg in client.receive_messages():
                if isinstance(msg, ResultMessage):
                    return
        # the CLI may fold the queued input into the running turn (one result)
        # or run it as its own turn (two results): drain until quiet
        for _ in range(2):
            try:
                await asyncio.wait_for(drain(), timeout=60)
            except asyncio.TimeoutError:
                break
        await _turn(client, "Reply with the single word: after.")
        await asyncio.sleep(float(os.environ.get("PREFIX_REPRO_SETTLE", "0")))
    return [sid], None


async def sc_cch_collision(args):
    sid = str(uuid.uuid4())
    await _session(args, [
        f"Run the Bash command `echo 'header: {CCH_PLACEHOLDER};'` and reply with the single word: shown.",
        "Reply with the single word: two.",
        "Reply with the single word: three.",
    ], session_id=sid)
    return [sid], None


async def sc_boundary_marker(args):
    """Exact standalone marker changes, not a substitute auto-memory toggle."""
    sid = str(uuid.uuid4())
    plain = "M4 driver stable section.\n\nM4 driver dynamic section."
    marked = plain.replace("\n\n", "\n\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n\n")
    await _session(args, ["Reply with the single word: one.", "Reply with the single word: two."],
                   session_id=sid, system_append=plain)
    await _session(args, ["Reply with the single word: three."], resume=sid, system_append=marked)
    await _session(args, ["Reply with the single word: four."], resume=sid, system_append=plain)
    return [sid], {"marker": "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"}


async def sc_boundary_flip(args):
    sid = str(uuid.uuid4())
    await _session(args, ["Reply with the single word: one.", "Reply with the single word: two."],
                   session_id=sid, env_extra={"CLAUDE_CODE_FORCE_GLOBAL_CACHE": "0"})
    await _session(args, ["Reply with the single word: three."],
                   resume=sid, env_extra={"CLAUDE_CODE_FORCE_GLOBAL_CACHE": "1"})
    await _session(args, ["Reply with the single word: four."],
                   resume=sid, env_extra={"CLAUDE_CODE_FORCE_GLOBAL_CACHE": "0"})
    return [sid], None


async def sc_resume_new_proc(args):
    sid = str(uuid.uuid4())
    await _session(args, ["Reply with the single word: one.", "Reply with the single word: two."], session_id=sid)
    if args.gap:
        await asyncio.sleep(args.gap)
    await _session(args, ["Reply with the single word: woke."], resume=sid)
    return [sid], None


async def sc_recovery(args):
    from obs_agent.jsonl_fork import fork_session_jsonl
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    client = ClaudeSDKClient(options=_options(args, session_id=sid))
    await client.connect()
    try:
        await _turn(client, "Reply with the single word: ready.")
        await _turn(client, "Reply with the single word: two.")
        # leave a tool_use in flight, then kill the CLI (crash / restart / 401 death)
        r = await _turn(client, "Say the word 'starting' and then run the Bash command `sleep 60`.",
                        stop_on_tool_use=True)
        await asyncio.sleep(2)
    finally:
        await client.disconnect()
    target = resolve_safe_jsonl_target(session_id=sid, cwd=cwd)
    if target is None or not target.target_uuid:
        raise RuntimeError(f"recovery: no safe target (turn result {r})")
    new_sid = fork_session_jsonl(session_id=sid, target_uuid=target.target_uuid, cwd=cwd,
                                 new_session_id=str(uuid.uuid4()))
    await _session(args, ["(recovered) Reply with the single word: back."], resume=new_sid)
    return [sid, new_sid], {"parent": sid, "child": new_sid,
                            "needs_recovery": target.health.needs_recovery,
                            "reason": target.health.unsafe_tail_reason}


async def sc_parallel_resume(args):
    """Parallel tool calls, then the same session resumed by a NEW CLI process
    (OBS kills idle CLIs: OBS_PROD_CLAUDE_KILL_ON_IDLE=1, so every wake after
    an idle gap is a new-process --resume). The CLI writes the result of the
    first parallel tool_use off the main parentUuid chain; on resume the chain
    walk drops it and the rebuilt assistant message loses that tool_use."""
    sid = str(uuid.uuid4())
    for name in ("a.txt", "b.txt", "c.txt"):
        (WORK_ROOT / "cwd" / name).parent.mkdir(parents=True, exist_ok=True)
        (WORK_ROOT / "cwd" / name).write_text(name + "\n")
    await _session(args, [
        "Reply with the single word: ready.",
        "In ONE message call the Read tool three times in parallel on a.txt, b.txt and c.txt "
        "(relative to the current directory), then reply with the single word: read.",
    ] + [f"Reply with the single word: warm{i}." for i in range(args.warm_turns)], session_id=sid)
    await _session(args, ["Reply with the single word: woke."], resume=sid)
    return [sid], None


async def sc_parallel_fork(args):
    import hashlib
    from obs_agent.context_jsonl import find_session_jsonl
    from obs_agent.jsonl_fork import fork_session_jsonl
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    sids, _ = await sc_parallel_resume(args)
    sid = sids[0]
    cwd = WORK_ROOT / "cwd"
    source = find_session_jsonl(session_id=sid, cwd=cwd)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = resolve_safe_jsonl_target(session_id=sid, cwd=cwd)
    final_uuid = target.health.last_real_assistant_uuid
    if target.health.complete_message_targets.get(final_uuid) != target.target_uuid:
        raise RuntimeError("parallel_fork: observed terminal response lacks confirmed completion")
    original_map = {e["uuid"]: e for e in (json.loads(line) for line in source.read_text().splitlines()) if e.get("uuid")}
    new_sid = fork_session_jsonl(session_id=sid, target_uuid=target.target_uuid, cwd=cwd)
    fork_path = source.parent / f"{new_sid}.jsonl"
    copied_map = {e["uuid"]: e for e in (json.loads(line) for line in fork_path.read_text().splitlines()) if e.get("uuid")}
    final_response_preserved = copied_map.get(final_uuid, {}).get("message") == original_map[final_uuid]["message"]
    source_unchanged = hashlib.sha256(source.read_bytes()).hexdigest() == before
    await _session(args, ["Reply with the single word: forked."], resume=new_sid)
    return [sid, new_sid], {"parent": sid, "child": new_sid, "final_response_preserved": final_response_preserved,
                            "original_source_sha256": before, "source_unchanged": source_unchanged}


async def sc_recovery_multi(args):
    """Like recovery, but the killed turn already completed several tool rounds
    (text, parallel reads, more tools) before the in-flight one — the shape of
    the 2026-10-09 00:07Z restart recovery (E2) that read only 37,167 tokens."""
    from obs_agent.jsonl_fork import fork_session_jsonl
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    import hashlib
    from obs_agent.context_jsonl import find_session_jsonl

    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    effect_path = cwd / f"effects-{sid}.txt"
    for name in ("a.txt", "b.txt", "c.txt"):
        (cwd / name).parent.mkdir(parents=True, exist_ok=True)
        (cwd / name).write_text(name + "\n")
    client = ClaudeSDKClient(options=_options(args, session_id=sid))
    await client.connect()
    try:
        await _turn(client, "Reply with the single word: ready.")
        await client.query(
            "Do these steps in order without pausing: (1) say 'working', (2) in ONE message call the "
            f"Read tool on a.txt and b.txt in parallel, (3) run Bash `echo step3 >> {effect_path.name}`, "
            f"(4) run Bash `echo step4 >> {effect_path.name}`, (5) run Bash `sleep 60`.")
        seen = 0
        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, ToolUseBlock) and b.name == "Bash" and "sleep" in str(b.input):
                        seen = 1
            if seen:
                break
            if isinstance(msg, ResultMessage):
                break
        await asyncio.sleep(2)
    finally:
        await client.disconnect()
    target = resolve_safe_jsonl_target(session_id=sid, cwd=cwd)
    if target is None or not target.target_uuid:
        raise RuntimeError("recovery_multi: no safe target")
    effects_before = effect_path.read_text().splitlines() if effect_path.exists() else []
    source = find_session_jsonl(session_id=sid, cwd=cwd)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    new_sid = fork_session_jsonl(session_id=sid, target_uuid=target.target_uuid, cwd=cwd,
                                 new_session_id=str(uuid.uuid4()))
    await _session(args, ["(recovered) Do not rerun prior tools. Reply with the single word: back."], resume=new_sid)
    effects_after = effect_path.read_text().splitlines() if effect_path.exists() else []
    return [sid, new_sid], {"parent": sid, "child": new_sid,
                            "needs_recovery": target.health.needs_recovery,
                            "reason": target.health.unsafe_tail_reason,
                            "effects_before": effects_before, "effects_after": effects_after,
                            "effects_unchanged": effects_before == effects_after == ["step3", "step4"],
                            "original_source_sha256": source_hash,
                            "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == source_hash}


async def sc_queued_resume(args):
    """queued_midturn, then the same session resumed by a NEW CLI process (the
    wake path under OBS idle eviction, and every restart/crash resume). Tests
    whether the live rendering of a mid-turn queued message equals the JSONL
    rendering the resumed process rebuilds."""
    sids, _ = await sc_queued_midturn(args)
    await _session(args, ["Reply with the single word: woke."], resume=sids[0])
    return sids, None


async def sc_poisoned_recovery(args):
    import hashlib
    from datetime import datetime, timezone
    from obs_agent.context_jsonl import find_session_jsonl
    from obs_agent.jsonl_fork import fork_session_jsonl
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    await _session(args, ["Reply with the single word: ready.", "Reply with the single word: two."], session_id=sid)
    path = find_session_jsonl(session_id=sid, cwd=cwd)
    target = resolve_safe_jsonl_target(session_id=sid, cwd=cwd)
    poison = {"type": "assistant", "uuid": str(uuid.uuid4()), "parentUuid": target.target_uuid,
              "sessionId": sid, "timestamp": datetime.now(timezone.utc).isoformat(), "isApiErrorMessage": True,
              "message": {"role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": "Prompt is too long"}]}}
    with path.open("a") as handle:
        handle.write(json.dumps(poison) + "\n")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    target = resolve_safe_jsonl_target(session_id=sid, cwd=cwd)
    child = fork_session_jsonl(session_id=sid, target_uuid=target.target_uuid, cwd=cwd)
    await _session(args, ["Reply with the single word: recovered."], resume=child)
    return [sid, child], {"parent": sid, "child": child, "reason": target.health.unsafe_tail_reason,
                         "source_unchanged": hashlib.sha256(path.read_bytes()).hexdigest() == before,
                         "original_source_sha256": before, "synthetic_row_is_test_injection": True}


async def sc_utility_route(args):
    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "utility.txt").write_text("public utility-route fixture\n")
    await _session(args, ["Reply with the single word: ready.",
                         "Run Bash exactly: pwd && python3 -c \"from pathlib import Path; print(Path('utility.txt').read_text())\". Then reply shown.",
                         "Reply with the single word: after."], session_id=sid)
    return [sid], None


SCENARIOS = {
    "baseline": (sc_baseline, "control; must pass"),
    "utility_route": (sc_utility_route, "Bash file-path utility remains on the explicit provider"),
    "reminder_block": (sc_reminder_block, "class A; expect FAIL on unfixed proxy"),
    "queued_midturn": (sc_queued_midturn, "notification splice"),
    "cch_collision": (sc_cch_collision, "billing placeholder collision; expect FAIL on unfixed proxy"),
    "boundary_flip": (sc_boundary_flip, "class B split-system flip-flop; expect FAIL on unfixed proxy"),
    "boundary_marker": (sc_boundary_marker, "exact standalone dynamic marker; expect FAIL on unfixed proxy"),
    "resume_new_proc": (sc_resume_new_proc, "wake/new-process resume (--gap for >5 min)"),
    "recovery": (sc_recovery, "real fork recovery, kill during the first tool call"),
    "recovery_multi": (sc_recovery_multi, "real fork recovery after several tool rounds; expect FAIL"),
    "queued_resume": (sc_queued_resume, "mid-turn queued message then new-process resume"),
    "parallel_resume": (sc_parallel_resume, "parallel tool calls then new-process resume; expect FAIL"),
    "parallel_fork": (sc_parallel_fork, "parallel same-ID resume then verbatim-source fork"),
    "poisoned_recovery": (sc_poisoned_recovery, "test-injected synthetic API-error tail excluded on recovery"),
}


def run(args, name):
    fn, note = SCENARIOS[name]
    since = time.time() - 1
    sids, meta = asyncio.run(fn(args))
    pairs = None
    if name in ("recovery", "recovery_multi", "parallel_fork", "poisoned_recovery"):
        rows_p = _rows(args.log_dir, [meta["parent"]], since)
        time.sleep(3)
        rows_c = _rows(args.log_dir, [meta["child"]], since)
        rows_p = _rows(args.log_dir, [meta["parent"]], since)
        pairs = []
        if rows_p and rows_c:
            pairs.append((rows_p[-1], rows_c[0]))  # the recovery boundary
        pairs += list(zip(rows_c, rows_c[1:]))
    rows, verdicts = evaluate(args.log_dir, sids, since, args.threshold, pairs=pairs)
    scoped_rows = [r for r in pd.read_index(args.log_dir) if r.get("session_id") in set(sids)
                   and r.get("ts", 0) >= since and str(r.get("path", "")).startswith("/v1/messages")
                   and "count_tokens" not in str(r.get("path", ""))]
    route_ok = all(r.get("route") == "cli-proxy" and r.get("model") == args.model for r in scoped_rows) if args.provider == "codex" else True
    utility_ids = []
    if name == "utility_route":
        for row in scoped_rows:
            wire = pd.load_request(row["req_id"], "wire", args.log_dir)
            if "Extract any file paths" in str(wire.get("system")):
                utility_ids.append(row["req_id"])
        route_ok = route_ok and bool(utility_ids)
    meta_ok = not meta or (meta.get("effects_unchanged", True) and meta.get("source_unchanged", True)
                           and meta.get("final_response_preserved", True))
    ok = bool(verdicts) and route_ok and meta_ok and all(v["ok"] for v in verdicts)
    return {"scenario": name, "note": note, "sessions": sids, "meta": meta,
            "provider": args.provider, "model": args.model, "route_ok": route_ok,
            "routes": sorted({r.get("route", "") for r in rows}),
            "requests": [r["req_id"] for r in rows], "pairs": verdicts, "ok": ok,
            "all_scoped_requests": [{"req_id": r["req_id"], "model": r.get("model"), "route": r.get("route"), "http_status": r.get("http_status")} for r in scoped_rows],
            "utility_requests": utility_ids}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--provider", choices=("anthropic", "codex"), default="anthropic",
                    help="explicit codex route uses Sol; legacy Anthropic route stays available")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--log-dir", default=pd.DEFAULT_LOG_DIR)
    ap.add_argument("--gap", type=int, default=0, help="idle seconds before resume_new_proc's resume")
    ap.add_argument("--warm-turns", type=int, default=0, help="turns after parallel tools to put divergence beyond lookback")
    ap.add_argument("--threshold", type=float, default=0.95)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.list or not args.scenarios:
        for k, (_, note) in SCENARIOS.items():
            print(f"{k:16s} {note}")
        return 0
    from obs_agent.config import resolve_model
    args.model = resolve_model(args.model or ("sol" if args.provider == "codex" else DEFAULT_MODEL))
    if args.provider == "codex" and not args.model.startswith("gpt-"):
        ap.error("--provider codex requires an explicit GPT model, not a Claude/local fallback")
    names = list(SCENARIOS) if args.scenarios == ["all"] else args.scenarios
    results = []
    for n in names:
        try:
            res = run(args, n)
        except Exception as e:  # report and continue with the next scenario
            res = {"scenario": n, "ok": False, "error": f"{type(e).__name__}: {e}", "pairs": []}
        results.append(res)
        if args.json:
            continue
        print(f"== {n}: {'PASS' if res['ok'] else 'FAIL'}  sessions={res.get('sessions')}")
        if res.get("error"):
            print("   error:", res["error"])
        for v in res.get("pairs", []):
            flag = "ok " if v["ok"] else "BAD"
            print(f"   {flag} {v['b']} {v['class']}{' NO-BP' if v['no_breakpoint'] else ''} "
                  f"cr={v['cache_read']} prior={v['prior_prompt']} ratio={v['ratio']}")
            if v["diff"]:
                print("      " + v["diff"].replace("\n", "\n      ")[:1200])
    if args.json:
        print(json.dumps(results, indent=1))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
