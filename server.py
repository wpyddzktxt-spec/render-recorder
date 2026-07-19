"""Tiny health-check server so Render keeps the service awake (free tier
spins down web services after 15 min of no traffic). Runs alongside monitor.py.
Binds to the port Render expects (PORT env, default 10000).
"""
import http.server
import os
import threading


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass


def start():
    port = int(os.environ.get("PORT", "10000"))
    srv = http.server.HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"[server] health check listening on 0.0.0.0:{port}", flush=True)
    return srv
