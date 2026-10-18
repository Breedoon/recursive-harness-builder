#!/usr/bin/env python3
"""Model-free reproduction of local-agent "Request timed out" hangs.

Why this exists (bead obs-3, 2026-10-16): local-model agents (Spark GLM via the
Spark cache proxy on 28931, 3090 Qwen via the gate) died after ~1 hour with
"Request timed out". The CLI debug log showed one streaming attempt going
silent, "Error streaming, falling back to non-streaming mode: The operation
timed out.", then eleven non-streaming attempts each failing after ~300 s.
The upstream was alive but silent: a long cold prefill (500k-token context)
sends no bytes until the first token.

This harness reproduces that without any model:

  fake upstream  a tiny Anthropic Messages server whose main-model responses
                 stall in a configurable way (before headers, after
                 message_start, non-streaming), optionally sending keepalives
                 (`event: ping` frames or SSE comments; whitespace for
                 non-streaming) during the stall.
  scratch proxy  src/cache_proxy.py (or --proxy-src PATH) on a spare port with
                 its own CACHE_PROXY_LOG_DIR and OBS_LOCAL_LLM_BASE_URL pointed
                 at the fake. Skipped with --direct (CLI talks to the fake).
  real CLI       the bundled Claude Code CLI, `claude -p`, model local-stall-*
                 so the proxy takes its local route, isolated HOME, debug file.

It records, per scenario, every request the fake saw (arrival, stream flag,
retry header, when the peer closed the socket) and the CLI's own debug-log
timeline, then classifies the outcome (ok / timed_out) and the measured gaps.

Usage:
  local_stall_repro.py run --scenario NAME [--direct] [--proxy-src PATH]
                           [--out DIR] [--max-retries N] [--max-wall S]
  local_stall_repro.py list
  local_stall_repro.py fake --port P [scenario knobs]   (debugging)

Custom knobs override the scenario preset:
  --stream-mode {ok,silent-before-headers,silent-after-start,404}
  --silence S          stall length for the streaming main request
  --ping-interval P    keepalive period during the stall (0 = none)
  --ping-kind {event,comment}
  --nonstream-mode {ok,silent,trickle}
  --nonstream-silence S
  --nonstream-trickle P

Each run takes up to silence + fallback time; scenarios are independent, so
run several at once (each picks free ports). Never points at production:
the proxy always gets its own log dir and the fake's address.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

OBS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PROXY_SRC = os.path.join(OBS_ROOT, "src", "cache_proxy.py")
PYTHON = os.path.join(OBS_ROOT, ".venv", "bin", "python")
CLI = os.path.join(OBS_ROOT, ".venv", "lib", "python3.12", "site-packages",
                   "claude_agent_sdk", "_bundled", "claude")
# Not /workspace/runtime/tmp: that is wiped when OBS restarts.
DEFAULT_OUT = "/workspace/runtime/local-stall-repro"
MAIN_MODEL = "local-stall-main"
AUX_MODEL = "local-stall-aux"
FAKE_TOKEN = "local-stall-test-token"

SCENARIOS = {
    # Control: everything answers at once.
    "control": dict(stream_mode="ok"),
    # Long prefill with no bytes after message_start (what TensorFold does):
    # streaming dies, fallback non-streaming also silent.
    "stall_after_start": dict(stream_mode="silent-after-start", silence=420,
                              nonstream_mode="silent", nonstream_silence=420),
    # Same stall, but the upstream sends `event: ping` every 15 s.
    "stall_after_start_pings": dict(stream_mode="silent-after-start", silence=420,
                                    ping_interval=15, ping_kind="event",
                                    nonstream_mode="silent", nonstream_silence=420),
    # Same, SSE comment keepalives instead of ping events.
    "stall_after_start_comments": dict(stream_mode="silent-after-start", silence=420,
                                       ping_interval=15, ping_kind="comment",
                                       nonstream_mode="silent", nonstream_silence=420),
    # No response headers at all for the whole stall (queued before admission).
    "stall_before_headers": dict(stream_mode="silent-before-headers", silence=420,
                                 nonstream_mode="silent", nonstream_silence=420),
    # Streaming 404 forces the CLI straight to non-streaming; that answer is
    # silent for 420 s: measures the non-streaming timer alone.
    "nonstream_silent": dict(stream_mode="404", nonstream_mode="silent",
                             nonstream_silence=420),
    # Non-streaming answer whose headers come at once and whose body is
    # preceded by a whitespace byte every 15 s (a candidate proxy fix).
    "nonstream_trickle": dict(stream_mode="404", nonstream_mode="trickle",
                              nonstream_silence=420, nonstream_trickle=15),
    # A stall far beyond the proxy's 600 s read timeout, with pings: proves
    # pings also keep the proxy->upstream read alive.
    # Spark watchdog recovery (bead obs-8): the streaming request hangs (a wedged
    # head), the watchdog verdict turns "wedged" at 40 s and "healthy" at 100 s.
    # The proxy must fail the stream retryably, hold the CLI's non-streaming
    # fallback until the verdict is healthy, then forward it: the turn
    # completes with text instead of dying.
    "wedge_recovery": dict(stream_mode="silent-after-start", silence=600,
                           nonstream_mode="ok", liveness="0:healthy,40:wedged,70:restarting,100:healthy"),
    "long_stall_pings": dict(stream_mode="silent-after-start", silence=780,
                             ping_interval=15, ping_kind="event",
                             nonstream_mode="silent", nonstream_silence=780),
}

DEFAULTS = dict(stream_mode="ok", silence=0, ping_interval=0, ping_kind="event",
                nonstream_mode="ok", nonstream_silence=0, nonstream_trickle=15, liveness="")


def _liveness_writer(path: str, timeline: str, stop: threading.Event, t0: float, log) -> None:
    """Write a watchdog verdict file following "SECONDS:STATE,..." (refreshed every 2 s)."""
    plan = sorted((float(a), b) for a, b in (x.split(":", 1) for x in timeline.split(",") if x))
    current = None
    while not stop.is_set():
        elapsed = time.time() - t0
        state = [st for at, st in plan if at <= elapsed][-1:] or ["healthy"]
        if state[0] != current:
            current = state[0]
            log(event="liveness", state=current, at=round(elapsed, 1))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"state": current, "reason": "local_stall_repro timeline", "since": t0,
                       "updated": time.time()}, f)
        os.replace(tmp, path)
        stop.wait(2.0)


# ── fake upstream ────────────────────────────────────────────────────────


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _message(model: str, text: str) -> dict:
    return {
        "id": "msg_local_stall", "type": "message", "role": "assistant",
        "model": model, "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 2,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    }


def _stream_events(model: str, text: str) -> list[bytes]:
    msg = _message(model, "")
    msg["content"] = []
    msg["stop_reason"] = None
    msg["usage"]["output_tokens"] = 1
    return [
        _sse("message_start", {"type": "message_start", "message": msg}),
        _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
        _sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text}}),
        _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 2}}),
        _sse("message_stop", {"type": "message_stop"}),
    ]


class FakeState:
    def __init__(self, cfg: dict, log_path: str):
        self.cfg = cfg
        self.log_path = log_path
        self.lock = threading.Lock()
        self.t0 = time.time()

    def log(self, **rec):
        rec["t"] = round(time.time() - self.t0, 3)
        rec["ts"] = time.strftime("%H:%M:%S", time.gmtime())
        with self.lock, open(self.log_path, "a") as f:
            f.write(json.dumps(rec) + "\n")


def _peer_closed(sock: socket.socket) -> bool:
    try:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def make_fake_handler(state: FakeState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _wait(self, seconds: float, rid: int, keepalive=None, period=0.0) -> bool:
            """Stall *seconds*; send keepalive() every *period*. False if peer left."""
            end = time.time() + seconds
            next_ka = time.time() + period if period else None
            while time.time() < end:
                if _peer_closed(self.connection):
                    state.log(req=rid, event="peer_closed")
                    return False
                if next_ka is not None and time.time() >= next_ka:
                    try:
                        keepalive()
                    except OSError as e:
                        state.log(req=rid, event="keepalive_write_failed", error=str(e))
                        return False
                    next_ka += period
                time.sleep(0.25)
            return True

        def _write(self, data: bytes):
            self.wfile.write(data)
            self.wfile.flush()

        def _chunk(self, data: bytes):
            self._write(b"%x\r\n%s\r\n" % (len(data), data))

        def do_GET(self):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self._write(body)

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                req = json.loads(raw or b"{}")
            except Exception:
                req = {}
            if "count_tokens" in self.path:
                body = b'{"input_tokens":10}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self._write(body)
                return
            model = str(req.get("model", ""))
            stream = bool(req.get("stream"))
            main = model.startswith(MAIN_MODEL)
            with state.lock:
                state.cfg.setdefault("_n", 0)
                state.cfg["_n"] += 1
                rid = state.cfg["_n"]
            state.log(req=rid, event="request", model=model, stream=stream, main=main,
                      retry=self.headers.get("x-stainless-retry-count"),
                      timeout_hdr=self.headers.get("x-stainless-timeout"))
            cfg = state.cfg if main else DEFAULTS
            try:
                if stream:
                    self._stream(rid, model, cfg)
                else:
                    self._nonstream(rid, model, cfg)
            except OSError as e:
                state.log(req=rid, event="write_failed", error=str(e))
            self.close_connection = True

        def _stream(self, rid, model, cfg):
            mode = cfg["stream_mode"]
            events = _stream_events(model, "OK-STALL-SURVIVED")
            if mode == "404":
                body = b'{"type":"error","error":{"type":"not_found_error","message":"no stream"}}'
                self.send_response(404)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self._write(body)
                state.log(req=rid, event="sent_404")
                return
            if mode == "silent-before-headers":
                if not self._wait(cfg["silence"], rid):
                    return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            self._chunk(events[0])
            state.log(req=rid, event="sent_message_start")
            if mode == "silent-after-start":
                kind = cfg["ping_kind"]
                frame = (b": keepalive\n\n" if kind == "comment"
                         else _sse("ping", {"type": "ping"}))
                if not self._wait(cfg["silence"], rid,
                                  keepalive=lambda: self._chunk(frame),
                                  period=cfg["ping_interval"]):
                    return
            for ev in events[1:]:
                self._chunk(ev)
            self._write(b"0\r\n\r\n")
            state.log(req=rid, event="completed")

        def _nonstream(self, rid, model, cfg):
            mode = cfg["nonstream_mode"]
            body = json.dumps(_message(model, "OK-STALL-SURVIVED")).encode()
            if mode == "silent":
                if not self._wait(cfg["nonstream_silence"], rid):
                    return
            if mode == "trickle":
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                state.log(req=rid, event="sent_headers")
                if not self._wait(cfg["nonstream_silence"], rid,
                                  keepalive=lambda: self._chunk(b" "),
                                  period=cfg["nonstream_trickle"]):
                    return
                self._chunk(body)
                self._write(b"0\r\n\r\n")
                state.log(req=rid, event="completed")
                return
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self._write(body)
            state.log(req=rid, event="completed")

    return Handler


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_fake(cfg: dict, log_path: str, port: int = 0):
    state = FakeState(cfg, log_path)
    srv = ThreadedServer(("127.0.0.1", port), make_fake_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


# ── scratch proxy ────────────────────────────────────────────────────────


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, timeout: float = 20) -> bool:
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def start_proxy(proxy_src: str, port: int, fake_url: str, log_dir: str, stdout,
                liveness_file: str = ""):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CACHE_PROXY_", "OBS_LOCAL_LLM_", "ANTHROPIC_"))}
    env.update({
        "PYTHONPATH": os.path.join(OBS_ROOT, "src"),
        "CACHE_PROXY_LOG_DIR": log_dir,
        "CACHE_PROXY_REQUEST_LOG": "0",
        "CACHE_PROXY_SAVE_BODIES": "0",
        "OBS_LOCAL_LLM_BASE_URL": fake_url,
        "OBS_LOCAL_LLM_AUTH_TOKEN": FAKE_TOKEN,
        "PYTHONUNBUFFERED": "1",
    })
    if liveness_file:
        env["OBS_LOCAL_LIVENESS_FILE"] = liveness_file
    p = subprocess.Popen([PYTHON, proxy_src, str(port)], env=env, stdout=stdout,
                         stderr=subprocess.STDOUT, start_new_session=True)
    if not wait_health(f"http://127.0.0.1:{port}/health"):
        p.kill()
        raise RuntimeError(f"scratch proxy on {port} did not become healthy")
    return p


# ── CLI driver and analysis ──────────────────────────────────────────────

_DEBUG_PATTERNS = [
    ("stream_started", re.compile(r"Stream started")),
    ("stream_fallback", re.compile(r"falling back to non-streaming mode: ?(.*)")),
    ("stream_404_fallback", re.compile(r"Streaming endpoint returned 404")),
    ("stream_timeout_sdk", re.compile(r"Streaming timeout \(SDK abort\)")),
    ("api_error", re.compile(r"API error \(attempt (\d+)/(\d+)\): (.*)")),
    ("request_error", re.compile(r"Error in API request: (.*)")),
    ("stall", re.compile(r"Streaming stall.*")),
]


def parse_debug(path: str) -> list[dict]:
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, errors="replace"):
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z) \[(\w+)\] (.*)", line)
        if not m:
            continue
        ts, _lvl, msg = m.groups()
        for name, rx in _DEBUG_PATTERNS:
            mm = rx.search(msg)
            if mm:
                out.append({"ts": ts, "event": name, "detail": msg[:300]})
                break
    return out


def _epoch(ts: str) -> float:
    import datetime as dt
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=dt.timezone.utc).timestamp()


def run_scenario(args) -> dict:
    cfg = dict(DEFAULTS)
    if args.scenario:
        cfg.update(SCENARIOS[args.scenario])
    for k in DEFAULTS:
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v
    name = args.scenario or "custom"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = os.path.join(args.out, f"{stamp}-{name}{'-direct' if args.direct else ''}")
    os.makedirs(out, exist_ok=True)
    fake_log = os.path.join(out, "fake.jsonl")
    fake, state = start_fake(cfg, fake_log)
    fake_url = f"http://127.0.0.1:{fake.server_address[1]}"
    proxy = None
    proxy_log = open(os.path.join(out, "proxy.log"), "w")
    proxy_src = os.path.abspath(args.proxy_src)
    liveness_file = os.path.join(out, "liveness.json") if cfg.get("liveness") else ""
    stop_liveness = threading.Event()
    if liveness_file:
        with open(liveness_file, "w") as f:
            json.dump({"state": "healthy", "since": time.time(), "updated": time.time()}, f)
    if args.direct:
        base = fake_url
    else:
        pport = free_port()
        proxy = start_proxy(proxy_src, pport, fake_url, os.path.join(out, "proxy-logs"),
                            proxy_log, liveness_file)
        base = f"http://127.0.0.1:{pport}"
    home = os.path.join(out, "home")
    os.makedirs(home, exist_ok=True)
    debug = os.path.join(out, "cli-debug.txt")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE", "OBS_", "CACHE_PROXY_"))}
    env.update({
        "HOME": home,
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": AUX_MODEL,
        "ANTHROPIC_SMALL_FAST_MODEL": AUX_MODEL,
        "CLAUDE_CODE_MAX_RETRIES": str(args.max_retries),
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        # Without these the CLI spends ~4 min fetching claude.ai MCP configs
        # with no OAuth before its first request (measured 2026-10-16).
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    })
    cmd = [CLI, "-p", "Reply with OK.", "--model", MAIN_MODEL, "--debug-file", debug,
           "--no-session-persistence", "--strict-mcp-config",
           "--mcp-config", '{"mcpServers":{}}', "--output-format", "text"]
    meta = {"scenario": name, "cfg": {k: cfg[k] for k in DEFAULTS}, "direct": args.direct,
            "proxy_src": None if args.direct else proxy_src, "base_url": base,
            "max_retries": args.max_retries, "cmd": " ".join(cmd), "out": out,
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    t0 = time.time()
    if liveness_file:
        threading.Thread(target=_liveness_writer, daemon=True,
                         args=(liveness_file, cfg["liveness"], stop_liveness, t0, state.log)).start()
    # stdin must be closed: `claude -p` otherwise waits for piped stdin.
    cli = subprocess.Popen(cmd, env=env, cwd=home, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, start_new_session=True)
    try:
        stdout, _ = cli.communicate(timeout=args.max_wall)
        rc = cli.returncode
    except subprocess.TimeoutExpired:
        os.killpg(cli.pid, signal.SIGKILL)
        stdout, _ = cli.communicate()
        rc = "killed_max_wall"
    elapsed = time.time() - t0
    stop_liveness.set()
    time.sleep(1.0)
    if proxy is not None:
        # Keep the proxy a little longer so the fake sees whether the proxy
        # releases its upstream connection after the CLI left.
        time.sleep(args.linger)
        os.killpg(proxy.pid, signal.SIGTERM)
        try:
            proxy.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proxy.pid, signal.SIGKILL)
    fake.shutdown()
    proxy_log.close()
    text = (stdout or b"").decode(errors="replace")
    fake_events = [json.loads(l) for l in open(fake_log)] if os.path.exists(fake_log) else []
    cli_events = parse_debug(debug)
    # Relative CLI timings from the first main request start.
    for ev in cli_events:
        ev["t"] = round(_epoch(ev["ts"]) - t0, 3)
    # Did the upstream main request get released when the CLI left early?
    # fake t is relative to the fake's own start (~runner t0); good to ~1 s.
    closes = [e["t"] for e in fake_events if e.get("event") == "peer_closed"]
    completes = [e["t"] for e in fake_events if e.get("event") == "completed"]
    release = None
    if rc == "killed_max_wall":
        release = {"cli_killed_at_s": round(elapsed, 1),
                   "upstream_peer_closed_at_s": closes[0] if closes else None,
                   "upstream_completed_at_s": completes[0] if completes else None,
                   "released": bool(closes) and (not completes or closes[0] < completes[0])}
    result = dict(meta)
    result.update({
        "upstream_release": release,
        "rc": rc, "elapsed_s": round(elapsed, 1),
        "outcome": "ok" if "OK-STALL-SURVIVED" in text else "failed",
        "cli_output_tail": text[-400:],
        "fake_events": fake_events, "cli_events": cli_events,
    })
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


def summarize(r: dict) -> str:
    lines = [f"scenario={r['scenario']} direct={r['direct']} outcome={r['outcome']} "
             f"rc={r['rc']} elapsed={r['elapsed_s']}s out={r['out']}"]
    for ev in r["fake_events"]:
        if ev.get("event") == "request" and not ev.get("main"):
            continue
        lines.append(f"  fake  t={ev['t']:>8}  {ev.get('event')} "
                     + " ".join(f"{k}={ev[k]}" for k in ("req", "stream", "retry", "error")
                                if k in ev))
    for ev in r["cli_events"]:
        lines.append(f"  cli   t={ev['t']:>8}  {ev['event']}: {ev['detail'][:140]}")
    if r.get("upstream_release"):
        lines.append(f"  upstream_release: {r['upstream_release']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("run", "fake"):
        p = sub.add_parser(name)
        p.add_argument("--scenario", choices=sorted(SCENARIOS))
        p.add_argument("--stream-mode", dest="stream_mode",
                       choices=["ok", "silent-before-headers", "silent-after-start", "404"])
        p.add_argument("--silence", type=float)
        p.add_argument("--ping-interval", dest="ping_interval", type=float)
        p.add_argument("--ping-kind", dest="ping_kind", choices=["event", "comment"])
        p.add_argument("--nonstream-mode", dest="nonstream_mode",
                       choices=["ok", "silent", "trickle"])
        p.add_argument("--nonstream-silence", dest="nonstream_silence", type=float)
        p.add_argument("--nonstream-trickle", dest="nonstream_trickle", type=float)
        if name == "run":
            p.add_argument("--direct", action="store_true",
                           help="CLI talks to the fake directly (no proxy)")
            p.add_argument("--proxy-src", default=DEFAULT_PROXY_SRC)
            p.add_argument("--out", default=DEFAULT_OUT)
            p.add_argument("--max-retries", type=int, default=0,
                           help="CLAUDE_CODE_MAX_RETRIES (0 = one attempt per mode)")
            p.add_argument("--max-wall", type=float, default=1500)
            p.add_argument("--linger", type=float, default=0,
                           help="keep the proxy alive N s after the CLI exits")
            p.add_argument("--json", action="store_true")
        else:
            p.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    if a.cmd == "list":
        for k, v in SCENARIOS.items():
            print(f"{k:28} {v}")
        return 0
    if a.cmd == "fake":
        cfg = dict(DEFAULTS)
        if a.scenario:
            cfg.update(SCENARIOS[a.scenario])
        for k in DEFAULTS:
            if getattr(a, k, None) is not None:
                cfg[k] = getattr(a, k)
        start_fake(cfg, "/dev/stdout", a.port)
        print(f"fake upstream on 127.0.0.1:{a.port} cfg={cfg}", flush=True)
        while True:
            time.sleep(3600)
    if not os.path.exists(a.proxy_src) and not a.direct:
        print(f"no proxy source at {a.proxy_src}", file=sys.stderr)
        return 2
    r = run_scenario(a)
    print(json.dumps(r, indent=2) if a.json else summarize(r))
    return 0 if r["outcome"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
