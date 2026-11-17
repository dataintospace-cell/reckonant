"""register_hmrc_defaulters.py -- the register "HMRC Tax defaulters" (HMRC's list of deliberate tax defaulters).  ONE file.

  python register_hmrc_defaulters.py                  CHECK the HMRC page; download and load any new / replaced list (daily_update.py runs this)
  python register_hmrc_defaulters.py --all            the same, but load EVERY list on the page (older ones too)
  python register_hmrc_defaulters.py --since 2026-03  load the lists from March 2026 onwards
  python register_hmrc_defaulters.py --force          load again even if the file is unchanged
  python register_hmrc_defaulters.py --file C:\\x.ods --month 2026-09     load a list you downloaded by hand
  python register_hmrc_defaulters.py show             print what is in the register
  python register_hmrc_defaulters.py reset            ONE-TIME: delete the whole "R10_hmrc_tax_defaulters" schema (asks you to type DROP)

THE SOURCE
  https://www.gov.uk/government/publications/publishing-details-of-deliberate-tax-defaulters-pddd
  HMRC adds a new list file (a spreadsheet) to that page about every three months (Nov 2025, Mar 2026, Jun 2026, Sep 2026 ...).
  Each file is the CURRENT list: names stay on it for 12 months and then drop off.

WHAT THIS SCRIPT DOES EACH RUN
  1. Reads the page (through the gov.uk content API; if that fails, the page itself) and lists the files on it.
  2. For each list from START_MONTH (September 2026) onwards it asks the server for the file size / date. A file it has already
     loaded and that has not changed is SKIPPED. A new file, or a file HMRC has replaced, is DOWNLOADED (kept in D:\\Reckonant\\data\\HMRC)
     and LOADED.  So you never need to download anything by hand.
  3. Reads every sheet of the file. The first row that looks like a header row gives the column names; every column of the
     spreadsheet becomes a column of the table, exactly as HMRC names it. Title lines above the header and notes below the
     table are ignored. A new column in a later list is added to the table automatically.
  4. Each row is one defaulter. The same row in the next list is recognised (same values) and is NOT added twice - its
     "Last listed" month moves on. A row that is on the newest list for the first time is logged "Added to list"; a row that was
     on the previous list and is missing from the newest one is logged "Removed from list" (it dropped off after 12 months, or HMRC
     removed it). The tracker (HMD-01) counts these per day.

TABLES, in schema "R10_hmrc_tax_defaulters"
    defaulter        one row per defaulter: Defaulter ID, First listed, Last listed, Still listed, Sheet, then HMRC's own columns
    defaulter_key    technical: the fingerprint of each row (so a row is never stored twice)
    list_file        one row per file loaded: month, link, size, when it was downloaded, rows read
    change_log       Added to list / Removed from list, with the date it was noticed
    v_defaulter      the view the tracker reads: defaulter + a "url" column (link to the HMRC file) last

Database password comes from pgpass.conf (as in your other scripts).
"""
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import psycopg
from psycopg import sql

# ---------------------------------------------------------------- settings
HOST = os.environ.get("PGHOST", "localhost")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
DBNAME = os.environ.get("PGDATABASE", "postgres")

SCHEMA = "R10_hmrc_tax_defaulters"
PAGE_URL = os.environ.get("HMRC_PAGE", "https://www.gov.uk/government/publications/publishing-details-of-deliberate-tax-defaulters-pddd")
API_URL = os.environ.get("HMRC_API", "https://www.gov.uk/api/content/government/publications/publishing-details-of-deliberate-tax-defaulters-pddd")
BASE_DIR = Path(os.environ.get("ALERT_HMRC_DIR", r"D:\Reckonant\data\HMRC"))
START_MONTH = "2026-09"                  # lists before this month are not loaded unless you use --all or --since
DELAY = float(os.environ.get("HMRC_DELAY", "2"))       # seconds between requests to gov.uk
UA = "Reckonant/1.0 (personal monitoring of a public gov.uk list)"
MAX_BYTES = 30 * 1024 * 1024
FILE_EXT = (".ods", ".xlsx", ".csv")

FIXED = ["Defaulter ID", "First listed", "Last listed", "Still listed", "Sheet"]     # the columns before HMRC's own columns
NOT_WANTED = set()                                                                 # header names (lower case) to leave out
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

I = sql.Identifier


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- web
def http(url, method="GET", timeout=60):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout)


def list_files_on_page():
    """[{month: date, title, url, kind}] for every list file on the HMRC page, newest first."""
    found = {}

    def add(url, title=""):
        url = urljoin(PAGE_URL, url)
        if not url.lower().split("?")[0].endswith(FILE_EXT):
            return
        name = url.split("?")[0].rsplit("/", 1)[-1].replace("_", " ")
        m = month_of(title) or month_of(name)
        if m:
            found[url] = {"month": m, "title": title or Path(name).stem, "url": url, "kind": url.lower().split("?")[0].rsplit(".", 1)[-1]}

    try:
        with http(API_URL) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        for a in (data.get("details") or {}).get("attachments") or []:
            add(a.get("url") or "", a.get("title") or "")
    except Exception as e:                                          # the API is not essential
        say(f"  (content API not used: {e})")
    if not found:
        time.sleep(DELAY)
        with http(PAGE_URL) as r:
            html = r.read().decode("utf-8", "replace")
        for m in re.finditer(r'href="([^"#]+\.(?:ods|xlsx|csv))(?:\?[^"]*)?"', html, flags=re.I):
            add(m.group(1))
    return sorted(found.values(), key=lambda f: f["month"], reverse=True)


def month_of(text):
    """First day of the month named in text ('... September 2026 ...'), or None."""
    m = re.search(r"(" + "|".join(MONTHS) + r")[\s_-]+(\d{4})", text or "", flags=re.I)
    if not m:
        return None
    return date(int(m.group(2)), MONTHS.index(m.group(1).lower()) + 1, 1)


def signature(url):
    try:
        with http(url, "HEAD", 30) as r:
            return f"{r.headers.get('Content-Length', '?')}|{r.headers.get('Last-Modified', '?')}"
    except Exception:
        return "?"


def download(url, month, kind):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    target = BASE_DIR / f"Deliberate_tax_defaulters_{month:%Y-%m}.{kind}"
    with http(url, timeout=120) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise RuntimeError("file is bigger than 30 MB - not downloaded")
    if kind in ("ods", "xlsx") and body[:2] != b"PK":
        raise RuntimeError("the download is not a spreadsheet file (HMRC may have changed the link)")
    target.write_bytes(body)
    return target


# ---------------------------------------------------------------- reading the spreadsheet
def clean(v):
    v = "" if v is None else str(v)
    return re.sub(r"[ \t\r\f\v]+", " ", v.replace("\xa0", " ")).strip()


def ods_sheets(path):
    """{sheet name: [[cell text, ...], ...]} read from an .ods file with the standard library only."""
    ns = {"t": "urn:oasis:names:tc:opendocument:xmlns:table:1.0", "x": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
          "o": "urn:oasis:names:tc:opendocument:xmlns:office:1.0"}
    T, X, O = "{%s}" % ns["t"], "{%s}" % ns["x"], "{%s}" % ns["o"]
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))

    def text_of(el):
        out = []

        def walk(e):
            if e.tag == X + "s":
                out.append(" " * int(e.get(X + "c", "1")))
            elif e.tag == X + "line-break":
                out.append("\n")
            elif e.tag == X + "tab":
                out.append(" ")
            if e.text and e.tag not in (X + "s",):
                out.append(e.text)
            for c in e:
                walk(c)
                if c.tail:
                    out.append(c.tail)
        for p in el.findall(X + "p") + el.findall(X + "h"):
            if out:
                out.append("\n")
            walk(p)
        return "".join(out)

    sheets = {}
    for tb in root.iter(T + "table"):
        rows = []
        for row in tb.iter(T + "table-row"):
            cells = []
            for c in row:
                if c.tag not in (T + "table-cell", T + "covered-table-cell"):
                    continue
                rep = int(c.get(T + "number-columns-repeated", "1"))
                txt = text_of(c)
                if not txt:
                    txt = c.get(O + "date-value") or c.get(O + "string-value") or c.get(O + "value") or ""
                cells.extend([clean(txt) if txt else ""] * (rep if txt else min(rep, 60)))
            while cells and not cells[-1]:
                cells.pop()
            rrep = int(row.get(T + "number-rows-repeated", "1"))
            for _ in range(rrep if any(cells) else 1):
                rows.append(cells)
        while rows and not any(rows[-1]):
            rows.pop()
        sheets[tb.get(T + "name") or f"Sheet{len(sheets) + 1}"] = rows
    return sheets


def xlsx_sheets(path):
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("this list is an .xlsx file - install the reader first:  pip install openpyxl")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = {}
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            cells = [clean(v.date() if isinstance(v, datetime) else v) for v in r]
            while cells and not cells[-1]:
                cells.pop()
            rows.append(cells)
        out[ws.title] = rows
    return out


def csv_sheets(path):
    raw = Path(path).read_bytes().decode("utf-8-sig", "replace")
    return {"List": [[clean(c) for c in r] for r in csv.reader(io.StringIO(raw))]}


def read_list(path):
    ext = str(path).lower().rsplit(".", 1)[-1]
    return {"ods": ods_sheets, "xlsx": xlsx_sheets, "csv": csv_sheets}[ext](path)


def key_of(h):
    """Header text -> key used to see that two headers are the same column (case, spaces, punctuation ignored)."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\(s\)", "", h.lower()))


def split_header(rows):
    """(header names, data rows) of one sheet. The header is the first row of 2+ filled cells followed by a data row of 2+ filled cells."""
    filled = [sum(1 for c in r if c) for r in rows]
    for i, n in enumerate(filled):
        if n >= 2 and i + 1 < len(rows) and filled[i + 1] >= 2 and not all(re.fullmatch(r"[\d.,£ -]+", c) for c in rows[i] if c):
            return rows[i], rows[i + 1:]
    return None, []


def records(sheets):
    """[(sheet, {header: value})] for every data row of every sheet. Duplicate headers inside a sheet are kept once (values joined if both filled)."""
    out = []
    for sname, rows in sheets.items():
        head, data = split_header(rows)
        if not head:
            continue
        names, seen = [], {}
        for j, h in enumerate(head):
            h = clean(h) or f"Column {j + 1}"
            k = key_of(h)
            names.append(seen.setdefault(k, h) if k in seen else h)
            seen.setdefault(k, h)
        for r in data:
            if sum(1 for c in r if c) < 2:                  # a note / total / blank line, not a defaulter
                continue
            rec = {}
            for h, v in zip(names, r + [""] * (len(names) - len(r))):
                if key_of(h) in NOT_WANTED:
                    continue
                if h in rec and rec[h] and v and rec[h] != v:
                    rec[h] = f"{rec[h]}; {v}"
                elif v or h not in rec:
                    rec[h] = v or rec.get(h, "")
            if any(rec.values()):
                out.append((sname, rec))
    return out


def fingerprint(sheet, rec):
    s = "|".join(f"{key_of(k)}={re.sub(r'[^a-z0-9£.]+', ' ', v.lower()).strip()}" for k, v in sorted(rec.items(), key=lambda kv: key_of(kv[0])) if v)
    return hashlib.sha1(f"{key_of(sheet)}|{s}".encode("utf-8")).hexdigest()


def best_name(rec):
    for k, v in rec.items():
        if v and "name" in k.lower() and not re.search(r"trad|previous|former|business|nature", k.lower()):
            return v
    for k, v in rec.items():
        if v and "name" in k.lower():
            return v
    return next((v for v in rec.values() if v), "")


def summary(rec):
    return "; ".join(f"{k}: {v}" for k, v in rec.items() if v)[:600]


# ---------------------------------------------------------------- database
def connect():
    return psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)


def setup(con):
    con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(I(SCHEMA)))
    S = I(SCHEMA)
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.defaulter (
        "Defaulter ID" bigserial PRIMARY KEY, "First listed" date, "Last listed" date, "Still listed" boolean DEFAULT true, "Sheet" text)''').format(s=S))
    con.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {s}.defaulter_key (row_hash text PRIMARY KEY, defaulter_id bigint NOT NULL)").format(s=S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.list_file (list_month date NOT NULL, url text NOT NULL, title text, signature text,
        file_path text, rows_read int, downloaded_at timestamptz DEFAULT now(), PRIMARY KEY (list_month, url))''').format(s=S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.change_log (change_id bigserial PRIMARY KEY, detected_date date NOT NULL DEFAULT current_date,
        list_month date, change_type text NOT NULL, defaulter_id bigint, name text, detail text)''').format(s=S))
    con.execute(sql.SQL("CREATE INDEX IF NOT EXISTS change_log_day ON {s}.change_log (detected_date)").format(s=S))
    con.execute(sql.SQL("COMMENT ON TABLE {s}.defaulter IS 'HMRC > Deliberate tax defaulters'").format(s=S))


def data_columns(con):
    return [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='defaulter' ORDER BY ordinal_position",
                                      (SCHEMA,)).fetchall() if r[0] not in FIXED]


def ensure_columns(con, headers):
    have = {key_of(c): c for c in data_columns(con)}
    for h in headers:
        if key_of(h) not in have:
            name = h if h not in FIXED else h + " (HMRC)"
            con.execute(sql.SQL("ALTER TABLE {s}.defaulter ADD COLUMN {c} text").format(s=I(SCHEMA), c=I(name)))
            have[key_of(h)] = name
    return have


def make_view(con):
    cols = [sql.SQL("d.{}").format(I(c)) for c in FIXED + data_columns(con)]
    con.execute(sql.SQL("DROP VIEW IF EXISTS {s}.v_defaulter").format(s=I(SCHEMA)))
    con.execute(sql.SQL('''CREATE VIEW {s}.v_defaulter AS SELECT {c}, f.url AS url FROM {s}.defaulter d
        LEFT JOIN LATERAL (SELECT url FROM {s}.list_file WHERE list_month = d."Last listed" ORDER BY downloaded_at DESC LIMIT 1) f ON true''').format(
        s=I(SCHEMA), c=sql.SQL(", ").join(cols)))


def load_list(con, month, url, path, sig, title):
    """Loads one list file. Returns (rows read, added, removed)."""
    recs = records(read_list(path))
    if not recs:
        raise RuntimeError("no table found in the file (no header row followed by data rows)")
    have = ensure_columns(con, list(dict.fromkeys(h for _, r in recs for h in r)))      # in the order HMRC gives them
    S = I(SCHEMA)
    newest = con.execute(sql.SQL('SELECT max("Last listed") FROM {s}.defaulter').format(s=S)).fetchone()[0]
    is_newest = newest is None or month >= newest
    seen, added = set(), 0
    for sheet, rec in recs:
        h = fingerprint(sheet, rec)
        if h in seen:
            continue
        seen.add(h)
        row = con.execute(sql.SQL("SELECT defaulter_id FROM {s}.defaulter_key WHERE row_hash = %s").format(s=S), (h,)).fetchone()
        if row:
            con.execute(sql.SQL('UPDATE {s}.defaulter SET "First listed" = LEAST("First listed", %s), "Last listed" = GREATEST("Last listed", %s), "Still listed" = true '
                                'WHERE "Defaulter ID" = %s').format(s=S), (month, month, row[0]))
            continue
        cols = [have[key_of(k)] for k in rec]
        did = con.execute(sql.SQL('INSERT INTO {s}.defaulter ("First listed", "Last listed", "Still listed", "Sheet", {c}) VALUES (%s, %s, true, %s, {v}) RETURNING "Defaulter ID"').format(
            s=S, c=sql.SQL(", ").join(I(c) for c in cols), v=sql.SQL(", ").join(sql.Placeholder() for _ in cols)),
            (month, month, sheet, *rec.values())).fetchone()[0]
        con.execute(sql.SQL("INSERT INTO {s}.defaulter_key VALUES (%s, %s)").format(s=S), (h, did))
        added += 1
        if is_newest:
            con.execute(sql.SQL("INSERT INTO {s}.change_log (list_month, change_type, defaulter_id, name, detail) VALUES (%s, 'Added to list', %s, %s, %s)").format(s=S),
                        (month, did, best_name(rec), summary(rec)))
    removed = 0
    if is_newest:                                   # on the previous list(s) but not on this one
        gone = con.execute(sql.SQL('SELECT d."Defaulter ID", k.row_hash FROM {s}.defaulter d JOIN {s}.defaulter_key k ON k.defaulter_id = d."Defaulter ID" '
                                   'WHERE d."Still listed" AND d."Last listed" < %s').format(s=S), (month,)).fetchall()
        for did, h in gone:
            if h in seen:
                continue
            cols = data_columns(con)
            r = con.execute(sql.SQL("SELECT {c} FROM {s}.defaulter WHERE \"Defaulter ID\" = %s").format(
                c=sql.SQL(", ").join(I(c) for c in cols) if cols else sql.SQL("1"), s=S), (did,)).fetchone()
            rec = {c: v for c, v in zip(cols, r or []) if v}
            con.execute(sql.SQL('UPDATE {s}.defaulter SET "Still listed" = false WHERE "Defaulter ID" = %s').format(s=S), (did,))
            con.execute(sql.SQL("INSERT INTO {s}.change_log (list_month, change_type, defaulter_id, name, detail) VALUES (%s, 'Removed from list', %s, %s, %s)").format(s=S),
                        (month, did, best_name(rec), summary(rec)))
            removed += 1
    con.execute(sql.SQL("DELETE FROM {s}.list_file WHERE list_month = %s AND url = %s").format(s=S), (month, url))
    con.execute(sql.SQL("INSERT INTO {s}.list_file (list_month, url, title, signature, file_path, rows_read) VALUES (%s, %s, %s, %s, %s, %s)").format(s=S),
                (month, url, title, sig, str(path), len(seen)))
    make_view(con)
    return len(seen), added, removed


# ---------------------------------------------------------------- commands
def update_main(args):
    force = "--force" in args
    start = START_MONTH
    if "--all" in args:
        start = "0000-00"
    if "--since" in args:
        start = args[args.index("--since") + 1]
    try:
        start_d = date(1900, 1, 1) if start == "0000-00" else datetime.strptime(start, "%Y-%m").date()
    except ValueError:
        say("--since needs a month like 2026-03")
        return 2

    with connect() as con:
        setup(con)
        if "--file" in args:                                       # a list downloaded by hand
            path = Path(args[args.index("--file") + 1])
            if "--month" not in args:
                say("--file needs --month, for example  --month 2026-09")
                return 2
            month = datetime.strptime(args[args.index("--month") + 1], "%Y-%m").date()
            n, a, r = load_list(con, month, path.resolve().as_uri(), path, "manual", path.name)
            say(f"{month:%B %Y}: {n} defaulters read, {a} added, {r} removed.")
            return 0

        files = list_files_on_page()
        if not files:
            say("No list files found on the HMRC page. The page may have changed - open it and tell Claude what you see.")
            return 1
        say("The HMRC page offers: " + ", ".join(f"{f['month']:%b %Y}" for f in files))
        todo = sorted((f for f in files if f["month"] >= start_d), key=lambda f: f["month"])          # oldest first, so "added / removed" is worked out in order
        if not todo:
            say(f"No list from {start} onwards on the page yet.")
        problems = 0
        for f in todo:
            time.sleep(DELAY)
            sig = signature(f["url"])
            old = con.execute(sql.SQL("SELECT signature FROM {s}.list_file WHERE list_month = %s AND url = %s").format(s=I(SCHEMA)), (f["month"], f["url"])).fetchone()
            if old and old[0] == sig and not force:
                say(f"  SKIP     {f['month']:%b %Y}: already loaded, file unchanged")
                continue
            say(f"  {'CHANGED ' if old else 'NEW     '} {f['month']:%b %Y}: downloading {f['url']}")
            try:
                time.sleep(DELAY)
                path = download(f["url"], f["month"], f["kind"])
                n, a, r = load_list(con, f["month"], f["url"], path, sig, f["title"])
                say(f"           {n} defaulters read - {a} added, {r} removed.")
            except Exception as e:
                problems += 1
                say(f"  FAILED   {f['month']:%b %Y}: {e}")
        return 1 if problems else 0


def show_main(args):
    with connect() as con:
        try:
            rows = con.execute(sql.SQL('SELECT list_month, rows_read, downloaded_at::date, url FROM {s}.list_file ORDER BY 1').format(s=I(SCHEMA))).fetchall()
            tot = con.execute(sql.SQL('SELECT count(*), count(*) FILTER (WHERE "Still listed") FROM {s}.defaulter').format(s=I(SCHEMA))).fetchone()
            chg = con.execute(sql.SQL("SELECT change_type, count(*) FROM {s}.change_log GROUP BY 1 ORDER BY 1").format(s=I(SCHEMA))).fetchall()
        except psycopg.Error:
            say("Nothing loaded yet - run  python register_hmrc_defaulters.py")
            return 0
        say("Lists loaded:")
        for m, n, d, u in rows:
            say(f"  {m:%b %Y}  {n} rows  (downloaded {d})")
        say(f"Defaulters stored: {tot[0]}   still on the newest list: {tot[1]}")
        for t, n in chg:
            say(f"  {t}: {n}")
        say("Columns: " + ", ".join(FIXED + data_columns(con)))
    return 0


def reset_main(args):
    if input(f'This deletes the whole schema "{SCHEMA}" (every defaulter and change). Type DROP to continue: ').strip() != "DROP":
        say("Nothing deleted.")
        return 0
    with connect() as con:
        con.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(I(SCHEMA)))
    say("Deleted. Run  python register_hmrc_defaulters.py  to load again.")
    return 0


def cli():
    args = sys.argv[1:]
    cmd = args[0] if args and not args[0].startswith("-") else "update"
    rest = args[1:] if args and not args[0].startswith("-") else args
    fn = {"update": update_main, "show": show_main, "reset": reset_main}.get(cmd)
    if not fn:
        say("Commands: update (default), show, reset")
        return 2
    try:
        return fn(rest) or 0
    except psycopg.OperationalError as e:
        say(f"Cannot reach the database: {e}")
        return 1
    except urllib.error.URLError as e:
        say(f"Cannot reach gov.uk: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
