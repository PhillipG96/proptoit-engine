#!/usr/bin/env python3
"""Lightweight indexer endpoint for n8n to call via HTTP Request.

Security: binds to localhost only (n8n runs on this host) and requires a
shared-secret token in the X-Indexer-Token header.

NOTE: default port changed from 8002 — that port belongs to the ChattWatch
API. Set INDEXER_PORT explicitly if you host something else here.
"""
import hmac
import os
import subprocess
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

INDEXER_TOKEN = os.getenv("INDEXER_TOKEN")


class IndexerHandler(BaseHTTPRequestHandler):
    def _deny(self, code: int, msg: str):
        body = json.dumps({"success": False, "error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not INDEXER_TOKEN:
            self._deny(503, "INDEXER_TOKEN not configured; endpoint disabled")
            return
        supplied = self.headers.get("X-Indexer-Token", "")
        if not hmac.compare_digest(supplied, INDEXER_TOKEN):
            self._deny(401, "unauthorized")
            return
        if self.path == '/index':
            result = subprocess.run(
                ['bash', '/home/magicofelipe/proptoit/collectors/meili_indexer.sh'],
                capture_output=True, text=True, timeout=300
            )
            response = {
                'success': result.returncode == 0,
                'stdout': result.stdout.strip(),
                'stderr': result.stderr.strip(),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

if __name__ == '__main__':
    host = os.getenv("INDEXER_HOST", "127.0.0.1")
    port = int(os.getenv("INDEXER_PORT", "8005"))
    server = HTTPServer((host, port), IndexerHandler)
    print(f'Indexer endpoint listening on {host}:{port}')
    server.serve_forever()
