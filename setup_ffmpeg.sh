#!/usr/bin/env bash
# Download static ffmpeg binary for Render free tier.
# Render free tier image lacks ffmpeg, and apt-get install fails
# (no root, restricted build). Use a pre-built static binary instead.
set -e

FFMPEG_DIR="/opt/ffmpeg"
FFMPEG_BIN="$FFMPEG_DIR/ffmpeg"

if [ -x "$FFMPEG_BIN" ]; then
    echo "ffmpeg already installed at $FFMPEG_BIN"
    $FFMPEG_BIN -version | head -1
    exit 0
fi

mkdir -p "$FFMPEG_DIR"
cd /tmp

# Use the BtbN build (smaller, ~30MB) - GPL licensed, has all common codecs
echo "Downloading static ffmpeg binary..."
curl -sSL --max-time 90 \
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
    -o ffmpeg.tar.xz

echo "Extracting..."
tar xJf ffmpeg.tar.xz
# Find the binary - directory name varies by version
FFMPEG_PATH=$(find . -name "ffmpeg" -type f -executable | head -1)
if [ -z "$FFMPEG_PATH" ]; then
    echo "ERROR: ffmpeg binary not found in archive"
    exit 1
fi

mv "$FFMPEG_PATH" "$FFMPEG_BIN"
chmod +x "$FFMPEG_BIN"
rm -rf ffmpeg.tar.xz ffmpeg-*

echo "ffmpeg installed:"
$FFMPEG_BIN -version | head -1
