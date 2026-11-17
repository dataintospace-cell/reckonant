"""
daily_update.py - the AUTOMATIC update. Windows runs it every morning at 10:00; you can also run it by hand.

    python daily_update.py                 update EVERY register, in this order (a failure does not stop the next one)
    python daily_update.py cqc             only the CQC register
    python daily_update.py gazette         only the Gazette register (corporate + personal notices)
    python daily_update.py company         only the company master
    python daily_update.py cqc gazette     any combination, always run in the order below

Each register is ONE file and does its whole job (download / load, then build its tables):
    cqc        register_cqc.py              downloads new or replaced CQC files, loads them, works out what changed
    gazette    register_gazette.py update   the last 7 days of every Gazette Companies / People category (7 days, not 1, so late notices are caught),
                                            reads up to 300 new notice pages, then refreshes the clean Corporate / Personal tables
    company    register_company_master.py   adds new companies from the registers to the company master and logs every change (runs LAST)
Then it writes one log file and e-mails you a short summary (success or what failed).

Put it in the same folder as the register files (D:\\AlertEngine) and test it by hand first.  To schedule it at 10:00 every day:
right-click install_daily_10am.ps1 > Run with PowerShell.   To add a register later: add one line to STEPS.
Your password comes from pgpass.conf and the e-mail app password from the SMTP_APP_PASSWORD environment variable.
"""
import os
import smtplib
import ssl
import subprocess
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get("ALERT_LOG_DIR", r"D:\Reckonant\logs"))

# short name, name shown in the log, script, arguments, time limit in minutes   (in the order they run)
STEPS = [
    ("cqc",     "CQC (download, load, changes)",                "register_cqc.py",             [],                         180),
    ("cqc_api", "CQC API (locations and providers changed today)", "register_cqc_api.py", ["changes"],                     60),
    ("gazette", "Gazette (corporate + personal notices)",       "register_gazette.py",         ["update", "--days", "7"],  240),
    ("hmrc",    "HMRC tax defaulters (deliberate defaulters list)", "register_hmrc_defaulters.py", [],                         30),
    ("sponsors", "Home Office licence sponsor register",        "register_sponsors.py",        [],                         30),
    ("contracts", "Contracts Finder (tenders and awards)",       "register_contracts.py",       [],                         90),
    ("findtender", "Find a Tender (tenders and awards)",       "register_findtender.py",      [],                         90),
    ("payments", "Payments Transparency Register",         "register_payments.py",        [],                         60),
    ("company", "Company master (Companies House)",             "register_company_master.py",  ["--limit", "1500"],        240),   # after every register (links their companies: by number, then by exact name; up to 3000 name searches a run)
    ("mapping", "Company number / name columns for every register", "register_company_columns.py", [], 5),
    ("accounts", "Companies House bulk accounts (download new files, load your companies)", "register_financials.py", ["bulk", "--max-files", "12"], 180),
    ("financials", "Company financials (Companies House accounts)", "register_financials.py",  ["--limit", "600"],        120),   # after the company master; reads the accounts of the next 600 companies
    ("columns", "Register columns workbook (for the front end)", "export_register_columns.py", [],                         30),
    ("sheet",   "Register spreadsheet (kept in step with the code)", "make_register_sheet.py",     [],                          30),
    ("reckonant", "Reckonant API reload (picks up today's data)", "reload_reckonant.py",         [],                          10),   # last: tells the Reckonant API to reload from PostgreSQL
    # ("next",  "Next register",                                "register_next.py",            [],                          60),
]

NOTIFY_EMAIL = True                      # False = no e-mail
EMAIL_TO = "chandramohan.msn@gmail.com"
EMAIL_FROM = "chandramohan.msn@gmail.com"
SMTP_HOST = "smtp.gmail.com"
EMAIL_ONLY_ON_PROBLEM = False            # True = e-mail only when a step fails


def send_mail(subject, body, html=None):
    if not NOTIFY_EMAIL:
        return
    pw = os.environ.get("SMTP_APP_PASSWORD")
    if not pw:
        print("EMAIL NOT SENT: environment variable SMTP_APP_PASSWORD is not set.")
        return
    pw = "".join(pw.split())          # Google shows the app password in 4 blocks with spaces - remove them
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_FROM, EMAIL_TO
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")      # table version; plain text stays as the fallback
    # try port 465 (SSL) first, then port 587 (STARTTLS) - some networks / antivirus block one of them
    errors = []
    for port, use_ssl in ((465, True), (587, False)):
        try:
            ctx = ssl.create_default_context()
            if use_ssl:
                s = smtplib.SMTP_SSL(SMTP_HOST, port, context=ctx, timeout=60)
            else:
                s = smtplib.SMTP(SMTP_HOST, port, timeout=60)
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
            with s:
                s.login(EMAIL_FROM, pw)
                s.send_message(msg)
            print("Email sent to", EMAIL_TO, "(port %d)" % port)
            return
        except smtplib.SMTPAuthenticationError as e:
            print("EMAIL FAILED: Gmail refused the login (%s). Make a new App Password and set SMTP_APP_PASSWORD again." % e)
            return
        except Exception as e:
            errors.append("port %d: %s" % (port, e))
    print("EMAIL FAILED:", " | ".join(errors))


def run_step(name, script, args, minutes, log):
    path = HERE / script
    t0 = time.time()
    log.write(f"\n===== {name}: {script} {' '.join(args)}  ({datetime.now():%H:%M:%S}) =====\n"); log.flush()
    if not path.exists():
        log.write(f"SKIPPED: {path} not found\n")
        return "FAILED", "script not found", 0
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    tail = []
    try:
        p = subprocess.Popen([sys.executable, "-u", str(path), *args], cwd=str(HERE), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        deadline = t0 + minutes * 60
        import threading
        def pump():
            for line in p.stdout:
                log.write(line); log.flush()
                if line.strip():
                    tail.append(line.rstrip())
                    del tail[:-6]
        th = threading.Thread(target=pump, daemon=True); th.start()
        while p.poll() is None:
            if time.time() > deadline:
                p.kill()
                log.write(f"\nTIMED OUT after {minutes} minutes\n")
                th.join(5)
                return "FAILED", f"timed out after {minutes} min", time.time() - t0
            time.sleep(2)
        th.join(10)
        status = "ok" if p.returncode == 0 else "FAILED"
        return status, (tail[-1] if tail else f"exit {p.returncode}") if status == "ok" else f"exit {p.returncode}: " + " | ".join(tail[-3:]), time.time() - t0
    except Exception as e:
        return "FAILED", str(e), time.time() - t0


def pid_alive(pid):
    """True if a process with this id is running (the lock file holds the id of the run that made it)."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259             # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return True


def main():
    wanted = [a.lower() for a in sys.argv[1:] if not a.startswith("-")]
    unknown = [w for w in wanted if w not in [x[0] for x in STEPS]]
    if unknown:
        print(f"Unknown register(s): {', '.join(unknown)}. Choose from: {', '.join(x[0] for x in STEPS)}")
        return 2
    todo = [x for x in STEPS if not wanted or x[0] in wanted]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOG_DIR / "daily_update.lock"
    if "--unlock" in sys.argv:
        lock.unlink(missing_ok=True)
        print("Lock removed. Run the daily update again.")
        return 0
    if lock.exists():
        try:
            pid = int(lock.read_text().strip() or 0)
        except ValueError:
            pid = 0
        age = time.time() - lock.stat().st_mtime
        if pid and pid != os.getpid() and pid_alive(pid) and age < 6 * 3600:
            print(f"Another daily update is really running (process {pid}, started {int(age / 60)} min ago). Stopping.")
            print("If you are sure it is stuck, close it in Task Manager (python.exe) or run:  python daily_update.py --unlock")
            return 2
        print("Found a lock left by a run that was closed part-way - ignoring it.")
    lock.write_text(str(os.getpid()))
    start = datetime.now()
    logfile = LOG_DIR / f"daily_{start:%Y-%m-%d}.log"
    results = []
    try:
        with open(logfile, "a", encoding="utf-8") as log:
            log.write(f"\n########## Daily update started {start:%Y-%m-%d %H:%M:%S} ##########\n")
            for key, name, script, args, minutes in todo:
                print(f"{datetime.now():%H:%M:%S} {name} ...", flush=True)
                status, info, secs = run_step(name, script, args, minutes, log)
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
    body = (f"Daily update {start:%d %b %Y}, started {start:%H:%M}, took {int((datetime.now() - start).total_seconds() / 60)} min.\n\n"
            + "\n".join(lines) + f"\n\nFull log: {logfile}\n")
    subject = ("PROBLEM - " if bad else "") + f"Alert Engine daily update {start:%d %b %Y}: " + ("some steps failed" if bad else "all steps ok")
    html = None
    try:                                  # professional table report (daily_report.py); falls back to the plain list above
        import alert_tracker_ui as ui
        import daily_report
        keyed = [(todo[i][0], n, st, inf, sec) for i, (n, st, inf, sec) in enumerate(results)]
        with ui.connect() as con:
            subject, html, body = daily_report.build_report(start, datetime.now(), keyed, con, ui.REGISTERS, logfile)
    except Exception as e:
        print("Report table could not be built, sending the plain list:", e)
    print("\n" + body)
    if bad or not EMAIL_ONLY_ON_PROBLEM:
        send_mail(subject, body, html)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
