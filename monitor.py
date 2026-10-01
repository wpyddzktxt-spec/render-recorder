#!/usr/bin/env python3
"""
24/7 recorder for JustKatrin (Stripchat), KatrinBloom (mybro/Stripchat wl)
and moonmaiden (BongaCams) on Render.
- Polls every 30s
- Validates HLS has real #EXTINF segments
- Stream-copy mid/low bitrate HLS so 8-min chunks stay under Telegram 50 MB
- On accidental oversize: split/trim and still deliver (never silent-drop good video)
- Continuous: while model still LIVE, next chunk starts immediately (no 30s gap)
"""
import base64
import hashlib
import http.server
import itertools
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
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
# Stripchat CDN rotates playlist hosts mid-stream → long single ffmpeg runs
# die with 404/403 after 1-3 min. Use short chunks + fresh master per chunk.
SC_CHUNK_MIN = int(os.environ.get("SC_CHUNK_MIN", "3"))
# 480p variant of the SC stream is ~1.4 Mbps; 3-min chunk ≈ 30 MB (fits TG).
HLS_TARGET_BW = int(os.environ.get("HLS_TARGET_BW", "1500000"))
TG_MAX_BYTES = int(os.environ.get("TG_MAX_BYTES", str(48 * 1024 * 1024)))
# Cap continuous record per detection wave (avoids stuck loops on stale liveness)
WAVE_MAX_MIN = int(os.environ.get("WAVE_MAX_MIN", "360"))

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
HEADERS_PLAIN = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*,*/*;q=0.8",
}

PLAYLIST_PORT = int(os.environ.get("PLAYLIST_PORT", "8765"))

# Known pkey -> pdkey pairs (community-maintained; screc / StreaMonitor)
MOUFLON_KEYS = {
    "Zeechoej4aleeshi": "ubahjae7goPoodi6",
    "Zokee2OhPh9kugh4": "Quean4cai9boJa5a",
    "Ook7quaiNgiyuhai": "EQueeGh2kaewa3ch",
}

_MOUFLON_PLACEHOLDER_RE = re.compile(r"^https?://\S+/media\.mp4\s*$")


def _mouflon_decode(encrypted_b64: str, key: str) -> str:
    """XOR base64 data with sha256(key) bytes -> UTF-8 string."""
    hash_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    encrypted_data = base64.b64decode(encrypted_b64 + "==")
    return bytes(a ^ b for (a, b) in zip(encrypted_data, itertools.cycle(hash_bytes))).decode("utf-8")


def clean_mouflon_playlist(text: str, pdkey: str) -> Optional[str]:
    """Decode MOUFLON v2 URIs into real segment URLs (absolute)."""
    if "#EXT-X-MOUFLON:URI:" not in text:
        return text
    out = []
    pending = None
    for line in text.splitlines():
        if line.startswith("#EXT-X-MOUFLON:URI:"):
            uri = line[len("#EXT-X-MOUFLON:URI:"):].strip()
            try:
                encoded_part = uri.split("_")[-2]
                decoded_part = _mouflon_decode(encoded_part[::-1], pdkey)
                pending = uri.replace(encoded_part, decoded_part)
            except Exception:
                pending = None
            continue
        if _MOUFLON_PLACEHOLDER_RE.match(line) and pending:
            out.append(pending)
            pending = None
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _known_pkey(text: str) -> Optional[str]:
    """First PSCH:v2 token in text that we have a pdkey for."""
    for tok in re.findall(r"#EXT-X-MOUFLON:PSCH:v2:(\S+)", text or ""):
        if tok in MOUFLON_KEYS:
            return tok
    return None


class PlaylistServer:
    """Local HLS proxy that decodes Stripchat MOUFLON v2 segment URLs.

    ffmpeg reads http://127.0.0.1:8765/playlist.m3u8; each request re-fetches
    the media playlist with psch=v2&pkey=<known>, decodes #EXT-X-MOUFLON:URI
    lines into real segment URLs and serves the clean playlist. Segment URLs
    point back to the CDN directly (no proxying needed).
    """

    def __init__(self, port: int = PLAYLIST_PORT):
        self.port = port
        self._lock = threading.Lock()
        self._media_url: Optional[str] = None
        self._psch: Optional[str] = None
        self._pkey: Optional[str] = None
        self._pdkey: Optional[str] = None
        self._headers: dict = {}
        self._master_url: Optional[str] = None
        self._cache_ts = 0.0
        self._cache_text: Optional[str] = None
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None

    def configure(self, media_url: str, psch: str, pkey: str, pdkey: str, headers: dict, master_url: Optional[str] = None) -> None:
        with self._lock:
            self._media_url = media_url
            self._psch = psch
            self._pkey = pkey
            self._pdkey = pdkey
            self._headers = dict(headers)
            self._master_url = master_url
            self._cache_ts = 0.0
            self._cache_text = None

    def start(self) -> None:
        if self._httpd:
            return
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), _PlaylistHandler)
        httpd.source = self  # type: ignore
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True, name="playlist-server").start()
        LOG.info("PlaylistServer on 127.0.0.1:%d", self.port)

    def get_clean_playlist(self) -> Optional[str]:
        with self._lock:
            if self._cache_text and time.time() - self._cache_ts < 2.0:
                return self._cache_text
            text = self._fetch_locked()
            if text:
                self._cache_text = text
                self._cache_ts = time.time()
            return text

    def _fetch_locked(self) -> Optional[str]:
        if not self._media_url:
            return None
        url = self._media_url
        if self._psch and self._pkey:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}psch={self._psch}&pkey={self._pkey}"
        try:
            r = requests.get(url, timeout=12, headers=self._headers)
        except Exception as e:
            LOG.debug("playlist fetch err: %s", e)
            return None
        if r.status_code == 200:
            return clean_mouflon_playlist(r.text, self._pdkey)
        if r.status_code == 403 and self._master_url:
            # Stream may have rotated keys; re-fetch master for fresh tokens
            try:
                rm = requests.get(self._master_url, timeout=12, headers=self._headers)
                pk = _known_pkey(rm.text)
                if pk:
                    self._pkey = pk
                    self._pdkey = MOUFLON_KEYS[pk]
                    sep = "&" if "?" in self._media_url else "?"
                    url2 = f"{self._media_url}{sep}psch=v2&pkey={self._pkey}"
                    r2 = requests.get(url2, timeout=12, headers=self._headers)
                    if r2.status_code == 200:
                        return clean_mouflon_playlist(r2.text, self._pdkey)
            except Exception as e:
                LOG.debug("master refresh err: %s", e)
        LOG.debug("playlist fetch status %s", r.status_code)
        return None


class _PlaylistHandler(http.server.BaseHTTPRequestHandler):
    server_version = "RecorderPlaylist/1.0"

    def do_GET(self):
        if self.path.split("?")[0] != "/playlist.m3u8":
            self.send_error(404)
            return
        text = self.server.source.get_clean_playlist()  # type: ignore
        if text is None:
            self.send_error(404)
            return
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


playlist_server = PlaylistServer()

MODELS = {
    "JustKatrin": {
        "platform": "stripchat",
        "check_url": "https://go.xxxiijmp.com/api/models?modelsList=JustKatrin&strict=1",
        "extract": "_extract_stripchat",
        "headers": HEADERS_SC,
    },
    "KatrinBloom": {
        "platform": "stripchat",  # mybro white-label of stripchat (same stream id 21286181)
        # Mirror API primary: returns the real HLS URL reliably.
        "check_url": "https://go.xxxiijmp.com/api/models?modelsList=KatrinBloom&strict=1",
        "extract": "_extract_stripchat",
        "headers": HEADERS_SC,
        # mybro alias API fallback (same stream id, isOnline signal)
        "fallback_url": "https://mybro.tv/api/v1/models/alias/katrinbloom",
        "fallback_extract": "_extract_bongacams",
        "fallback_headers": HEADERS_PLAIN,
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

    Adaptive probing for Stripchat MOUFLON v2:
    1. Fetch master; pick variant closest to HLS_TARGET_BW (not max — max blows TG 50 MB).
    2. If a known pkey is present, prefer the MOUFLON decode path via the local
       PlaylistServer (robust against CDN key rotation).
    3. Otherwise fall back to the plain media playlist (works while live).
    4. NEVER return the master as playable — that was the old failure wave
       (ffmpeg fails, check_live stays truthy, endless 90-min loop).
    """
    try:
        r = requests.get(url, timeout=12, headers=headers)
        if r.status_code != 200:
            return None
        text = r.text or ""
        if "#EXTINF" in text and "#EXT-X-STREAM-INF" not in text:
            # Already a media playlist (e.g. BongaCams, or plain Stripchat media)
            if "#EXT-X-MOUFLON:URI:" not in text:
                return url, text.count("#EXTINF"), 0
            pkey = _known_pkey(text)
            if pkey:
                return _serve_mouflon(url, pkey, headers, master_url=url, bw=0)
            return None
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
        # MOUFLON v2 path: known pkey in master -> local decode server
        pkey = _known_pkey(text)
        if pkey:
            served = _serve_mouflon(media_url, pkey, headers, master_url=url, bw=bw_picked)
            if served:
                return served
        # Plain media fallback (works while live)
        r2 = requests.get(media_url, timeout=12, headers=headers)
        if r2.status_code == 200:
            t2 = r2.text or ""
            if "#EXTINF" in t2:
                LOG.info("HLS plain media bw=%d target=%d url=...%s", bw_picked, HLS_TARGET_BW, media_url[-70:])
                return media_url, t2.count("#EXTINF"), bw_picked
        # Never return master as playable
        LOG.warning("media playlist unusable (mouflon+plain failed) — not falling back to master")
        return None
    except Exception as e:
        LOG.debug("HLS probe failed: %s", e)
        return None


def _serve_mouflon(media_url: str, pkey: str, headers: dict, master_url: Optional[str], bw: int) -> Optional[Tuple[str, int, int]]:
    """Configure the local PlaylistServer and verify it serves a decoded playlist."""
    pdkey = MOUFLON_KEYS[pkey]
    playlist_server.configure(media_url, "v2", pkey, pdkey, headers, master_url=master_url)
    local = f"http://127.0.0.1:{PLAYLIST_PORT}/playlist.m3u8"
    try:
        rl = requests.get(local, timeout=15, headers=HEADERS_PLAIN)
        if rl.status_code == 200 and "#EXTINF" in (rl.text or ""):
            LOG.info("HLS MOUFLON-v2 decoded pkey=%s bw=%d -> local proxy", pkey, bw)
            return local, (rl.text or "").count("#EXTINF"), bw
    except Exception as e:
        LOG.debug("local playlist probe failed: %s", e)
    return None


def _stream_key(url: str) -> str:
    """Extract numeric stream id from HLS URL for cross-model dedupe."""
    mm = re.search(r"/(\d{6,})/", url or "")
    return mm.group(1) if mm else (url or "")[:120]


def _extract_stripchat(data: dict, name: str = "JustKatrin", headers: Optional[dict] = None) -> Optional[dict]:
    if data.get("count", 0) == 0:
        return None
    m = (data.get("models") or [{}])[0]
    stream = m.get("stream") if isinstance(m.get("stream"), dict) else {}
    hls = stream.get("url") if stream else None
    if not hls:
        return None
    if headers is None:
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
        "key": _stream_key(hls),
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
        "key": _stream_key(hls),
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
    live = fn(data, name)

    # Fallback chain: e.g. KatrinBloom on mybro is online but streamUrl is empty,
    # so resolve the real HLS from the stripchat whitelabel mirror.
    if not live and cfg.get("fallback_url"):
        try:
            r2 = requests.get(cfg["fallback_url"], timeout=12)
            if r2.status_code == 200:
                fdata = r2.json()
                ffn = globals()[cfg["fallback_extract"]]
                live = ffn(fdata, name, cfg.get("fallback_headers"))
                if live:
                    LOG.info(
                        "[%s] resolved via fallback mirror viewers=%d key=%s",
                        name, live.get("viewers", 0), live.get("key"),
                    )
        except Exception as e:
            LOG.debug("%s: fallback check failed: %s", name, e)
    return live


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
        "-protocol_whitelist",
        "file,http,https,tcp,tls,crypto,data",
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


# Async Telegram delivery: the next chunk starts recording immediately while
# the previous one uploads, so the inter-chunk gap is only the live re-check.
_send_queue: "queue.Queue[Tuple[Path, str, int]]" = queue.Queue()


def _send_worker():
    from datetime import datetime as _dt, timezone as _tz
    while True:
        try:
            part, name, viewers = _send_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            ok = send_telegram(part, name, viewers)
            if ok:
                server.STATUS["last_chunk"] = f"{name} {_dt.now(_tz.utc).strftime('%H:%M:%S')} UTC ({part.stat().st_size // 1_048_576} MB)"
                part.unlink(missing_ok=True)
            else:
                LOG.warning("Keeping %s after failed send", part.name)
        except Exception as e:
            LOG.error("send worker error for %s: %s", part.name, e)


def process_live(name: str, live: dict, duration_s: int, state: dict) -> None:
    """Record one chunk; deliver to TG asynchronously (no inter-chunk stall)."""
    state[name] = {"status": "recording", "ts": time.time()}
    save_state(state)

    headers = live.get("headers") or _headers_for(name, live.get("hls", ""))
    chunk = record_chunk(name, live["hls"], duration_s, headers)
    if not chunk:
        # Retry once with a fresh check_live probe (CDN rotates hosts/keys)
        LOG.info("[%s] chunk failed — re-probe live state", name)
        fresh = check_live(name)
        if fresh and fresh.get("hls"):
            chunk = record_chunk(name, fresh["hls"], duration_s, fresh.get("headers") or headers)

    if chunk:
        parts = fit_for_telegram(chunk)
        for part in parts:
            _send_queue.put((part, name, live.get("viewers", 0)))
        if not parts:
            LOG.warning("[%s] no deliverable parts", name)
    else:
        LOG.warning("[%s] record_chunk returned None", name)

    state[name] = {"status": "done", "ts": time.time()}
    save_state(state)


def main():
    threading.Thread(target=_send_worker, daemon=True, name="tg-send").start()
    server.start()
    LOG.info("=== Recorder starting on Render ===")
    LOG.info(
        "Poll=%ss chunk=%dmin sc_chunk=%dmin target_bw=%d TG_max=%.0fMB models=%s",
        POLL_SEC,
        CHUNK_MIN,
        SC_CHUNK_MIN,
        HLS_TARGET_BW,
        TG_MAX_BYTES / 1_048_576,
        list(MODELS),
    )
    if not FFMPEG_BIN or not Path(FFMPEG_BIN).exists():
        LOG.error("ffmpeg not found. Tried: %s", _candidates)
        sys.exit(1)
    LOG.info("ffmpeg: %s", FFMPEG_BIN)

    try:
        playlist_server.start()
    except Exception as e:
        LOG.error("PlaylistServer failed to start: %s", e)

    state = load_state()
    iteration = 0
    # stream-key -> timestamp of last recorded chunk (dedupe white-label mirrors)
    recent_keys: Dict[str, float] = {}
    DEDUPE_SEC = int(os.environ.get("DEDUPE_SEC", "900"))  # 15 min

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

                # Skip white-label mirror of a stream recorded moments ago
                key = live.get("key") or ""
                if key and recent_keys.get(key, 0) > 0 and time.time() - recent_keys[key] < DEDUPE_SEC:
                        LOG.info("[%s] LIVE but stream %s already recorded recently — skip", name, key)
                        state[name] = {"status": "deduped", "ts": time.time()}
                        continue

                any_live = True
                duration_s = (SC_CHUNK_MIN if MODELS[name]["platform"] == "stripchat" else CHUNK_MIN) * 60
                LOG.info(
                    "[%s] LIVE viewers=%d segs=%d bw=%s key=%s chunk=%ds",
                    name,
                    live["viewers"],
                    live["segs"],
                    live.get("bw"),
                    key,
                    duration_s,
                )

                # Continuous series while still live: record back-to-back
                # Cap continuous record per detection wave to avoid stuck loops
                wave_start = time.time()
                while time.time() - wave_start < WAVE_MAX_MIN * 60:
                    process_live(name, live, duration_s, state)
                    if key:
                        recent_keys[key] = time.time()
                    # Refresh immediately — no POLL_SEC wait while still live.
                    # check_live re-resolves the master URL (CDN rotates hosts)
                    live = check_live(name)
                    if not live:
                        LOG.info("[%s] no longer public-live after chunk", name)
                        state[name] = {"status": "offline", "ts": time.time()}
                        break
                    key = live.get("key") or key
                    LOG.info(
                        "[%s] still LIVE viewers=%d — next chunk (fresh master)",
                        name,
                        live.get("viewers", 0),
                    )
                    time.sleep(1)  # tiny pause for CDN URL rotation
                save_state(state)

            save_state(state)
            # expose statuses to TG /status command
            try:
                server.STATUS["models"] = {k: dict(v) for k, v in state.items() if isinstance(v, dict)}
            except Exception:
                pass
        except Exception as e:
            LOG.exception("loop error: %s", e)

        # Only sleep full poll when nobody was live (else continuous already ran)
        time.sleep(5 if any_live else POLL_SEC)


if __name__ == "__main__":
    main()
