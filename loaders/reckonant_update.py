"""reckonant_update.py - Reckonant's own daily update.

Runs Reckonant's loaders (copied from the Alert Engine, writing to the R1_ .. R16_ schemas),
then asks the Reckonant API to reload. Scheduled at 18:00 BST by install_reckonant_update.ps1.

    python reckonant_update.py                 run every step
    python reckonant_update.py cqc company     run only these steps (still in the usual order)

Environment (all optional unless noted):
    RK_REFRESH_TOKEN           needed for the last step (API reload); same value as the API's token
    RK_API_URL                 default http://localhost:8001
    RK_ACCOUNTS_NO_DOWNLOAD=1  use the accounts zips already in ACCOUNTS_DIR, do not download (today only)
    ACCOUNTS_DIR               folder of accounts zips (default D:\\Reckonant\\data\\Accounts)
    RK_LOG_DIR                 default D:\\Reckonant\\logs
    SMTP_APP_PASSWORD          e-mail app password; without it no e-mail is sent
"""
import os
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get("RK_LOG_DIR", r"D:\Reckonant\logs"))
API = os.environ.get("RK_API_URL", "http://localhost:8001").rstrip("/")

ACCOUNTS_ARGS = ["bulk", "--max-files", "12"] + (["--no-download"] if os.environ.get("RK_ACCOUNTS_NO_DOWNLOAD") == "1" else [])

# key, name shown in the log, script (None = API reload), arguments, time limit in minutes (in the order they run)
STEPS = [
    ("cqc",       "CQC (download, load, changes)",                   "register_cqc.py",             [],                          180),
    ("cqc_api",   "CQC API (changed locations and providers)",       "register_cqc_api.py",         ["changes"],                  60),
    ("gazette",   "Gazette (corporate + personal notices)",          "register_gazette.py",         ["update", "--days", "7"],   240),
    ("hmrc",      "HMRC tax defaulters",                             "register_hmrc_defaulters.py", [],                           30),
    ("sponsors",  "Home Office licence sponsors",                    "register_sponsors.py",        [],                           30),
    ("contracts", "Contracts Finder",                                "register_contracts.py",       [],                           90),
    ("findtender","Find a Tender",                                   "register_findtender.py",      [],                           90),
    ("payments",  "Payments Transparency Register",                  "register_payments.py",        [],                           60),
    ("company",   "Company master (Companies House)",                "register_company_master.py",  ["--limit", "1500"],         240),
    ("mapping",   "Company number / name columns",                   "register_company_columns.py", [],                            5),
    ("accounts",  "Companies House bulk accounts",                   "register_financials.py",      ACCOUNTS_ARGS,               180),
    ("financials","Company financials",                              "register_financials.py",      ["--limit", "600"],          120),
    ("columns",   "Register columns workbook",                       "export_register_columns.py",  [],                           30),
    ("api",       "Reckonant API reload",                            None,                          [],                           10),
]


def send_mail(subject, body):
    if os.environ.get("SMTP_APP_PASSWORD") is None:
        print("E-mail not sent: SMTP_APP_PASSWORD is not set.")
        return
    pw = "".join(os.environ["SMTP_APP_PASSWORD"].split())
    to = "chandramohan.msn@gmail.com"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, to, to
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
            s.login(to, pw)
            s.send_message(msg)
        print("E-mail sent.")
    except Exception as e:
        print("E-mail failed:", e)


def run_script(script, args, minutes, log):
    path = HERE / script
    t0 = time.time()
    log.write(f"\n===== {script} {' '.join(args)}  ({datetime.now():%H:%M:%S}) =====\n"); log.flush()
    if not path.exists():
        log.write(f"SKIPPED: {path} not found\n")
        return "FAILED", "script not found", 0
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    tail = []
    try:
        p = subprocess.Popen([sys.executable, "-u", str(path), *args], cwd=str(HERE), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace")

        def pump():
            for line in p.stdout:
                log.write(line); log.flush()
                if line.strip():
                    tail.append(line.rstrip()); del tail[:-6]

        th = threading.Thread(target=pump, daemon=True); th.start()
        deadline = t0 + minutes * 60
        while p.poll() is None:
            if time.time() > deadline:
                p.kill()
                log.write(f"\nTIMED OUT after {minutes} minutes\n")
                th.join(5)
                return "FAILED", f"timed out after {minutes} min", time.time() - t0
            time.sleep(2)
        th.join(10)
        if p.returncode == 0:
            return "ok", (tail[-1] if tail else "done"), time.time() - t0
        return "FAILED", f"exit {p.returncode}: " + " | ".join(tail[-3:]), time.time() - t0
    except Exception as e:
        return "FAILED", str(e), time.time() - t0


def reload_api(log):
    t0 = time.time()
    log.write(f"\n===== API reload ({datetime.now():%H:%M:%S}) =====\n"); log.flush()
    req = urllib.request.Request(API + "/api/refresh", method="POST",
                                 headers={"X-Refresh-Token": os.environ.get("RK_REFRESH_TOKEN", "")})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            msg = f"HTTP {r.status}"
            status = "ok"
    except urllib.error.HTTPError as e:
        msg, status = f"HTTP {e.code} (check RK_REFRESH_TOKEN)", "FAILED"
    except urllib.error.URLError as e:
        msg, status = f"API not reachable ({e.reason})", "FAILED"
    log.write(msg + "\n")
    return status, msg, time.time() - t0


def main():
    wanted = [a.lower() for a in sys.argv[1:] if not a.startswith("-")]
    keys = [s[0] for s in STEPS]
    unknown = [w for w in wanted if w not in keys]
    if unknown:
        print(f"Unknown step(s): {', '.join(unknown)}. Choose from: {', '.join(keys)}")
        return 2
    todo = [s for s in STEPS if not wanted or s[0] in wanted]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOG_DIR / "reckonant_update.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 12 * 3600:
        print("Another Reckonant update is running (lock file found). Stopping.")
        print("If it is stuck, delete:", lock)
        return 2
    lock.write_text(str(os.getpid()))

    start = datetime.now()
    logfile = LOG_DIR / f"reckonant_update_{start:%Y-%m-%d}.log"
    results = []
    try:
        with open(logfile, "a", encoding="utf-8") as log:
            log.write(f"\n########## Reckonant update started {start:%Y-%m-%d %H:%M:%S} ##########\n")
            for key, name, script, args, minutes in todo:
                print(f"{datetime.now():%H:%M:%S} {name} ...", flush=True)
                if script is None:
                    status, info, secs = reload_api(log)
                else:
                    status, info, secs = run_script(script, args, minutes, log)
                results.append((name, status, info, secs))
                print(f"{datetime.now():%H:%M:%S} {name}: {status} ({int(secs)} s) {info}", flush=True)
            log.write(f"\n########## Finished {datetime.now():%H:%M:%S} ##########\n")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass

    bad = [r for r in results if r[1] != "ok"]
    lines = [f"{'OK    ' if st == 'ok' else 'FAILED'}  {n} ({int(s)} s): {i}" for n, st, i, s in results]
    body = (f"Reckonant update {start:%d %b %Y}, started {start:%H:%M}, took "
            f"{int((datetime.now() - start).total_seconds() / 60)} min.\n\n" + "\n".join(lines)
            + f"\n\nFull log: {logfile}\n")
    subject = ("PROBLEM - " if bad else "") + f"Reckonant update {start:%d %b %Y}: " + ("some steps failed" if bad else "all steps ok")
    print("\n" + body)
    send_mail(subject, body)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
