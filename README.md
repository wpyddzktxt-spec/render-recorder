# Render Recorder

24/7 auto-recorder for JustKatrin (Stripchat) and moonmaiden (BongaCams) on Render free tier.

## Files
- `monitor.py` — main loop, polls every 30s, records 10-min chunks via ffmpeg, sends to Telegram
- `server.py` — tiny health-check server (keeps Render free tier awake)
- `render.yaml` — Render service definition
- `requirements.txt` — Python deps (just `requests`)

## Setup
1. Push to a GitHub repo
2. Connect repo on render.com → New Web Service → it reads `render.yaml` automatically
3. Set environment variables in Render dashboard:
   - `BOT_TOKEN` = your Telegram bot token
   - `CHAT_ID` = target chat ID
4. Free tier: 750 hours/month, region Frankfurt

## How it works
- Polls every 30s
- For each model: GET status API, parse HLS URL, verify playlist has `#EXTINF` segments
- If live with segments: record 10-min chunk via ffmpeg `-c copy` to /tmp, send to Telegram as video
- State persists in `/tmp/recorder_state.json` to avoid duplicate work on restarts
- Health endpoint at `/` and `/health` keeps the Render service awake

## Cron on Render
The `*/2 * * * *` cron in GitHub Actions (record-jk.yml) is kept as backup.
The Render monitor is the primary 24/7 service.
