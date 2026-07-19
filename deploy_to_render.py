#!/usr/bin/env python3
"""Deploy render-recorder to Render.com via API."""
import os
import sys
import time
import requests

API = "https://api.render.com/v1"


def main():
    key = os.environ.get("RENDER_API_KEY")
    if not key:
        print("ERROR: set RENDER_API_KEY env var")
        sys.exit(1)
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    bot_token = os.environ.get("BOT_TOKEN", "")
    chat_id = os.environ.get("CHAT_ID", "652")

    print("=== 1. Get owner ID ===")
    r = requests.get(f"{API}/owners", headers=headers, timeout=15)
    r.raise_for_status()
    owners = r.json()
    if not owners:
        print("No owners found")
        sys.exit(1)
    owner_id = owners[0]["owner"]["id"]
    print(f"  owner_id: {owner_id}")

    print("=== 2. Create Web Service from GitHub repo ===")
    payload = {
        "type": "web_service",
        "name": "auto-recorder",
        "ownerId": owner_id,
        "repo": "https://github.com/wpyddzktxt-spec/render-recorder",
        "branch": "main",
        "autoDeploy": "yes",
        "serviceDetails": {
            "region": "frankfurt",
            "plan": "free",
            "runtime": "python",
            "buildCommand": "apt-get update -qq && apt-get install -y -qq ffmpeg && pip install -r requirements.txt",
            "startCommand": "python monitor.py",
            "healthCheckPath": "/health",
            "envVars": [
                {"key": "POLL_SEC", "value": "30"},
                {"key": "CHUNK_MIN", "value": "10"},
                {"key": "PYTHONUNBUFFERED", "value": "1"},
                {"key": "CHAT_ID", "value": chat_id},
                {"key": "BOT_TOKEN", "value": bot_token},
            ],
        },
    }
    r = requests.post(f"{API}/services", headers=headers, json=payload, timeout=30)
    if r.status_code not in (201, 202):
        print(f"Create failed {r.status_code}: {r.text}")
        sys.exit(1)
    svc = r.json()
    svc_id = svc["id"]
    print(f"  service_id: {svc_id}")
    print(f"  name: {svc.get('name')}")
    print(f"  dashboard: https://dashboard.render.com/web/{svc_id}")

    print("=== 3. Wait for first deploy to start ===")
    for i in range(20):
        time.sleep(5)
        r = requests.get(f"{API}/services/{svc_id}", headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            print(f"  status: {d.get('serviceDetails',{}).get('buildPlan', d.get('suspended', 'unknown'))}")
        rd = requests.get(f"{API}/services/{svc_id}/deploys?limit=1", headers=headers, timeout=10)
        if rd.status_code == 200:
            deploys = rd.json()
            if deploys:
                print(f"  latest deploy status: {deploys[0].get('status')}")
                break

    print("=== DONE ===")
    print(f"  URL: https://dashboard.render.com/web/{svc_id}")
    print(f"  Logs: https://dashboard.render.com/web/{svc_id}/logs")
    print(f"  Service will be live at: https://auto-recorder.onrender.com")


if __name__ == "__main__":
    main()
