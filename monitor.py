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
from typing import Optional, Tuple
from urllib.parse import urljoin

import requests
import shutil

import server  # noqa: E402  (health endpoint for Render free tier)

HLS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://bongacams.com/",
    "Accept": "*/*",
}

# Resolve ffmpeg in order: FFMPEG_BIN env -> $HOME/ffmpeg -> /opt/ffmpeg -> /usr/bin/ffmpeg -> shutil.which
_candidates = [
    os.environ.get("FFMPEG_BIN"),
    os.path.join(os.environ.get("HOME", ""), "ffmpeg", "ffmpeg"),
    "/opt/ffmpeg/ffmpeg",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
]
for _c in _candidates:
    if _c and Path(_c).exists() and os.access(_c, os.X_OK):
        FFMPEG_BIN = _c
        break
else:
    _which = shutil.which("ffmpeg")
    if _which:
        FFMPEG_BIN = _which
    else:
        FFMPEG_BIN = None  # resolved later in main(); main() will sys.exit(1) if missing

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


def _probe_hls(url: str) -> Optional[Tuple[str, int]]:
    """Fetch HLS playlist; if master (EXT-X-STREAM-INF), follow best variant.

    Returns (playable_url, segment_count) or None if empty/offline stash.
    BongaCams often gives a master playlist without #EXTINF — media is in chunks.m3u8.
    """
    try:
        r = requests.get(url, timeout=10, headers=HLS_HEADERS)
        if r.status_code != 200:
            return None
        text = r.text or ""
        if "#EXTINF" in text:
            return url, text.count("#EXTINF")
        # Master playlist — pick last (usually highest) stream variant
        if "#EXT-X-STREAM-INF" in text:
            variants = []
            lines = text.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("#EXT-X-STREAM-INF"):
                    bw = 0
                    if "BANDWIDTH=" in line:
                        try:
                            bw = int(line.split("BANDWIDTH=")[1].split(",")[0])
                        except Exception:
                            bw = 0
                    j = i + 1
                    while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                        j += 1
                    if j < len(lines):
                        variants.append((bw, urljoin(url, lines[j].strip())))
                        i = j
                i += 1
            if not variants:
                return None
            variants.sort(key=lambda x: x[0])
            media_url = variants[-1][1]
            r2 = requests.get(media_url, timeout=10, headers=HLS_HEADERS)
            if r2.status_code != 200:
                return None
            t2 = r2.text or ""
            if "#EXTINF" not in t2:
                return None
            # Feed master URL to ffmpeg (it picks variants); segs from media
            return url, t2.count("#EXTINF")
        return None
    except Exception as e:
        LOG.debug("HLS probe failed: %s", e)
        return None


def _extract_stripchat(data: dict) -> Optional[dict]:
    """Return {hls, viewers} if live with segments, else None."""
    if data.get("count", 0) == 0:
        return None
    m = (data.get("models") or [{}])[0]
    hls = m.get("stream", {}).get("url") if isinstance(m.get("stream"), dict) else None
    if not hls:
        return None
    probed = _probe_hls(hls)
    if not probed:
        return None
    play_url, segs = probed
    return {"hls": play_url, "viewers": m.get("viewersCount", 0), "segs": segs}


def _extract_bongacams(data: dict) -> Optional[dict]:
    """Return {hls, viewers} if live with segments, else None.

    mybro.tv may keep isOnline=true briefly after offline, and streamUrl often
    points to a *master* playlist (EXT-X-STREAM-INF) without #EXTINF. We:
      1) require isOnline=true
      2) resolve master → media and require real #EXTINF segments
    onlineChangedAt is go-live timestamp (not a heartbeat) — do NOT use as
    a short freshness window or multi-hour streams get rejected.
    """
    m = data.get("model", {})
    if not m.get("isOnline"):
        return None

    hls = m.get("streamUrl") or m.get("hlsPlaylistUrl")
    if not hls:
        return None

    if "bcvcdn.com" not in hls and "bongacams" not in hls:
        LOG.debug("bongacams: non-bcvcdn URL %s — trying anyway", hls[:80])

    probed = _probe_hls(hls)
    if not probed:
        LOG.debug("bongacams: HLS empty/unresolvable — %s", hls[:80])
        return None
    play_url, segs = probed
    return {"hls": play_url, "viewers": m.get("viewersCount", 0), "segs": segs}


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
        FFMPEG_BIN,
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
    if not FFMPEG_BIN or not Path(FFMPEG_BIN).exists():
        LOG.error("ffmpeg not found. Tried: %s", _candidates)
        sys.exit(1)
    LOG.info("ffmpeg: %s", FFMPEG_BIN)

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
