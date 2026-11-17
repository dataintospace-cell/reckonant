"""register_gazette.py -- the Gazette register: corporate and personal notices.  ONE file, run it command by command.

  python register_gazette.py                 = update: load the last 7 days of every Companies / People category, read the new
                                                 notice pages, then refresh the clean tables   (this is what daily_update.py runs)
  python register_gazette.py update --days 30      the same for the last 30 days (more options: see "load" below)
  python register_gazette.py setup           create / upgrade the tables and views in "R5_insolvency" (safe to repeat)
  python register_gazette.py load [options]  only fetch notices + read notice pages (the old insolvency_load.py), e.g.
        load --tree                              read the Gazette category tree (Companies / People) and save it
        load --trees --days 30                   load every category for the last 30 days
        load --enrich-only --enrich-all --enrich-limit 3000     read the notice pages not read yet (slow: 10 s per page)
        load --enrich-only --refill-old          also read again pages that an older version of the script read
        load --find "dividend"                   where does this kind of notice show up
  python register_gazette.py tables          build / refresh the clean tables (one per Gazette category), see below
  python register_gazette.py tables --reset  drop and rebuild them from the notices already saved (nothing is fetched)
  python register_gazette.py reset           ONE-TIME clean-up: delete all Gazette data and start empty (asks for YES; --backup keeps the old schema)
  python register_gazette.py check           read-only health check of the whole database (duplicate / empty columns, counts)

Schemas:
  "R5_insolvency"          raw store: gazette_notice, gazette_notice_field (every key : value), gazette_category, load_log ... (the tracker's matching uses it)
  "R7_corporate_notices"   33 tables, one per Companies category      } columns = the keys of the Gazette notice,
  "R8_personal_notices"    18 tables, one per Personal insolvency category   }   "Gazette link" last
  "R9_other_notices"       the other People categories (appointments, name changes, honours, wills)

The text of each part follows.  -------------------------------------------------------------------------------------------------

==== load: the Gazette data feed ====
load -- fetch the last N days of Gazette insolvency notices and save them in PostgreSQL.

  python register_gazette.py load              last 30 days, insolvency notice codes only (corporate + personal)
  python register_gazette.py load --days 7     last 7 days
  python register_gazette.py load --all        every Gazette notice type (planning, deceased estates, ...): many more pages
  python register_gazette.py load --test       fetch only the first page and show it (and one notice page), nothing is saved
  python register_gazette.py load --tree       read the Gazette's Companies / People category tree (codes G...) and save it (no notices)
  python register_gazette.py load --trees      load EVERY category under Companies and People, one query per category (about 70), e.g. --trees --days 7
  python register_gazette.py load --enrich-only      only fill the notice details of notices already saved
  python register_gazette.py load --enrich-limit 2000   read up to 2000 notice pages in this run (default 300)
  python register_gazette.py load --no-enrich  do not read notice pages

NOTICE DETAILS: the data feed does not give everything the Gazette shows in a notice's "Notice details" box (Notice category, Notice type,
Publication date and time, Edition, Notice ID, Notice code, Issue number, Page number). Whatever the feed gives is saved; for the rest the
script opens the notice page itself (https://www.thegazette.co.uk/notice/<id>) ONCE per notice, waiting 10 seconds between pages as the
Gazette's robots.txt asks, newest insolvency notices first. The columns are in "R5_insolvency".gazette_notice; the view
"R5_insolvency".v_gazette_notice_detail shows them with the link to the notice as the LAST column.

Source: The Gazette public notice data feed (Atom, 100 notices per page):
  https://www.thegazette.co.uk/all-notices/notice/data.feed
The Gazette's robots.txt asks for 10 seconds between requests, so this script waits 10 s between pages (the run takes roughly
1 minute per 6 pages; the last 30 days of insolvency notices is usually a few dozen pages).

Saves into the tables made by the setup command ("R5_insolvency".gazette_notice and load_log). A notice already stored is
not duplicated: it is updated and marked as seen again. Each finished page is committed, so if the run stops you can simply run it again.

Connection: same as your other scripts, password from pgpass.conf.  Env: PGHOST, PGPORT, PGUSER, PGDATABASE.
"""
import argparse, datetime as dt, html, json, os, re, sys, time, urllib.error, urllib.request
from html.parser import HTMLParser
import psycopg
from psycopg import sql


# =====================================================================
#  1. Tables and views  (was insolvency_setup.py)
# =====================================================================

HOST = os.environ.get("PGHOST", "localhost")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
DBNAME = os.environ.get("PGDATABASE", "postgres")
SCHEMA = "R5_insolvency"

# ---- Gazette notice codes and groups (same grouping the Signals collectors use) -------------------------------------
GROUPS = ["Winding-up petition", "Winding-up order", "Winding-up petition dismissed", "Administration",
          "Creditors' voluntary liquidation", "Members' voluntary liquidation", "Liquidator appointed",
          "Other corporate insolvency notice", "Prohibited names, moratoria and cross-border",
          "Bankruptcy order", "Other personal insolvency notice", "Company notices (restorations, capital, disclaimers)",
          "Partnership notices", "Deceased estates", "Changes of name", "Planning, transport and environment",
          "State and other notices"]
EXACT = {"2450": 0, "2451": 0, "2452": 1, "2461": 2, "2410": 3, "2411": 3, "2441": 4, "2431": 5, "2432": 6, "2443": 6,
         "2454": 6, "2403": 8, "2404": 8, "2405": 8, "2503": 9, "2504": 9, "2903": 13, "2604": 13, "2901": 14}
CODES = ([str(c) for c in range(1100, 1125)] + [str(c) for c in (1302, 1501, 1502, 1503, 1504, 1510, 1601, 1602, 1603, 1608,
         1801, 1802, 1803, 1901, 1902, 2101, 2102, 2103, 2104, 2301, 2302)] + [str(c) for c in range(2400, 2471)] +
         [str(c) for c in range(2500, 2531)] + [str(c) for c in range(2600, 2621)] + [str(c) for c in range(2700, 2711)] +
         [str(c) for c in range(2901, 2906)])


def group_of(code):
    if code in EXACT:
        return EXACT[code]
    n = int(code)
    if 2400 <= n < 2500: return 7
    if 2500 <= n < 2600: return 10
    if 2600 <= n < 2700: return 11
    if 2700 <= n < 2800: return 12
    if 2900 <= n < 3000: return 14
    if 1500 <= n < 2200: return 15
    return 16


def kind_of(g):          # groups 0-8 = corporate insolvency, 9-10 = personal insolvency, the rest are other Gazette notices
    return "corporate" if g <= 8 else "personal" if g <= 10 else "other"


# corporate / personal / other, per notice
KIND = ("CASE WHEN gc.insolvency_kind IS NOT NULL AND gc.insolvency_kind <> 'other' THEN gc.insolvency_kind "      # the Gazette tree (category code) decides first
        "WHEN gc.category_code IS NOT NULL THEN 'other' "
        "WHEN n.detail_fetched_at IS NOT NULL AND n.notice_category ILIKE '%corporate insolvency%' THEN 'corporate' "
        "WHEN n.detail_fetched_at IS NOT NULL AND n.notice_category ILIKE '%personal insolvency%' THEN 'personal' "
        "WHEN n.detail_fetched_at IS NOT NULL AND n.notice_category IS NOT NULL THEN 'other' "
        "ELSE COALESCE(t.insolvency_kind, 'other') END")
# the notice type as the Gazette names it (from the notice page, else from the tree, else the built-in group)
LABEL = "COALESCE(n.notice_type, gc.label, t.notice_type_name, t.group_name, n.notice_code)"
TOP = "COALESCE(gc.tree_top, t.tree_top)"

DDL = f'''
CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";

-- Lookup: every Gazette notice code and which group it belongs to
CREATE TABLE IF NOT EXISTS "{SCHEMA}".gazette_notice_type (
    notice_code     text PRIMARY KEY,
    group_name      text NOT NULL,
    insolvency_kind text NOT NULL CHECK (insolvency_kind IN ('corporate','personal','other')),
    is_insolvency   boolean NOT NULL
);

-- One row per Gazette notice. notice_id is the number in https://www.thegazette.co.uk/notice/<notice_id>
CREATE TABLE IF NOT EXISTS "{SCHEMA}".gazette_notice (
    notice_id       bigint PRIMARY KEY,
    notice_code     text NOT NULL,
    category        text,                  -- the Gazette's own category, e.g. "Corporate Insolvency"
    title           text,                  -- notice title as published
    party_name      text,                  -- company or person the notice is about (cleaned from the title)
    company_number  text,                  -- when the notice states one, e.g. 01234567 or SC123456
    court_number    text,
    postcode        text,
    address         text,                  -- deceased-estate and personal notices
    published_date  date NOT NULL,
    body_text       text,                  -- notice text with markup removed
    event           text,                  -- one-line summary used by the Signals page
    url             text NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gazette_notice_date_idx    ON "{SCHEMA}".gazette_notice (published_date DESC);
CREATE INDEX IF NOT EXISTS gazette_notice_code_idx    ON "{SCHEMA}".gazette_notice (notice_code, published_date DESC);
CREATE INDEX IF NOT EXISTS gazette_notice_company_idx ON "{SCHEMA}".gazette_notice (company_number) WHERE company_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS gazette_notice_party_idx   ON "{SCHEMA}".gazette_notice (lower(party_name) text_pattern_ops);

-- The Gazette's "Notice details" panel (Notice category, Notice type, Publication date, Edition, Notice ID, Notice code,
-- Issue number, Page number). Filled from the data feed when it has them, otherwise from the notice page (the load command).
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS notice_category   text;
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS notice_type       text;
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS published_at      timestamp;      -- publication date AND time
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS edition           text;           -- e.g. The London Gazette
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS issue_number      text;
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS page_number       text;
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS detail_fetched_at timestamptz;     -- when the notice page was read
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS feed_fields       jsonb;          -- every f: field the data feed gave
-- where each notice code sits in the Gazette's Companies / People tree (filled by:  python register_gazette.py load --tree)
-- The Gazette's own filter tree (category codes G...). Leaves under Companies and People are what the loader queries.
CREATE TABLE IF NOT EXISTS "{SCHEMA}".gazette_category (
    category_code   text PRIMARY KEY,       -- e.g. G406030005
    label           text NOT NULL,          -- e.g. Bankruptcy orders
    parent_code     text,
    parent_label    text,                   -- e.g. Personal insolvency
    tree_top        text,                   -- Companies | People
    tree_path       text,                   -- People > Personal insolvency > Bankruptcy orders
    is_leaf         boolean NOT NULL DEFAULT true,
    insolvency_kind text NOT NULL DEFAULT 'other' CHECK (insolvency_kind IN ('corporate','personal','other')),
    read_at         timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS category_code text;
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS fields_parsed boolean;  -- key : value pairs of the notice saved in gazette_notice_field

-- EVERY key : value of a notice: section 'details' = the Notice details box (Notice category, Notice type, ...),
-- section 'content' = the notice text (Person status, Birth details, Capacity of office holder(s), Date of Appointment, ...).
CREATE TABLE IF NOT EXISTS "{SCHEMA}".gazette_notice_field (
    notice_id  bigint NOT NULL REFERENCES "{SCHEMA}".gazette_notice (notice_id) ON DELETE CASCADE,
    section    text   NOT NULL CHECK (section IN ('details','content')),
    seq        integer NOT NULL,            -- order on the page
    key        text   NOT NULL,
    value      text,
    PRIMARY KEY (notice_id, section, seq)
);
CREATE INDEX IF NOT EXISTS gazette_notice_field_key_idx ON "{SCHEMA}".gazette_notice_field (key, lower(value) text_pattern_ops);
ALTER TABLE "{SCHEMA}".gazette_notice ADD COLUMN IF NOT EXISTS body_full boolean;      -- body_text is the full text from the notice page
CREATE INDEX IF NOT EXISTS gazette_notice_catcode_idx ON "{SCHEMA}".gazette_notice (category_code, published_date DESC);
ALTER TABLE "{SCHEMA}".gazette_notice_type ADD COLUMN IF NOT EXISTS tree_top         text;       -- Companies | People
ALTER TABLE "{SCHEMA}".gazette_notice_type ADD COLUMN IF NOT EXISTS tree_path        text;       -- Companies > Corporate insolvency > ...
ALTER TABLE "{SCHEMA}".gazette_notice_type ADD COLUMN IF NOT EXISTS notice_type_name text;

-- Insolvency Service restrictions list (bankruptcy / debt relief restrictions orders and undertakings)
CREATE TABLE IF NOT EXISTS "{SCHEMA}".restriction_order (
    court_number    text PRIMARY KEY,
    person_name     text,
    kind            text,                  -- Restriction Order | Restriction Undertaking
    order_date      date,
    length_text     text,                  -- e.g. "7 years"
    postcode        text,
    conduct         text,
    url             text,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS restriction_order_date_idx ON "{SCHEMA}".restriction_order (order_date DESC);
CREATE INDEX IF NOT EXISTS restriction_order_name_idx ON "{SCHEMA}".restriction_order (lower(person_name) text_pattern_ops);

-- One row per load run
CREATE TABLE IF NOT EXISTS "{SCHEMA}".load_log (
    run_id          bigserial PRIMARY KEY,
    source          text NOT NULL,         -- gazette | restrictions
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    from_date       date,
    to_date         date,
    rows_fetched    integer,
    rows_new        integer,
    rows_updated    integer,
    status          text NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','failed')),
    message         text
);

-- WHICH notices are insolvency: once a notice's own page has been read, the Gazette's "Notice category" decides
-- (Corporate Insolvency / Personal Insolvency; anything else = other notices). Before that, the notice code list decides.
CREATE OR REPLACE VIEW "{SCHEMA}".v_insolvency_notice AS
SELECT n.notice_id, n.published_date, {KIND} AS insolvency_kind, t.group_name, n.notice_code, n.category,
       n.party_name, n.company_number, n.court_number, n.postcode, n.event, n.url, n.title, n.body_text,
       n.notice_category, n.notice_type, n.published_at, n.edition, n.issue_number, n.page_number,
       {LABEL} AS type_label
FROM "{SCHEMA}".gazette_notice n
LEFT JOIN "{SCHEMA}".gazette_notice_type t USING (notice_code)
LEFT JOIN "{SCHEMA}".gazette_category gc ON gc.category_code = n.category_code
WHERE {KIND} <> 'other';

-- Every notice (insolvency or not) with the "Notice details" fields; the LAST column is the link to the notice on the Gazette site.
DROP VIEW IF EXISTS "{SCHEMA}".v_gazette_notice_detail;
CREATE VIEW "{SCHEMA}".v_gazette_notice_detail AS
SELECT n.notice_id, n.notice_code, n.notice_category, n.notice_type, {LABEL} AS type_label, {KIND} AS insolvency_kind,
       n.published_date AS publication_date, n.published_at AS publication_date_time, n.edition, n.issue_number, n.page_number,
       {TOP} AS tree_top, t.group_name, n.party_name, n.company_number, n.court_number, n.postcode, n.address,
       n.detail_fetched_at, n.category_code, n.url AS notice_link
FROM "{SCHEMA}".gazette_notice n
LEFT JOIN "{SCHEMA}".gazette_notice_type t USING (notice_code)
LEFT JOIN "{SCHEMA}".gazette_category gc ON gc.category_code = n.category_code;

CREATE OR REPLACE VIEW "{SCHEMA}".v_gazette_notice_field AS
SELECT f.notice_id, n.published_date, {KIND} AS insolvency_kind, {LABEL} AS type_label, n.party_name,
       f.section, f.seq, f.key, f.value, n.url AS notice_link
FROM "{SCHEMA}".gazette_notice_field f
JOIN "{SCHEMA}".gazette_notice n USING (notice_id)
LEFT JOIN "{SCHEMA}".gazette_notice_type t USING (notice_code)
LEFT JOIN "{SCHEMA}".gazette_category gc ON gc.category_code = n.category_code;
'''


def setup_main():
    try:
        with psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True) as con:
            con.execute(DDL)
            with con.cursor() as cur:
                cur.executemany(
                    f'''INSERT INTO "{SCHEMA}".gazette_notice_type (notice_code, group_name, insolvency_kind, is_insolvency)
                        VALUES (%s,%s,%s,%s) ON CONFLICT (notice_code) DO UPDATE
                        SET group_name=EXCLUDED.group_name, insolvency_kind=EXCLUDED.insolvency_kind, is_insolvency=EXCLUDED.is_insolvency''',
                    [(c, GROUPS[group_of(c)], kind_of(group_of(c)), kind_of(group_of(c)) != "other") for c in CODES])
            try:   # partial-name search for the UI; needs the contrib module, which the Windows installer includes
                con.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                con.execute(f'CREATE INDEX IF NOT EXISTS gazette_notice_party_trgm ON "{SCHEMA}".gazette_notice USING gin (party_name gin_trgm_ops)')
                con.execute(f'CREATE INDEX IF NOT EXISTS restriction_order_name_trgm ON "{SCHEMA}".restriction_order USING gin (person_name gin_trgm_ops)')
                trgm = "yes"
            except Exception as e:
                trgm = "no (" + str(e).splitlines()[0] + ")"
            print(f'Schema "{SCHEMA}" ready in database "{DBNAME}".')
            for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_type, table_name", (SCHEMA,)).fetchall():
                n = con.execute(f'SELECT count(*) FROM "{SCHEMA}"."{t}"').fetchone()[0]
                print(f"  {t:24} {n} rows")
            print("  partial-name search index (pg_trgm):", trgm)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect: {e}\nCheck PostgreSQL is running and pgpass.conf has  localhost:5432:*:postgres:YOURPASSWORD")


# =====================================================================
#  2. Load notices from the Gazette  (was insolvency_load.py)
# =====================================================================

HOST = os.environ.get("PGHOST", "localhost"); PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres"); DBNAME = os.environ.get("PGDATABASE", "postgres")
SCHEMA = "R5_insolvency"
FEED = os.environ.get("GAZETTE_FEED", "https://www.thegazette.co.uk/all-notices/notice/data.feed")
SITE = os.environ.get("GAZETTE_SITE", "https://www.thegazette.co.uk")
ENRICH_LIMIT = 300                                              # notice pages read in one run
DELAY = float(os.environ.get("GAZETTE_DELAY", "10"))          # seconds between requests (the Gazette's Crawl-delay)
UA = "Mozilla/5.0 (compatible; Reckonant local loader)"

NUM = re.compile(r"Company (?:Number|No\.?|Registration Number)[:\s]*([A-Z]{0,2}\d{5,8})", re.I)
MATTER = re.compile(r"In the Matter of (.*?)\s*(?:\(|and in the matter|$)", re.I)
PC = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b")
COURT = re.compile(r"Court Number:?\s*([A-Z0-9\-]+)")
DECEASED = re.compile(r"Address of Deceased\s+(.*?)(?:\s+Date of|\s+Name of|$)")


def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch(url, tries=6):
    last = None
    for i in range(tries):
        time.sleep(DELAY)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/atom+xml, */*"})
            with urllib.request.urlopen(req, timeout=120) as r:
                t = r.read().decode("utf-8", "ignore")
            if "<feed" in t:
                return t
            last = "response was not an Atom feed"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
        wait = 15 * (i + 1)
        print(f"  retry {i + 1}/{tries - 1} in {wait}s ({last})", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"Gazette feed failed after {tries} tries: {last}")


def feed_extra(e):
    """Notice-details fields the feed entry itself carries (names vary, so every f: field is kept in feed_fields as well)."""
    ff = {k: html.unescape(v).strip() for k, v in re.findall(r"<f:([A-Za-z0-9\-]+)[^>]*>([^<]{0,200})</f:\1>", e)}
    first = lambda *n: next((ff[x] for x in n if ff.get(x)), None)
    edition, issue, page = first("edition", "edition-name"), first("issue", "issue-number", "issue-no"), first("page-number", "page", "start-page")
    m = re.search(r"/(London|Edinburgh|Belfast)/issue/(\d+)(?:/page/(\d+))?", e)
    if m:
        edition, issue, page = edition or f"The {m.group(1)} Gazette", issue or m.group(2), page or m.group(3)
    pub_at = None
    pa = re.search(r"<published>([^<]+)</published>", e)
    if pa and "T" in pa.group(1):
        try:
            pub_at = dt.datetime.fromisoformat(pa.group(1).strip().replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    term = re.search(r'<category term="([^"]+)"', e)
    return dict(notice_type=first("notice-type", "notice-type-name") or (html.unescape(term.group(1)) if term else None), edition=edition, issue_number=issue, page_number=page,
                published_at=pub_at, feed_fields=json.dumps(ff) if ff else None)


def parse(feed_xml, category_code=None):
    out = []
    for e in feed_xml.split("<entry>")[1:]:
        nid = (re.search(r"/notice/(\d+)", e) or [None, ""])[1]
        code = (re.search(r"<f:notice-code>(\d+)", e) or [None, ""])[1]
        if not nid or not code:
            continue
        cat = html.unescape((re.search(r'<category term="([^"]+)"', e) or [None, ""])[1])
        title = text((re.search(r"<title>(.*?)</title>", e, re.S) or [None, ""])[1])
        body = text(e[e.find("<content"):]) if "<content" in e else ""
        name = title or text((MATTER.search(body) or [None, ""])[1]) or body[:90]
        name = re.sub(r"\s*\(?Company (?:Number|No).*$", "", name, flags=re.I).strip(" ,")
        if name.lower() == cat.lower() and body:
            name = body[:110].rsplit(" ", 1)[0] + "..." if len(body) > 110 else body
        num = (NUM.search(body) or [None, ""])[1]
        court = (COURT.search(body) or [None, ""])[1]
        pc = (PC.search(body) or [None, ""])[1]
        addr = (DECEASED.search(body) or [None, ""])[1]
        pub = (re.search(r"<published>(\d{4}-\d\d-\d\d)", e) or [None, ""])[1]
        event = f"{name[:120]}: {cat}" if name and cat else (name or cat)
        out.append(dict(notice_id=int(nid), notice_code=code, category=cat or None, title=title or None, party_name=name[:300] or None,
                        company_number=num or None, court_number=court or None, postcode=pc or None, address=addr[:300] or None,
                        published_date=pub, body_text=body or None, event=event or None, url=f"https://www.thegazette.co.uk/notice/{nid}",
                        notice_category=None, category_code=category_code, **feed_extra(e)))
    return [r for r in out if r["published_date"]]


UPSERT = f'''
INSERT INTO "{SCHEMA}".gazette_notice (notice_id, notice_code, category, title, party_name, company_number, court_number, postcode,
                                       address, published_date, body_text, event, url, notice_category, notice_type, published_at,
                                       edition, issue_number, page_number, feed_fields, category_code)
VALUES (%(notice_id)s, %(notice_code)s, %(category)s, %(title)s, %(party_name)s, %(company_number)s, %(court_number)s, %(postcode)s,
        %(address)s, %(published_date)s, %(body_text)s, %(event)s, %(url)s, %(notice_category)s, %(notice_type)s, %(published_at)s,
        %(edition)s, %(issue_number)s, %(page_number)s, %(feed_fields)s::jsonb, %(category_code)s)
ON CONFLICT (notice_id) DO UPDATE SET
    category=EXCLUDED.category, title=EXCLUDED.title, party_name=EXCLUDED.party_name, company_number=EXCLUDED.company_number,
    court_number=EXCLUDED.court_number, postcode=EXCLUDED.postcode, address=EXCLUDED.address, published_date=EXCLUDED.published_date,
    body_text=CASE WHEN "{SCHEMA}".gazette_notice.body_full THEN "{SCHEMA}".gazette_notice.body_text ELSE EXCLUDED.body_text END, event=EXCLUDED.event,
    category_code=COALESCE("{SCHEMA}".gazette_notice.category_code, EXCLUDED.category_code), url=EXCLUDED.url, last_seen_at=now(),
    notice_category=COALESCE("{SCHEMA}".gazette_notice.notice_category, EXCLUDED.notice_category),
    notice_type=COALESCE("{SCHEMA}".gazette_notice.notice_type, EXCLUDED.notice_type),
    published_at=COALESCE("{SCHEMA}".gazette_notice.published_at, EXCLUDED.published_at),
    edition=COALESCE("{SCHEMA}".gazette_notice.edition, EXCLUDED.edition),
    issue_number=COALESCE("{SCHEMA}".gazette_notice.issue_number, EXCLUDED.issue_number),
    page_number=COALESCE("{SCHEMA}".gazette_notice.page_number, EXCLUDED.page_number),
    feed_fields=COALESCE(EXCLUDED.feed_fields, "{SCHEMA}".gazette_notice.feed_fields)
RETURNING (xmax = 0) AS inserted'''


# ---------------------------------------------------------------------
#  The notice page: "Notice details" box
# ---------------------------------------------------------------------
DETAIL_LABELS = ["Notice category", "Notice type", "Publication date", "Edition", "Notice ID", "Notice code", "Issue number", "Page number"]


def fetch_page(url, tries=4):
    last = None
    for i in range(tries):
        time.sleep(DELAY)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=60) as r:
                t = r.read().decode("utf-8", "ignore")
            if "Notice category" in t or "Notice details" in t:
                return t
            last = "page has no Notice details box"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 404:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


class _NoticeDiv(HTMLParser):
    """Finds the notice body: the <div ... data-gazettes="Notice"> (nothing of the site menus, footer or side links is inside it)."""
    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "col", "area", "base", "wbr"}

    def __init__(self, page):
        super().__init__(convert_charrefs=False)
        self.page, self.depth, self.start, self.end = page, 0, None, None
        self.lines = [0]
        for ln in page.split("\n"):
            self.lines.append(self.lines[-1] + len(ln) + 1)

    def _pos(self):
        line, off = self.getpos()
        return self.lines[line - 1] + off

    def handle_starttag(self, tag, attrs):
        if self.end is not None or tag != "div":
            return
        if self.start is None:
            if dict(attrs).get("data-gazettes") == "Notice":
                self.depth = 1
                self.start = self._pos() + len(self.get_starttag_text())
        else:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.start is None or self.end is not None or tag != "div":
            return
        self.depth -= 1
        if self.depth == 0:
            self.end = self._pos()


def notice_body_html(page):
    p = _NoticeDiv(page)
    try:
        p.feed(page)
    except Exception:
        pass
    return page[p.start:p.end] if p.start is not None and p.end is not None else None


def parse_notice_page(page):
    """The page has <dl class="metadata"><dt>Notice category:</dt><dd>..</dd>...</dl> followed by <div class="content"> (the full notice text)."""
    out = {}
    dl = re.search(r'(?is)<dl[^>]*class="[^"]*metadata[^"]*"[^>]*>(.*?)</dl>', page)
    raw, pub = {}, None
    if dl:
        for dt_, attrs, val in re.findall(r"(?is)<dt[^>]*>(.*?)</dt>\s*<dd([^>]*)>(.*?)</dd>", dl.group(1)):
            raw[text(dt_).rstrip(":").strip()] = text(val)
            if "Publication date" in dt_:
                pub = (re.search(r'content="([^"]+)"', attrs) or [None, None])[1]
    else:                                              # older layout: flat text
        t = text(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page))
        i = t.find("Notice category")
        seg = t[i:] if i >= 0 else ""
        pos = sorted((m.start(), m.end(), lab) for lab in DETAIL_LABELS for m in [re.search(re.escape(lab) + r"\s*:", seg)] if m)
        for k, (st, en, lab) in enumerate(pos):
            raw[lab] = seg[en:(pos[k + 1][0] if k + 1 < len(pos) else en + 40)].strip()[:60]
    out = dict(notice_category=raw.get("Notice category") or None, notice_type=raw.get("Notice type") or None,
               edition=raw.get("Edition") or None, issue_number=raw.get("Issue number") or None,
               page_number=raw.get("Page number") or None, page_notice_id=raw.get("Notice ID"), page_notice_code=raw.get("Notice code"))
    if pub:
        try:
            out["published_at"] = dt.datetime.fromisoformat(pub.replace("Z", ""))
        except ValueError:
            pass
    if "published_at" not in out:
        m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?:,?\s+(\d{1,2}):(\d{2}))?", raw.get("Publication date", ""))
        if m and m.group(2).lower()[:3] in MON:
            try:
                out["published_at"] = dt.datetime(int(m.group(3)), MON[m.group(2).lower()[:3]], int(m.group(1)), int(m.group(4) or 0), int(m.group(5) or 0))
            except ValueError:
                pass
    inner = notice_body_html(page)
    body = text(re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", inner)) if inner else ""
    out["body_text"] = body or None
    out["fields"] = parse_fields(page, list(raw.items()))
    out["labels_found"] = len(raw)
    return out


class _ContentParser(HTMLParser):
    """Splits the notice text into blocks (paragraphs, headings, list items, table cells), each with the Gazette's own data-gazettes hint if it has one."""
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "dt", "dd", "div", "tr", "br"}

    def __init__(self):
        super().__init__()
        self.blocks, self.buf, self.stack, self.heads, self.col, self.row = [], [], [], {}, 0, 0
        self.in_head = False

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if t:
            hint = next((h for _, h, _ in reversed(self.stack) if h), None)
            tag = self.stack[-1][0] if self.stack else ""
            self.blocks.append((t, hint, tag, self.col if tag in ("td", "th") else None, self.row))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.BLOCK:
            self._flush()
        if tag == "tr":
            self.row += 1; self.col = 0
        if tag in ("td", "th"):
            self.col += 1
        if tag not in ("br", "img", "input", "meta", "link", "hr"):
            self.stack.append((tag, a.get("data-gazettes"), a))

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self._flush()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, d):
        self.buf.append(d)


KEY = re.compile(r"^([A-Za-z][A-Za-z0-9 ()/'&.,\-]{1,60}?)\s*:\s+(\S.*)$")


def _camel(h):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", h or "").strip()


def parse_fields(page, details):
    """-> [(section, seq, key, value)]: the Notice details box as Key : Value, then every piece of the notice text as Key : Value.
    A line 'Birth details: 28 September 1965' gives key 'Birth details'; a line without a label takes the Gazette's own tag for it
    (Court Name, Case Number ...) or the key 'Text'."""
    out, seq = [], 0
    for k, v in details:
        seq += 1
        out.append(("details", seq, k, v))
    inner = notice_body_html(page)
    if not inner:
        return out
    inner = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", inner)
    p = _ContentParser()
    p.feed(inner)
    p._flush()
    seq = 0
    heads, pend = {}, None
    for t, hint, tag, col, row in p.blocks:
        if tag == "dt":                               # definition list: <dt>Deceased Surname:</dt><dd>Butcher</dd>  ->  key : value
            pend = (t.rstrip(":").strip(), hint)
            continue
        if tag == "dd" and pend:
            seq += 1
            grp = pend[1] if pend[1] and pend[1] != "Notice" else None
            out.append(("content", seq, f"{_camel(grp)} - {pend[0]}" if grp else pend[0], t))
            pend = None
            continue
        if tag in ("td", "th"):                       # table cell: key = column heading (first row) else Column n
            if tag == "th" or row == 1 and not heads.get(col):
                heads[col] = t
                if tag == "th":
                    continue
            key = f"{heads.get(col, f'Column {col}')} (row {row})"
            seq += 1
            out.append(("content", seq, key, t))
            continue
        pre = re.match(r"^(.{3,120}?)\s+(Person status:\s.*)$", t)         # name and 'Person status' in one block
        if pre:
            seq += 1
            out.append(("content", seq, "Name", pre.group(1).strip()))
            t = pre.group(2)
        m = KEY.match(t)
        if m and len(m.group(1).split()) <= 7 and not re.match(r"^\d", m.group(1)):
            key, val = m.group(1).strip(), m.group(2).strip()
        else:
            key = _camel(hint)[:1].upper() + _camel(hint)[1:] if hint and hint != "Notice" else ("Heading" if tag in ("h1", "h2", "h3", "h4") else "Text")
            if hint == "Person":
                key = "Name" if tag in ("h1", "h2", "h3", "h4") else "Person details"
            val = t
        seq += 1
        out.append(("content", seq, key, val))
        cn = re.search(r"Company (?:Number|No\.?|Registration Number)[:\s]*([A-Z]{0,2}\d{5,8})", val, re.I)
        if cn and not key.lower().startswith("company number"):
            seq += 1
            out.append(("content", seq, "Company Number", cn.group(1).upper()))
        for sk, sv in re.findall(r"([A-Z][A-Za-z]+(?: [a-z]+){0,2}):(?=[^\s/])\s*([^,;]+)", val):      # inline 'Telephone number:0300 ...', 'Email address:x@y'
            seq += 1
            out.append(("content", seq, f"{key} - {sk}", sv.strip()))
    return out


MON = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def enrich(con, limit, everything=False, refill_old=False):
    """Reads the notice page of notices whose page has not been read (newest insolvency notices first). Each page is read once; it gives the
    Notice details AND the full notice text (the feed text is cut short - the "Birth details" of a person can be missing from it)."""
    rows = con.execute(f'''SELECT n.notice_id FROM "{SCHEMA}".gazette_notice n
                          LEFT JOIN "{SCHEMA}".gazette_notice_type t USING (notice_code)
                          LEFT JOIN "{SCHEMA}".gazette_category gc ON gc.category_code = n.category_code
                          WHERE (n.fields_parsed IS NOT TRUE AND (%s OR n.detail_fetched_at IS NULL))
                            AND (%s OR COALESCE(NULLIF(gc.insolvency_kind, 'other'), t.insolvency_kind, 'other') <> 'other')
                          ORDER BY (COALESCE(NULLIF(gc.insolvency_kind, 'other'), t.insolvency_kind, 'other') <> 'other') DESC,
                                   n.published_date DESC, n.notice_id DESC LIMIT %s''', (refill_old, everything, limit)).fetchall()
    left = con.execute(f'SELECT count(*) FROM "{SCHEMA}".gazette_notice WHERE fields_parsed IS NOT TRUE').fetchone()[0]
    st = con.execute(f'''SELECT count(*) FILTER (WHERE fields_parsed), count(*) FILTER (WHERE fields_parsed IS NOT TRUE AND detail_fetched_at IS NOT NULL),
                                count(*) FILTER (WHERE detail_fetched_at IS NULL) FROM "{SCHEMA}".gazette_notice''').fetchone()
    print(f"Notice pages: {st[0]} read and saved, {st[1]} read earlier by an older version (not read again unless --refill-old), {st[2]} never read."
          f" This run reads {len(rows)}{' (more remain)' if len(rows) >= limit else ''}"
          f"{'' if everything else ' - insolvency notices only; add --enrich-all for the rest'}.", flush=True)
    if not rows:
        print("Notice details: nothing to read." if not left else f"Notice details: no insolvency notices left to read ({left} other notices wait for --enrich-all).")
        return
    print(f"Notice details: reading {len(rows)} notice pages ({int(len(rows) * (DELAY + 1) / 60) + 1} minutes; the Gazette asks for {DELAY:.0f} s between pages)...", flush=True)
    ok = bad = consecutive = 0
    for i, (nid,) in enumerate(rows, 1):
        try:
            d = parse_notice_page(fetch_page(f"{SITE}/notice/{nid}"))
            if d.get("labels_found", 0) < 3:
                raise RuntimeError("the Notice details box was not recognised")
            consecutive = 0
        except RuntimeError as e:
            bad += 1
            consecutive += 1
            print(f"  {nid}: {e}", flush=True)
            if consecutive >= 5:
                print("Five problems in a row - stopping the notice pages for this run (the notices stay saved; run again later).")
                break
            continue
        con.execute(f'''UPDATE "{SCHEMA}".gazette_notice SET notice_category=COALESCE(%(notice_category)s, notice_category),
                          notice_type=COALESCE(%(notice_type)s, notice_type), published_at=COALESCE(%(published_at)s, published_at),
                          edition=COALESCE(%(edition)s, edition), issue_number=COALESCE(%(issue_number)s, issue_number),
                          page_number=COALESCE(%(page_number)s, page_number),
                          body_text=COALESCE(%(body_text)s, body_text), body_full=(%(body_text)s IS NOT NULL), fields_parsed=true,
                          detail_fetched_at=now() WHERE notice_id=%(id)s''',
                    dict(d, published_at=d.get("published_at"), id=nid))
        con.execute(f'DELETE FROM "{SCHEMA}".gazette_notice_field WHERE notice_id=%s', (nid,))
        for sec, sq, k, v in d.get("fields", []):
            con.execute(f'INSERT INTO "{SCHEMA}".gazette_notice_field (notice_id, section, seq, key, value) VALUES (%s,%s,%s,%s,%s)', (nid, sec, sq, k[:200], v))
        ok += 1
        if i % 25 == 0:
            print(f"  ...{i} of {len(rows)} pages read", flush=True)
    print(f"Notice details: {ok} saved, {bad} problems. Notices whose page is still unread: "
          f"{con.execute(f'SELECT count(*) FROM {chr(34)}{SCHEMA}{chr(34)}.gazette_notice WHERE detail_fetched_at IS NULL').fetchone()[0]}.")


# ---------------------------------------------------------------------
#  The Gazette's Companies / People notice-type tree
# ---------------------------------------------------------------------
class _TreeParser(HTMLParser):
    """Reads the filter tree of the all-notices page: <li><div><input name="categorycode[]" value="G..."><label for=id>Name</label></div><ul>children</ul></li>."""
    def __init__(self):
        super().__init__()
        self.stack, self.nodes, self.labels = [], [], {}
        self._lab = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li":
            self.stack.append({"node": None})
        elif tag == "label":
            self._lab = {"for": a.get("for"), "buf": []}
        elif tag == "input" and (a.get("type") or "").lower() == "checkbox" and (a.get("name") or "").startswith("categorycode") and self.stack:
            parent = next((f["node"] for f in reversed(self.stack[:-1]) if f["node"] is not None), None)
            node = dict(code=(a.get("value") or "").strip(), id=a.get("id"), label="", parent=parent)
            self.nodes.append(node)
            self.stack[-1]["node"] = len(self.nodes) - 1

    def handle_endtag(self, tag):
        if tag == "li" and self.stack:
            self.stack.pop()
        elif tag == "label" and self._lab:
            if self._lab["for"]:
                self.labels[self._lab["for"]] = re.sub(r"\s+", " ", " ".join(self._lab["buf"])).strip()
            self._lab = None

    def handle_data(self, d):
        if self._lab is not None and d.strip():
            self._lab["buf"].append(d.strip())


TREE_TOPS = ("Companies", "People")
PARTNERSHIP_INSOLVENCY = ("bankruptcy orders", "petitions to wind-up", "winding-up order")


def read_tree(page):
    """-> list of dict(category_code, label, parent_code, parent_label, tree_top, tree_path, is_leaf, insolvency_kind) for everything under Companies and People."""
    p = _TreeParser()
    p.feed(page)
    for n in p.nodes:
        n["label"] = p.labels.get(n["id"], n["code"])
    kids = {}
    for i, n in enumerate(p.nodes):
        if n["parent"] is not None:
            kids.setdefault(n["parent"], []).append(i)

    def path_of(i):
        out = []
        while i is not None:
            out.append(p.nodes[i]["label"])
            i = p.nodes[i]["parent"]
        return list(reversed(out))
    best = {}
    for i, n in enumerate(p.nodes):
        path = path_of(i)
        top = next((t for t in TREE_TOPS if path[0].strip().lower() == t.lower()), None)
        if not top or not re.fullmatch(r"G\d{6,10}", n["code"]):
            continue
        leaf = i not in kids
        full = " > ".join(path)
        low = full.lower()
        kind = "other"
        if "insolvency" in low:
            kind = "corporate" if top == "Companies" else "personal"
        elif "partnerships" in low and n["label"].strip().lower() in PARTNERSHIP_INSOLVENCY:
            kind = "corporate"
        par = p.nodes[n["parent"]] if n["parent"] is not None else None
        best[n["code"]] = dict(category_code=n["code"], label=n["label"], parent_code=par["code"] if par else None,
                               parent_label=par["label"] if par else None, tree_top=top, tree_path=full, is_leaf=leaf, insolvency_kind=kind)
    return list(best.values())


def discover_tree(con, save_html=True):
    """Reads the all-notices page, finds every category (checkbox) under 'Companies' and 'People' and saves them in gazette_category."""
    url = os.environ.get("GAZETTE_TREE_URL", f"{SITE}/all-notices/notice")
    if os.path.exists(url):
        page = open(url, encoding="utf-8").read()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
        time.sleep(DELAY)
        with urllib.request.urlopen(req, timeout=60) as r:
            page = r.read().decode("utf-8", "ignore")
        if save_html:
            open("gazette_tree_page.html", "w", encoding="utf-8").write(page)
    cats = read_tree(page)
    if not cats:
        print(f"The Companies / People categories could not be read from {url} (the page may have changed).\n"
              f"  The page was saved as gazette_tree_page.html in this folder - send it to me and I will adjust the reader.")
        return 0
    with con.transaction():
        for c in cats:
            con.execute(f'''INSERT INTO "{SCHEMA}".gazette_category (category_code, label, parent_code, parent_label, tree_top, tree_path, is_leaf, insolvency_kind)
                            VALUES (%(category_code)s,%(label)s,%(parent_code)s,%(parent_label)s,%(tree_top)s,%(tree_path)s,%(is_leaf)s,%(insolvency_kind)s)
                            ON CONFLICT (category_code) DO UPDATE SET label=EXCLUDED.label, parent_code=EXCLUDED.parent_code, parent_label=EXCLUDED.parent_label,
                              tree_top=EXCLUDED.tree_top, tree_path=EXCLUDED.tree_path, is_leaf=EXCLUDED.is_leaf, insolvency_kind=EXCLUDED.insolvency_kind, read_at=now()''', c)
    for top in TREE_TOPS:
        mine = [c for c in cats if c["tree_top"] == top]
        leaves = [c for c in mine if c["is_leaf"]]
        print(f"  {top}: {len(leaves)} notice categories (leaves) in {len(mine)} tree entries; insolvency: "
              f"{sum(1 for c in leaves if c['insolvency_kind'] != 'other')}")
    return len(cats)


def pull(con, base, category_code, label=""):
    """Reads every page of one feed query and saves the notices. -> (fetched, new, updated)"""
    fetched = new = upd = 0
    page, total = 1, None
    while True:
        t = fetch(base + f"&results-page={page}")
        if total is None:
            total = int((re.search(r"<f:total>(\d+)", t) or [None, "0"])[1])
            if total == 0:
                return 0, 0, 0
            print(f"  {label}: {total} notices, {-(-total // 100)} page(s)", flush=True)
        rows = parse(t, category_code)
        with con.transaction():
            for r in rows:
                if con.execute(UPSERT, r).fetchone()[0]:
                    new += 1
                else:
                    upd += 1
        fetched += len(rows)
        if not rows or page * 100 >= total:
            return fetched, new, upd
        page += 1


def load_trees(con, start, today):
    """One feed query per category (leaf) under Companies and People, so every notice is saved with its Gazette category."""
    leaves = con.execute(f'''SELECT category_code, tree_path FROM "{SCHEMA}".gazette_category
                            WHERE is_leaf AND tree_top IN ('Companies','People') ORDER BY tree_top DESC, tree_path''').fetchall()
    print(f"Gazette notices {start} to {today}: {len(leaves)} categories under Companies and People "
          f"(about {int(len(leaves) * (DELAY + 3) / 60) + 1} minutes plus extra pages).", flush=True)
    run_id = con.execute(f'INSERT INTO "{SCHEMA}".load_log (source, from_date, to_date) VALUES (%s,%s,%s) RETURNING run_id',
                         ("gazette", start, today)).fetchone()[0]
    fetched = new = upd = 0
    failed = []
    for k, (code, path) in enumerate(leaves, 1):
        base = f"{FEED}?categorycode={code}&start-publish-date={start}&end-publish-date={today}&results-page-size=100&sort-by=latest-date"
        try:
            f, n, u = pull(con, base, code, f"[{k}/{len(leaves)}] {path}")
            fetched += f; new += n; upd += u
        except (RuntimeError, KeyboardInterrupt, Exception) as e:
            failed.append(f"{code} {path}: {e}")
            print(f"  {path}: FAILED ({e}) - continuing with the next category", flush=True)
            if isinstance(e, KeyboardInterrupt):
                break
    status = "ok" if len(failed) < len(leaves) else "failed"
    con.execute(f'UPDATE "{SCHEMA}".load_log SET finished_at=now(), rows_fetched=%s, rows_new=%s, rows_updated=%s, status=%s, message=%s WHERE run_id=%s',
                (fetched, new, upd, status, "; ".join(failed)[:900] or None, run_id))
    return status, fetched, new, upd, failed


KNOWN_CODES = set()


def find_notices(con, term):
    """Where does a kind of notice (e.g. 'intended dividend') end up in the tracker, and why."""
    p = f"%{term}%"
    S = f'"{SCHEMA}"'
    print(f'Notice types matching "{term}" in the Gazette tree / code list:')
    for c in con.execute(f"SELECT category_code, tree_path, insolvency_kind FROM {S}.gazette_category WHERE tree_path ILIKE %s ORDER BY 2", (p,)).fetchall():
        print(f"  category {c[0]}  {c[1]}  ({'insolvency: ' + c[2] if c[2] != 'other' else 'not insolvency'})")
    rows = con.execute(f"""SELECT notice_code, COALESCE(notice_type_name, group_name), tree_path, insolvency_kind, is_insolvency FROM {S}.gazette_notice_type
                           WHERE notice_type_name ILIKE %s OR group_name ILIKE %s OR tree_path ILIKE %s ORDER BY 1""", (p, p, p)).fetchall()
    for r in rows:
        print(f"  code {r[0]:6} {r[1]}  | tree: {r[2] or '(not read yet - run --tree)'} | insolvency list says: {r[3]}{' (loaded)' if r[4] else ''}")
    if not rows:
        print("  (no numeric notice code matches; the category list above is what the loader uses)")
    print(f'Notices saved that match "{term}":')
    rows = con.execute(f"""SELECT d.type_label, d.notice_code, d.insolvency_kind, d.tree_top, count(*), max(d.publication_date)
                           FROM {S}.v_gazette_notice_detail d JOIN {S}.gazette_notice n USING (notice_id)
                           WHERE d.type_label ILIKE %s OR n.title ILIKE %s OR n.category ILIKE %s OR d.notice_type ILIKE %s
                           GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC""", (p, p, p, p)).fetchall()
    for label, code, kind, top, n, last in rows:
        reg = {"corporate": "INS-02", "personal": "INS-01"}.get(kind) or {"Companies": "GAZ-01", "People": "GAZ-02"}.get(top, "GAZ-03")
        print(f"  {n:5} notices, latest {last}: {label} (code {code}) -> tracker register {reg}")
    if not rows:
        print("  none saved yet. If the type is listed above, run  python register_gazette.py load --trees --days 30  to load it.")



def load_main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="how many days back (default 30)")
    ap.add_argument("--all", action="store_true", help="every notice type, not only insolvency")
    ap.add_argument("--test", action="store_true", help="fetch the first page only and print it; save nothing")
    ap.add_argument("--tree", action="store_true", help="read the Companies / People notice-type tree of the Gazette and save it")
    ap.add_argument("--trees", action="store_true", help="load every notice type under Companies and People")
    ap.add_argument("--enrich-only", action="store_true", help="only read the notice pages of notices already saved")
    ap.add_argument("--enrich-limit", type=int, default=ENRICH_LIMIT, help="most notice pages read in one run")
    ap.add_argument("--no-enrich", action="store_true", help="do not read notice pages")
    ap.add_argument("--enrich-all", action="store_true", help="also read the pages of notices that are not insolvency (slow; their type comes from the tree)")
    ap.add_argument("--refill-old", action="store_true", help="also read again the pages an OLDER version of this script read (they have no key:value rows)")
    ap.add_argument("--reread", action="store_true", help="read EVERY saved notice page again (use once after a reader fix, with --enrich-only)")
    ap.add_argument("--find", metavar="TEXT", help="show how notices / notice types matching TEXT are classified and where they show in the tracker")
    a = ap.parse_args()
    today = dt.date.today(); start = today - dt.timedelta(days=a.days)
    try:
        con = psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect: {e}\nCheck PostgreSQL is running and pgpass.conf is set up.")
    con.execute(DDL)                                 # makes sure the tables, columns and views exist
    global KNOWN_CODES
    KNOWN_CODES = {r[0] for r in con.execute(f'SELECT notice_code FROM "{SCHEMA}".gazette_notice_type')}
    if a.tree:
        print("Reading the Gazette notice-type tree (Companies and People)...")
        discover_tree(con)
        return
    if a.find:
        find_notices(con, a.find)
        return
    if a.reread:
        done = con.execute(f'SELECT count(*) FROM "{SCHEMA}".gazette_notice WHERE fields_parsed').fetchone()[0]
        if input(f"--reread wipes the {done} notice pages already read and reads ALL of them again (about 10 s each). Type YES to do that, anything else to cancel: ").strip() != "YES":
            sys.exit("Cancelled - nothing changed. (Without --reread only the notices not yet read are read.)")
        con.execute(f'UPDATE "{SCHEMA}".gazette_notice SET fields_parsed = NULL, body_full = NULL')
        con.execute(f'DELETE FROM "{SCHEMA}".gazette_notice_field')
        print("All saved notices will be read again (insolvency notices first).")
    if a.enrich_only:
        enrich(con, a.enrich_limit, a.enrich_all, a.refill_old)
        return
    if a.trees:
        print("Reading the Gazette notice-category tree (Companies and People)...", flush=True)
        if not discover_tree(con):
            sys.exit(1)
        status, fetched, new, upd, failed = load_trees(con, start, today)
        if status == "ok" and not a.no_enrich:
            try:
                enrich(con, a.enrich_limit, a.enrich_all, a.refill_old)
            except (Exception, KeyboardInterrupt) as e:
                print(f"Notice pages stopped: {type(e).__name__}: {e} (the notices are saved; run  python register_gazette.py load --enrich-only  later).")
        print(f"Done. {fetched} notices read: {new} new, {upd} already in the table." + (f" {len(failed)} categories failed - run again." if failed else ""))
        for r in con.execute(f'SELECT tree_top, insolvency_kind, count(*) FROM "{SCHEMA}".v_gazette_notice_detail WHERE publication_date >= %s AND tree_top IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2', (start,)):
            print(f"  {r[0]:10} {r[1]:10} {r[2]}")
        sys.exit(0 if status == "ok" else 1)
    where = "" if a.all else " WHERE is_insolvency"
    with con.transaction():
        codes = [r[0] for r in con.execute(f'SELECT notice_code FROM "{SCHEMA}".gazette_notice_type' + where + " ORDER BY 1")]
    if not codes:
        sys.exit(f'No notice codes found: run  python register_gazette.py setup  first (schema "{SCHEMA}").')
    base = f"{FEED}?noticetypes={','.join(codes)}&start-publish-date={start}&end-publish-date={today}&results-page-size=100&sort-by=latest-date"
    print(f"Gazette notices {start} to {today}, {len(codes)} notice codes ({'all types' if a.all else 'Companies + People tree' if a.trees and 'tree_top' in where else 'insolvency only'})", flush=True)

    run_id = None
    if not a.test:
        with con.transaction():
            run_id = con.execute(f'INSERT INTO "{SCHEMA}".load_log (source, from_date, to_date) VALUES (%s,%s,%s) RETURNING run_id',
                                 ("gazette", start, today)).fetchone()[0]
    fetched = new = upd = 0
    try:
        page, total = 1, None
        while True:
            t = fetch(base + f"&results-page={page}")
            if total is None:
                total = int((re.search(r"<f:total>(\d+)", t) or [None, "0"])[1])
                print(f"The Gazette reports {total} notices, {-(-total // 100)} pages, about {int(-(-total // 100) * (DELAY + 3) / 60) + 1} minutes.", flush=True)
            rows = parse(t)
            if a.test:
                for r in rows[:5]:
                    print(" ", r["published_date"], r["notice_code"], r["company_number"] or "-", "|", r["event"], "|", r["url"])
                for r in rows[:3]:
                    print(f"  notice {r['notice_id']}: feed gave  type={r['notice_type']}  edition={r['edition']}  issue={r['issue_number']}  page={r['page_number']}  time={r['published_at']}")
                    print(f"      all f: fields in the feed entry: {r['feed_fields'] or '(none)'}")
                if rows:
                    try:
                        d = parse_notice_page(fetch_page(f"{SITE}/notice/{rows[0]['notice_id']}"))
                        print(f"  notice page {rows[0]['notice_id']} gives: " + ", ".join(f"{k}={v}" for k, v in d.items() if v))
                    except RuntimeError as e:
                        print("  notice page could not be read:", e)
                print(f"first page parsed: {len(rows)} notices. Nothing saved (--test).")
                return
            with con.transaction():
                for r in rows:
                    if con.execute(UPSERT, r).fetchone()[0]:
                        new += 1
                    else:
                        upd += 1
            fetched += len(rows)
            print(f"page {page}: {len(rows)} notices (new so far {new}, already stored {upd})", flush=True)
            if not rows or page * 100 >= total:
                break
            page += 1
        status, msg = "ok", None
    except (Exception, KeyboardInterrupt) as e:
        status, msg = "failed", f"{type(e).__name__}: {e}"
        print("STOPPED:", msg, "- the pages already saved are kept; run again to continue.", flush=True)
    with con.transaction():
        con.execute(f'UPDATE "{SCHEMA}".load_log SET finished_at=now(), rows_fetched=%s, rows_new=%s, rows_updated=%s, status=%s, message=%s WHERE run_id=%s',
                    (fetched, new, upd, status, msg, run_id))
    if status == "ok" and not a.no_enrich:
        try:
            enrich(con, a.enrich_limit, a.enrich_all, a.refill_old)
        except (Exception, KeyboardInterrupt) as e:
            print(f"Notice pages stopped: {type(e).__name__}: {e} (the notices are saved; run  python register_gazette.py load --enrich-only  later).")
    if status == "ok":
        print(f"Done. {fetched} notices read: {new} new, {upd} already in the table.")
        for r in con.execute(f'SELECT insolvency_kind, count(*) FROM "{SCHEMA}".v_insolvency_notice WHERE published_date >= %s GROUP BY 1 ORDER BY 1', (start,)):
            print(f"  {r[0]:10} {r[1]}")
    sys.exit(0 if status == "ok" else 1)


# =====================================================================
#  3. One clean table per category  (was notice_tables.py)
# =====================================================================

HOST = os.environ.get("PGHOST", "localhost"); PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres"); DBNAME = os.environ.get("PGDATABASE", "postgres")
SRC = "R5_insolvency"
S_CORP, S_PERS, S_OTHER = "R7_corporate_notices", "R8_personal_notices", "R9_other_notices"

HEAD = [("Notice ID", "bigint"), ("Notice category", "text"), ("Notice type", "text"), ("Publication date", "timestamp"),
        ("Edition", "text"), ("Notice code", "text"), ("Issue number", "text"), ("Page number", "text")]
TAIL = [("Gazette link", "text")]
NOT_WANTED = {"heading", "notice title"}      # page headings: repeat the notice type, not data
RESERVED = {c.lower() for c, _ in HEAD + TAIL} | {"notice text"}
DETAIL_KEYS = {"notice category", "notice type", "publication date", "edition", "notice id", "notice code", "issue number", "page number"}


def schema_of(top, path):
    if top == "Companies":
        return S_CORP
    return S_PERS if "personal insolvency" in (path or "").lower() else S_OTHER


def norm(c):
    """same key, different spelling (case, spaces, punctuation, '(s)') -> same column"""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(s\)", "", c.lower()))


def table_of(label):
    t = "".join(ch.lower() if ch.isalnum() else "_" for ch in label)
    t = "_".join(x for x in t.split("_") if x)
    return t[:55]


def clip(s, n=60):
    s = " ".join(str(s).split()).rstrip(":").strip()
    while len(s.encode("utf-8")) > n:
        s = s[:-1]
    return s


def ddl(schema, table, dyn, name=None):
    cols = [sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(t)) for c, t in HEAD]
    cols += [sql.SQL("{} text").format(sql.Identifier(c)) for c in dyn]
    cols += [sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(t)) for c, t in TAIL]
    return sql.SQL("CREATE TABLE {}.{} ({}, PRIMARY KEY ({}))").format(
        sql.Identifier(schema), sql.Identifier(name or table), sql.SQL(", ").join(cols), sql.Identifier("Notice ID"))


def existing_dyn(con, schema, table):
    cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table))]
    return None if not cols else [c for c in cols if c.lower() not in RESERVED]


def ensure_table(con, schema, table, need, comment=None):
    """Creates the table, or rebuilds it (columns in order: left box, keys, Text, Gazette link) when a new key appears."""
    have = existing_dyn(con, schema, table)
    if have is None:
        con.execute(ddl(schema, table, need))
        if comment:
            con.execute(sql.SQL("COMMENT ON TABLE {}.{} IS {}").format(sql.Identifier(schema), sql.Identifier(table), sql.Literal(comment)))
        return list(need)
    low = {c.lower() for c in have}
    new = [c for c in need if c.lower() not in low]
    if not new:
        return have
    allc = have + new
    old_cols = [c for c, _ in HEAD] + have + [c for c, _ in TAIL]
    with con.transaction():
        con.execute(ddl(schema, table, allc, name=table + "__new"))
        con.execute(sql.SQL("INSERT INTO {s}.{n} ({c}) SELECT {c} FROM {s}.{o}").format(
            s=sql.Identifier(schema), n=sql.Identifier(table + "__new"), o=sql.Identifier(table), c=sql.SQL(", ").join(map(sql.Identifier, old_cols))))
        con.execute(sql.SQL("DROP TABLE {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
        con.execute(sql.SQL("ALTER TABLE {}.{} RENAME TO {}").format(sql.Identifier(schema), sql.Identifier(table + "__new"), sql.Identifier(table)))
        if comment:
            con.execute(sql.SQL("COMMENT ON TABLE {}.{} IS {}").format(sql.Identifier(schema), sql.Identifier(table), sql.Literal(comment)))
        con.execute(sql.SQL("ALTER INDEX IF EXISTS {}.{} RENAME TO {}").format(sql.Identifier(schema), sql.Identifier(table + "__new_pkey"), sql.Identifier(table + "_pkey")))
    return allc


def build(con, days=None, setup_only=False):
    cats = con.execute(f'''SELECT category_code, label, tree_top, tree_path FROM "{SRC}".gazette_category
                           WHERE is_leaf AND tree_top IN ('Companies','People') ORDER BY tree_top DESC, tree_path''').fetchall()
    if not cats:
        sys.exit('The Gazette category tree is not saved yet. Run  python register_gazette.py load --tree  first.')
    for s in (S_CORP, S_PERS, S_OTHER):
        con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(s)))
    report = []
    for code, label, top, path in cats:
        schema, table = schema_of(top, path), table_of(label)
        rows = []
        if not setup_only:
            q = f'''SELECT n.notice_id, n.notice_category, n.notice_type, COALESCE(n.published_at, n.published_date::timestamp), n.edition, n.notice_code,
                           n.issue_number, n.page_number, n.body_text, n.url FROM "{SRC}".gazette_notice n
                    WHERE n.category_code = %s AND n.fields_parsed'''
            args = [code]
            if days:
                q += " AND n.published_date >= current_date - %s"
                args.append(days)
            rows = con.execute(q, args).fetchall()
        fields = {}
        if rows:
            for nid, sec, seq, k, v in con.execute(f'SELECT notice_id, section, seq, key, value FROM "{SRC}".gazette_notice_field WHERE notice_id = ANY(%s) ORDER BY notice_id, section, seq',
                                                   ([r[0] for r in rows],)):
                fields.setdefault(nid, []).append((sec, k, v))
        names, per, has_text = {}, {}, False       # norm(header) -> header as first seen ; notice -> {header: value}
        for nid, fl in fields.items():
            d = per.setdefault(nid, {})
            for sec, k, v in fl:
                v = (v or "").strip()
                if not v or (sec == "details" and k.lower() in DETAIL_KEYS):
                    continue
                if k == "Text":                    # lines without a key: all in ONE column, "Text"
                    d["Text"] = f"{d['Text']}\n{v}" if "Text" in d else v
                    has_text = True
                    continue
                h = clip(k)
                if not h or h.lower() in RESERVED or h.lower() in NOT_WANTED or h.lower() == "text":
                    continue                       # a left-box key repeated inside the text: the left box is the one kept
                h = names.setdefault(norm(h), h)   # same key spelt differently = the same column
                if h in d:
                    if v not in d[h].split(" | "):
                        d[h] += " | " + v          # key repeated in one notice: one column, values joined
                else:
                    d[h] = v
        dyn = list(names.values()) + (["Text"] if has_text else [])
        have = ensure_table(con, schema, table, dyn, comment=path)
        if rows:
            allc = [c for c, _ in HEAD] + have + [c for c, _ in TAIL]
            lows = {c.lower(): c for c in allc}
            ins = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
                sql.Identifier(schema), sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, allc)),
                sql.SQL(", ").join(sql.Placeholder() * len(allc)), sql.Identifier("Notice ID"),
                sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in allc[1:]))
            with con.transaction():
                for r in rows:
                    nid = r[0]
                    vals = dict(zip([c for c, _ in HEAD], r[:8]))
                    vals["Notice category"] = vals["Notice category"] or None
                    vals["Gazette link"] = r[9]
                    for h, v in per.get(nid, {}).items():
                        vals[lows.get(h.lower(), h)] = v
                    con.execute(ins, [vals.get(c) for c in allc])
        if rows:                                   # drop key columns that are empty in every row
            dcols = [c for c in have if c.lower() not in RESERVED]
            if dcols:
                cnt = con.execute(sql.SQL("SELECT {} FROM {}.{}").format(sql.SQL(", ").join(sql.SQL("count({})").format(sql.Identifier(c)) for c in dcols),
                                                                         sql.Identifier(schema), sql.Identifier(table))).fetchone()
                for c, k in zip(dcols, cnt):
                    if k == 0:
                        con.execute(sql.SQL("ALTER TABLE {}.{} DROP COLUMN {}").format(sql.Identifier(schema), sql.Identifier(table), sql.Identifier(c)))
                        have = [x for x in have if x != c]
        report.append((schema, table, len(rows), len(have)))
    return report


def tables_main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true", help="only create the empty tables")
    ap.add_argument("--days", type=int, help="only notices published in the last N days")
    ap.add_argument("--reset", action="store_true", help="drop the three schemas and rebuild")
    a = ap.parse_args()
    try:
        con = psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect: {e}\nCheck PostgreSQL is running and pgpass.conf is set up.")
    if a.reset:
        for s in (S_CORP, S_PERS, S_OTHER):
            con.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(s)))
    rep = build(con, a.days, a.setup)
    cur = None
    for schema, table, n, k in rep:
        if schema != cur:
            cur = schema
            print(f'\n"{schema}"')
        print(f"  {table:55} {n:5} notices, {k} key columns")
    print(f"\nDone. {sum(r[2] for r in rep)} notices written to {len(rep)} tables.")


# =====================================================================
#  4. Reset  (was insolvency_reset.py)
# =====================================================================

HOST = os.environ.get("PGHOST", "localhost"); PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres"); DBNAME = os.environ.get("PGDATABASE", "postgres")
SCHEMAS = ["R5_insolvency", "R7_corporate_notices", "R8_personal_notices", "R9_other_notices"]


def reset_main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backup", action="store_true", help='keep the old data: rename "R5_insolvency" instead of deleting it')
    ap.add_argument("--yes", action="store_true", help="do not ask")
    a = ap.parse_args()
    try:
        con = psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect: {e}")
    print("Will " + ("rename" if a.backup else "DELETE") + " these schemas and everything in them:")
    present = []
    for s in SCHEMAS:
        tabs = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE'", (s,))]
        if not tabs and not con.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name=%s", (s,)).fetchone():
            continue
        present.append(s)
        n = sum(con.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(s), sql.Identifier(t))).fetchone()[0] for t in tabs)
        print(f'  "{s}": {len(tabs)} tables, {n} rows')
    if not present:
        print("  (none exist yet)")
    elif not a.yes and input('Type YES to continue: ').strip() != "YES":
        sys.exit("Nothing changed.")
    stamp = dt.date.today().strftime("%Y%m%d")
    for s in present:
        if a.backup and s == "R5_insolvency":
            con.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(f"5_insolvency_old_{stamp}")))
            con.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(sql.Identifier(s), sql.Identifier(f"5_insolvency_old_{stamp}")))
        else:
            con.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(s)))
    setup_main()
    print("\nClean. Now run:  python register_gazette.py load --tree   then   --trees --days 30   then   --enrich-only --enrich-limit 3000   then   python register_gazette.py tables")


# =====================================================================
#  5. Database check  (was check_db.py)
# =====================================================================

HOST = os.environ.get("PGHOST", "localhost"); PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres"); DBNAME = os.environ.get("PGDATABASE", "postgres")


def check_norm(c):
    c = re.sub(r"\s*\((text|content)\)\s*$", "", c.lower())
    return re.sub(r"[^a-z0-9]", "", c)


def check_main():
    try:
        con = psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect: {e}")
    schemas = [r[0] for r in con.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name !~ '^(pg_|information_schema)' ORDER BY 1")]
    problems = 0
    for s in schemas:
        tabs = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY 1", (s,))]
        if not tabs:
            continue
        print(f'\n=== schema "{s}"  ({len(tabs)} tables)')
        for t in tabs:
            cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (s, t))]
            ident = lambda c: sql.Identifier(c)
            n = con.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(ident(s), ident(t))).fetchone()[0]
            msgs = []
            seen = {}
            for c in cols:
                seen.setdefault(check_norm(c), []).append(c)
            for k, v in seen.items():
                if len(v) > 1:
                    msgs.append("duplicate-looking columns: " + " / ".join(f'"{x}"' for x in v))
            if n and len(cols) <= 400:
                q = sql.SQL("SELECT " + ", ".join(["count({c%d}) , md5(coalesce(string_agg({c%d}::text, '|' ORDER BY ctid), ''))" % (i, i) for i in range(len(cols))]) + " FROM {s}.{t}")
                q = q.format(**{f"c{i}": ident(c) for i, c in enumerate(cols)}, s=ident(s), t=ident(t))
                r = con.execute(q).fetchone()
                by = {}
                for i, c in enumerate(cols):
                    cnt, h = r[2 * i], r[2 * i + 1]
                    if cnt == 0 and len(cols) > 1:
                        msgs.append(f'empty column: "{c}"')
                    elif cnt:
                        by.setdefault(h, []).append(c)
                for v in by.values():
                    if len(v) > 1:
                        msgs.append("identical values in: " + " = ".join(f'"{x}"' for x in v))
            print(f"  {t:50} {n:7} rows {len(cols):4} cols" + ("" if not msgs else "   <-- " + str(len(msgs)) + " problem(s)"))
            for m in msgs:
                print("        -", m)
            problems += len(msgs)
    if "R5_insolvency" in schemas:
        print('\n=== Gazette notice checks ("R5_insolvency")')
        q = lambda x: con.execute(x).fetchone()[0]
        S = '"R5_insolvency"'
        print("  notices saved:                          ", q(f"SELECT count(*) FROM {S}.gazette_notice"))
        print("  without a Gazette category code:        ", q(f"SELECT count(*) FROM {S}.gazette_notice WHERE category_code IS NULL"), "(old rows; they get a code when the loader sees them again)")
        print("  notice page not read yet:               ", q(f"SELECT count(*) FROM {S}.gazette_notice WHERE fields_parsed IS NOT TRUE"))
        print("  key:value rows saved:                   ", q(f"SELECT count(*) FROM {S}.gazette_notice_field"))
        tot = 0
        for s in ("R7_corporate_notices", "R8_personal_notices", "R9_other_notices"):
            if s in schemas:
                for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE'", (s,)).fetchall():
                    tot += con.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(s), sql.Identifier(t))).fetchone()[0]
        print("  rows in the clean notice tables:        ", tot, "(should equal notices with a category code whose page was read)")
        print("  ready for the clean tables:             ", q(f"SELECT count(*) FROM {S}.gazette_notice WHERE category_code IS NOT NULL AND fields_parsed"))
        print("  last loads:")
        for r in con.execute(f"SELECT started_at::timestamp(0), status, rows_fetched, rows_new, left(coalesce(message,''),80) FROM {S}.load_log ORDER BY run_id DESC LIMIT 5"):
            print("   ", *r)
    print(f"\nDone. {problems} problem(s) found." + ("" if problems else " Nothing to fix."))


# =====================================================================
#  Command line
# =====================================================================

COMMANDS = {"setup": setup_main, "load": load_main, "tables": tables_main, "reset": reset_main, "check": check_main}


def cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "update"
    rest = sys.argv[2:] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else sys.argv[1:]
    prog = sys.argv[0]
    if cmd in COMMANDS:
        sys.argv = [prog] + rest
        return COMMANDS[cmd]()
    if cmd != "update":
        sys.exit(f"Unknown command '{cmd}'. Commands: update (default), setup, load, tables, reset, check.")
    # update = load every Companies / People category, read pages, then the clean tables
    code = 0
    sys.argv = [prog, "--trees"] + (rest if "--days" in rest else ["--days", "7"] + rest)
    try:
        load_main()
    except SystemExit as e:
        code = e.code or 0
    if code:
        print(f"The Gazette load ended with an error (exit {code}); the clean tables are still refreshed from what was saved.")
    sys.argv = [prog]
    tables_main()
    return code


if __name__ == "__main__":
    sys.exit(cli())
