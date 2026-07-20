"""Tiny health-check server so Render keeps the service awake (free tier
spins down web services after ~15 min of no inbound HTTP). Runs alongside
monitor.py. Binds to PORT (default 10000).

Also self-pings the public URL every few minutes so the process stays warm
even if external GitHub cron is delayed/skipped.
"""
import http.server
import logging
import os
import threading
import time
import urllib.request

LOG = logging.getLogger("recorder.server")

KEEPALIVE_SEC = int(os.environ.get("KEEPALIVE_SEC", "240"))  # 4 min


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


def _public_url() -> str:
    base = (
        os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("KEEPALIVE_URL")
        or ""
    ).rstrip("/")
    if base:
        return f"{base}/health"
    port = int(os.environ.get("PORT", "10000"))
    return f"http://127.0.0.1:{port}/health"


def _keepalive_loop():
    url = _public_url()
    time.sleep(30)
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "render-keepalive/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = resp.getcode()
            LOG.info("keepalive ping %s -> %s", url, code)
        except Exception as e:
            LOG.warning("keepalive ping failed: %s", e)
        time.sleep(KEEPALIVE_SEC)


def start():
    port = int(os.environ.get("PORT", "10000"))
    srv = http.server.HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="health-http")
    t.start()
    print(f"[server] health check listening on 0.0.0.0:{port}", flush=True)

    kt = threading.Thread(target=_keepalive_loop, daemon=True, name="keepalive")
    kt.start()
    print(f"[server] self-keepalive every {KEEPALIVE_SEC}s -> {_public_url()}", flush=True)
    return srv
