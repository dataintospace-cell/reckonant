"""register_cqc.py -- the CQC register (care directory, with filters, with ratings, deactivated locations).  ONE file.

  python register_cqc.py                  = update + changes   (this is what daily_update.py runs)
  python register_cqc.py update           only download / import the CQC files
  python register_cqc.py diff             only work out what changed (every missing diff table)
  python register_cqc.py diff --latest    only the newest pair of each table
  python register_cqc.py --force          import everything again / build all diff tables again
  python register_cqc.py update --history look in the History folders again for files you put there by hand

UPDATE reads the CQC page every run and picks the newest file of each register. A new file name is imported as a new dated table.
A file that CQC replaced under the SAME name (new size / date on the CQC site) is detected and imported again as a new table
dated with the day CQC changed it. Files that have not changed are skipped.  The log of each run is in d:/AlertEngine/CQC/logs.

DIFF compares every table with the one before it and saves the differences in the same schema (diff_<previous>_<current>_<name>):
change_type ADDED / REMOVED / CHANGED, key_value, changed_columns and a _prev and _curr column for every column.

Settings (folders, e-mail, database) are at the top of each of the two parts below.   The text of each part follows. ------------------------

==== update ====
"""
import csv
import os
import re
import shutil
import smtplib
import ssl
import sys
from datetime import datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, unquote

import gdown
import psycopg
import requests
from psycopg import sql
from python_calamine import CalamineWorkbook


# =====================================================================
#  1. Download and import the CQC files  (was cqc_updated.py)
# =====================================================================

# =====================================================================
#  Settings: change these
# =====================================================================
PAGE_URL = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
DRIVE_FOLDER_URL = "https://drive.google.com/drive/folders/1x7jHU6i93to0_98z-HLzVg0EU6LL0LYh?usp=sharing"

BASE_DIR = Path(r"D:\Reckonant\data\CQC")          # logs and the Google Drive register files
HISTORY_DIR = Path(r"D:\Reckonant\data\History")    # one sub-folder per category (see below)

# The four "folders" in PostgreSQL (schemas). They are created only if missing.
# Files for each one are kept in a matching folder under HISTORY_DIR.
# Order matters when a loose file has to be guessed from its name (most specific first).
CATEGORIES = [
    dict(schema="R4_deactivated_locations",
         folder_key="deactivatedlocations", file_keys=["deactivated"],
         default_base="deactivated_locations"),
    dict(schema="R3_care_directory_with_ratings",
         folder_key="caredirectorywithratings", file_keys=["rating"],
         default_base="latest_ratings"),
    dict(schema="R2_care_directory_with_filters",
         folder_key="caredirectorywithfilters",
         file_keys=["hsca", "filters", "activelocations", "dualregistration"],
         default_base="hsca_active_locations"),
    dict(schema="R1_cqc_care_directory",
         folder_key="cqccaredirectory", file_keys=["directory"],
         default_base="cqc_directory"),
]
CAT_BY_SCHEMA = {c["schema"]: c for c in CATEGORIES}

# What is downloaded from the CQC page on every run.
# "sheets" = sheets of the .ods file that are imported (one table each).
ONLINE_JOBS = [
    {
        "name": "CQC care directory (csv)",
        "schema": "R1_cqc_care_directory",
        "type": "csv",
    },
    {
        "name": "Care directory with filters",
        "schema": "R2_care_directory_with_filters",
        "type": "ods",
        "url_keyword": "hsca_active_locations",
        "link_text": "care directory with filters",
        "sheets": ["HSCA_Active_Locations", "Dual_Registration_Locations"],
    },
    {
        "name": "Care directory with ratings",
        "schema": "R3_care_directory_with_ratings",
        "type": "ods",
        "url_keyword": "latest_ratings",
        "link_text": "care directory with ratings",
        "sheets": ["Locations", "Providers"],
    },
    {
        "name": "Deactivated locations",
        "schema": "R4_deactivated_locations",
        "type": "ods",
        "url_keyword": "deactivated_locations",
        "link_text": "deactivated locations",
        "sheets": ["Deactivated_Locations"],
        "fallback_largest": True,     # if that sheet name is missing, use the biggest sheet
    },
]

# CQC register files in Google Drive (unchanged: still saved in the "public" schema
# with <month>_<year>_<name> table names)
INCLUDE_DRIVE = True
DRIVE_SHEETS = {
    "nominated_individuals": "nominated_individuals",
    "Registered_managers": "registered_managers",
    "Regulated_activities": "regulated_activities",
}

MIN_SHEET_COLUMNS = 3      # history import: sheets with fewer columns (notes, contents) are skipped

# Command line options:
#   python cqc_updated.py             normal run (history import only the very first time)
#   python cqc_updated.py --history   look in the History folders again for new files
#   python cqc_updated.py --force     import everything again (overwrites tables)
FORCE_REIMPORT = "--force" in sys.argv
RUN_HISTORY_AGAIN = "--history" in sys.argv
DELETE_ODS_AFTER = False

DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "postgres"
DB_USER = "postgres"       # password comes from pgpass.conf

# Email alerts (set NOTIFY_EMAIL = False to switch off)
NOTIFY_EMAIL = True
EMAIL_TO = "chandramohan.msn@gmail.com"
EMAIL_FROM = "chandramohan.msn@gmail.com"
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
NOTIFY_ON_ERROR = True
EMAIL_MAX_LINES = 40
# =====================================================================

HEADERS = {"User-Agent": "Mozilla/5.0 (CQC data import script)"}
LOG_TABLE = "cqc_import_log"              # in the public schema
HISTORY_MARKER = ("__history_import__", "done")
CONVERTED_DIR = "_converted_csv"          # sheets converted to csv are saved here
SPREADSHEET_EXT = (".ods", ".xlsx", ".xls")
NEW_ITEMS = []   # (table, source file, rows) for the email


# =====================================================================
#  Email
# =====================================================================
def send_email(subject, body):
    if not NOTIFY_EMAIL:
        return
    app_password = os.environ.get("SMTP_APP_PASSWORD")
    if not app_password:
        print("EMAIL NOT SENT: environment variable SMTP_APP_PASSWORD is not set.")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_FROM, EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(),
                              timeout=60) as server:
            server.login(EMAIL_FROM, app_password)
            server.send_message(msg)
        print("Email sent to", EMAIL_TO)
    except Exception as e:
        print("EMAIL FAILED:", e)


def notify_new_files():
    if not NEW_ITEMS:
        print("No new files. No email sent.")
        return
    lines = [f"{len(NEW_ITEMS)} table(s) were created or updated:", ""]
    for table, source, rows in NEW_ITEMS[:EMAIL_MAX_LINES]:
        lines.append(f"- {table}: {rows:,} rows  (from {source})")
    if len(NEW_ITEMS) > EMAIL_MAX_LINES:
        lines.append(f"... and {len(NEW_ITEMS) - EMAIL_MAX_LINES} more (see the log file).")
    lines += ["", f"Files saved under: {HISTORY_DIR}", f"Run time: {datetime.now():%d %B %Y %H:%M}"]
    send_email(f"CQC data: new files imported ({datetime.now():%d %b %Y})", "\n".join(lines))


# =====================================================================
#  CQC web page links
# =====================================================================
class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((urljoin(PAGE_URL, self._href), "".join(self._text).strip()))
            self._href = None


def get_page_links():
    r = requests.get(PAGE_URL, headers=HEADERS, timeout=60)
    r.raise_for_status()
    p = LinkParser()
    p.feed(r.text)
    return p.links


def newest(cands):
    """Several links for the same file: the one with the latest date in its name (else the first on the page)."""
    return max(enumerate(cands), key=lambda ic: (name_date(filename_from_url(ic[1])), -ic[0]))[1] if cands else None


def find_csv_url(links):
    url = newest([h for h, t in links if h.lower().endswith(".csv") and "directory" in h.lower()]) \
        or newest([h for h, t in links if "care directory" in t.lower() and "csv" in t.lower()])
    if not url:
        raise SystemExit("Could not find the CQC directory CSV link.")
    return url


def find_ods_url(links, url_keyword, link_text):
    url = newest([h for h, t in links if h.lower().endswith(".ods") and url_keyword in h.lower()]) \
        or newest([h for h, t in links if link_text in t.lower() and ".ods" in h.lower()])
    if not url:
        raise SystemExit(f"Could not find the ODS link for '{link_text}'.")
    return url


# =====================================================================
#  Has the file on the CQC site changed? (same name, new content)
# =====================================================================
def remote_signature(url):
    """Size + last-modified of the file on the CQC site, without downloading it. -> (signature text or None, last-modified date or None)"""
    try:
        r = requests.head(url, headers=HEADERS, timeout=60, allow_redirects=True)
        if r.status_code >= 400 or not (r.headers.get("Content-Length") or r.headers.get("Last-Modified")):
            r = requests.get(url, headers=HEADERS, timeout=60, stream=True)
            r.close()
        h = r.headers
        lm = h.get("Last-Modified")
        sig = f"{h.get('Content-Length', '')}|{lm or ''}" if (h.get("Content-Length") or lm) else (h.get("ETag") or None)
        lm_date = None
        if lm:
            try:
                from email.utils import parsedate_to_datetime
                lm_date = parsedate_to_datetime(lm).astimezone().replace(tzinfo=None)
            except Exception:
                pass
        return sig, lm_date
    except Exception as e:
        print("  (could not check the file on the CQC site:", e, ")")
        return None, None


def stored_signature(conn, key):
    r = conn.execute(sql.SQL("SELECT table_name FROM {} WHERE source_file = %s AND table_name LIKE '__sig__:%%' ORDER BY imported_at DESC LIMIT 1")
                     .format(sql.Identifier(LOG_TABLE)), (key,)).fetchone()
    return r[0][len("__sig__:"):] if r else None


def store_signature(conn, key, sig):
    if sig:
        log_row(conn, key, "__sig__:" + sig, 0)


def dated_name(name, when):
    """'28_October_2026_CQC_directory.csv' + 4 Nov 2026 -> '04_November_2026_CQC_directory.csv' (no date in the name: it is put in front)."""
    stamp = f"{when.day:02d}_{when.strftime('%B')}_{when.year}"
    return DATE_RE.sub(stamp, name, count=1) if DATE_RE.search(name) else f"{stamp}_{name}"


# =====================================================================
#  Names and dates
# =====================================================================
DATE_RE = re.compile(r"(\d{1,2})[ _-]+([A-Za-z]+)[ _-]+(\d{4})")


def name_date(filename):
    """'01_October_2026_x.ods' -> datetime(2026, 10, 1); datetime.min if no date in the name."""
    m = DATE_RE.search(filename)
    if m:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt)
            except ValueError:
                pass
    return datetime.min


def file_date(path):
    """Date used in the table name: from the file name, else the file's modified date."""
    d = name_date(path.name)
    if d == datetime.min:
        d = datetime.fromtimestamp(path.stat().st_mtime)
        print(f"WARNING: no date in the name '{path.name}'. Using its modified date "
              f"{d:%d %B %Y} for the table name.")
    return d


def date_prefix(d):
    """datetime(2026, 10, 1) -> '01_october_2026'"""
    return f"{d.day:02d}_{d.strftime('%B').lower()}_{d.year}"


def clean_name(s):
    return re.sub(r"\W+", "_", s.strip()).strip("_").lower()


def make_table(prefix, base, used):
    """Table name = day_month_year_name, at most 63 characters and unique inside one file."""
    base = clean_name(base) or "data"
    name = f"{prefix}_{base}"
    if len(name) > 63:
        name = name[:63].rstrip("_")
    n = 2
    while name in used:
        name = f"{name[:58].rstrip('_')}_{n}"
        n += 1
    used.add(name)
    return name


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def filename_from_url(url):
    return unquote(url.split("/")[-1].split("?")[0])


# Used only by the Google Drive part (monthly folders and monthly table names)
def month_year(filename):
    d = name_date(filename)
    return (d if d != datetime.min else datetime.now()).strftime("%B_%Y")


def month_folder(filename):
    folder = BASE_DIR / month_year(filename)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# =====================================================================
#  History folders on disk
# =====================================================================
_FOLDER_CACHE = {}


def match_folder_category(folder_name):
    """Which category does a History sub-folder belong to? (None if unknown)"""
    n = norm(folder_name)
    for cat in CATEGORIES:
        if cat["folder_key"] in n or n == norm(cat["schema"]):
            return cat
    return None


def classify_file(filename):
    """For loose files placed directly in HISTORY_DIR: guess the category from the name."""
    n = norm(filename)
    for cat in CATEGORIES:
        if any(k in n for k in cat["file_keys"]):
            return cat
    return None


def category_folder(cat):
    """The History folder for a category. An existing, matching folder is used;
    otherwise HISTORY_DIR\\<schema name> is created."""
    key = cat["schema"]
    if key in _FOLDER_CACHE:
        return _FOLDER_CACHE[key]
    folder = None
    if HISTORY_DIR.exists():
        for p in sorted(HISTORY_DIR.iterdir()):
            if (p.is_dir() and not p.name.startswith(("_", "."))
                    and match_folder_category(p.name) is cat):
                folder = p
                break
    if folder is None:
        folder = HISTORY_DIR / cat["schema"]
        folder.mkdir(parents=True, exist_ok=True)
    _FOLDER_CACHE[key] = folder
    return folder


def is_data_file(p):
    return (p.is_file() and p.suffix.lower() in SPREADSHEET_EXT + (".csv",)
            and not p.name.startswith((".", "~")) and not p.name.endswith("#"))


def collect_history_files():
    """Returns [(category, file path, category folder)], oldest first."""
    if not HISTORY_DIR.exists():
        print(f"History folder not found: {HISTORY_DIR}")
        return []
    found = []
    for p in sorted(HISTORY_DIR.iterdir()):
        if p.is_dir():
            if p.name.startswith(("_", ".")):
                continue
            cat = match_folder_category(p.name)
            if cat is None:
                print(f"WARNING: folder '{p.name}' does not match any category. Ignored.")
                continue
            for f in p.rglob("*"):
                if is_data_file(f) and CONVERTED_DIR not in f.relative_to(p).parts:
                    found.append((cat, f, p))
        elif is_data_file(p):
            cat = classify_file(p.name)
            if cat is None:
                print(f"WARNING: could not tell which category '{p.name}' belongs to. Ignored.")
                continue
            found.append((cat, p, category_folder(cat)))
    found.sort(key=lambda t: (name_date(t[1].name), t[1].name))
    return found


# =====================================================================
#  Downloads
# =====================================================================
def fetch_online_file(url, name, folder):
    """Saves the file in the category folder. Reuses a copy that is already on disk."""
    target = folder / safe_name(name)
    if target.exists() and target.stat().st_size > 0:
        print("Already downloaded:", target)
        return target
    if BASE_DIR.exists():                      # copy from the older monthly download folders
        for old in BASE_DIR.glob(f"*/{name}"):
            if old.is_file() and old.stat().st_size > 0:
                shutil.copy2(old, target)
                print("Copied existing download:", old, "->", target)
                return target
    print("Downloading", url)
    tmp = target.with_name(target.name + ".part")
    with requests.get(url, headers=HEADERS, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
    tmp.replace(target)
    print("Saved to:", target)
    return target


def download_drive_file(file_id, name):
    target = month_folder(name) / safe_name(name)
    if target.exists() and target.stat().st_size > 0:
        print("Already downloaded:", target)
        return target
    print("Downloading from Drive:", name)
    tmp = target.with_name(target.name + ".part")
    result = gdown.download(id=file_id, output=str(tmp), quiet=False)
    if not result or not tmp.exists() or tmp.stat().st_size == 0:
        raise SystemExit(f"Could not download '{name}' from Google Drive.")
    tmp.replace(target)
    print("Saved to:", target)
    return target


# =====================================================================
#  Spreadsheet sheet -> CSV
# =====================================================================
def cell_to_text(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def largest_sheet_name(wb):
    best, best_rows = None, -1
    for n in wb.sheet_names:
        sheet = wb.get_sheet_by_name(n)
        rows = getattr(sheet, "total_height", None)
        if rows is None:
            rows = len(sheet.to_python())
        if rows > best_rows:
            best, best_rows = n, rows
    return best


def sheet_to_csv(wb, sheet_name, out_path):
    """Converts one sheet to a csv file. An existing csv is reused."""
    if out_path.exists() and out_path.stat().st_size > 0:
        print("CSV already exists:", out_path)
        return out_path
    print(f"Reading sheet '{sheet_name}' (this can take a minute)...", flush=True)
    data = wb.get_sheet_by_name(sheet_name).to_python()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for row in data:
            w.writerow([cell_to_text(v) for v in row])
    tmp.replace(out_path)
    print("Saved:", out_path)
    return out_path


# =====================================================================
#  CSV reading and header detection
# =====================================================================
def read_rows(path):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.reader(f))
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"Could not decode the CSV file: {path}")


def find_header_index(rows, scan=20):
    """First row that is nearly as wide as the widest of the first `scan` rows."""
    counts = [sum(1 for c in r if c.strip()) for r in rows[:scan]]
    if not counts or max(counts) == 0:
        raise ValueError("no header row")
    threshold = max(counts) * 0.8
    for i, c in enumerate(counts):
        if c >= threshold:
            return i


def clean_headers(raw_header):
    header, seen = [], {}
    for i, name in enumerate(raw_header):
        name = re.sub(r"\W+", "_", name.strip()).strip("_").lower()[:55]
        if not name:
            name = f"col_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        header.append(name)
    return header


# =====================================================================
#  PostgreSQL
# =====================================================================
def ensure_log_table(conn):
    conn.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {} (
            source_file   TEXT NOT NULL,
            table_name    TEXT NOT NULL,
            rows_imported INTEGER,
            imported_at   TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (source_file, table_name)
        )""").format(sql.Identifier(LOG_TABLE)))


def ensure_schemas(conn):
    """Creates the four schemas ("folders") only if they do not exist yet."""
    for cat in sorted(CATEGORIES, key=lambda c: c["schema"]):
        name = cat["schema"]
        exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (name,)).fetchone()
        if exists:
            print(f"Schema {name}: already exists (not created again)")
        else:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
            print(f"Schema {name}: CREATED")


def qname(schema, table):
    """How a table is written in the log: plain name for public, schema.table otherwise."""
    return table if schema == "public" else f"{schema}.{table}"


def source_key(schema, filename):
    return f"{schema}/{filename}"


def log_row(conn, source, table_name, rows):
    conn.execute(sql.SQL("""
        INSERT INTO {} (source_file, table_name, rows_imported, imported_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (source_file, table_name)
        DO UPDATE SET rows_imported = EXCLUDED.rows_imported, imported_at = now()
        """).format(sql.Identifier(LOG_TABLE)), (source, table_name, rows))


def log_exists(conn, source, table_name):
    return conn.execute(
        sql.SQL("SELECT 1 FROM {} WHERE source_file = %s AND table_name = %s")
        .format(sql.Identifier(LOG_TABLE)), (source, table_name)).fetchone() is not None


def file_done(conn, key):
    """True if every sheet of this file was already imported."""
    return (not FORCE_REIMPORT) and log_exists(conn, key, "__done__")


def mark_done(conn, key):
    log_row(conn, key, "__done__", 0)


def already_imported(conn, source, schema, table):
    if FORCE_REIMPORT:
        return False
    if not log_exists(conn, source, qname(schema, table)):
        return False
    reg = f'"{schema}"."{table}"'
    return conn.execute("SELECT to_regclass(%s)", (reg,)).fetchone()[0] is not None


def resolve_table(conn, base, source):
    """Google Drive files only: <month>_<year>_<base>. If the file was imported before,
    its earlier table name is reused."""
    row = conn.execute(
        sql.SQL("SELECT table_name FROM {} WHERE source_file = %s AND table_name ~ %s "
                "ORDER BY imported_at DESC LIMIT 1").format(sql.Identifier(LOG_TABLE)),
        (source, f"^[a-z]+_[0-9]{{4}}_{base}$")).fetchone()
    return row[0] if row else f"{month_year(source).lower()}_{base}"


def import_csv(conn, csv_path, schema, table, source_file, min_cols=1):
    """Imports a csv into schema.table (replacing the table if it exists).
    Returns the number of rows, or None if the file has no usable data."""
    rows = read_rows(csv_path)
    try:
        h_idx = find_header_index(rows)
    except ValueError:
        print(f"SKIP (empty sheet, no header found): {csv_path.name}")
        return None
    header = clean_headers(rows[h_idx])
    data_rows = [r for r in rows[h_idx + 1:] if any(c.strip() for c in r)]
    if len(header) < min_cols or not data_rows:
        print(f"SKIP (only {len(header)} column(s) and {len(data_rows)} data row(s)): {csv_path.name}")
        return None

    print(f"\n[{schema}.{table}] header on line {h_idx + 1} (skipped {h_idx} line(s) above it)")
    print("Columns:", header)
    print("Data rows:", len(data_rows), flush=True)

    target = sql.Identifier(schema, table)
    # One transaction: an existing table is replaced only if the new import succeeds.
    with conn.transaction():
        cols = sql.SQL(", ").join(sql.SQL("{} TEXT").format(sql.Identifier(c)) for c in header)
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(target))
        conn.execute(sql.SQL("CREATE TABLE {} ({})").format(target, cols))

        with conn.cursor() as cur:
            with cur.copy(sql.SQL("COPY {} FROM STDIN").format(target)) as copy:
                for row in data_rows:
                    row = (row + [""] * len(header))[:len(header)]
                    copy.write_row([v if v != "" else None for v in row])

        count = conn.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(target)).fetchone()[0]
        log_row(conn, source_file, qname(schema, table), count)

    print(f"Rows imported into {schema}.{table}: {count:,}")
    NEW_ITEMS.append((f"{schema}.{table}", source_file, count))
    return count


# =====================================================================
#  Importing one file (history or newly downloaded)
# =====================================================================
def process_workbook(conn, path, cat, folder, wanted=None, fallback_largest=False, min_cols=1):
    schema = cat["schema"]
    key = source_key(schema, path.name)
    prefix = date_prefix(file_date(path))

    wb = CalamineWorkbook.from_path(str(path))
    print("Sheets in file:", wb.sheet_names, flush=True)

    # List of (real sheet name, name used in the table)
    if wanted is None:
        sheets = [(n, n) for n in wb.sheet_names]
    else:
        lookup = {n.lower(): n for n in wb.sheet_names}
        sheets = []
        for w in wanted:
            real = lookup.get(w.lower())
            if real is None and fallback_largest:
                real = largest_sheet_name(wb)
                print(f"Sheet '{w}' not found. Using the largest sheet instead: '{real}'")
            if real is None:
                raise SystemExit(f"Sheet '{w}' not found in {path.name}. "
                                 f"Sheets in file: {wb.sheet_names}")
            sheets.append((real, w))

    used = set()
    out_dir = folder / CONVERTED_DIR
    for real, base in sheets:
        table = make_table(prefix, base, used)
        if already_imported(conn, key, schema, table):
            print(f"SKIP: {schema}.{table} already imported.")
            continue
        csv_path = sheet_to_csv(wb, real, out_dir / f"{prefix}_{clean_name(real)}.csv")
        import_csv(conn, csv_path, schema, table, key, min_cols=min_cols)
    del wb


def process_file(conn, cat, path, folder, wanted=None, fallback_largest=False):
    """Converts every sheet of a spreadsheet to csv and imports it, or imports a csv directly.
    `wanted` = list of sheets to import (None = all sheets that contain data)."""
    schema = cat["schema"]
    key = source_key(schema, path.name)
    print(f"\n----- {path.name}  ->  schema {schema} -----", flush=True)

    if path.suffix.lower() == ".csv":
        prefix = date_prefix(file_date(path))
        base = clean_name(DATE_RE.sub(" ", path.stem)) or cat["default_base"]
        table = make_table(prefix, base, set())
        if already_imported(conn, key, schema, table):
            print(f"SKIP: {schema}.{table} already imported.")
        else:
            import_csv(conn, path, schema, table, key)
    else:
        min_cols = MIN_SHEET_COLUMNS if wanted is None else 1
        process_workbook(conn, path, cat, folder, wanted, fallback_largest, min_cols)

    mark_done(conn, key)


# =====================================================================
#  One-time import of the History folders
# =====================================================================
def history_needed(conn):
    if FORCE_REIMPORT or RUN_HISTORY_AGAIN:
        return True
    return not log_exists(conn, *HISTORY_MARKER)


def run_history(conn):
    print("\n=== History import ===")
    files = collect_history_files()
    print(f"{len(files)} file(s) found in {HISTORY_DIR}")
    for n, (cat, path, folder) in enumerate(files, 1):
        key = source_key(cat["schema"], path.name)
        if file_done(conn, key):
            print(f"[{n}/{len(files)}] SKIP (already imported): {path.name}")
            continue
        print(f"[{n}/{len(files)}] {path.name}", flush=True)
        process_file(conn, cat, path, folder, wanted=None)
    log_row(conn, *HISTORY_MARKER, 0)
    print("History import finished.")


# =====================================================================
#  Weekly / monthly check of the CQC page
# =====================================================================
def run_online(conn, links):
    for job in ONLINE_JOBS:
        cat = CAT_BY_SCHEMA[job["schema"]]
        print(f"\n=== {job['name']}  ->  {cat['schema']} ===")
        if job["type"] == "csv":
            url = find_csv_url(links)
        else:
            url = find_ods_url(links, job["url_keyword"], job["link_text"])
        name = filename_from_url(url)
        key = source_key(cat["schema"], safe_name(name))
        print(f"The CQC page offers: {name}")

        sig, modified = remote_signature(url)
        done = file_done(conn, key)
        old = stored_signature(conn, key)
        local_name = name
        if done:
            if sig and old and sig != old:
                # same file name, but CQC has replaced the content: import it again as a NEW dated table
                when = modified or datetime.now()
                local_name = dated_name(name, when)
                if local_name == name:
                    local_name = dated_name(name, datetime.now())
                if local_name == name:
                    print(f"SKIP: {name} changed again on the same day it was imported. Run  python cqc_updated.py --force  to import it again.")
                    store_signature(conn, key, sig)
                    continue
                if file_done(conn, source_key(cat["schema"], safe_name(local_name))):
                    print(f"SKIP: {name} changed on {when:%d %B %Y} and that version was already imported.")
                    store_signature(conn, key, sig)
                    continue
                print(f"CHANGED: {name} has new content on the CQC site (was {old}, now {sig}). Importing it as {local_name}.")
            else:
                if sig and not old:
                    store_signature(conn, key, sig)
                print(f"SKIP: {name} was already imported and has not changed.")
                continue

        folder = category_folder(cat)
        path = fetch_online_file(url, local_name, folder)
        process_file(conn, cat, path, folder, wanted=job.get("sheets"),
                     fallback_largest=job.get("fallback_largest", False))
        mark_done(conn, key)
        store_signature(conn, key, sig)
        if DELETE_ODS_AFTER and path.suffix.lower() in SPREADSHEET_EXT:
            path.unlink()
            print("Deleted", path)


# =====================================================================
#  Google Drive folder (CQC register) - unchanged behaviour
# =====================================================================
def list_drive_files():
    files = gdown.download_folder(url=DRIVE_FOLDER_URL, output=str(BASE_DIR / "_drive_tmp"),
                                  quiet=True, skip_download=True, use_cookies=False)
    if not files:
        raise SystemExit("Could not list the Google Drive folder. Check it is shared with "
                         "'Anyone with the link'.")
    return [(f.id, os.path.basename(f.path)) for f in files]


def drive_sheet_to_csv(book_path, sheet_name):
    wb = CalamineWorkbook.from_path(str(book_path))
    real = {n.lower(): n for n in wb.sheet_names}.get(sheet_name.lower())
    if not real:
        print(f"WARNING: sheet '{sheet_name}' not found in {book_path.name}. "
              f"Sheets in file: {wb.sheet_names}")
        return None
    out = book_path.parent / f"{month_year(book_path.name)}_{sheet_name}.csv"
    return sheet_to_csv(wb, real, out)


def process_drive(conn):
    files = list_drive_files()
    print("Files found in Drive folder:")
    for _, name in files:
        print("  -", name)

    supported = [(i, n) for i, n in files if n.lower().endswith(SPREADSHEET_EXT + (".csv",))]
    if not supported:
        print("No .ods, .xlsx, .xls or .csv files found in the Drive folder.")
        return

    newest = max(name_date(n) for _, n in supported)
    if newest != datetime.min:
        supported = [(i, n) for i, n in supported if name_date(n) == newest]

    for file_id, name in supported:
        source = f"{name} [{file_id}]"
        ext = os.path.splitext(name)[1].lower()

        if ext == ".csv":
            base = next((b for s, b in DRIVE_SHEETS.items() if norm(s) in norm(name)), None)
            if not base:
                print(f"Ignoring {name} (does not match a wanted sheet name).")
                continue
            table = resolve_table(conn, base, source)
            if already_imported(conn, source, "public", table):
                print(f"SKIP: {name} already imported into {table}.")
                continue
            path = download_drive_file(file_id, name)
            import_csv(conn, path, "public", table, source)
        else:
            pending = []
            for sheet, base in DRIVE_SHEETS.items():
                table = resolve_table(conn, base, source)
                if already_imported(conn, source, "public", table):
                    print(f"SKIP: sheet {sheet} from {name} already imported into {table}.")
                else:
                    pending.append((sheet, table))
            if not pending:
                continue
            path = download_drive_file(file_id, name)
            for sheet, table in pending:
                sheet_csv = drive_sheet_to_csv(path, sheet)
                if sheet_csv:
                    import_csv(conn, sheet_csv, "public", table, source)
            if DELETE_ODS_AFTER:
                path.unlink()
                print("Deleted", path)


# =====================================================================
#  Run
# =====================================================================
def update_main():
    print("Script started.", flush=True)
    if FORCE_REIMPORT:
        print("--force: everything will be imported again and tables overwritten.")

    with psycopg.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
                         autocommit=True, connect_timeout=30) as conn:
        ensure_log_table(conn)

        # 1. The four schemas: created only if they are missing
        ensure_schemas(conn)

        # 2. History folders: imported once (or again with --history)
        if history_needed(conn):
            run_history(conn)
        else:
            print("\nHistory import was already done. (Use --history to look for new files.)")

        # 3. Check the CQC page for new files
        links = get_page_links()
        run_online(conn, links)

        # 4. CQC register in Google Drive
        if INCLUDE_DRIVE:
            print("\n=== CQC register (Google Drive) ===")
            process_drive(conn)

    print("\nAll done. Files are in:", HISTORY_DIR)


class Tee:
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


# =====================================================================
#  2. What changed since the previous file  (was cqc_diff.py)
# =====================================================================

# =====================================================================
#  Settings: change these
# =====================================================================

SCHEMAS = [
    "R1_cqc_care_directory",
    "R2_care_directory_with_filters",
    "R3_care_directory_with_ratings",
    "R4_deactivated_locations",
]

SUMMARY_TABLE = "cqc_diff_summary"        # one row per diff table, in the public schema
LOG_DIR = Path(r"D:\Reckonant\data\CQC\logs")

# Columns tried (in this order) to match a row in the previous table with the same row in
# the current table. A column is only used if it is filled in and unique in BOTH tables.
# If none qualifies, whole rows are compared instead (rows can then only be ADDED or REMOVED).
KEY_CANDIDATES = [
    "location_id",
    "cqc_location_id_for_office_use_only",
    "provider_id",
    "cqc_provider_id_for_office_use_only",
]

FORCE = "--force" in sys.argv
LATEST_ONLY = "--latest" in sys.argv
# =====================================================================

META_COLUMNS = ("change_type", "key_value", "changed_columns")
LAYOUT = "cqc_diff layout 2: previous and current values side by side"
TABLE_RE = re.compile(r"^(\d{2})_([a-z]+)_(\d{4})_(.+)$")


# =====================================================================
#  Finding the tables to compare
# =====================================================================
def parse_table(name):
    """'01_october_2026_locations' -> (date, '01_october_2026', 'locations'); None if not that pattern."""
    m = TABLE_RE.match(name)
    if not m:
        return None
    try:
        d = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y")
    except ValueError:
        return None
    return d, f"{m.group(1)}_{m.group(2)}_{m.group(3)}", m.group(4)


def load_tables(conn, schema):
    """{table name: [column names in order]} for the base tables of a schema."""
    rows = conn.execute("""
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = %s AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position""", (schema,)).fetchall()
    tables = {}
    for t, c in rows:
        tables.setdefault(t, []).append(c)
    return tables


def load_diff_layouts(conn, schema):
    """{diff table name: layout comment} so older diff tables can be rebuilt."""
    rows = conn.execute(r"""
        SELECT c.relname, obj_description(c.oid, 'pg_class')
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r' AND c.relname LIKE 'diff\_%%'""", (schema,)).fetchall()
    return {name: comment for name, comment in rows}


def find_pairs(tables):
    """Groups tables by name (without the date) and pairs each period with the one before it."""
    families, ignored = {}, []
    for name in tables:
        if name.startswith("diff_"):
            continue
        parsed = parse_table(name)
        if parsed is None:
            ignored.append(name)
            continue
        d, prefix, base = parsed
        families.setdefault(base, []).append((d, prefix, name))

    pairs = []
    for base, items in sorted(families.items()):
        items.sort()
        consecutive = list(zip(items, items[1:]))
        if LATEST_ONLY:
            consecutive = consecutive[-1:]
        for a, b in consecutive:
            pairs.append(dict(base=base, prev=a[2], curr=b[2],
                              prev_prefix=a[1], curr_prefix=b[1]))
    return pairs, families, ignored


def diff_table_name(pair, used):
    """diff_<previous date>_<current date>_<name>, cut to PostgreSQL's 63-character limit."""
    name = f"diff_{pair['prev_prefix']}_{pair['curr_prefix']}_{pair['base']}"
    if len(name) > 63:
        name = name[:63].rstrip("_")
    n = 2
    while name in used:
        name = f"{name[:58].rstrip('_')}_{n}"
        n += 1
    used.add(name)
    return name


# =====================================================================
#  Comparing two tables
# =====================================================================
def choose_key(conn, schema, prev, curr, common):
    """First candidate column that is filled in and unique in both tables, else None."""
    candidates = [c for c in KEY_CANDIDATES if c in common]
    candidates += [c for c in common if "id" in c.split("_") and c not in candidates]
    for col in candidates[:8]:
        unique = True
        for table in (prev, curr):
            total, distinct, filled = conn.execute(
                sql.SQL("SELECT COUNT(*), COUNT(DISTINCT {k}), COUNT({k}) FROM {t}").format(
                    k=sql.Identifier(col), t=sql.Identifier(schema, table))).fetchone()
            if not (total > 0 and total == distinct == filled):
                unique = False
                break
        if unique:
            return col
    return None


def output_columns(pcols, ccols):
    """Names of the side-by-side columns: <col>_prev and <col>_curr, kept to 63 characters.
    Returns (columns in display order, {(column, 'prev' or 'curr'): output name})."""
    order = list(ccols) + [c for c in pcols if c not in ccols]
    used, names = set(META_COLUMNS), {}
    for col in order:
        for side, present in (("prev", col in pcols), ("curr", col in ccols)):
            if not present:
                continue
            base = col if len(col) + 5 <= 63 else col[:58]
            name = f"{base}_{side}"
            n = 2
            while name in used:
                name = f"{base[:55]}_{side}_{n}"
                n += 1
            used.add(name)
            names[(col, side)] = name
    return order, names


def build_diff(conn, schema, pair, tables, diff_name):
    prev, curr = pair["prev"], pair["curr"]
    pcols, ccols = tables[prev], tables[curr]
    common = [c for c in ccols if c in pcols]
    cols_added = [c for c in ccols if c not in pcols]
    cols_removed = [c for c in pcols if c not in ccols]
    if not common:
        raise ValueError("the two tables have no column names in common")

    order, names = output_columns(pcols, ccols)
    prev_all = [c for c in order if (c, "prev") in names]
    curr_all = [c for c in order if (c, "curr") in names]

    P, C, D = (sql.Identifier(schema, prev), sql.Identifier(schema, curr),
               sql.Identifier(schema, diff_name))
    key = choose_key(conn, schema, prev, curr, common)

    def insert_cols(prev_list, curr_list):
        return sql.SQL(", ").join(
            [sql.Identifier(m) for m in META_COLUMNS]
            + [sql.Identifier(names[(c, "prev")]) for c in prev_list]
            + [sql.Identifier(names[(c, "curr")]) for c in curr_list])

    def select_list(first_three, prev_list, curr_list, prev_alias="p", curr_alias="c"):
        parts = list(first_three)
        parts += [sql.SQL("{}.{}").format(sql.Identifier(prev_alias), sql.Identifier(c)) for c in prev_list]
        parts += [sql.SQL("{}.{}").format(sql.Identifier(curr_alias), sql.Identifier(c)) for c in curr_list]
        return sql.SQL(", ").join(parts)

    rows_prev = conn.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(P)).fetchone()[0]
    rows_curr = conn.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(C)).fetchone()[0]

    with conn.transaction():
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(D))
        col_defs = [sql.SQL("{} TEXT").format(sql.Identifier(m)) for m in META_COLUMNS]
        for col in order:                      # the _prev and _curr columns sit next to each other
            for side in ("prev", "curr"):
                if (col, side) in names:
                    col_defs.append(sql.SQL("{} TEXT").format(sql.Identifier(names[(col, side)])))
        conn.execute(sql.SQL("CREATE TABLE {} ({})").format(D, sql.SQL(", ").join(col_defs)))

        changed = 0
        if key:
            k = sql.Identifier(key)
            added = conn.execute(sql.SQL(
                "INSERT INTO {d} ({ic}) SELECT {sel} FROM {c} AS c "
                "WHERE NOT EXISTS (SELECT 1 FROM {p} AS p WHERE p.{k} = c.{k})").format(
                d=D, ic=insert_cols([], curr_all), c=C, p=P, k=k,
                sel=select_list([sql.SQL("'ADDED'"), sql.SQL("c.{}").format(k), sql.SQL("NULL")],
                                [], curr_all))).rowcount
            removed = conn.execute(sql.SQL(
                "INSERT INTO {d} ({ic}) SELECT {sel} FROM {p} AS p "
                "WHERE NOT EXISTS (SELECT 1 FROM {c} AS c WHERE c.{k} = p.{k})").format(
                d=D, ic=insert_cols(prev_all, []), c=C, p=P, k=k,
                sel=select_list([sql.SQL("'REMOVED'"), sql.SQL("p.{}").format(k), sql.SQL("NULL")],
                                prev_all, []))).rowcount

            compare_cols = [c for c in common if c != key]
            if compare_cols:
                cases = sql.SQL(", ").join(
                    sql.SQL("CASE WHEN c.{col} IS DISTINCT FROM p.{col} THEN {lit}::text END").format(
                        col=sql.Identifier(c), lit=sql.Literal(c)) for c in compare_cols)
                changed = conn.execute(sql.SQL(
                    "INSERT INTO {d} ({ic}) SELECT {sel} "
                    "FROM {c} AS c JOIN {p} AS p ON p.{k} = c.{k} "
                    "CROSS JOIN LATERAL (SELECT array_remove(ARRAY[{cases}], NULL) AS ch) AS x "
                    "WHERE cardinality(x.ch) > 0").format(
                    d=D, ic=insert_cols(prev_all, curr_all), c=C, p=P, k=k, cases=cases,
                    sel=select_list([sql.SQL("'CHANGED'"), sql.SQL("c.{}").format(k),
                                     sql.SQL("array_to_string(x.ch, ', ')")],
                                    prev_all, curr_all))).rowcount
            mode = f"key: {key}"
        else:
            # No unique key: whole rows are compared, so rows can only be ADDED or REMOVED
            cc = sql.SQL(", ").join(sql.Identifier(c) for c in common)
            added = conn.execute(sql.SQL(
                "INSERT INTO {d} ({ic}) SELECT {sel} FROM "
                "(SELECT {cc} FROM {c} EXCEPT ALL SELECT {cc} FROM {p}) AS t").format(
                d=D, ic=insert_cols([], common), cc=cc, c=C, p=P,
                sel=select_list([sql.SQL("'ADDED'"), sql.SQL("NULL"), sql.SQL("NULL")],
                                [], common, curr_alias="t"))).rowcount
            removed = conn.execute(sql.SQL(
                "INSERT INTO {d} ({ic}) SELECT {sel} FROM "
                "(SELECT {cc} FROM {p} EXCEPT ALL SELECT {cc} FROM {c}) AS t").format(
                d=D, ic=insert_cols(common, []), cc=cc, c=C, p=P,
                sel=select_list([sql.SQL("'REMOVED'"), sql.SQL("NULL"), sql.SQL("NULL")],
                                common, [], prev_alias="t"))).rowcount
            mode = "whole row (no unique key found)"

        conn.execute(sql.SQL("COMMENT ON TABLE {} IS {}").format(D, sql.Literal(LAYOUT)))

        conn.execute(sql.SQL("""
            INSERT INTO {s} (schema_name, diff_table, previous_table, current_table, compare_mode,
                             rows_previous, rows_current, rows_added, rows_removed, rows_changed,
                             columns_added, columns_removed, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (schema_name, diff_table) DO UPDATE SET
                previous_table = EXCLUDED.previous_table, current_table = EXCLUDED.current_table,
                compare_mode = EXCLUDED.compare_mode, rows_previous = EXCLUDED.rows_previous,
                rows_current = EXCLUDED.rows_current, rows_added = EXCLUDED.rows_added,
                rows_removed = EXCLUDED.rows_removed, rows_changed = EXCLUDED.rows_changed,
                columns_added = EXCLUDED.columns_added, columns_removed = EXCLUDED.columns_removed,
                created_at = now()""").format(s=sql.Identifier(SUMMARY_TABLE)),
            (schema, diff_name, prev, curr, mode, rows_prev, rows_curr, added, removed,
             changed, ", ".join(cols_added) or None, ", ".join(cols_removed) or None))

    return dict(mode=mode, added=added, removed=removed, changed=changed,
                cols_added=cols_added, cols_removed=cols_removed)


# =====================================================================
#  Run
# =====================================================================
def ensure_summary_table(conn):
    conn.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {} (
            schema_name     TEXT NOT NULL,
            diff_table      TEXT NOT NULL,
            previous_table  TEXT,
            current_table   TEXT,
            compare_mode    TEXT,
            rows_previous   INTEGER,
            rows_current    INTEGER,
            rows_added      INTEGER,
            rows_removed    INTEGER,
            rows_changed    INTEGER,
            columns_added   TEXT,
            columns_removed TEXT,
            created_at      TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (schema_name, diff_table)
        )""").format(sql.Identifier(SUMMARY_TABLE)))


def diff_main():
    print("Script started.", flush=True)
    created, skipped, failed = [], 0, []

    with psycopg.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER,
                         autocommit=True, connect_timeout=30) as conn:
        ensure_summary_table(conn)

        for schema in SCHEMAS:
            print(f"\n=== {schema} ===")
            exists = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone()
            if not exists:
                print("Schema not found. Run cqc_updated.py first.")
                continue

            tables = load_tables(conn, schema)
            layouts = load_diff_layouts(conn, schema)
            pairs, families, ignored = find_pairs(tables)

            for base, items in sorted(families.items()):
                note = "" if len(items) > 1 else "  (only one period so far, nothing to compare yet)"
                print(f"  {base}: {len(items)} table(s){note}")
            if ignored:
                print(f"  Ignored (name does not start with day_month_year_): {', '.join(sorted(ignored))}")

            used = set()          # names given out in this run (names are the same on every run)
            for pair in pairs:
                name = diff_table_name(pair, used)
                if name in tables and not FORCE:
                    if layouts.get(name) == LAYOUT:
                        skipped += 1
                        print(f"  SKIP (already exists): {name}")
                        continue
                    print(f"  REBUILD (older layout): {name}")

                print(f"  Comparing {pair['prev']}  ->  {pair['curr']}", flush=True)
                try:
                    r = build_diff(conn, schema, pair, tables, name)
                except (psycopg.Error, ValueError) as e:
                    print(f"  FAILED {name}: {e}")
                    failed.append((schema, name, str(e)))
                    continue
                created.append((schema, name, r))
                print(f"     -> {name}: {r['added']:,} added, {r['removed']:,} removed, "
                      f"{r['changed']:,} changed  [{r['mode']}]")
                if r["cols_added"]:
                    print(f"     new columns: {', '.join(r['cols_added'])}")
                if r["cols_removed"]:
                    print(f"     dropped columns: {', '.join(r['cols_removed'])}")

    print("\n" + "=" * 60)
    print(f"Diff tables built: {len(created)}   already up to date (skipped): {skipped}   failed: {len(failed)}")
    for schema, name, r in created:
        print(f"  {schema}.{name}: +{r['added']:,} / -{r['removed']:,} / changed {r['changed']:,}")
    for schema, name, err in failed:
        print(f"  FAILED {schema}.{name}: {err}")
    print("Overview of all diffs:  SELECT * FROM public." + SUMMARY_TABLE + " ORDER BY created_at DESC;")
    return 1 if failed else 0


# =====================================================================
#  Command line
# =====================================================================

def update_cli():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = open(log_dir / f"import_{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log_file)
    sys.stderr = sys.stdout

    print(f"\n----- Run started {datetime.now():%Y-%m-%d %H:%M:%S} -----")
    try:
        update_main()
        notify_new_files()
    except BaseException as e:
        if isinstance(e, SystemExit) and e.code in (0, None):
            notify_new_files()
            raise
        message = e.code if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        print("ERROR:", message)
        if NOTIFY_ON_ERROR:
            extra = ""
            if NEW_ITEMS:
                extra = "\n\nImported before the error:\n" + "\n".join(
                    f"- {t}: {r:,} rows ({s})" for t, s, r in NEW_ITEMS[:EMAIL_MAX_LINES])
            send_email(f"CQC import FAILED ({datetime.now():%d %b %Y})",
                       f"The CQC import failed:\n\n{message}{extra}\n\nSee the log in {log_dir}")
        sys.exit(1)


def diff_cli():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(LOG_DIR / f"diff_{datetime.now():%Y-%m-%d}.log", "a", encoding="utf-8")
        sys.stdout = Tee(sys.__stdout__, log_file)
        sys.stderr = sys.stdout
    except OSError:
        pass                      # run without a log file if the folder cannot be created

    print(f"\n----- Diff run started {datetime.now():%Y-%m-%d %H:%M:%S} -----")
    try:
        sys.exit(diff_main())
    except psycopg.OperationalError as e:
        print(f"ERROR: could not connect to PostgreSQL: {e}")
        sys.exit(1)


def cli():
    global LATEST_ONLY
    args = sys.argv[1:]
    cmd = args[0] if args and not args[0].startswith("-") else "all"
    if cmd == "update":
        return update_cli()
    if cmd == "diff":
        return diff_cli()
    if cmd != "all":
        sys.exit(f"Unknown command '{cmd}'. Commands: all (default), update, diff.")
    try:
        update_cli()
    except SystemExit as e:
        if e.code not in (0, None):
            raise
    LATEST_ONLY = "--force" not in sys.argv          # the daily run compares only the newest pair
    return diff_cli()


if __name__ == "__main__":
    sys.exit(cli())
