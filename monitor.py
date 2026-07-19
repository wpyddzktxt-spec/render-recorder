#!/usr/bin/env python3
"""
24/7 recorder for JustKatrin (Stripchat) and moonmaiden (BongaCams) on Render.
- Polls every 30s
- Validates HLS playlist has #EXTINF segments (not just #EXTM3U)
- Records 10-min chunks via ffmpeg copy
- Sends to Telegram chat_id
- Persists state across restarts (last-dispatch timestamp) to avoid duplicate work
"""
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

import server  # noqa: E402  (health endpoint for Render free tier)

# ---- Config from env ----
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
POLL_SEC = int(os.environ.get("POLL_SEC", "30"))
CHUNK_MIN = int(os.environ.get("CHUNK_MIN", "10"))
RECHECK_OK_AFTER_CHUNK = os.environ.get("RECHECK_OK_AFTER_CHUNK", "1") == "1"

LOG = logging.getLogger("recorder")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

STATE_FILE = Path("/tmp/recorder_state.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---- Models ----
MODELS = {
    "JustKatrin": {
        "platform": "stripchat",
        "check_url": "https://go.xxxiijmp.com/api/models?modelsList=JustKatrin&strict=1",
        "extract": "_extract_stripchat",
    },
    "moonmaiden": {
        "platform": "bongacams",
        "check_url": "https://mybro.tv/api/v1/models/alias/moonmaiden_",
        "extract": "_extract_bongacams",
    },
}


def _extract_stripchat(data: dict) -> Optional[dict]:
    """Return {hls, viewers} if live with segments, else None."""
    if data.get("count", 0) == 0:
        return None
    m = (data.get("models") or [{}])[0]
    hls = m.get("stream", {}).get("url") if isinstance(m.get("stream"), dict) else None
    if not hls:
        return None
    # Verify HLS playlist has actual segments
    try:
        r = requests.get(hls, timeout=8)
        if r.status_code != 200:
            return None
        if "#EXTINF" not in r.text:
            return None
        segs = r.text.count("#EXTINF")
    except Exception as e:
        LOG.debug("HLS probe failed: %s", e)
        return None
    return {"hls": hls, "viewers": m.get("viewersCount", 0), "segs": segs}


def _extract_bongacams(data: dict) -> Optional[dict]:
    """Return {hls, viewers} if live with segments, else None.

    Critical: mybro.tv caches streamUrl after a model goes offline — the CDN
    keeps responding 200 but the playlist has zero #EXTINF segments. We
    require ALL of: isOnline=true, onlineChangedAt within FRESH_MIN, and
    HLS playlist actually has segments. This is the only way to distinguish
    a live stream from a cached/offline one.
    """
    FRESH_MIN = 5  # onlineChangedAt must be within last 5 min
    m = data.get("model", {})
    if not m.get("isOnline"):
        return None
    # Check onlineChangedAt freshness
    oca = m.get("onlineChangedAt") or ""
    if oca:
        try:
            ts = datetime.fromisoformat(oca.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
            if age_min > FRESH_MIN:
                LOG.debug(
                    "bongacams: onlineChangedAt too old (%.1f min) — likely cached",
                    age_min,
                )
                return None
        except Exception:
            pass

    hls = m.get("streamUrl") or m.get("hlsPlaylistUrl")
    if not hls:
        return None

    # Discriminate BongaCams CDN vs Stripchat CDN — only BongaCams is bcvcdn
    if "bcvcdn.com" not in hls and "bongacams" not in hls:
        # Some BongaCams models stream from Stripchat CDN; still valid HLS
        LOG.debug("bongacams: non-bcvcdn URL %s — trying anyway", hls[:80])

    try:
        r = requests.get(hls, timeout=8)
        if r.status_code != 200:
            return None
        if "#EXTINF" not in r.text:
            LOG.debug("bongacams: HLS empty (no #EXTINF) — %s", hls[:80])
            return None
        segs = r.text.count("#EXTINF")
    except Exception as e:
        LOG.debug("bongacams: HLS probe failed: %s", e)
        return None
    return {"hls": hls, "viewers": m.get("viewersCount", 0), "segs": segs}


def check_live(name: str) -> Optional[dict]:
    cfg = MODELS[name]
    try:
        r = requests.get(cfg["check_url"], timeout=10)
        if r.status_code != 200:
            LOG.debug("%s: HTTP %d", name, r.status_code)
            return None
        data = r.json()
    except Exception as e:
        LOG.debug("%s: check failed: %s", name, e)
        return None
    fn = globals()[cfg["extract"]]
    return fn(data)


def record_chunk(name: str, hls: str, duration_s: int) -> Optional[Path]:
    """Record duration_s of HLS stream to /tmp/<name>_<ts>.mp4 via ffmpeg copy."""
    out = Path(f"/tmp/{name}_{int(time.time())}.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel", "error",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "3",
        "-i", hls,
        "-t", str(duration_s),
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(out),
    ]
    LOG.info("Recording %s for %ds -> %s", name, duration_s, out.name)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=duration_s + 30)
    except subprocess.TimeoutExpired:
        LOG.warning("ffmpeg timeout, using partial %s", out)
    if not out.exists():
        return None
    size = out.stat().st_size
    if size < 50_000:
        LOG.warning("Chunk too small: %d bytes, removing", size)
        out.unlink(missing_ok=True)
        return None
    return out


def send_telegram(path: Path, name: str, viewers: int, duration_s: int) -> bool:
    """Send recorded file as video to Telegram."""
    cap = f"🎥 {name} | {duration_s // 60} min | {viewers} viewers"
    url = f"{TG_API}/sendVideo"
    with path.open("rb") as f:
        r = requests.post(
            url,
            data={"chat_id": CHAT_ID, "supports_streaming": "true", "caption": cap},
            files={"video": (path.name, f, "video/mp4")},
            timeout=300,
        )
    try:
        j = r.json()
    except Exception:
        LOG.error("TG: bad response %s", r.text[:200])
        return False
    if j.get("ok"):
        LOG.info("Sent %s (%d bytes) to TG", path.name, path.stat().st_size)
        return True
    LOG.error("TG send failed: %s", j)
    return False


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        LOG.debug("state save: %s", e)


def main():
    server.start()
    LOG.info("=== Recorder starting on Render ===")
    LOG.info("Poll=%ss chunk=%dmin models=%s", POLL_SEC, CHUNK_MIN, list(MODELS))

    state = load_state()
    duration_s = CHUNK_MIN * 60
    iteration = 0

    while True:
        iteration += 1
        try:
            for name in MODELS:
                live = check_live(name)
                if not live:
                    if iteration % 10 == 1:
                        LOG.info("[%s] offline", name)
                    state[name] = {"status": "offline", "ts": time.time()}
                    continue

                LOG.info("[%s] LIVE viewers=%d segs=%d", name, live["viewers"], live["segs"])
                # Avoid duplicate recording within same minute (state guard)
                last = state.get(name, {})
                if last.get("status") == "recording" and time.time() - last.get("ts", 0) < 60:
                    LOG.info("[%s] already recording, skip", name)
                    continue

                state[name] = {"status": "recording", "ts": time.time()}
                save_state(state)

                chunk = record_chunk(name, live["hls"], duration_s)
                if chunk:
                    ok = send_telegram(chunk, name, live["viewers"], duration_s)
                    if ok:
                        chunk.unlink(missing_ok=True)
                state[name] = {"status": "done", "ts": time.time()}
                save_state(state)

            save_state(state)
        except Exception as e:
            LOG.exception("loop error: %s", e)

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
