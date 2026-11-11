"""Lane-local, stateless Streamable HTTP MCP fixture for WP-02."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

URL = "http://127.0.0.1:18755"
VAULT = Path('/workspace/runtime/git/obs-vault-active')
FIXTURE = Path(__file__).resolve().parents[3] / 'deploy/obs-e2e/fixture-vault/.mcp.json'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == '/stop':
            self.send_response(204)
            self.end_headers()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_response(405)
            self.end_headers()

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        method = request.get('method')
        if 'id' not in request:
            self.send_response(202)
            self.end_headers()
            return
        if method == 'initialize':
            result = {'protocolVersion': request['params']['protocolVersion'], 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'wp02-stub', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'probe', 'description': 'Return the WP02 fixed marker',
                                 'inputSchema': {'type': 'object', 'properties': {}}}]}
        elif method == 'tools/call':
            result = {'content': [{'type': 'text', 'text': 'WP02-FIXED-MCP-RESULT'}]}
        else:
            result = {}
        body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    if not os.environ.get('OBS_E2E_LANE'):
        raise RuntimeError('WP02 fixture requires an isolated E2E lane')
    if sys.argv[1] == 'serve':
        (VAULT / '.mcp.json').write_bytes(FIXTURE.read_bytes())
        settings_path = VAULT / '.claude/settings.json'
        settings = json.loads(settings_path.read_text())
        settings['disabledMcpjsonServers'] = ['wp02-disabled']
        settings_path.write_text(json.dumps(settings))
        server = ThreadingHTTPServer(('127.0.0.1', 18755), Handler)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    elif sys.argv[1] == 'ready':
        deadline = time.monotonic() + 20
        while True:
            try:
                urlopen(URL, timeout=1)
            except Exception as exc:
                if getattr(exc, 'code', None) == 405:
                    break
                if time.monotonic() >= deadline:
                    raise
                threading.Event().wait(0.1)
    elif sys.argv[1] == 'stop':
        urlopen(URL + '/stop', timeout=5).close()


if __name__ == '__main__':
    main()
