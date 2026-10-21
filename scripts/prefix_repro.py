#!/usr/bin/env python3
"""Live prompt-cache prefix reproduction drivers (one scenario per miss class).

CACHE-PREFIX: these drivers exist because unit tests "have utterly failed at
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
import re
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
        # obs-22 E3: run the scenario against any CLI binary (default: the SDK's
        # bundled 2.1.59). The OBS-owned replacement binary is qualified by
        # running this whole regression with --cli-path pointing at it.
        **({"cli_path": args.cli_path} if getattr(args, "cli_path", None) else {}),
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


def _initial_user_prompt(prompt, session_id=None):
    # Independent roots must not share the model+first-message cache/pin namespace.
    return f"{prompt}\n\n[cache-regression fixture: {session_id or uuid.uuid4()}]"


async def _session(args, prompts, **kw):
    async with ClaudeSDKClient(options=_options(args, **kw)) as client:
        for i, p in enumerate(prompts):
            if i == 0 and not kw.get("resume"):
                p = _initial_user_prompt(p, kw.get("session_id"))
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
    kept = []
    for r in out:
        if ((r.get("sizes") or {}).get("pre") or 0) < 20000:
            continue
        wire = pd.load_request(r["req_id"], "wire", log_dir)
        if not wire.get("tools"):
            continue
        msgs = wire.get("messages") or []
        r["_first"] = json.dumps(pd.canon(msgs[0]), sort_keys=True) if msgs else ""
        r["_n_msgs"] = len(msgs)
        kept.append(r)
    # keep the scenario's main model only
    by = {}
    for r in kept:
        by.setdefault(r["model"], []).append(r)
    if not by:
        return []
    main = max(by, key=lambda m: len(by[m]))
    # Drop utility side calls of the same model (on the GPT route the CLI's
    # "extract file paths"-style helpers share the session id AND the model since
    # acee2c4). Every request of one conversation starts with the same first
    # message; a helper call starts with its own prompt and is SHORT. A request
    # is dropped only if both hold (first message differs from the session's
    # most common one AND it has no more messages than the conversation already
    # had), so a real break of the first message on resume (which always adds
    # messages) is never hidden.
    # Without this filter the harness paired side calls with the conversation
    # and reported false config-changed FAILs (E1 fixer round F4, 2026-10-12).
    from collections import Counter
    per_session = {}
    for r in by[main]:
        per_session.setdefault(r.get("session_id"), Counter())[r["_first"]] += 1
    major = {sid: c.most_common(1)[0][0] for sid, c in per_session.items()}
    result, max_n = [], {}
    for r in by[main]:
        sid = r.get("session_id")
        if r["_first"] != major[sid] and r["_n_msgs"] <= max_n.get(sid, 0):
            continue
        max_n[sid] = max(max_n.get(sid, 0), r["_n_msgs"])
        result.append(r)
    return result


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
        await _turn(client, _initial_user_prompt("Reply with the single word: ready.", sid))
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
    # The fleet's cache entries live 1 hour, the harness's only 5 minutes: the
    # CLI asks for a 1-hour TTL only on a subscription login AND when the
    # GrowthBook allowlist tengu_prompt_cache_1h_config matches, and
    # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC (set by _options) turns GrowthBook
    # off. So for a real wake test (--gap > 300) the CLI runs like the fleet:
    # GrowthBook on. Verified 2026-10-12: with it off, even Opus wrote only
    # ephemeral_5m and a 330 s wake read 0 despite byte-identical history.
    env = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": ""} if args.gap > 290 else None
    await _session(args, ["Reply with the single word: one.", "Reply with the single word: two."],
                   session_id=sid, env_extra=env)
    if args.gap:
        await asyncio.sleep(args.gap)
    await _session(args, ["Reply with the single word: woke."], resume=sid, env_extra=env)
    return [sid], None


async def sc_recovery(args):
    from obs_agent.jsonl_fork import fork_session_jsonl
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    client = ClaudeSDKClient(options=_options(args, session_id=sid))
    await client.connect()
    try:
        await _turn(client, _initial_user_prompt("Reply with the single word: ready.", sid))
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
        await _turn(client, _initial_user_prompt("Reply with the single word: ready.", sid))
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


async def sc_recovered_poison(args):
    """vault-1bc2: a mid-session API-error row the session already recovered
    from must not pull a later tail recovery back before it. A turn capped by
    CLAUDE_CODE_MAX_OUTPUT_TOKENS makes the real CLI write a synthetic
    "exceeded the output token maximum" row and continue the same turn with a
    real answer chained to it (as prod a00b512b line 79 chains to a timeout).
    A test-injected tail poison then forces a recovery. The old analyzer
    recovered to the row before the first poison (history-changed, re-bill);
    the fixed one keeps the continuation (append). Anthropic only: on the GPT
    route the cap produces no synthetic row (2026-10-17 gate run), and the
    analyzer under test is provider-independent; GPT recovery forks stay
    covered by recovery, recovery_multi, poisoned_recovery and resumed_poison."""
    import hashlib
    from datetime import datetime, timezone
    from obs_agent.context_jsonl import find_session_jsonl
    from obs_agent.jsonl_fork import fork_session_jsonl
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    await _session(args, ["Reply with the single word: ready."], session_id=sid)
    await _session(args, ["Write the numbers from one to four hundred in English words, comma separated, nothing else.",
                          "Reply with the single word: three."],
                   resume=sid, env_extra={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1500"})
    # An uncapped process for the last parent request, so the recovery pair
    # compares like with like (the cap changes the request's thinking budget).
    await _session(args, ["Reply with the single word: four."], resume=sid)
    path = find_session_jsonl(session_id=sid, cwd=cwd)
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    poisons = [r["uuid"] for r in rows if r.get("isApiErrorMessage") and r.get("uuid")]
    if not poisons:
        raise RuntimeError("recovered_poison: the capped turn produced no synthetic error row")
    first_poison = poisons[0]
    chained = [r.get("type") for r in rows if r.get("parentUuid") == first_poison]
    head = resolve_safe_jsonl_target(session_id=sid, cwd=cwd).target_uuid
    tail = {"type": "assistant", "uuid": str(uuid.uuid4()), "parentUuid": head,
            "sessionId": sid, "timestamp": datetime.now(timezone.utc).isoformat(), "isApiErrorMessage": True,
            "message": {"role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": "Request timed out"}]}}
    with path.open("a") as handle:
        handle.write(json.dumps(tail) + "\n")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    target = resolve_safe_jsonl_target(session_id=sid, cwd=cwd)
    lines = target.health.uuid_line_numbers
    child = fork_session_jsonl(session_id=sid, target_uuid=target.target_uuid, cwd=cwd)
    await _session(args, ["Reply with the single word: recovered."], resume=child)
    return [sid, child], {"parent": sid, "child": child, "reason": target.health.unsafe_tail_reason,
                         "cli_rows_chained_to_poison": chained,
                         "recovery_kept_continuation": lines.get(target.target_uuid, 0) > lines[first_poison],
                         "source_unchanged": hashlib.sha256(path.read_bytes()).hexdigest() == before,
                         "original_source_sha256": before, "tail_poison_is_test_injection": True}


async def sc_resumed_poison(args):
    """vault-1bc2 on both routes (G1 verifier F2): the production shape of
    a00b512b line 79 / be9169ea, where a persistent client's next turn chained
    to a synthetic "Request timed out" row and was answered. No trigger makes
    the GPT route write such a row on demand (the output cap does not, and
    API_TIMEOUT_MS=6000 did not time out on either route, 2026-10-17), so the
    three rows of that shape are test injections: the synthetic row, the user
    row chained to it and its answer. Everything after is real: resumed turns
    whose history carries the synthetic row through the route's translation,
    a test-injected tail poison, the recovery fork and its resumed turn. The
    recovery must keep the continuation (append); the old analyzer refuses the
    resume (poisoned tail) or recovers before the first poison."""
    import hashlib
    from datetime import datetime, timezone
    from obs_agent.context_jsonl import find_session_jsonl
    from obs_agent.jsonl_fork import fork_session_jsonl
    from obs_agent.jsonl_health import resolve_safe_jsonl_target

    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"

    def row(kind, parent, content, **extra):
        now = datetime.now(timezone.utc).isoformat()
        out = {"type": kind, "uuid": str(uuid.uuid4()), "parentUuid": parent, "sessionId": sid,
               "timestamp": now, "message": {"role": kind, "content": content}}
        for key, value in extra.items():
            (out["message"] if key in ("model", "stop_reason", "id") else out)[key] = value
        return out

    def synthetic(parent):
        return row("assistant", parent, [{"type": "text", "text": "Request timed out"}],
                   isApiErrorMessage=True, model="<synthetic>", stop_reason="stop_sequence")

    await _session(args, ["Reply with the single word: ready.", "Reply with the single word: two."], session_id=sid)
    path = find_session_jsonl(session_id=sid, cwd=cwd)
    mid = synthetic(resolve_safe_jsonl_target(session_id=sid, cwd=cwd).target_uuid)
    asked = row("user", mid["uuid"], "Reply with the single word: again.")
    answered = row("assistant", asked["uuid"], [{"type": "text", "text": "again"}],
                   model=args.model or "claude-haiku-4-5", stop_reason="end_turn", id=f"msg_{uuid.uuid4().hex}")
    with path.open("a") as handle:
        for injected in (mid, asked, answered):
            handle.write(json.dumps(injected) + "\n")
    await _session(args, ["Reply with the single word: three.", "Reply with the single word: four."], resume=sid)
    tail = synthetic(resolve_safe_jsonl_target(session_id=sid, cwd=cwd).target_uuid)
    with path.open("a") as handle:
        handle.write(json.dumps(tail) + "\n")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    target = resolve_safe_jsonl_target(session_id=sid, cwd=cwd)
    lines = target.health.uuid_line_numbers
    child = fork_session_jsonl(session_id=sid, target_uuid=target.target_uuid, cwd=cwd)
    await _session(args, ["Reply with the single word: recovered."], resume=child)
    return [sid, child], {"parent": sid, "child": child, "reason": target.health.unsafe_tail_reason,
                         "recovery_kept_continuation": lines.get(target.target_uuid, 0) > lines[mid["uuid"]],
                         "source_unchanged": hashlib.sha256(path.read_bytes()).hexdigest() == before,
                         "original_source_sha256": before,
                         "test_injections": ["mid synthetic row", "user row chained to it", "its answer", "tail poison"]}


async def sc_utility_route(args):
    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "utility.txt").write_text("public utility-route fixture\n")
    await _session(args, ["Reply with the single word: ready.",
                         "Run Bash exactly: pwd && python3 -c \"from pathlib import Path; print(Path('utility.txt').read_text())\". Then reply shown.",
                         "Reply with the single word: after."], session_id=sid)
    return [sid], None


async def sc_empty_result_resume(args):
    sid = str(uuid.uuid4())
    cwd = WORK_ROOT / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    await _session(args, ["Reply with the single word: ready.",
                         "Run Bash exactly: python3 -c 'pass'. Then reply with the single word: done."]
                   + [f"Reply with the single word: warm{i}." for i in range(args.warm_turns)], session_id=sid)
    await _session(args, ["Reply with the single word: resumed."], resume=sid)
    return [sid], None


REMINDER_TAG = "system" + "-reminder"  # built at runtime: agents reading this file see it intact


def _direct_probe(args, sid, since, variants, chain=False):
    """Send hand-built variants of one real CLI request through the proxy under test.

    The CLI cannot be made to produce some live shapes on demand (a reminder
    spliced INTO a tool result; a server-side flag flip). So the scenario first
    drives the real CLI through the proxy, takes the exact body the CLI sent
    (the proxy's "pre" log), and then sends variants of that body directly with
    the CLI's own OAuth token. Every variant goes through the proxy exactly like
    CLI traffic. Verdict per later variant vs the first (vs the previous one with
    chain=True): the bytes the proxy sent upstream must be identical/append
    (prefix_diff) and the cache read must be >= threshold of that variant's prompt.
    """
    import urllib.request
    time.sleep(3)
    rows = [r for r in pd.read_index(args.log_dir) if r.get("session_id") == sid
            and r.get("ts", 0) >= since and r.get("http_status") == 200
            and str(r.get("path", "")).startswith("/v1/messages")
            and ((r.get("sizes") or {}).get("pre") or 0) >= 20000]
    if not rows:
        raise RuntimeError("no captured CLI request to build variants from")
    base_row = rows[-1]
    base = pd.load_request(base_row["req_id"], "pre", args.log_dir)
    headers = {k: v for k, v in (base_row.get("client_headers") or {}).items()
               if k.lower() in ("anthropic-beta", "anthropic-version", "user-agent", "x-app")}
    creds = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())
    headers["authorization"] = "Bearer " + creds["claudeAiOauth"]["accessToken"]
    headers["content-type"] = "application/json"
    if "context-management-2025-06-27" not in headers.get("anthropic-beta", ""):
        headers["anthropic-beta"] = ",".join(
            [p for p in [headers.get("anthropic-beta", ""), "context-management-2025-06-27"] if p])
    nonce = f"direct probe {uuid.uuid4()}"
    t_start = time.time()
    usages = []
    for name, build in variants:
        b = json.loads(json.dumps(base))
        b["stream"] = False
        b.pop("temperature", None)
        b["messages"][0]["content"].insert(0, {"type": "text", "text": nonce})
        # earlier thinking blocks carry signatures bound to the original
        # context; the nonce invalidates them, and they are not under test
        for m in b["messages"]:
            if m.get("role") == "assistant" and isinstance(m.get("content"), list):
                m["content"] = [c for c in m["content"]
                                if c.get("type") not in ("thinking", "redacted_thinking")] or m["content"]
        build(b)
        req = urllib.request.Request(args.base_url + "/v1/messages",
                                     data=json.dumps(b).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=300) as resp:
            usages.append((name, json.loads(resp.read()).get("usage") or {}))
        time.sleep(2)
    time.sleep(3)
    sent = sorted([r for r in pd.read_index(args.log_dir) if r.get("session_id") == sid
                   and r.get("ts", 0) >= t_start], key=lambda r: r["ts"])
    if len(sent) < len(variants):
        raise RuntimeError(f"only {len(sent)} of {len(variants)} probe requests in the proxy log")
    def prompt_total(usage):
        return sum(usage.get(k) or 0 for k in
                   ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens"))

    first = sent[0]
    A = pd.load_request(first["req_id"], "wire", args.log_dir)
    total_a = prompt_total(usages[0][1])
    a_name = usages[0][0]
    verdicts = []
    for (name, usage), row in zip(usages[1:], sent[1:]):
        B = pd.load_request(row["req_id"], "wire", args.log_dir)
        res = pd.diff_requests(A, B, pd._beta(first), pd._beta(row))
        text = pd.format_result(res)
        cls = text.split()[0]
        cr = usage.get("cache_read_input_tokens") or 0
        ratio = cr / total_a if total_a else 0.0
        ok = cls in ("identical", "append", "tail-block-append") and ratio >= args.threshold
        verdicts.append({"a": f"{a_name} {first['req_id']}", "b": f"{name} {row['req_id']}",
                         "class": cls, "no_breakpoint": False, "cache_read": cr,
                         "prior_prompt": total_a, "ratio": round(ratio, 4), "ok": ok,
                         "diff": text if not ok else ""})
        if chain:  # compare each variant with the one before it (a growing conversation)
            first, A, total_a, a_name = row, B, prompt_total(usage), name
    return {"direct_usage": dict(usages), "direct_verdicts": verdicts}


async def sc_ws_resume(args):
    """Whitespace-edged tool output that carried a reminder in the live request,
    replayed from JSONL on resume (the 2026-10-12 break, requests 598bb917 and
    e5f25ac9). Live, the CLI trims a tool output and splices a reminder into
    it (its periodic task reminder does this); the JSONL replay has the
    untrimmed output and no reminder. A real CLI session produces a tool result
    with leading spaces, then the probe sends the live form (trimmed + spliced
    reminder) and the replay form (untrimmed) of that exact request. The proxy
    must make them byte-identical."""
    sid = str(uuid.uuid4())
    since = time.time() - 1
    await _session(args, [
        "Reply with the single word: ready.",
        "Run the Bash command `printf '      indented output\\nsecond line\\n'` "
        "and then reply with the single word: shown.",
        "Reply with the single word: after.",
    ], session_id=sid)

    def tool_results(b):
        return [blk for m in b["messages"] if m["role"] == "user" and isinstance(m["content"], list)
                for blk in m["content"] if blk.get("type") == "tool_result"
                and isinstance(blk.get("content"), str) and blk["content"].startswith(" ")]

    def live(b):
        blocks = tool_results(b)
        if not blocks:
            raise RuntimeError("ws_resume: no whitespace-led tool result captured")
        for blk in blocks:
            blk["content"] = (blk["content"].strip() + f"\n\n<{REMINDER_TAG}>\nprefix_repro "
                              f"ws_resume periodic reminder\n</{REMINDER_TAG}>")

    def replay(b):
        if not tool_results(b):
            raise RuntimeError("ws_resume: no whitespace-led tool result captured")

    meta = _direct_probe(args, sid, since, [("live", live), ("replay", replay)])
    return [sid], meta


async def sc_volatile_flip(args):
    """Server-driven prompt variant flip inside one conversation (2026-10-09
    09:28Z and 2026-10-12 15:21Z episodes): the "# Output efficiency" system
    section (probably GrowthBook flag tengu_swann_brevity, cached in the fleet's
    shared ~/.claude.json -- a hypothesis from reading the bundled CLI, not
    observed end-to-end) and the context_management field appear/disappear between
    two requests of the same conversation. The CLI cannot be flipped on demand
    without touching the fleet's shared config, so this probe sends three
    variants of one real CLI request (A: section + context_management; B: both
    removed; C: both back). The proxy must send B and C upstream exactly as A.
    Since ops-30 (2026-10-16 04:10Z, ~3.7M re-billed) the variants also carry
    the "# MCP Server Instructions" section the CLI rebuilds from the MCP
    servers connected in each process, placed as observed (before gitStatus):
    A has it, B drops it (server not connected), C has it, D has different
    instructions text. B, C and D must all go upstream exactly as A."""
    if args.provider != "anthropic":
        raise RuntimeError("volatile_flip is an Anthropic-route scenario")
    sid = str(uuid.uuid4())
    since = time.time() - 1
    await _session(args, ["Reply with the single word: ready."], session_id=sid)
    oe = ("# Output efficiency\n\nIMPORTANT: Go straight to the point. Be extra concise.\n\n"
          "Keep your text output brief and direct.\n\n")

    mcp = ("# MCP Server Instructions\n\nThe following MCP servers have provided instructions "
           "for how to use their tools and resources:\n\n## exa\nOnly use tools returned by "
           "tools/list.\n\n")

    def with_section(on, mcp_text=mcp):
        def build(b):
            b["max_tokens"] = 2048
            b["thinking"] = {"type": "enabled", "budget_tokens": 1024}
            sys2 = b["system"][2]
            # drop any section the harness CLI's own MCP servers produced, so
            # every variant carries exactly the one under test
            t = re.sub(r"(?ms)^# MCP Server Instructions\n.*?(?:\n\n(?=# |gitStatus: )|\Z)",
                       "", sys2["text"])
            cut = t.rfind("\n# ")  # insert before the last top-level heading
            if on:
                t = t[:cut + 1] + oe + t[cut + 1:] if cut >= 0 else t + "\n\n" + oe
                b["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
            if mcp_text:  # as the CLI places it: before gitStatus, else before the last heading
                g = t.find("\ngitStatus: ")
                cut = g if g >= 0 else t.rfind("\n# ")
                t = t[:cut + 1] + mcp_text + t[cut + 1:] if cut >= 0 else t + "\n\n" + mcp_text
            sys2["text"] = t
        return build

    mcp_d = mcp.replace("## exa\n", "## exa\nInstructions changed on reconnect.\n")
    meta = _direct_probe(args, sid, since, [("A-section-on", with_section(True)),
                                           ("B-section-off", with_section(False, None)),
                                           ("C-section-on", with_section(True)),
                                           ("D-mcp-changed", with_section(True, mcp_d))])
    return [sid], meta


_TTL_PAD = "Padding so the conversation, not the system prompt, dominates the prompt. " * 120


def _ttl_variant(ttl, turns):
    """Build a variant: every breakpoint at ``ttl``; ``turns`` assistant/user
    pairs (each user turn ~2k tokens) appended, breakpoint on the last block,
    as the CLI places it."""
    def build(b):
        for m in b["messages"]:
            for blk in (m["content"] if isinstance(m.get("content"), list) else []):
                blk.pop("cache_control", None)
        for k in range(turns):
            b["messages"].append({"role": "assistant", "content": [{"type": "text", "text": f"ok {k}"}]})
            b["messages"].append({"role": "user", "content": [
                {"type": "text", "text": f"turn {k}. {_TTL_PAD}Reply with the single word: ok."}]})
        last = b["messages"][-1]["content"]
        if isinstance(last, str):
            b["messages"][-1]["content"] = last = [{"type": "text", "text": last}]
        last[-1]["cache_control"] = {"type": "ephemeral"}
        for blk in (b.get("system") or []) + [x for m in b["messages"] for x in
                                             (m["content"] if isinstance(m.get("content"), list) else [])]:
            cc = blk.get("cache_control") if isinstance(blk, dict) else None
            if isinstance(cc, dict):
                rest = {k: v for k, v in cc.items() if k not in ("type", "ttl")}
                cc.clear()
                cc["type"] = "ephemeral"
                if ttl == "1h":
                    cc["ttl"] = "1h"
                cc.update(rest)
    return build


async def sc_ttl_flip(args):
    """The 2026-10-20 break (vault-h6yt, session 5eb20a42): the CLI chooses the
    breakpoint TTL once per process (1h only when the GrowthBook allowlist it
    latched lists the query source; it reads that from the fleet's shared,
    non-atomically rewritten ~/.claude.json). After a restart one conversation
    ran at 5m for a whole process, then at 1h again after the next restart. A
    1h request cannot read 5m entries, and the old 1h entries were too far back
    for the provider's ~20-block lookback, so a pure byte append re-billed
    ~0.72M tokens. The probe replays that: A at 1h, twelve growing 5m requests,
    then a 1h request; each compared with the one before. The proxy must send
    every one upstream at 1h."""
    if args.provider != "anthropic":
        raise RuntimeError("ttl_flip is an Anthropic-route scenario")
    sid = str(uuid.uuid4())
    since = time.time() - 1
    await _session(args, ["Reply with the single word: ready."], session_id=sid)
    variants = [("A-1h", _ttl_variant("1h", 0))]
    variants += [(f"B{k}-5m", _ttl_variant("5m", k)) for k in range(1, 13)]
    variants += [("C-1h", _ttl_variant("1h", 13))]
    meta = _direct_probe(args, sid, since, variants, chain=True)
    return [sid], meta


async def sc_ttl_flip_down(args):
    """The other direction (vault-h6yt): a conversation running at 5m whose next
    process asks for 1h while its 5m entries are still warm (the 08:10:53Z
    request after a 118 s restart gap). A 1h request reads nothing written at
    5m, so the proxy must keep it at 5m until the conversation goes idle."""
    if args.provider != "anthropic":
        raise RuntimeError("ttl_flip_down is an Anthropic-route scenario")
    sid = str(uuid.uuid4())
    since = time.time() - 1
    await _session(args, ["Reply with the single word: ready."], session_id=sid)
    meta = _direct_probe(args, sid, since, [("A-5m", _ttl_variant("5m", 1)),
                                           ("B-1h", _ttl_variant("1h", 2)),
                                           ("C-1h", _ttl_variant("1h", 3))], chain=True)
    return [sid], meta


SCENARIOS = {
    "baseline": (sc_baseline, "control; must pass"),
    "utility_route": (sc_utility_route, "Bash file-path utility remains on the explicit provider"),
    "empty_result_resume": (sc_empty_result_resume, "no-output Bash tool result then new-process replay"),
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
    "recovered_poison": (sc_recovered_poison, "mid-session API error already recovered from; tail recovery keeps the continuation (vault-1bc2)"),
    "resumed_poison": (sc_resumed_poison, "resumed turn chained to an injected API-error row; tail recovery keeps it, both routes (vault-1bc2)"),
    "ws_resume": (sc_ws_resume, "whitespace-edged tool output + spliced reminder, then new-process resume"),
    "volatile_flip": (sc_volatile_flip, "server flag flips system section/context_management mid-conversation (Anthropic)"),
    "ttl_flip": (sc_ttl_flip, "CLI process at 5m inside a 1h conversation, then 1h again (Anthropic, vault-h6yt)"),
    "ttl_flip_down": (sc_ttl_flip_down, "warm 5m conversation whose next process asks for 1h (Anthropic, vault-h6yt)"),
}


def _provider_errors(log_dir, since):
    """5xx responses the proxy logged since *since* (any session)."""
    return [{"req_id": r["req_id"], "http_status": r.get("http_status"), "route": r.get("route")}
            for r in pd.read_index(log_dir)
            if r.get("ts", 0) >= since and (r.get("http_status") or 0) >= 500]


ANTHROPIC_ONLY = {"volatile_flip", "ws_resume", "recovered_poison", "ttl_flip", "ttl_flip_down"}


def run(args, name):
    fn, note = SCENARIOS[name]
    since = time.time() - 1
    try:
        sids, meta = asyncio.run(asyncio.wait_for(fn(args), timeout=args.scenario_timeout))
    except BaseException as e:  # timeout or driver error: say whether the provider failed
        if isinstance(e, KeyboardInterrupt):
            raise
        errs = _provider_errors(args.log_dir, since)
        return {"scenario": name, "note": note, "ok": False, "pairs": [],
                "status": "PROVIDER-ERROR" if errs else "ERROR",
                "error": f"{type(e).__name__}: {e}",
                "provider_errors": errs[:20]}
    pairs = None
    if name in ("recovery", "recovery_multi", "parallel_fork", "poisoned_recovery", "recovered_poison",
                "resumed_poison"):
        rows_p = _rows(args.log_dir, [meta["parent"]], since)
        time.sleep(3)
        rows_c = _rows(args.log_dir, [meta["child"]], since)
        rows_p = _rows(args.log_dir, [meta["parent"]], since)
        pairs = []
        if rows_p and rows_c:
            pairs.append((rows_p[-1], rows_c[0]))  # the recovery boundary
        pairs += list(zip(rows_c, rows_c[1:]))
    if meta and "direct_verdicts" in meta:  # volatile_flip: verdicts come from direct calls
        rows, verdicts = _rows(args.log_dir, sids, since), meta["direct_verdicts"]
    else:
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
    errs = [{"req_id": r["req_id"], "http_status": r.get("http_status")} for r in scoped_rows
            if (r.get("http_status") or 0) >= 500]
    # A FAIL whose sessions also hit 5xx upstream errors is reported separately:
    # it may be provider overload, not a cache-prefix regression. Read the pairs.
    status = "PASS" if ok else ("PROVIDER-ERROR" if errs else "FAIL")
    return {"scenario": name, "note": note, "sessions": sids, "meta": meta, "status": status,
            "provider_errors": errs,
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
    ap.add_argument("--cli-path", default=None,
                    help="CLI binary the SDK launches (default: the SDK's bundled Claude Code 2.1.59)")
    ap.add_argument("--gap", type=int, default=0, help="idle seconds before resume_new_proc's resume")
    ap.add_argument("--warm-turns", type=int, default=0, help="turns after parallel tools to put divergence beyond lookback")
    ap.add_argument("--threshold", type=float, default=0.95)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--json-out", default=None,
                    help="also write the results list to this file after EVERY scenario "
                         "(a killed run keeps what finished)")
    ap.add_argument("--scenario-timeout", type=int, default=900,
                    help="seconds per scenario before it is stopped and reported "
                         "(PROVIDER-ERROR if the proxy logged 5xx upstream errors meanwhile)")
    args = ap.parse_args(argv)
    if args.cli_path:
        cli = Path(args.cli_path)
        if not (cli.is_file() and os.access(cli, os.X_OK)):
            ap.error(f"--cli-path {cli} is not an executable file")
        args.cli_path = str(cli.resolve())
    if args.list or not args.scenarios:
        for k, (_, note) in SCENARIOS.items():
            print(f"{k:16s} {note}")
        return 0
    from obs_agent.config import resolve_model
    args.model = resolve_model(args.model or ("sol" if args.provider == "codex" else DEFAULT_MODEL))
    if args.provider == "codex" and not args.model.startswith("gpt-"):
        ap.error("--provider codex requires an explicit GPT model, not a Claude/local fallback")
    if args.scenarios == ["all"]:
        names = [k for k in SCENARIOS if args.provider == "anthropic" or k not in ANTHROPIC_ONLY]
    else:
        names = args.scenarios
    if args.gap > 290:
        print("NOTE: --gap > 300 needs 1-hour cache entries. resume_new_proc then runs the CLI with "
              "GrowthBook on (like the fleet); check that the first requests' usage shows "
              "ephemeral_1h_input_tokens > 0 — if only ephemeral_5m is written, a zero read after the "
              "gap is cache expiry, not a prefix break (read the pair class instead).", file=sys.stderr)
    results = []
    for n in names:
        try:
            res = run(args, n)
        except Exception as e:  # report and continue with the next scenario
            res = {"scenario": n, "ok": False, "error": f"{type(e).__name__}: {e}", "pairs": []}
        results.append(res)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(results, indent=1))
        if args.json:
            continue
        label = res.get("status") or ("PASS" if res["ok"] else "FAIL")
        print(f"== {n}: {label}  sessions={res.get('sessions')}", flush=True)
        if res.get("provider_errors"):
            print(f"   provider 5xx during scenario: {len(res['provider_errors'])} "
                  f"(e.g. {res['provider_errors'][0]['req_id']})")
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
