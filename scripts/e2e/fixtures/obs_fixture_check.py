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
With ``--require-pre`` the candidate's own bytes ("pre", before the proxy) must
also extend the logged wire body and the proxy must report it normalized
nothing (index ``norm.action == "not_found"``): the gate for the OBS-owned
binary, which must make the proxy's rewrites unnecessary (ADR-0015).

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
      --fixtures /workspace/runtime/e2e/fixtures/byte-resume --out /workspace/runtime/e2e/runs/fixture-check/X --reverse

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

from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

from obs_agent.session import _DEFAULT_SDK_ENV as OBS_DEFAULT_SDK_ENV  # noqa: E402  the env every OBS session gets

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


def replay_mcp_servers(logged_tools: list) -> dict:
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
        schema = t.get("input_schema") or {"type": "object", "properties": {}}

        def make(short=short, desc=t.get("description", ""), schema=schema):
            @tool(short, desc, schema)
            async def handler(args):
                return {"content": [{"type": "text", "text": "fixture replay server"}]}
            return handler
        servers.setdefault(server, []).append(make())
    return {s: create_sdk_mcp_server(name=s, version="1.0.0", tools=tools) for s, tools in servers.items()}


def launch_options(meta: dict, wire: dict, *, cli_path: str, base_url: str, config_dir: Path,
                   cwd: str, resume: str) -> ClaudeAgentOptions:
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
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)
    kw: dict = {}
    if meta.get("profile") == "harness":
        # scripts/prefix_repro.py _options()
        env.update({"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_FORCE_GLOBAL_CACHE": "0"})
        if meta.get("route") == "cli-proxy":
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
            env["ANTHROPIC_SMALL_FAST_MODEL"] = model
        kw.update(setting_sources=[], allowed_tools=["Bash", "Read"], cli_model=model)
    else:
        # src/obs_agent/session.py ClaudeAgentOptions(): project settings, the
        # in-process obs-agent server (+ exa from the vault's .mcp.json),
        # effort/thinking env. MCP servers are replay servers (see above).
        thinking = wire.get("thinking") or {}
        if thinking.get("type") == "enabled" and thinking.get("budget_tokens"):
            env["MAX_THINKING_TOKENS"] = str(thinking["budget_tokens"])
            env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"
        effort = (wire.get("output_config") or {}).get("effort")
        if effort:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
        cli_model = model + ("[1m]" if "context-1m" in beta else "")
        kw.update(setting_sources=["project"], cli_model=cli_model,
                  mcp_servers=replay_mcp_servers(wire.get("tools") or []))
    os.environ.pop("CLAUDECODE", None)
    return ClaudeAgentOptions(
        cli_path=cli_path,
        model=kw.pop("cli_model"),
        cwd=cwd,
        env=env,
        resume=resume,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code"},
        extra_args={"strict-mcp-config": None} if meta.get("profile") != "harness" else {},
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
    path = pd.find_request_file(req_id, part, log_dir)
    text = pd._read_bytes(path).decode("utf-8")
    return json.loads(text.replace(str(config_dir), REAL_CONFIG_DIR))


def compare(logged: dict, logged_beta: str | None, cand: dict, cand_beta: str | None) -> dict:
    # INVARIANT(cache-bytes): the verdict is prefix_diff's cache-prefix class on
    # the proxy-redacted bodies (same redaction on both sides). Do not
    # canonicalize anything here beyond what prefix_diff does (cache_control
    # positions only): every other difference is a real cache break.
    # Doc: Projects/Personal Projects/OBS Platform/CLI Runtime/Components/Fixtures and Byte Checker.md
    res = pd.diff_requests(logged, cand, logged_beta, cand_beta)
    cls = res.get("classification")
    return {"class": cls, "ok": cls in OK_CLASSES,
            "first_divergence": res.get("first_divergence"),
            "config_diffs": [{"key": d["key"]} for d in res.get("config_diffs") or []],
            "logged_messages": res.get("a_messages"), "candidate_messages": res.get("b_messages"),
            "summary": pd.format_result(res).splitlines()[0] if cls not in OK_CLASSES else cls}


async def check_case(case: Path, args, env: Env) -> dict:
    meta = json.loads((case / "meta.json").read_text())
    cid = meta["case_id"]
    wire = json.loads(gzip.decompress((case / "last_request.json.gz").read_bytes()))
    work = Path(args.out) / "cases" / cid
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
    shutil.copyfile(case / "session.jsonl", sess)
    from obs_agent.jsonl_replay import prepare_session_replay
    replay = None
    try:
        replay = prepare_session_replay(session_id=sid, cwd=Path(cwd), source_path=sess)
    except Exception as e:
        replay = {"error": f"{type(e).__name__}: {e}"}
    rep = {"case_id": cid, "tags": meta.get("tags"), "profile": meta.get("profile"),
           "model": meta.get("model"), "route": meta.get("route"), "cli_path": args.cli_path,
           "prepare_session_replay": bool(replay)}
    t0 = time.time()
    opts = launch_options(meta, wire, cli_path=args.cli_path, base_url=env.base_url,
                          config_dir=config, cwd=cwd, resume=sid)
    rep["forward_run"] = await drive(opts, CONTINUATION.format(case=cid), args.case_timeout)
    time.sleep(1.5)  # the proxy's request log writes on a background thread
    row = first_main_request(env.log_dir, sid, t0)
    if row is None:
        rep.update(verdict="FAIL", reason="no model request captured from the candidate")
        return rep
    rep["candidate_req_id"] = row["req_id"]
    rep["candidate_norm_action"] = (row.get("norm") or {}).get("action")
    cand_wire = load_candidate(row["req_id"], "wire", env.log_dir, config)
    cand_beta = pd._beta(row)
    logged_beta = meta.get("anthropic_beta")
    rep["wire"] = compare(wire, logged_beta, cand_wire, cand_beta)
    ok = rep["wire"]["ok"]
    if args.require_pre or args.report_pre:
        cand_pre = load_candidate(row["req_id"], "pre", env.log_dir, config)
        rep["pre"] = compare(wire, logged_beta, cand_pre, cand_beta)
        rep["proxy_normalized_nothing"] = rep["candidate_norm_action"] == "not_found"
        if args.require_pre:
            ok = ok and rep["pre"]["ok"] and rep["proxy_normalized_nothing"]
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
    rep["verdict"] = "PASS" if ok else "FAIL"
    return rep


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
    ap.add_argument("--report-pre", action="store_true", help="report the pre comparison without requiring it")
    ap.add_argument("--reverse", action="store_true", help="also resume the candidate-written session under --reverse-cli")
    ap.add_argument("--reverse-cli", default=PINNED)
    ap.add_argument("--case-timeout", type=float, default=180)
    args = ap.parse_args(argv)
    args.cli_path = str(Path(args.cli_path).resolve())
    if not args.out:
        args.out = f"/workspace/runtime/e2e/runs/fixture-check/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
