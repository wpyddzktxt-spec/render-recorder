"""Tiny health-check server so Render keeps the service awake (free tier
spins down web services after 15 min of no traffic). Runs alongside monitor.py."""
import http.server
import threading


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
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
    srv = http.server.HTTPServer(("0.0.0.0", 10000), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
