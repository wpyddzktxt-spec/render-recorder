#!/usr/bin/env python3
"""
24/7 recorder for JustKatrin (Stripchat) and moonmaiden (BongaCams) on Render.
- Polls every 30s
- Validates HLS has real #EXTINF segments
- Stream-copy mid/low bitrate HLS so 8-min chunks stay under Telegram 50 MB
- On accidental oversize: split/trim and still deliver (never silent-drop good video)
- Continuous: while model still LIVE, next chunk starts immediately (no 30s gap)
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
import shutil

import server  # noqa: E402  (health endpoint for Render free tier)

# Resolve ffmpeg
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
    FFMPEG_BIN = _which if _which else None

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
POLL_SEC = int(os.environ.get("POLL_SEC", "30"))
CHUNK_MIN = int(os.environ.get("CHUNK_MIN", "8"))
# Stay under bot API ~50 MB. 8 min @ ~800 kbps ≈ 48 MB.
HLS_TARGET_BW = int(os.environ.get("HLS_TARGET_BW", "900000"))
TG_MAX_BYTES = int(os.environ.get("TG_MAX_BYTES", str(48 * 1024 * 1024)))

LOG = logging.getLogger("recorder")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

STATE_FILE = Path("/tmp/recorder_state.json")
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

HEADERS_BC = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://bongacams.com/",
    "Origin": "https://bongacams.com",
    "Accept": "*/*",
}
HEADERS_SC = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://stripchat.com/",
    "Origin": "https://stripchat.com",
    "Accept": "*/*",
}

MODELS = {
    "JustKatrin": {
        "platform": "stripchat",
        "check_url": "https://go.xxxiijmp.com/api/models?modelsList=JustKatrin&strict=1",
        "extract": "_extract_stripchat",
        "headers": HEADERS_SC,
    },
    "moonmaiden": {
        "platform": "bongacams",
        "check_url": "https://mybro.tv/api/v1/models/alias/moonmaiden_",
        "extract": "_extract_bongacams",
        "headers": HEADERS_BC,
    },
}


def _headers_for(name: str, url: str = "") -> dict:
    cfg = MODELS.get(name) or {}
    if cfg.get("headers"):
        return dict(cfg["headers"])
    u = (url or "").lower()
    if "bcvcdn" in u or "bonga" in u:
        return dict(HEADERS_BC)
    if "stripchat" in u or "doppiocdn" in u or "saawsedge" in u or "stripcdn" in u:
        return dict(HEADERS_SC)
    return dict(HEADERS_SC)


def _probe_hls(url: str, headers: dict) -> Optional[Tuple[str, int, int]]:
    """Return (playable_url, segment_count, bandwidth) or None.

    Prefer media playlist closest to HLS_TARGET_BW (not max — max blows TG 50 MB).
    For live streams media URLs often rotate/403; still prefer media so bitrate is stable.
    If media later 403s, outer loop refreshes master via fresh check_live.
    """
    try:
        r = requests.get(url, timeout=12, headers=headers)
        if r.status_code != 200:
            return None
        text = r.text or ""
        if "#EXTINF" in text and "#EXT-X-STREAM-INF" not in text:
            return url, text.count("#EXTINF"), 0
        if "#EXT-X-STREAM-INF" not in text:
            return None

        variants: List[Tuple[int, str]] = []
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

        # Prefer ≤ target if available (guarantees smaller files); else closest under/allclose.
        under = [v for v in variants if 0 < v[0] <= HLS_TARGET_BW]
        if under:
            under.sort(key=lambda x: x[0])
            picked = under[-1]  # highest that still fits budget
        else:
            variants.sort(key=lambda x: (abs(x[0] - HLS_TARGET_BW), x[0]))
            picked = variants[0]

        bw_picked, media_url = picked
        r2 = requests.get(media_url, timeout=12, headers=headers)
        if r2.status_code != 200:
            # Fall back to master — ffmpeg may still pull a variant
            LOG.warning("media playlist HTTP %s — using master", r2.status_code)
            return url, 1, bw_picked
        t2 = r2.text or ""
        if "#EXTINF" not in t2:
            return None
        LOG.info("HLS variant bw=%d target=%d url=...%s", bw_picked, HLS_TARGET_BW, media_url[-70:])
        return media_url, t2.count("#EXTINF"), bw_picked
    except Exception as e:
        LOG.debug("HLS probe failed: %s", e)
        return None


def _extract_stripchat(data: dict, name: str = "JustKatrin") -> Optional[dict]:
    if data.get("count", 0) == 0:
        return None
    m = (data.get("models") or [{}])[0]
    stream = m.get("stream") if isinstance(m.get("stream"), dict) else {}
    hls = stream.get("url") if stream else None
    if not hls:
        return None
    headers = _headers_for(name, hls)
    probed = _probe_hls(hls, headers)
    if not probed:
        LOG.warning("stripchat: listed live but HLS empty (%s)", (hls or "")[:90])
        return None
    play_url, segs, bw = probed
    return {
        "hls": play_url,
        "master": hls,
        "viewers": m.get("viewersCount", 0),
        "segs": segs,
        "bw": bw,
        "headers": headers,
    }


def _extract_bongacams(data: dict, name: str = "moonmaiden") -> Optional[dict]:
    m = data.get("model", {})
    if not m.get("isOnline"):
        return None
    hls = m.get("streamUrl") or m.get("hlsPlaylistUrl")
    if not hls:
        return None
    headers = _headers_for(name, hls)
    probed = _probe_hls(hls, headers)
    if not probed:
        LOG.warning(
            "bongacams: isOnline but HLS empty viewers=%s url=%s",
            m.get("viewersCount"),
            (hls or "")[:100],
        )
        return None
    play_url, segs, bw = probed
    return {
        "hls": play_url,
        "master": hls,
        "viewers": m.get("viewersCount", 0),
        "segs": segs,
        "bw": bw,
        "headers": headers,
    }


def check_live(name: str) -> Optional[dict]:
    cfg = MODELS[name]
    try:
        r = requests.get(cfg["check_url"], timeout=12)
        if r.status_code != 200:
            LOG.debug("%s: HTTP %d", name, r.status_code)
            return None
        data = r.json()
    except Exception as e:
        LOG.debug("%s: check failed: %s", name, e)
        return None
    fn = globals()[cfg["extract"]]
    return fn(data, name)


def _ffprobe_bin() -> str:
    if FFMPEG_BIN:
        cand = FFMPEG_BIN.replace("ffmpeg", "ffprobe")
        if Path(cand).exists():
            return cand
    return shutil.which("ffprobe") or "ffprobe"


def _ffprobe_duration(path: Path) -> float:
    try:
        p = subprocess.run(
            [
                _ffprobe_bin(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return float((p.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def _ffmpeg_headers_arg(headers: dict) -> str:
    return "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"


def record_chunk(name: str, hls: str, duration_s: int, headers: dict) -> Optional[Path]:
    """Record duration_s of HLS via stream-copy. Refine headers fit the CDN."""
    out = Path(f"/tmp/{name}_{int(time.time())}.mp4")
    hdr = _ffmpeg_headers_arg(headers)
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-loglevel",
        "warning",
        "-headers",
        hdr,
        "-user_agent",
        headers.get("User-Agent", "Mozilla/5.0"),
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "4xx,5xx",
        "-reconnect_delay_max",
        "5",
        "-rw_timeout",
        "15000000",
        "-i",
        hls,
        "-t",
        str(duration_s),
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(out),
    ]
    LOG.info("Recording %s for %ds (copy) -> %s", name, duration_s, out.name)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _, err = proc.communicate(timeout=duration_s + 60)
        except subprocess.TimeoutExpired:
            LOG.warning("ffmpeg hard time, SIGINT finalize %s", out.name)
            try:
                proc.send_signal(signal.SIGINT)
                _, err = proc.communicate(timeout=25)
            except Exception:
                proc.kill()
                _, err = proc.communicate(timeout=10)
        if err:
            err_s = err.decode(errors="replace")[-500:]
            if err_s.strip():
                LOG.info("ffmpeg stderr: %s", err_s.replace("\n", " | "))
        if proc.returncode not in (0, None, 255, -2, 130):
            LOG.warning("ffmpeg exit=%s", proc.returncode)
    except Exception as e:
        LOG.error("ffmpeg spawn failed: %s", e)
        return None

    if not out.exists():
        return None
    size = out.stat().st_size
    dur = _ffprobe_duration(out)
    LOG.info("Recorded %s size=%.2f MB duration=%.1fs", out.name, size / 1_048_576, dur)
    if size < 100_000 or dur < 5.0:
        LOG.warning("Chunk unusable size=%d dur=%.1f — drop", size, dur)
        out.unlink(missing_ok=True)
        return None
    return out


def _copy_trim(src: Path, dst: Path, start: float, length: float) -> bool:
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-loglevel",
        "error",
        "-ss",
        str(max(0.0, start)),
        "-i",
        str(src),
        "-t",
        str(max(1.0, length)),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(dst),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        return p.returncode == 0 and dst.exists() and dst.stat().st_size > 50_000
    except Exception as e:
        LOG.warning("trim failed: %s", e)
        return False


def fit_for_telegram(path: Path) -> List[Path]:
    """If over TG_MAX_BYTES, split into <=2 playable parts; never silent-drop."""
    size = path.stat().st_size
    if size <= TG_MAX_BYTES:
        return [path]

    dur = _ffprobe_duration(path)
    if dur < 10:
        # Can't split usefully — drop (broken)
        LOG.warning("Oversize %.1f MB but dur=%.1fs — drop", size / 1_048_576, dur)
        path.unlink(missing_ok=True)
        return []

    # Ideal seconds per part under budget (with 5% slack)
    sec_budget = max(20.0, dur * (TG_MAX_BYTES / size) * 0.92)
    parts: List[Path] = []
    t = 0.0
    idx = 0
    while t < dur - 3 and idx < 4:  # at most 4 parts
        length = min(sec_budget, dur - t)
        if length < 8:
            break
        part = path.with_name(f"{path.stem}_p{idx}{path.suffix}")
        if _copy_trim(path, part, t, length):
            ps = part.stat().st_size
            pd = _ffprobe_duration(part)
            LOG.info("split part%d size=%.2f MB dur=%.1fs", idx, ps / 1_048_576, pd)
            if ps <= TG_MAX_BYTES and pd >= 5:
                parts.append(part)
            else:
                # still too big — shorten this part
                part.unlink(missing_ok=True)
                shorter = max(15.0, length * 0.7)
                if _copy_trim(path, part, t, shorter):
                    if part.stat().st_size <= TG_MAX_BYTES:
                        parts.append(part)
                        length = shorter
                    else:
                        part.unlink(missing_ok=True)
                        break
                else:
                    break
        else:
            break
        t += length
        idx += 1

    path.unlink(missing_ok=True)
    if not parts:
        LOG.warning("Could not fit %s for Telegram", path.name)
    return parts


def send_telegram(path: Path, name: str, viewers: int) -> bool:
    size_mb = path.stat().st_size / 1_048_576
    dur = _ffprobe_duration(path)
    mins = max(1, int(round(dur / 60.0))) if dur >= 5 else 0
    secs = int(dur) % 60 if dur >= 5 else 0
    cap = f"🎥 {name} | {mins}m{secs:02d}s | {size_mb:.0f} MB | {viewers} viewers"
    url = f"{TG_API}/sendVideo"
    LOG.info("Sending %s (%.2f MB, %.1fs) to TG", path.name, size_mb, dur)
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
        LOG.info("Sent %s to TG", path.name)
        return True
    # Fallback: document (same 50 MB hard limit, but sometimes works when video rejects)
    LOG.warning("sendVideo failed: %s — try sendDocument", j)
    url2 = f"{TG_API}/sendDocument"
    with path.open("rb") as f:
        r2 = requests.post(
            url2,
            data={"chat_id": CHAT_ID, "caption": cap},
            files={"document": (path.name, f, "video/mp4")},
            timeout=300,
        )
    try:
        j2 = r2.json()
    except Exception:
        LOG.error("TG doc bad response %s", r2.text[:200])
        return False
    if j2.get("ok"):
        LOG.info("Sent %s as document to TG", path.name)
        return True
    LOG.error("TG sendDocument failed: %s", j2)
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


def process_live(name: str, live: dict, duration_s: int, state: dict) -> None:
    """Record + send one chunk for a live model."""
    state[name] = {"status": "recording", "ts": time.time()}
    save_state(state)

    headers = live.get("headers") or _headers_for(name, live.get("hls", ""))
    chunk = record_chunk(name, live["hls"], duration_s, headers)
    if not chunk:
        # Retry once with master playlist + refresh
        master = live.get("master") or live["hls"]
        LOG.info("[%s] retry with master playlist", name)
        chunk = record_chunk(name, master, duration_s, headers)

    if chunk:
        parts = fit_for_telegram(chunk)
        for i, part in enumerate(parts):
            ok = send_telegram(part, name, live.get("viewers", 0))
            if ok:
                part.unlink(missing_ok=True)
            else:
                LOG.warning("Keeping %s after failed send", part.name)
        if not parts:
            LOG.warning("[%s] no deliverable parts", name)
    else:
        LOG.warning("[%s] record_chunk returned None", name)

    state[name] = {"status": "done", "ts": time.time()}
    save_state(state)


def main():
    server.start()
    LOG.info("=== Recorder starting on Render ===")
    LOG.info(
        "Poll=%ss chunk=%dmin target_bw=%d TG_max=%.0fMB models=%s",
        POLL_SEC,
        CHUNK_MIN,
        HLS_TARGET_BW,
        TG_MAX_BYTES / 1_048_576,
        list(MODELS),
    )
    if not FFMPEG_BIN or not Path(FFMPEG_BIN).exists():
        LOG.error("ffmpeg not found. Tried: %s", _candidates)
        sys.exit(1)
    LOG.info("ffmpeg: %s", FFMPEG_BIN)

    state = load_state()
    duration_s = CHUNK_MIN * 60
    iteration = 0

    while True:
        iteration += 1
        any_live = False
        try:
            for name in MODELS:
                live = check_live(name)
                if not live:
                    if iteration % 10 == 1:
                        LOG.info("[%s] offline", name)
                    state[name] = {"status": "offline", "ts": time.time()}
                    continue

                any_live = True
                LOG.info(
                    "[%s] LIVE viewers=%d segs=%d bw=%s",
                    name,
                    live["viewers"],
                    live["segs"],
                    live.get("bw"),
                )

                # Continuous series while still live: record back-to-back
                # Cap ~90 min of continuous record per detection wave to avoid stuck loops
                wave_start = time.time()
                while time.time() - wave_start < 90 * 60:
                    process_live(name, live, duration_s, state)
                    # Refresh immediately — no POLL_SEC wait while still live
                    live = check_live(name)
                    if not live:
                        LOG.info("[%s] no longer public-live after chunk", name)
                        state[name] = {"status": "offline", "ts": time.time()}
                        break
                    LOG.info(
                        "[%s] still LIVE viewers=%d — next chunk",
                        name,
                        live.get("viewers", 0),
                    )
                    time.sleep(2)  # tiny pause for CDN URL rotation
                save_state(state)

            save_state(state)
        except Exception as e:
            LOG.exception("loop error: %s", e)

        # Only sleep full poll when nobody was live (else continuous already ran)
        time.sleep(5 if any_live else POLL_SEC)


if __name__ == "__main__":
    main()
