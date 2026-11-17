"""register_sponsors.py -- the register "Home Office licence sponsor register" (Employment & Workforce).  ONE file.

  python register_sponsors.py                 CHECK the gov.uk page; download the LATEST file if it is new; load it; log what changed (daily_update.py runs this)
  python register_sponsors.py --force         load the latest file again even if it looks unchanged
  python register_sponsors.py --file C:\\x.csv    load a file you downloaded by hand (the first ever load, or a test)
  python register_sponsors.py show            print what is in the register
  python register_sponsors.py reset           ONE-TIME: delete the whole "R11_sponsor_register" schema (asks you to type DROP)

THE SOURCE
  https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers
  The Home Office publishes ONE file (the whole register: every organisation licensed to sponsor workers) and replaces it
  on working days. The file only ever holds today's register, so this script keeps the history itself.

WHAT THIS SCRIPT DOES EACH RUN
  1. Reads the page (gov.uk content API first, then the page itself) and finds the newest CSV file. It always takes the LATEST - the
     link changes now and then, so nothing is hard-coded.
  2. Asks the server for the file's size / date. Unchanged since the last load -> SKIP. New or replaced -> DOWNLOAD (kept in
     D:\\Reckonant\\data\\Sponsors, the last 14 copies) and LOAD.
  3. Every column of the file becomes a column of the table, named as the Home Office names it (a new column later is added automatically).
     An organisation is recognised by its name + town + county + route (the columns whose headings contain those words); the other
     columns (for example "Type & Rating") are its details.
  4. Compared with the previous load it logs, with the date noticed:
         Added to register     a new organisation / route (or one that came back)
         Removed from register an organisation that is no longer on the register (licence ended, suspended or revoked)
         Details changed       the same organisation with a different detail, e.g. "Type & Rating: Worker (A rating) -> Worker (B rating)"
     The very first load logs nothing (everything would be "added"); it simply becomes the starting point.
  5. SAFETY: if the new file has less than half the rows of the previous one (a broken or partial download) it is NOT loaded.
     Use --force only if you know the register really shrank.

TABLES, in schema "R11_sponsor_register"
    sponsor          one row per organisation / route: Sponsor ID, First seen, Last seen, On register, then the Home Office columns
    change_log       Added to register / Removed from register / Details changed, with the date noticed and what changed
    file_snapshot    one row per file loaded: link, size / date signature, rows, when
    v_sponsor        the view the tracker reads: sponsor + a "url" column (link to the file) last
    v_sponsor_search the view the tracker's Users tab searches

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
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import psycopg
from psycopg import sql

# ---------------------------------------------------------------- settings
HOST = os.environ.get("PGHOST", "localhost")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
DBNAME = os.environ.get("PGDATABASE", "postgres")

SCHEMA = "R11_sponsor_register"
PAGE_URL = os.environ.get("SPONSOR_PAGE", "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers")
API_URL = os.environ.get("SPONSOR_API", "https://www.gov.uk/api/content/government/publications/register-of-licensed-sponsors-workers")
BASE_DIR = Path(os.environ.get("ALERT_SPONSOR_DIR", r"D:\Reckonant\data\Sponsors"))
KEEP_FILES = 14
DELAY = float(os.environ.get("SPONSOR_DELAY", "2"))
UA = "Reckonant/1.0 (personal monitoring of a public gov.uk register)"
MAX_BYTES = 150 * 1024 * 1024
MIN_KEEP = 0.5                                   # a new file smaller than this share of the old one is refused

FIXED = ["Sponsor ID", "First seen", "Last seen", "On register"]
HASH = "row_hash"
IDENTITY_WORDS = ("organisation", "name", "town", "city", "county", "route")      # headings containing these identify an organisation
I = sql.Identifier


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- web
def http(url, method="GET", timeout=60):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout)


def newest_file():
    """(url, title, page date) of the newest CSV on the page."""
    cands, page_date = [], ""

    def add(url, title=""):
        url = urljoin(PAGE_URL, url)
        if url.lower().split("?")[0].endswith(".csv"):
            cands.append((url, title or url.rsplit("/", 1)[-1]))

    try:
        with http(API_URL) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        page_date = (data.get("public_updated_at") or "")[:10]
        for a in (data.get("details") or {}).get("attachments") or []:
            add(a.get("url") or "", a.get("title") or "")
    except Exception as e:
        say(f"  (content API not used: {e})")
    if not cands:
        time.sleep(DELAY)
        with http(PAGE_URL) as r:
            html = r.read().decode("utf-8", "replace")
        for m in re.finditer(r'href="([^"#]+\.csv)(?:\?[^"]*)?"', html, flags=re.I):
            add(m.group(1))
    if not cands:
        return None, "", page_date
    say("The page offers: " + "; ".join(t for _, t in cands))
    workers = [c for c in cands if "student" not in (c[0] + c[1]).lower()] or cands
    url, title = workers[0]
    return url, title, page_date


def signature(url):
    try:
        with http(url, "HEAD", 30) as r:
            return f"{r.headers.get('Content-Length', '?')}|{r.headers.get('Last-Modified', '?')}"
    except Exception:
        return "?"


def download(url):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with http(url, timeout=180) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise RuntimeError("file is bigger than 150 MB - not downloaded")
    if body[:5].lower() in (b"<!doc", b"<html") or body[:2] == b"PK":
        raise RuntimeError("the download is a web page or a spreadsheet, not a CSV (HMRC / Home Office may have changed the link)")
    target = BASE_DIR / f"Sponsors_{datetime.now():%Y-%m-%d}.csv"
    target.write_bytes(body)
    for old in sorted(BASE_DIR.glob("Sponsors_*.csv"))[:-KEEP_FILES]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


# ---------------------------------------------------------------- reading the file
def clean(v):
    return re.sub(r"[ \t\r\f\v]+", " ", ("" if v is None else str(v)).replace("\xa0", " ")).strip()


def key_of(h):
    return re.sub(r"[^a-z0-9]+", "", h.lower())


def read_csv(path):
    """(header names, [row values]) - the first row with 3+ filled cells is the header."""
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")
    rows = [[clean(c) for c in r] for r in csv.reader(io.StringIO(text))]
    for i, r in enumerate(rows):
        if sum(1 for c in r if c) >= 3:
            names, seen = [], set()
            for j, h in enumerate(r):
                h = h or f"Column {j + 1}"
                while key_of(h) in seen:
                    h += " (2)"
                seen.add(key_of(h))
                names.append(h)
            return names, [x + [""] * (len(names) - len(x)) for x in rows[i + 1:] if any(x)]
    raise RuntimeError("no table found in the file (no header row with 3 or more columns)")


def split_columns(names):
    ident = [h for h in names if any(w in h.lower() for w in IDENTITY_WORDS) and "rating" not in h.lower()]
    return ident or list(names), [h for h in names if h not in ident]


def name_column(names):
    for w in ("organisation", "name"):
        for h in names:
            if w in h.lower():
                return h
    return names[0]


def fingerprint(ident_values):
    return hashlib.sha1("|".join(re.sub(r"[^a-z0-9]+", " ", v.lower()).strip() for v in ident_values).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- database
def connect():
    return psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)


def setup(con):
    S = I(SCHEMA)
    con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.sponsor ("Sponsor ID" bigserial PRIMARY KEY, "First seen" date, "Last seen" date,
        "On register" boolean DEFAULT true, row_hash text UNIQUE NOT NULL)''').format(s=S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.change_log (change_id bigserial PRIMARY KEY, detected_date date NOT NULL DEFAULT current_date,
        change_type text NOT NULL, sponsor_id bigint, name text, detail text)''').format(s=S))
    con.execute(sql.SQL("CREATE INDEX IF NOT EXISTS change_log_day ON {s}.change_log (detected_date)").format(s=S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.file_snapshot (snapshot_id bigserial PRIMARY KEY, url text, title text, signature text,
        page_updated text, rows_read int, file_path text, loaded_at timestamptz DEFAULT now())''').format(s=S))
    con.execute(sql.SQL("COMMENT ON TABLE {s}.sponsor IS 'Home Office > Licensed sponsors (workers)'").format(s=S))


def data_columns(con):
    return [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='sponsor' ORDER BY ordinal_position",
                                      (SCHEMA,)).fetchall() if r[0] not in FIXED and r[0] != HASH]


def ensure_columns(con, headers):
    have = {key_of(c): c for c in data_columns(con)}
    for h in headers:
        if key_of(h) not in have:
            name = h if h not in FIXED + [HASH] else h + " (Home Office)"
            con.execute(sql.SQL("ALTER TABLE {s}.sponsor ADD COLUMN {c} text").format(s=I(SCHEMA), c=I(name)))
            have[key_of(h)] = name
    return have


def make_views(con):
    cols = data_columns(con)
    S = I(SCHEMA)
    nm = name_column(cols) if cols else None
    con.execute(sql.SQL("DROP VIEW IF EXISTS {s}.v_sponsor_search").format(s=S))
    con.execute(sql.SQL("DROP VIEW IF EXISTS {s}.v_sponsor").format(s=S))
    con.execute(sql.SQL('''CREATE VIEW {s}.v_sponsor AS SELECT {c}, f.url AS url FROM {s}.sponsor d
        LEFT JOIN LATERAL (SELECT url FROM {s}.file_snapshot ORDER BY snapshot_id DESC LIMIT 1) f ON true''').format(
        s=S, c=sql.SQL(", ").join(sql.SQL("d.{}").format(I(c)) for c in FIXED + cols)))
    if nm:
        det = sql.SQL("concat_ws('; ', {})").format(sql.SQL(", ").join(
            sql.SQL("CASE WHEN COALESCE(d.{c}, '') <> '' THEN {l} || ': ' || d.{c} END").format(c=I(c), l=sql.Literal(c)) for c in cols))
        con.execute(sql.SQL('''CREATE VIEW {s}.v_sponsor_search AS SELECT d."First seen" AS d,
            'Licensed sponsor' || CASE WHEN d."On register" THEN '' ELSE ' (removed from register)' END AS ty, d.{nm}::text AS nm, NULL::text AS cn,
            {det}::text AS det, v.url::text AS url, d."Sponsor ID" AS sid FROM {s}.sponsor d JOIN {s}.v_sponsor v ON v."Sponsor ID" = d."Sponsor ID"''').format(s=S, nm=I(nm), det=det))


def detail_expr(alias, cols):
    """SQL text 'col: value; col: value' for one row, in the file's column order, empty values left out."""
    return sql.SQL("concat_ws('; ', {})").format(sql.SQL(", ").join(
        sql.SQL("CASE WHEN COALESCE({a}.{c}, '') <> '' THEN {l} || ': ' || {a}.{c} END").format(a=sql.SQL(alias), c=I(c), l=sql.Literal(c)) for c in cols))


def load_file(con, path, url, title, sig, page_date, force):
    names, rows = read_csv(path)
    if not rows:
        raise RuntimeError("the file has a header but no rows")
    ident, attrs = split_columns(names)
    nm = name_column(names)
    have = ensure_columns(con, names)
    S = I(SCHEMA)
    idx = {h: i for i, h in enumerate(names)}

    prev = con.execute(sql.SQL('SELECT count(*) FILTER (WHERE "On register"), count(*) FROM {s}.sponsor').format(s=S)).fetchone()
    first_load = prev[1] == 0
    uniq = {}
    for r in rows:
        uniq.setdefault(fingerprint([r[idx[h]] for h in ident]), r)
    if prev[0] and len(uniq) < prev[0] * MIN_KEEP and not force:
        raise RuntimeError(f"the new file has {len(uniq)} rows but the register held {prev[0]} - looks like a broken download, NOT loaded (use --force if the register really shrank)")

    cols_db = [have[key_of(h)] for h in names]
    with con.transaction():
        con.execute(sql.SQL("DROP TABLE IF EXISTS pg_temp.sp_new"))
        con.execute(sql.SQL("CREATE TEMP TABLE sp_new ({c}, row_hash text PRIMARY KEY) ON COMMIT DROP").format(c=sql.SQL(", ").join(sql.SQL("{} text").format(I(c)) for c in cols_db)))
        with con.cursor().copy(sql.SQL("COPY sp_new ({c}, row_hash) FROM STDIN").format(c=sql.SQL(", ").join(I(c) for c in cols_db))) as cp:
            for h, r in uniq.items():
                cp.write_row([*r, h])
        a_db = [have[key_of(h)] for h in attrs]
        n_db = I(have[key_of(nm)])
        added = changed = removed = back = 0

        # details changed (same organisation, different details)
        if a_db:
            diff = sql.SQL(" OR ").join(sql.SQL("s.{c} IS DISTINCT FROM n.{c}").format(c=I(c)) for c in a_db)
            what = sql.SQL("concat_ws('; ', {})").format(sql.SQL(", ").join(
                sql.SQL("CASE WHEN s.{c} IS DISTINCT FROM n.{c} THEN {l} || ': ' || COALESCE(s.{c}, '') || ' -> ' || COALESCE(n.{c}, '') END").format(c=I(c), l=sql.Literal(c)) for c in a_db))
            if not first_load:
                changed = con.execute(sql.SQL('''WITH ch AS (SELECT s."Sponsor ID" AS id, n.{nm} AS nm, {what} AS what FROM {s}.sponsor s JOIN sp_new n ON n.row_hash = s.row_hash
                        WHERE s."On register" AND ({diff})), ins AS (INSERT INTO {s}.change_log (change_type, sponsor_id, name, detail)
                        SELECT 'Details changed', id, nm, what FROM ch RETURNING 1) SELECT count(*) FROM ins''').format(s=S, nm=n_db, what=what, diff=diff)).fetchone()[0]
            con.execute(sql.SQL("UPDATE {s}.sponsor s SET {sets} FROM sp_new n WHERE n.row_hash = s.row_hash AND ({diff})").format(
                s=S, sets=sql.SQL(", ").join(sql.SQL("{c} = n.{c}").format(c=I(c)) for c in a_db), diff=diff))

        # came back after being removed
        back = con.execute(sql.SQL('''WITH b AS (UPDATE {s}.sponsor s SET "On register" = true FROM sp_new n WHERE n.row_hash = s.row_hash AND NOT s."On register"
                RETURNING s."Sponsor ID" AS id, n.{nm} AS nm, {dt} AS dt),
                ins AS (INSERT INTO {s}.change_log (change_type, sponsor_id, name, detail) SELECT 'Added to register', id, nm, 'Back on the register - ' || COALESCE(dt, '') FROM b RETURNING 1)
                SELECT count(*) FROM ins''').format(s=S, nm=n_db, dt=detail_expr('s', cols_db))).fetchone()[0]
        con.execute(sql.SQL('UPDATE {s}.sponsor s SET "Last seen" = current_date FROM sp_new n WHERE n.row_hash = s.row_hash').format(s=S))

        # new organisations
        ins_cols = [I(c) for c in cols_db]
        log = "" if first_load else ", ins AS (INSERT INTO {s}.change_log (change_type, sponsor_id, name, detail) SELECT 'Added to register', id, nm, dt FROM i RETURNING 1)"
        q = ('WITH i AS (INSERT INTO {s}.sponsor ("First seen", "Last seen", "On register", row_hash, {c}) SELECT current_date, current_date, true, n.row_hash, {nc} '
             'FROM sp_new n WHERE NOT EXISTS (SELECT 1 FROM {s}.sponsor s WHERE s.row_hash = n.row_hash) '
             'RETURNING "Sponsor ID" AS id, {nm} AS nm, {dt} AS dt)' + log + ' SELECT count(*) FROM i')
        added = con.execute(sql.SQL(q).format(s=S, c=sql.SQL(", ").join(ins_cols), nc=sql.SQL(", ").join(sql.SQL("n.{}").format(c) for c in ins_cols),
                                              nm=n_db, dt=detail_expr('sponsor', cols_db))).fetchone()[0]

        # no longer on the register
        removed = con.execute(sql.SQL('''WITH r AS (UPDATE {s}.sponsor s SET "On register" = false WHERE s."On register" AND NOT EXISTS (SELECT 1 FROM sp_new n WHERE n.row_hash = s.row_hash)
                RETURNING s."Sponsor ID" AS id, s.{nm} AS nm, {dt} AS dt),
                ins AS (INSERT INTO {s}.change_log (change_type, sponsor_id, name, detail) SELECT 'Removed from register', id, nm, dt FROM r RETURNING 1)
                SELECT count(*) FROM ins''').format(s=S, nm=n_db, dt=detail_expr('s', cols_db))).fetchone()[0]
        con.execute(sql.SQL("INSERT INTO {s}.file_snapshot (url, title, signature, page_updated, rows_read, file_path) VALUES (%s, %s, %s, %s, %s, %s)").format(s=S),
                    (url, title, sig, page_date, len(uniq), str(path)))
    make_views(con)
    return len(uniq), first_load, added + back, removed, changed


# ---------------------------------------------------------------- commands
def update_main(args):
    force = "--force" in args
    with connect() as con:
        setup(con)
        if data_columns(con):
            make_views(con)                                   # keeps the views current after an update of this script
        if "--file" in args:
            path = Path(args[args.index("--file") + 1])
            n, first, a, r, c = load_file(con, path, path.resolve().as_uri(), path.name, "manual", "", force)
            say(f"{n} organisations read." + (" First load: this is now the starting point, nothing logged." if first else f" {a} added, {r} removed, {c} changed."))
            return 0
        url, title, page_date = newest_file()
        if not url:
            say("No CSV found on the gov.uk page. The page may have changed - open it and tell Claude what you see.")
            return 1
        time.sleep(DELAY)
        sig = signature(url)
        last = con.execute(sql.SQL("SELECT url, signature, loaded_at::date FROM {s}.file_snapshot ORDER BY snapshot_id DESC LIMIT 1").format(s=I(SCHEMA))).fetchone()
        if last and last[0] == url and last[1] == sig and not force:
            say(f"  SKIP     latest file unchanged since {last[2]}  ({title})")
            return 0
        say(f"  {'CHANGED ' if last else 'NEW     '} downloading the latest file: {title}")
        time.sleep(DELAY)
        path = download(url)
        n, first, a, r, c = load_file(con, path, url, title, sig, page_date, force)
        say(f"           {n} organisations read." + (" First load: this is the starting point, nothing logged." if first else f" {a} added, {r} removed, {c} changed."))
        return 0


def show_main(args):
    with connect() as con:
        S = I(SCHEMA)
        try:
            snap = con.execute(sql.SQL("SELECT title, rows_read, loaded_at::date FROM {s}.file_snapshot ORDER BY snapshot_id DESC LIMIT 5").format(s=S)).fetchall()
            tot = con.execute(sql.SQL('SELECT count(*), count(*) FILTER (WHERE "On register") FROM {s}.sponsor').format(s=S)).fetchone()
            chg = con.execute(sql.SQL("SELECT change_type, count(*) FROM {s}.change_log GROUP BY 1 ORDER BY 1").format(s=S)).fetchall()
        except psycopg.Error:
            say("Nothing loaded yet - run  python register_sponsors.py")
            return 0
        say("Last loads:")
        for t, n, d in snap:
            say(f"  {d}  {n} rows  {t}")
        say(f"Organisations stored: {tot[0]}   on the register now: {tot[1]}")
        for t, n in chg:
            say(f"  {t}: {n}")
        say("Columns: " + ", ".join(FIXED + data_columns(con)))
    return 0


def reset_main(args):
    if input(f'This deletes the whole schema "{SCHEMA}" (every sponsor and change). Type DROP to continue: ').strip() != "DROP":
        say("Nothing deleted.")
        return 0
    with connect() as con:
        con.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(I(SCHEMA)))
    say("Deleted. Run  python register_sponsors.py  to load again.")
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
    except RuntimeError as e:
        say(f"FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
