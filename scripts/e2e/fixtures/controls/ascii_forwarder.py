#!/usr/bin/env python3
"""Positive control for the checker's serialization gate (obs-22 E3, design-v2 §10.1).

A local HTTP forwarder that re-serializes every JSON request body into the
form the OBS-owned binary must emit -- ``json.dumps(body, separators=(",", ":"))``
with ensure_ascii -- and forwards it unchanged otherwise. Put in front of the
pinned 2.1.59 (see ascii-cli.sh), it makes 2.1.59 look like a binary that
emits the required serialization, so the gate can be shown to PASS such a
binary and FAIL raw UTF-8 (2.1.59 alone).

Usage: ascii_forwarder.py <port-file> <upstream-base-url>   (picks a free port)
"""
import http.client
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT_FILE, UPSTREAM = sys.argv[1], sys.argv[2].rstrip("/")
U = urllib.parse.urlsplit(UPSTREAM)
HOP = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _fwd(self, body: bytes | None):
        conn = http.client.HTTPConnection(U.hostname, U.port or 80, timeout=600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        if body is not None:
            headers["content-length"] = str(len(body))
        conn.request(self.command, U.path + self.path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            # INVARIANT(cache-bytes): the same call the proxy makes; this IS the
            # reference form the serialization gate checks for.
            body = json.dumps(json.loads(raw), separators=(",", ":")).encode()
        except ValueError:
            body = raw
        self._fwd(body)

    def do_GET(self):
        self._fwd(None)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
with open(PORT_FILE, "w") as f:
    f.write(str(srv.server_address[1]))
srv.serve_forever()
