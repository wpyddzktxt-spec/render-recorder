"""Tiny health-check server so Render keeps the service awake (free tier
spins down web services after ~15 min of no inbound HTTP). Runs alongside
monitor.py. Binds to PORT (default 10000).

Also self-pings the public URL every few minutes so the process stays warm
even if external GitHub cron is delayed/skipped.
"""
import http.server
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

LOG = logging.getLogger("recorder.server")

KEEPALIVE_SEC = int(os.environ.get("KEEPALIVE_SEC", "240"))  # 4 min

# Shared status info that monitor.py updates; TG commands read it
STATUS = {
    "models": {},       # name -> {status, ts}
    "last_chunk": None, # "name 2026-09-01 12:00 8m30s 45MB"
    "started": time.time(),
}


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


# ---------------- Telegram command listener ----------------

def _tg(method: str, **params):
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        return {}
    try:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}", data=data
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        LOG.warning("tg %s failed: %s", method, e)
        return {}


def _fmt_status() -> str:
    import datetime
    up_min = int((time.time() - STATUS["started"]) / 60)
    lines = [f"🤖 Recorder up {up_min} min"]
    for name, info in sorted(STATUS["models"].items()):
        ts = info.get("ts") or 0
        ago = int(time.time() - ts) if ts else -1
        ago_s = f"{ago // 60}m ago" if ago >= 0 else "?"
        icon = {"offline": "⚫", "recording": "🔴", "done": "✅", "deduped": "⏭"}.get(info.get("status"), "❔")
        lines.append(f"{icon} {name}: {info.get('status')} ({ago_s})")
    if not STATUS["models"]:
        lines.append("(статусы появятся после первого опроса)")
    if STATUS.get("last_chunk"):
        lines.append(f"📹 последний чанк: {STATUS['last_chunk']}")
    return "\n".join(lines)


def _handle_command(chat_id: str, text: str):
    cmd = (text or "").strip().lower().split("@")[0]
    if cmd in ("/start", "/help", "menu"):
        msg = (
            "Это бот-рекордер @Su4e4ki_bot.\n"
            "Он сам пишет стримы и присылает видео — кнопок нет.\n\n"
            "Команды:\n"
            "/status — статус моделей и сервисa\n"
            "/models — список моделей\n"
            "Видео приходят автоматически, когда модель в эфире."
        )
        _tg("sendMessage", chat_id=chat_id, text=msg)
    elif cmd == "/status":
        _tg("sendMessage", chat_id=chat_id, text=_fmt_status())
    elif cmd == "/models":
        names = ", ".join(sorted(STATUS["models"]) or ["загрузка…"])
        _tg("sendMessage", chat_id=chat_id, text=f"Модели: {names}")
    else:
        _tg("sendMessage", chat_id=chat_id, text="Неизвестная команда. /status /models /help")


def _tg_loop():
    offset = 0
    while True:
        try:
            r = _tg("getUpdates", offset=offset, timeout=25)
            for upd in r.get("result") or []:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                text = msg.get("text") or ""
                cb = upd.get("callback_query")
                if cb:
                    _tg("answerCallbackQuery", callback_query_id=cb["id"], text="Кнопки не поддерживаются — используй /status")
                    m2 = cb.get("message") or {}
                    c2 = (m2.get("chat") or {}).get("id")
                    if c2:
                        _handle_command(str(c2), "/help")
                elif chat and text.startswith("/"):
                    _handle_command(str(chat), text)
        except Exception as e:
            LOG.warning("tg loop: %s", e)
            time.sleep(5)


def start():
    port = int(os.environ.get("PORT", "10000"))
    if os.environ.get("BOT_TOKEN") and os.environ.get("TG_COMMANDS", "1") == "1":
        gt = threading.Thread(target=_tg_loop, daemon=True, name="tg-commands")
        gt.start()
        print("[server] TG command listener on (/status /models /help)", flush=True)
    srv = http.server.HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="health-http")
    t.start()
    print(f"[server] health check listening on 0.0.0.0:{port}", flush=True)

    kt = threading.Thread(target=_keepalive_loop, daemon=True, name="keepalive")
    kt.start()
    print(f"[server] self-keepalive every {KEEPALIVE_SEC}s -> {_public_url()}", flush=True)
    return srv
