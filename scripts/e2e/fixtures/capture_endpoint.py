#!/usr/bin/env python3
"""No-spend model endpoint for byte-fixture checks (obs-22 E3, contract C2).

A tiny HTTP server that plays the upstream API for a scratch cache proxy. Every
request is recorded byte for byte, and every model call is answered with a
fixed, valid Anthropic Messages response. Nothing reaches a real provider and
no credits are spent.

Routes (the proxy is pointed at two prefixes so it keeps telling its Claude
route from its CLIProxyAPI route apart):
  /anthropic/...   stands in for https://api.anthropic.com
  /cliproxy/...    stands in for CLIProxyAPI (http://127.0.0.1:8317)

Responses:
  POST .../v1/messages             SSE stream (or JSON when stream=false) with
                                   one text block, CAPTURE_REPLY_TEXT (default
                                   "ok"), stop_reason end_turn, zero cache usage
  POST .../v1/messages/count_tokens  {"input_tokens": 1}
  anything else                     {} with 200

Records under --out: <seq>.json with {route, path, headers (authorization and
any secret-looking header dropped), body_sha256} and <seq>.body (raw bytes).
The bodies can hold conversation text: never commit or print them unfiltered.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SECRET_HEADER_WORDS = ("authorization", "api-key", "x-api-key", "cookie", "token", "secret")


class Recorder:
    def __init__(self, out: Path):
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.seq = itertools.count(1)
        self.lock = threading.Lock()

    def save(self, route: str, path: str, headers: dict, body: bytes) -> int:
        with self.lock:
            n = next(self.seq)
        meta = {
            "seq": n,
            "route": route,
            "path": path,
            "headers": {k: v for k, v in headers.items()
                        if not any(w in k.lower() for w in SECRET_HEADER_WORDS)},
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_len": len(body),
        }
        (self.out / f"{n:06d}.body").write_bytes(body)
        (self.out / f"{n:06d}.json").write_text(json.dumps(meta, indent=1))
        return n


def sse_events(model: str, text: str) -> bytes:
    mid = "msg_capture_" + uuid.uuid4().hex[:20]
    usage0 = {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0}
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage0}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in events)


def json_message(model: str, text: str) -> bytes:
    return json.dumps({
        "id": "msg_capture_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
        "model": model, "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0},
    }).encode()


def make_handler(rec: Recorder, reply_text: str):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("request-id", "req_capture_" + uuid.uuid4().hex[:16])
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n) if n else b""
            path = self.path
            route = "other"
            for prefix in ("/anthropic", "/cliproxy"):
                if path.startswith(prefix):
                    route, path = prefix[1:], path[len(prefix):] or "/"
            rec.save(route, path, dict(self.headers), body)
            if "/count_tokens" in path:
                return self._send(200, b'{"input_tokens": 1}', "application/json")
            if path.startswith("/v1/messages"):
                try:
                    req = json.loads(body or b"{}")
                except ValueError:
                    req = {}
                model = str(req.get("model") or "capture-model")
                if req.get("stream"):
                    return self._send(200, sse_events(model, reply_text), "text/event-stream")
                return self._send(200, json_message(model, reply_text), "application/json")
            return self._send(200, b"{}", "application/json")

        def do_GET(self):
            self._send(200, b"{}", "application/json")

        do_HEAD = do_GET

    return H


def serve(port: int, out: Path, reply_text: str = "ok") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Recorder(out), reply_text))
    threading.Thread(target=srv.serve_forever, daemon=True, name="capture-endpoint").start()
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reply", default=os.environ.get("CAPTURE_REPLY_TEXT", "ok"))
    args = ap.parse_args(argv)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Recorder(Path(args.out)), args.reply))
    print(f"capture endpoint on :{args.port} -> {args.out}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
