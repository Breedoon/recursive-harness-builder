"""Tiny HTTP server: records the first POST /v1/messages body per run, answers 400."""
import http.server, json, sys, os
out = sys.argv[2]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length', 0))
        body = self.rfile.read(n)
        if '/v1/messages' in self.path and 'count_tokens' not in self.path:
            with open(out, 'ab') as f:
                f.write(body + b'\n')
        self.send_response(400)
        self.send_header('content-type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"type":"error","error":{"type":"invalid_request_error","message":"capture"}}')
    def do_GET(self):
        self.send_response(404); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()
