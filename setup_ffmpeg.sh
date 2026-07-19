#!/usr/bin/env bash
# Download a static ffmpeg build into $HOME/ffmpeg.
# Works on Render free-tier Python runtime (no apt, no root).
# Set FFMPEG_BIN=$HOME/ffmpeg/ffmpeg to point monitor.py at it.
set -euo pipefail

DEST="${HOME}/ffmpeg"
mkdir -p "$DEST"

# johnvansickle.com hosts full static ffmpeg builds (LGPL, ~80MB).
# We stream the tarball straight to disk; build memory stays low because
# tar/xz decode in <50MB chunks, well under Render's 512MB build cap.
URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
TMP="/tmp/ffmpeg-static.tar.xz"

echo "[setup_ffmpeg] downloading $URL"
curl -sSL --fail -o "$TMP" "$URL"

echo "[setup_ffmpeg] extracting"
tar -xJf "$TMP" -C /tmp/
SRC_DIR=$(ls -d /tmp/ffmpeg-*-amd64-static | head -1)
cp "$SRC_DIR/ffmpeg"  "$DEST/ffmpeg"
cp "$SRC_DIR/ffprobe" "$DEST/ffprobe"
chmod +x "$DEST/ffmpeg" "$DEST/ffprobe"
rm -rf "$SRC_DIR" "$TMP"

echo "[setup_ffmpeg] done. ffmpeg: $($DEST/ffmpeg -version 2>&1 | head -1)"
