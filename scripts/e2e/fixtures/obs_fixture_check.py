#!/usr/bin/env python3
"""obs-fixture-check: does a CLI binary resume real sessions byte-continuously? (obs-22 E3, C2)

For every fixture case (see build_fixtures.py) the checker:
  1. copies the case's real 2.1.59 ``session.jsonl`` into a scratch Claude
     config dir, applies OBS's own resume preparation (prepare_session_replay),
  2. launches the candidate binary through the official Python SDK the way OBS
     does (``cli_path``, ``--resume``), sends one fixed continuation message,
  3. captures the first model request the candidate sends. The request goes
     through a scratch copy of the repo's cache proxy (identical normalization,
     pins and redaction) whose upstreams are the no-spend capture endpoint, so
     nothing reaches a provider,
  4. compares that request with the logged request of the fixture.

Verdict per case (cache-prefix semantics, scripts/prefix_diff.py):
  PASS  the captured post-proxy body ("wire") extends the logged wire body:
        class ``append`` (or ``tail-block-append``) -- same tools, same system,
        every logged message identical, new messages only after them.
  FAIL  anything else; the report names the first divergence with a JSON path
        and byte offset (``first_divergence``) and the class.
With ``--require-pre`` (the gate for the OBS-owned binary, design-v2 §1.5.1,
§1.12, §10.1), three more checks must pass:
  serialization        the candidate's own bytes ("pre") are exactly Python's
                       ``json.dumps(body, separators=(",", ":"))`` form
                       (ensure_ascii): no raw UTF-8, no other spacing/escaping.
                       2.1.59 fails this on every main request (its system
                       prompt and tool texts hold non-ASCII); that is expected.
  expected transforms  the scratch proxy's index row shows only the expected
                       transforms (metadata suffix, GPT cache key): every other
                       norm counter 0, no pin applied (§1.12 field list).
  manifest             when the case has ``expected_manifest.json``, the
                       candidate wrote a sidecar manifest that equals it under
                       the §10.3 equality rules.
``--report-pre`` reports all three (and the old pre-vs-wire prefix compare)
without requiring them.

Compaction (cases with ``meta.compaction``, tag ``compact-anthropic``): the
case's ``session_precompact.jsonl`` is resumed with the capture's lowered
auto-compact threshold and its exact next prompt. The candidate's compaction
request must be byte-identical to the logged one, and its first
post-compaction request may differ from the logged one only inside
messages[0] (the summary text: the capture endpoint answers "ok").

``--reverse`` (ADR-0009): after the forward run the session file holds rows the
candidate wrote. The reverse step resumes that file under ``--reverse-cli``
(default: the pinned 2.1.59) with a second continuation and asserts its request
extends the candidate's forward request. A candidate whose sessions 2.1.59 can
not continue breaks rollback.

Calibration: ``--cli-path`` set to the pinned 2.1.59 must PASS every case. A
case it can not pass is a checker or fixture defect, not a binary defect.

Example:
  .venv/bin/python scripts/e2e/fixtures/obs_fixture_check.py \\
      --cli-path /workspace/runtime/reference/claude-code-src/pinned-2.1.59/claude \\
      --fixtures /workspace/runtime/e2e/fixtures/byte-resume --out /workspace/runtime/e2e/fixtures/_checks/X --reverse

Nothing here spends model credit. Output under --out: report.json (per case),
proxy/ (scratch proxy log), capture/ (raw upstream requests), cases/ (scratch
config dirs). Captured bodies contain conversation text: never commit them.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import prefix_diff as pd  # noqa: E402
import capture_endpoint  # noqa: E402
from obs_agent.jsonl_replay import prepare_session_replay  # noqa: E402  OBS's own resume preparation

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

from obs_agent.session import _DEFAULT_SDK_ENV as OBS_DEFAULT_SDK_ENV  # noqa: E402  the env every OBS session gets

ORIG_SHELL = os.environ.get("SHELL")
PINNED = "/workspace/runtime/reference/claude-code-src/pinned-2.1.59/claude"
FROZEN_PINS = Path("/workspace/runtime/preserved/cache-proxy-request-log-freeze-20261020T200401Z/state/effort-pins.json")
CONTINUATION = "Fixture continuation {case}: reply with the single word ok."
OK_CLASSES = ("append", "tail-block-append")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sanitize(path: str) -> str:
    """Claude Code's project-dir name for a cwd (every non-alphanumeric -> '-')."""
    return "".join(c if c.isalnum() else "-" for c in path)


def seed_pins(dst: Path) -> None:
    """Copy the frozen pin store with every last-used time refreshed.

    The proxy prunes pins idle for 2 days (VARIANT_PIN_TTL) when it saves; the
    fixtures replay conversations from 2026-10-10..20, so their pins must look
    fresh or the scratch proxy would forget the logged variants and TTLs.
    """
    if not FROZEN_PINS.exists():
        return
    data = json.loads(FROZEN_PINS.read_text())
    now = time.time()
    for table in ("bases", "anchors", "variants", "ttls"):
        for v in (data.get(table) or {}).values():
            if isinstance(v, list) and len(v) >= 2:
                v[1] = now
    (dst / "effort-pins.json").write_text(json.dumps(data))


class Env:
    """Capture endpoint + scratch proxy pointed at it."""

    def __init__(self, out: Path):
        self.out = out
        self.capture_dir = out / "capture"
        self.proxy_dir = out / "proxy"
        self.proxy_dir.mkdir(parents=True, exist_ok=True)
        seed_pins(self.proxy_dir)
        self.stub_port = free_port()
        self.stub = capture_endpoint.serve(self.stub_port, self.capture_dir)
        self.proxy_port = free_port()
        env = dict(os.environ, CACHE_PROXY_LOG_DIR=str(self.proxy_dir), CACHE_PROXY_REQUEST_LOG="1",
                   CACHE_PROXY_IDLE_EXIT_SECONDS="1800")
        self.proxy_log = open(self.proxy_dir / "proxy.out", "w")
        self.proxy = subprocess.Popen(
            [sys.executable, str(HERE / "stub_proxy.py"), str(self.proxy_port), str(self.stub_port)],
            env=env, stdout=self.proxy_log, stderr=subprocess.STDOUT)
        self.base_url = f"http://127.0.0.1:{self.proxy_port}"
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base_url + "/health", timeout=2).read()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("scratch proxy did not come up; see " + str(self.proxy_dir / "proxy.out"))
        self.log_dir = str(self.proxy_dir / "requests")

    def close(self):
        self.proxy.terminate()  # our own child, by PID
        try:
            self.proxy.wait(10)
        except subprocess.TimeoutExpired:
            self.proxy.kill()
        self.stub.shutdown()


def replay_mcp_servers(logged_tools: list, only: set[str] | None = None) -> dict:
    """In-process MCP servers that list exactly the logged MCP tool definitions.

    The real servers (OBS's obs-agent tools, the remote exa server) may have
    changed since the request was logged; a replay server serves the logged
    name/description/schema so the candidate's tool list can be compared.
    """
    servers: dict[str, list] = {}
    for t in logged_tools:
        name = t.get("name", "")
        if not name.startswith("mcp__"):
            continue
        _, server, short = name.split("__", 2)
        if only is not None and server not in only:
            continue
        schema = t.get("input_schema") or {"type": "object", "properties": {}}

        def make(short=short, desc=t.get("description", ""), schema=schema):
            @tool(short, desc, schema)
            async def handler(args):
                return {"content": [{"type": "text", "text": "fixture replay server"}]}
            return handler
        servers.setdefault(server, []).append(make())
    return {s: create_sdk_mcp_server(name=s, version="1.0.0", tools=tools) for s, tools in servers.items()}


def launch_options(meta: dict, wire: dict, *, cli_path: str, base_url: str, config_dir: Path,
                   cwd: str, resume: str, extra_env: dict | None = None) -> ClaudeAgentOptions:
    model = meta["model"]
    beta = meta.get("anthropic_beta") or ""
    env = {} if meta.get("profile") == "harness" else dict(OBS_DEFAULT_SDK_ENV)
    env.update({
        "ANTHROPIC_BASE_URL": base_url,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        # no real credential is ever given to a fixture run: the capture
        # endpoint accepts any bearer token
        "CLAUDE_CODE_OAUTH_TOKEN": "fixture-capture-not-a-credential",
        "ENABLE_TOOL_SEARCH": "false",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_EAGER_FLUSH": "1",
    })
    bi = meta.get("bootstrap_inputs")
    if bi:
        # design-v2 §6.1: the binary under test bootstraps legacy manifests from
        # these (read-only); 2.1.59 ignores them
        env.update({"OBS_CLI_BOOTSTRAP_INDEX_DIRS": ":".join(bi["index_dirs"]),
                    "OBS_CLI_BOOTSTRAP_PINS": bi["pins"], "OBS_CLI_CLOCK_OVERRIDE": str(bi["now"])})
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        os.environ.pop(k, None)
    kw: dict = {}
    if meta.get("profile") == "harness":
        # scripts/prefix_repro.py _options(), run from an agent shell (SHELL set)
        if ORIG_SHELL:
            os.environ["SHELL"] = ORIG_SHELL
        env.update({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_FORCE_GLOBAL_CACHE": "0"})
        if meta.get("route") == "cli-proxy":
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = model
        kw.update(setting_sources=[], allowed_tools=["Bash", "Read"], cli_model=model)
    else:
        # src/obs_agent/session.py ClaudeAgentOptions(): project settings, the
        # in-process obs-agent server (+ exa from the vault's .mcp.json),
        # effort/thinking env. MCP servers are replay servers (see above).
        from obs_agent.effort import build_effort_env
        # OBS's own effort bridge (session.py), with the effort the logged body
        # shows; "high" is the API default and leaves no output_config.
        effort = (wire.get("output_config") or {}).get("effort") or "high"
        env.update(build_effort_env(model, effort, {}))
        if meta.get("route") == "cli-proxy":
            # session.py: hosted non-Claude models authenticate with the
            # CLIProxyAPI key (API-key mode, no oauth beta); a dummy suffices here
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env["ANTHROPIC_API_KEY"] = "fixture-capture-not-a-credential"
        else:
            thinking = wire.get("thinking") or {}
            if thinking.get("type") == "enabled" and thinking.get("budget_tokens"):
                env["MAX_THINKING_TOKENS"] = str(thinking["budget_tokens"])
                env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
        # The OBS daemon runs without SHELL; the CLI then writes "Shell:
        # unknown" into the system prompt's environment block.
        os.environ.pop("SHELL", None)
        cli_model = model + ("[1m]" if "context-1m" in beta else "")
        # Only OBS's in-process server is replayed (its tool set moves with OBS
        # releases). Servers from the project's .mcp.json (exa) connect for real,
        # exactly as for an OBS agent: replacing them with in-process replay
        # servers drops the resources capability and with it 2.1.59's
        # ListMcpResourcesTool/ReadMcpResourceTool, which changes the tool list.
        kw.update(setting_sources=["project"], cli_model=cli_model,
                  mcp_servers=replay_mcp_servers(wire.get("tools") or [], only={"obs-agent"}))
    os.environ.pop("CLAUDECODE", None)
    env.update(extra_env or {})
    return ClaudeAgentOptions(
        cli_path=cli_path,
        model=kw.pop("cli_model"),
        cwd=cwd,
        env=env,
        resume=resume,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code"},
        **kw,
    )


async def drive(options: ClaudeAgentOptions, prompt: str, timeout: float) -> dict:
    out = {"result": None, "error": None}
    try:
        async with asyncio.timeout(timeout):
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, ResultMessage):
                        out["result"] = {"subtype": msg.subtype, "is_error": msg.is_error,
                                         "session_id": msg.session_id}
    except Exception as e:  # recorded per case
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def first_main_request(log_dir: str, session_id: str, since: float) -> dict | None:
    rows = [r for r in pd.read_index(log_dir)
            if r.get("session_id") == session_id and r.get("ts", 0) >= since
            and str(r.get("path", "")).startswith("/v1/messages")
            and "count_tokens" not in str(r.get("path", ""))]
    rows.sort(key=lambda r: r["ts"])
    for r in rows:
        try:
            wire = pd.load_request(r["req_id"], "wire", log_dir)
        except Exception:
            continue
        if wire.get("tools"):
            return r
    return None


REAL_CONFIG_DIR = str(Path.home() / ".claude")


def load_candidate(req_id: str, part: str, log_dir: str, config_dir: Path) -> dict:
    """A captured body with the checker's scratch config-dir path mapped back.

    The CLI writes its config dir into the system prompt (the auto-memory
    section). The checker must run the candidate on a scratch config dir so it
    never touches real transcripts, and that is the ONLY reason this one path
    differs from production. Mapping it back is the one canonicalization the
    checker adds on top of prefix_diff; it is applied to the candidate only.
    """
    # INVARIANT(cache-bytes): the scratch config-dir path is the only thing mapped
    # back, and only on the candidate side. Adding any other rewrite here hides
    # real prefix breaks from the checker (a false PASS).
    # Doc: vault:Projects/Personal Projects/OBS Platform/CLI Runtime/Components/Fixtures and Byte Checker#Canonicalization
    path = pd.find_request_file(req_id, part, log_dir)
    text = pd._read_bytes(path).decode("utf-8")
    return json.loads(text.replace(str(config_dir), REAL_CONFIG_DIR))


def serialization_check(raw: bytes) -> dict:
    """Is ``raw`` exactly the proxy's serialization of itself? (design-v2 §1.5.1, §10.1)

    The stored ``pre`` is redacted, but the redactor substitutes ASCII
    placeholders inside string values, which leaves this property unchanged.
    """
    # INVARIANT(cache-bytes): the reference form is json.dumps with
    # separators=(",", ":") and the default ensure_ascii=True, the exact call at
    # src/cache_proxy.py (body = json.dumps(data, separators=...)). Changing
    # either side makes the gate accept raw UTF-8 or reject the binary's form.
    # Doc: vault:Projects/Personal Projects/OBS Platform/CLI Runtime/Components/Fixtures and Byte Checker#Serialization gate
    try:
        canon_bytes = json.dumps(json.loads(raw), separators=(",", ":")).encode()
    except ValueError as e:
        return {"ok": False, "reason": f"unparseable: {e}"}
    if canon_bytes == raw:
        return {"ok": True}
    off = next((i for i, (x, y) in enumerate(zip(raw, canon_bytes)) if x != y), min(len(raw), len(canon_bytes)))
    return {"ok": False, "first_offset": off, "raw_utf8": any(b > 127 for b in raw),
            "non_ascii_bytes": sum(1 for b in raw if b > 127), "raw_len": len(raw), "canonical_len": len(canon_bytes),
            "raw_context": raw[max(0, off - 20):off + 20].decode("utf-8", "replace"),
            "canonical_context": canon_bytes[max(0, off - 20):off + 20].decode("ascii", "replace")}


# design-v2 §1.12: an index row passes "expected transforms only" when these
# are all zero / empty (metadata and cache_key are the expected transforms)
ZERO_NORM = ("billing", "strings", "reminders", "git_status", "tools", "dynamic_boundary", "ws_trimmed",
             "effort_stripped", "schema_sanitized")


def expected_transforms_only(row: dict) -> dict:
    n = row.get("norm") or {}
    bad = {k: n.get(k) for k in ZERO_NORM if n.get(k)}
    if ((n.get("volatile_pin") or {}).get("applied") or []):
        bad["volatile_pin.applied"] = n["volatile_pin"]["applied"]
    if (n.get("ttl_pin") or {}).get("applied") is not None:
        bad["ttl_pin.applied"] = n["ttl_pin"]["applied"]
    ep = row.get("effort_pin") or n.get("effort_pin") or {}
    if ep.get("inserted") or ep.get("changed"):
        bad["effort_pin"] = {k: ep.get(k) for k in ("inserted", "changed")}
    lc = n.get("local_compact")
    if isinstance(lc, dict) and lc.get("action") == "rewritten":
        bad["local_compact"] = "rewritten"
    return {"ok": not bad, "non_expected": bad}


MANIFEST_EXEMPT = ("created_at", "created_by")


def manifest_check(expected_path: Path, config: Path, cwd: str, sid: str) -> dict:
    """§10.3 equality: every field equal except created_at, created_by, bootstrap.reason."""
    sidecar = config / "projects" / sanitize(cwd) / f"{sid}.obs-manifest.json"
    if not expected_path.exists():
        return {"status": "no-expected", "ok": True}
    if not sidecar.exists():
        return {"status": "absent", "ok": False, "looked_at": str(sidecar)}
    exp = json.loads(expected_path.read_text())
    got = json.loads(sidecar.read_text())
    diffs = []
    for k in sorted(set(exp) | set(got)):
        if k in MANIFEST_EXEMPT or k.startswith("_"):
            continue
        a, b = exp.get(k), got.get(k)
        if k == "bootstrap" and isinstance(a, dict) and isinstance(b, dict):
            a = {x: y for x, y in a.items() if x != "reason"}
            b = {x: y for x, y in b.items() if x != "reason"}
        # INVARIANT(cache-bytes): tools_json is compared as exact text (bytes),
        # system as exact JSON values; no normalization of either.
        if a != b:
            diffs.append(k)
    return {"status": "match" if not diffs else "mismatch", "ok": not diffs, "fields": diffs}


def compare(logged: dict, logged_beta: str | None, cand: dict, cand_beta: str | None) -> dict:
    # INVARIANT(cache-bytes): the verdict is prefix_diff's cache-prefix class on
    # the proxy-redacted bodies (same redaction on both sides). Do not
    # canonicalize anything here beyond what prefix_diff does (cache_control
    # positions only): every other difference is a real cache break.
    # Doc: vault:Projects/Personal Projects/OBS Platform/CLI Runtime/Components/Fixtures and Byte Checker#Verdict
    res = pd.diff_requests(logged, cand, logged_beta, cand_beta)
    cls = res.get("classification")
    return {"class": cls, "ok": cls in OK_CLASSES,
            "first_divergence": res.get("first_divergence"),
            "config_diffs": [{"key": d["key"]} for d in res.get("config_diffs") or []],
            "logged_messages": res.get("a_messages"), "candidate_messages": res.get("b_messages"),
            "summary": pd.format_result(res).splitlines()[0] if cls not in OK_CLASSES else cls}


def prime_proxy(env: Env, meta: dict, case: Path, body: bytes | None = None) -> dict:
    """Recreate the proxy's per-conversation pins from the logged request.

    The production proxy pins a conversation's volatile system sections,
    context_management, effort and breakpoint TTL as first seen, keyed by model
    plus first message, and forgets them after 2 idle days -- so the frozen pin
    store no longer holds pins for most fixture conversations. Sending the
    logged post-proxy body once through the scratch proxy (it goes to the
    capture endpoint) records exactly the values the conversation was pinned
    to. Limits: the body is the redacted stored form, so the key matches only
    when the first message holds nothing the redactor replaced (the case
    report shows a remaining config/system difference then).
    """
    import urllib.request
    if body is None:
        body = gzip.decompress((case / "last_request.json.gz").read_bytes())
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
               "user-agent": "obs-fixture-check-prime", "authorization": "Bearer fixture-capture",
               "anthropic-beta": meta.get("anthropic_beta") or ""}
    req = urllib.request.Request(env.base_url + "/v1/messages?beta=true", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        return {"primed": True}
    except Exception as e:
        return {"primed": False, "error": f"{type(e).__name__}: {e}"}


def setup_case(case: Path, meta: dict, work: Path,
               session_file: str = "session.jsonl") -> tuple[Path, str, Path, object]:
    """Scratch config dir with the fixture session, prepared the way OBS resumes."""
    if work.exists():
        shutil.rmtree(work)
    config = work / "config"
    config.mkdir(parents=True)
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        # account identity + cached feature flags the CLI reads; no credential
        shutil.copyfile(claude_json, config / ".claude.json")
    cwd = (meta.get("session_cut") or {}).get("cwd") or str(work / "cwd")
    if not Path(cwd).is_dir():
        Path(cwd).mkdir(parents=True, exist_ok=True)
    sid = meta["session_id"]
    proj = config / "projects" / sanitize(cwd)
    proj.mkdir(parents=True)
    real_memory = Path(REAL_CONFIG_DIR) / "projects" / sanitize(cwd) / "memory"
    if real_memory.is_dir():
        # the auto-memory section of the system prompt lists this directory's
        # index file; today's copy (the logged run saw the then-current one)
        shutil.copytree(real_memory, proj / "memory")
    sess = proj / f"{sid}.jsonl"
    shutil.copyfile(case / session_file, sess)
    try:
        replay = prepare_session_replay(session_id=sid, cwd=Path(cwd), source_path=sess)
    except Exception as e:
        replay = {"error": f"{type(e).__name__}: {e}"}
    return config, cwd, sess, replay


async def forward(case: Path, meta: dict, wire: dict, args, env: Env, work: Path,
                  prime_body: bytes | None) -> tuple[dict, dict | None, Path, str, Path]:
    config, cwd, sess, replay = setup_case(case, meta, work)
    run = {"prepare_session_replay": bool(replay)}
    if not args.no_prime:
        run["prime"] = prime_proxy(env, meta, case, prime_body)
    t0 = time.time()
    opts = launch_options(meta, wire, cli_path=args.cli_path, base_url=env.base_url,
                          config_dir=config, cwd=cwd, resume=meta["session_id"])
    run["forward_run"] = await drive(opts, CONTINUATION.format(case=meta["case_id"]), args.case_timeout)
    time.sleep(1.5)  # the proxy's request log writes on a background thread
    return run, first_main_request(env.log_dir, meta["session_id"], t0), config, cwd, sess


def unredacted_first_message_prime(env: Env, row: dict, case: Path) -> bytes | None:
    """Prime body whose first message is the candidate's own (unredacted) one.

    The pins are keyed by model + first message of the NORMALIZED body; when
    the stored logged body had something redacted in its first message the
    logged-body prime lands on another key. The capture endpoint holds the
    unredacted upstream bytes of the candidate's request (matched by the
    index's wire_sha256); its first message gives the right key, the rest of
    the logged body gives the values the conversation was pinned to.
    """
    want = row.get("wire_sha256")
    for meta_file in sorted(env.capture_dir.glob("*.json")):
        m = json.loads(meta_file.read_text())
        if m.get("body_sha256") == want:
            cap = json.loads(meta_file.with_suffix(".body").read_bytes())
            logged = json.loads(gzip.decompress((case / "last_request.json.gz").read_bytes()))
            # The re-serialized body is used ONLY to prime pins; it is never a
            # comparison operand (compare() reads the proxy log's stored bytes).
            if cap.get("messages") and logged.get("messages"):
                logged["messages"][0] = cap["messages"][0]
                return json.dumps(logged).encode()
    return None


async def check_case(case: Path, args, env: Env) -> dict:
    extra_envs: list = []
    try:
        return await _check_case(case, args, env, extra_envs)
    finally:
        for e in extra_envs:
            e.close()


async def _check_case(case: Path, args, env: Env, extra_envs: list) -> dict:
    meta = json.loads((case / "meta.json").read_text())
    cid = meta["case_id"]
    wire = json.loads(gzip.decompress((case / "last_request.json.gz").read_bytes()))
    sid = meta["session_id"]
    work = Path(args.out) / "cases" / cid
    rep = {"case_id": cid, "tags": meta.get("tags"), "profile": meta.get("profile"),
           "model": meta.get("model"), "route": meta.get("route"), "cli_path": args.cli_path}
    run, row, config, cwd, sess = await forward(case, meta, wire, args, env, work, None)
    rep.update(run)
    if (row is not None and not args.no_prime
            and ((row.get("norm") or {}).get("volatile_pin") or {}).get("recorded")):
        # the logged-body prime missed the conversation's pin key: redo the case
        # on a fresh scratch proxy primed with the candidate's own first message
        body = unredacted_first_message_prime(env, row, case)
        if body is not None:
            env2 = Env(work.parent / f"{cid}.retry")
            extra_envs.append(env2)  # closed by check_case after the comparison
            run, row2, config, cwd, sess = await forward(case, meta, wire, args, env2, work, body)
            rep.update(run)
            rep["prime_retry"] = True
            if row2 is not None:
                env, row = env2, row2
                rep["proxy_dir"] = str(env2.proxy_dir)
        else:
            rep["prime_retry"] = "no capture matched the candidate request"
    if row is None:
        rep.update(verdict="FAIL", reason="no model request captured from the candidate")
        return rep
    rep["candidate_req_id"] = row["req_id"]
    rep["candidate_norm_action"] = (row.get("norm") or {}).get("action")
    cand_wire = load_candidate(row["req_id"], "wire", env.log_dir, config)
    # wire is compared with the betas sent UPSTREAM on both sides (the proxy
    # adds context-management when it restores a pinned setting); pre with the
    # betas the CLI itself sent
    logged_row = json.loads((case / "index_row.json").read_text())
    cand_beta = pd._beta(row)
    rep["wire"] = compare(wire, pd._beta(logged_row), cand_wire, cand_beta)
    ok = rep["wire"]["ok"]
    if args.require_pre or args.report_pre or args.require_serialization:
        cand_pre = load_candidate(row["req_id"], "pre", env.log_dir, config)
        rep["pre"] = compare(wire, (logged_row.get("client_headers") or {}).get("anthropic-beta"),
                             cand_pre, (row.get("client_headers") or {}).get("anthropic-beta"))
        raw_pre = pd._read_bytes(pd.find_request_file(row["req_id"], "pre", env.log_dir))
        rep["serialization"] = serialization_check(raw_pre)
        rep["expected_transforms"] = expected_transforms_only(row)
        rep["manifest"] = manifest_check(case / "expected_manifest.json", config, cwd, sid)
        if args.require_serialization:
            ok = ok and rep["serialization"]["ok"]
        if args.require_pre:
            ok = (ok and rep["serialization"]["ok"] and rep["expected_transforms"]["ok"]
                  and rep["manifest"]["ok"])
    if args.reverse and ok:
        t1 = time.time()
        ropts = launch_options(meta, wire, cli_path=args.reverse_cli, base_url=env.base_url,
                               config_dir=config, cwd=cwd, resume=sid)
        try:
            prepare_session_replay(session_id=sid, cwd=Path(cwd), source_path=sess)
        except Exception as e:
            rep["reverse_prepare_error"] = f"{type(e).__name__}: {e}"
        rep["reverse_run"] = await drive(ropts, "Fixture reverse continuation: reply ok.", args.case_timeout)
        time.sleep(1.5)
        rrow = first_main_request(env.log_dir, sid, t1)
        if rrow is None:
            rep["reverse"] = {"ok": False, "reason": "no request captured from the reverse CLI"}
        else:
            r_wire = load_candidate(rrow["req_id"], "wire", env.log_dir, config)
            rep["reverse"] = compare(cand_wire, cand_beta, r_wire, pd._beta(rrow))
            rep["reverse"]["req_id"] = rrow["req_id"]
            rep["reverse"]["cli"] = args.reverse_cli
        ok = ok and rep["reverse"].get("ok", False)
    if meta.get("compaction") and not args.no_compaction:
        rep["compaction_check"] = await compaction_step(case, meta, wire, args, env, work)
        ok = ok and rep["compaction_check"]["ok"]
    rep["verdict"] = "PASS" if ok else "FAIL"
    return rep


async def compaction_step(case: Path, meta: dict, wire: dict, args, env: Env, work: Path) -> dict:
    """Resume just before the logged compaction and require the same compaction request."""
    comp = meta["compaction"]
    cwork = work.parent / (work.name + ".compact")
    config, cwd, sess, _ = setup_case(case, meta, cwork, "session_precompact.jsonl")
    sid = meta["session_id"]
    t0 = time.time()
    opts = launch_options(meta, wire, cli_path=args.cli_path, base_url=env.base_url, config_dir=config,
                          cwd=cwd, resume=sid, extra_env=comp.get("env"))
    out = {"run": await drive(opts, comp["continuation"], args.case_timeout)}
    time.sleep(1.5)
    logged_c = json.loads(gzip.decompress((case / f"{comp['request']}.json.gz").read_bytes()))
    logged_rows = json.loads((case / "extra_index_rows.json").read_text())
    c_tools = [t.get("name") for t in logged_c.get("tools") or []]
    rows = sorted((r for r in pd.read_index(env.log_dir) if r.get("session_id") == sid and r.get("ts", 0) >= t0
                   and "count_tokens" not in str(r.get("path", ""))), key=lambda r: r["ts"])
    cand_c = cand_post = None
    for r in rows:
        try:
            w = load_candidate(r["req_id"], "wire", env.log_dir, config)
        except Exception:
            continue
        names = [t.get("name") for t in w.get("tools") or []]
        if cand_c is None and names == c_tools and w.get("max_tokens") == logged_c.get("max_tokens"):
            cand_c = (r, w)
        elif cand_c is not None and w.get("tools") and names != c_tools:
            cand_post = (r, w)
            break
    if cand_c is None:
        out.update(ok=False, reason="no compaction request captured from the candidate")
        return out
    r, w = cand_c
    out["compaction"] = compare(logged_c, pd._beta(logged_rows[comp["request"]]), w, pd._beta(r))
    # INVARIANT(cache-bytes): the compaction request is gated on exact bytes of
    # the stored (identically redacted) wire bodies, not only the prefix class.
    stored_logged = gzip.decompress((case / f"{comp['request']}.json.gz").read_bytes())
    stored_cand = pd._read_bytes(pd.find_request_file(r["req_id"], "wire", env.log_dir)).decode(
        "utf-8").replace(str(config), REAL_CONFIG_DIR).encode()
    out["compaction"]["bytes_equal"] = stored_logged == stored_cand
    ok = out["compaction"]["class"] == "identical" and out["compaction"]["bytes_equal"]
    if cand_post is None:
        out["post"] = {"ok": False, "reason": "no post-compaction main request captured"}
        ok = False
    else:
        pr, pw = cand_post
        logged_p = json.loads(gzip.decompress((case / f"{comp['post_request']}.json.gz").read_bytes()))
        res = compare(logged_p, pd._beta(logged_rows[comp["post_request"]]), pw, pd._beta(pr))
        path = (res.get("first_divergence") or {}).get("path") or ""
        # the summary text is the capture endpoint's canned reply: only
        # messages[0] may differ (or nothing)
        res["ok"] = res["class"] in ("identical", "append") or (
            res["class"] == "history-changed" and path.startswith("messages[0]"))
        out["post"] = res
        ok = ok and res["ok"]
    out["ok"] = ok
    return out


async def amain(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = sorted(p for p in Path(args.fixtures).iterdir() if (p / "meta.json").exists())
    if args.case:
        cases = [c for c in cases if c.name in set(args.case)]
    if args.tag:
        cases = [c for c in cases
                 if set(args.tag) & set(json.loads((c / "meta.json").read_text()).get("tags") or [])]
    env = Env(out)
    results = []
    try:
        for c in cases:
            rep = await check_case(c, args, env)
            results.append(rep)
            line = f"{rep['verdict']:4s} {rep['case_id']:44s} wire={rep.get('wire', {}).get('summary', rep.get('reason'))}"
            if "pre" in rep:
                line += f" pre={rep['pre']['summary']} norm={rep.get('candidate_norm_action')}"
            if "serialization" in rep:
                line += f" ser={'ok' if rep['serialization']['ok'] else 'RAW' if rep['serialization'].get('raw_utf8') else 'bad'}"
            if "compaction_check" in rep:
                cc = rep["compaction_check"]
                line += f" compaction={(cc.get('compaction') or {}).get('class', cc.get('reason'))}" \
                        f"/bytes={(cc.get('compaction') or {}).get('bytes_equal')} post={(cc.get('post') or {}).get('class')}"
            if "reverse" in rep:
                line += f" reverse={rep['reverse'].get('summary', rep['reverse'].get('reason'))}"
            print(line, flush=True)
            (out / "report.json").write_text(json.dumps(results, indent=1, default=str))
    finally:
        env.close()
    n_pass = sum(r["verdict"] == "PASS" for r in results)
    summary = {"cli_path": args.cli_path, "cases": len(results), "pass": n_pass,
               "fail": len(results) - n_pass, "require_pre": args.require_pre, "reverse": args.reverse,
               "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"\nRESULT: {'PASS' if n_pass == len(results) and results else 'FAIL'} "
          f"{n_pass}/{len(results)} cases   (files: {out})")
    return 0 if results and n_pass == len(results) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cli-path", required=True)
    ap.add_argument("--fixtures", default="/workspace/runtime/e2e/fixtures/byte-resume")
    ap.add_argument("--out", default=None)
    ap.add_argument("--case", action="append", help="only these case ids (repeatable)")
    ap.add_argument("--tag", action="append", help="only cases with any of these tags (repeatable)")
    ap.add_argument("--require-pre", action="store_true",
                    help="also require the candidate's own bytes to extend the logged body and the "
                         "proxy to normalize nothing (the OBS-owned binary's gate)")
    ap.add_argument("--require-serialization", action="store_true",
                    help="require only the serialization gate (ensure_ascii compact pre bytes) on top of the wire check")
    ap.add_argument("--report-pre", action="store_true", help="report the pre comparison without requiring it")
    ap.add_argument("--reverse", action="store_true", help="also resume the candidate-written session under --reverse-cli")
    ap.add_argument("--reverse-cli", default=PINNED)
    ap.add_argument("--case-timeout", type=float, default=180)
    ap.add_argument("--no-compaction", action="store_true", help="skip the compaction step of compaction cases")
    ap.add_argument("--no-prime", action="store_true",
                    help="do not recreate the conversation's proxy pins from the logged body first")
    args = ap.parse_args(argv)
    args.cli_path = str(Path(args.cli_path).resolve())
    if not args.out:
        args.out = f"/workspace/runtime/e2e/fixtures/_checks/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
