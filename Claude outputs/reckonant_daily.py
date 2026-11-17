"""reckonant_daily.py - Reckonant daily refresh.

Tells the Reckonant API (port 8001) to reload its data from PostgreSQL, then
checks that it answers and reports how many companies it now holds.
Runs on its own. It does not touch the Alert Engine.

    python reckonant_daily.py

Optional environment variables:
    RK_API_URL        default http://localhost:8001
    RK_REFRESH_TOKEN  must match the token the Reckonant API was started with
    RK_LOG_DIR        default D:\\Reckonant\\logs
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = os.environ.get("RK_API_URL", "http://localhost:8001").rstrip("/")
TOKEN = os.environ.get("RK_REFRESH_TOKEN", "")
LOG_DIR = Path(os.environ.get("RK_LOG_DIR", r"D:\Reckonant\logs"))


def log(msg):
    now = datetime.now()
    line = f"{now:%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"reckonant_{now:%Y-%m-%d}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def call(path, method="GET", headers=None, timeout=600):
    req = urllib.request.Request(API + path, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main():
    log("Reckonant daily refresh started")
    try:
        status, body = call("/api/refresh", "POST", {"X-Refresh-Token": TOKEN})
        log(f"Reload requested: HTTP {status} {body[:300]}")
    except urllib.error.HTTPError as e:
        log(f"Reload FAILED: HTTP {e.code} (check RK_REFRESH_TOKEN matches the API's token)")
        return 1
    except urllib.error.URLError as e:
        log(f"Reload FAILED: API not reachable at {API} ({e.reason})")
        return 1

    try:
        _, body = call("/api/companies/page?offset=0&limit=1")
        total = json.loads(body).get("total")
        log(f"Check OK: API now holds {total} companies")
    except Exception as e:
        log(f"Check FAILED after reload: {e}")
        return 1

    log("Reckonant daily refresh finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
