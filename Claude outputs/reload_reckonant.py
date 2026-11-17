"""reload_reckonant.py - tells the Reckonant API to reload its data from PostgreSQL.

Run by daily_update.py as its last step (see the "reckonant" line in STEPS).
Can also be run by hand:  python reload_reckonant.py

Environment variables (optional):
    RK_API_URL        default http://localhost:8001
    RK_REFRESH_TOKEN  must match the token the Reckonant API was started with
"""
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("RK_API_URL", "http://localhost:8001").rstrip("/")
TOKEN = os.environ.get("RK_REFRESH_TOKEN", "")


def main():
    req = urllib.request.Request(
        API + "/api/refresh",
        method="POST",
        headers={"X-Refresh-Token": TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            body = r.read(300).decode("utf-8", "replace")
            print("Reckonant API reload:", r.status, body)
            return 0
    except urllib.error.URLError as e:
        # API not running, wrong or missing token (401/403), or timeout
        print("Reckonant API reload FAILED:", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
