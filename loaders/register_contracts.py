"""register_contracts.py -- the register "Contracts Finder" (UK government tenders and contract awards).  ONE file.

  python register_contracts.py                  pull the last 3 days (the very first run starts on 6 Nov 2026) and load what is new (daily_update.py runs this)
  python register_contracts.py --days 30        pull the last 30 days (a backfill; run it in pieces if you want a long history)
  python register_contracts.py --from 2026-09-01 [--to 2026-09-30]     pull a date range
  python register_contracts.py show             print what is in the register
  python register_contracts.py reset            ONE-TIME: delete the whole "R12_contracts_finder" schema (asks you to type DROP)

THE SOURCE
  GET https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?publishedFrom={day}T00:00:00&publishedTo={next-day}T00:00:00&limit=100
  The Cabinet Office's open API for Contracts Finder. One call returns up to 100 "releases" (an OCDS standard format); the script
  follows the pages until the day is complete. No key is needed. The API blocks a client that is too fast (HTTP 403 for 5 minutes),
  so the script waits 1 second between calls and, if it is blocked, waits 5 minutes and tries again (twice at most).

WHAT IS STORED   (schema "R12_contracts_finder")
    notice        one row per release (a notice, or a later change to it): published time, stage (planning / tender / award), title,
                  description, buyer, value, procurement method and category, tender and contract dates, CPV codes, region, whether it is suitable for
                  SMEs / VCSEs, who it was awarded to, and a link to the notice on Contracts Finder (the "url" column, last)
    supplier      one row per supplier named in an AWARD: name, the Companies House number when the notice gives one, address / postcode, award value and date
    notice_raw    the complete release exactly as the API sent it (so nothing is lost; switch off with RAW_JSON = False)
    change_log    what happened each day:
                      Future opportunity    a planned tender (a "planning" notice) not seen before
                      New opportunity       a tender open for bids, not seen before
                      Contract awarded      an award notice - the winner(s) and value are in the detail
                      Notice updated        a later release of a notice already held (dates extended, corrections, closing)
    fetch_log     which days were pulled, how many releases, how many new

LINK TO THE COMPANY MASTER
  Suppliers named in award notices often come with their Companies House number. Those numbers are queued for the company master
  (register_company_master.py reads the register CON-01), so each winner's directors and PSCs are fetched automatically.

Database password comes from pgpass.conf (as in your other scripts).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from urllib.parse import quote, urljoin

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

# ---------------------------------------------------------------- settings
HOST = os.environ.get("PGHOST", "localhost")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
DBNAME = os.environ.get("PGDATABASE", "postgres")

SCHEMA = "R12_contracts_finder"
API = os.environ.get("CF_API", "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search")
STAGES = os.environ.get("CF_STAGES", "")                  # e.g. "planning,tender,award" ; empty = the API's default (all)
LIMIT = 100
FIRST_FROM = "2026-11-06"   # the very first run starts on this date (Friday 6 Nov 2026); later runs re-read the last DAYS_UPDATE days
DAYS_UPDATE = 3             # later runs: re-read the last N days (late edits and anything missed)
DELAY = float(os.environ.get("CF_DELAY", "1"))
BLOCK_WAIT = int(os.environ.get("CF_BLOCK_WAIT", "300"))   # seconds to wait after an HTTP 403
RAW_JSON = True
UA = "Reckonant/1.0 (personal monitoring of the public Contracts Finder API)"
MIN_WINDOW = timedelta(minutes=1)

I = sql.Identifier


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- web
def get_json(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 403 and attempt < 2:
                say(f"  blocked by the API (HTTP 403 - too many calls); waiting {BLOCK_WAIT // 60} minutes ...")
                time.sleep(BLOCK_WAIT)
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(15)
                continue
            raise


def stamp(t):
    return quote(t.strftime("%Y-%m-%dT%H:%M:%S"), safe="")


def window_url(a, b):
    u = f"{API}?publishedFrom={stamp(a)}&publishedTo={stamp(b)}&limit={LIMIT}"
    return u + (f"&stages={quote(STAGES, safe=',')}" if STAGES else "")


def next_url(data, base):
    links = data.get("links") or {}
    nxt = links.get("next") if isinstance(links, dict) else None
    nxt = nxt or data.get("next") or data.get("nextPage")
    if isinstance(nxt, dict):
        nxt = nxt.get("href") or nxt.get("url")
    if nxt:
        return urljoin(API, nxt)
    cur = data.get("nextCursor") or data.get("cursor")
    return f"{base}&cursor={quote(str(cur), safe='=')}" if cur else None


def fetch_window(a, b, depth=0):
    """Every release published in [a, b). Follows the 'next' link; if a full page comes back with no next link, the window is split in two."""
    base = window_url(a, b)
    url, pages = base, 0
    while url:
        time.sleep(DELAY)
        data = get_json(url)
        rels = data.get("releases") or []
        yield from rels
        pages += 1
        url = next_url(data, base)
        if not url and len(rels) >= LIMIT and pages == 1:
            if b - a <= MIN_WINDOW:
                say(f"  warning: {len(rels)} releases in one minute ({a} - {b}); some may be missing")
                return
            mid = a + (b - a) / 2
            mid = mid.replace(microsecond=0)
            yield from fetch_window(a, mid, depth + 1)
            yield from fetch_window(mid, b, depth + 1)
            return
        if pages > 400:
            say("  stopped after 400 pages for one window - something is wrong")
            return


# ---------------------------------------------------------------- reading a release
def num(v):
    try:
        f = None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None
    return None if f is not None and (f != f or f in (float("inf"), float("-inf"))) else f   # Infinity / NaN -> empty


def clean_json(x):
    """The API sometimes sends the bare word Infinity (e.g. maximumLotsBidPerSupplier); PostgreSQL json refuses it, so it becomes the text "Infinity"."""
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return str(x).replace("inf", "Infinity").replace("nan", "NaN")
    if isinstance(x, dict):
        return {k: clean_json(v) for k, v in x.items()}
    if isinstance(x, list):
        return [clean_json(v) for v in x]
    return x


def norm_company_number(n):
    n = re.sub(r"[^0-9A-Za-z]", "", str(n or "")).upper()
    return n.zfill(8) if n.isdigit() else n


def ids_of(party):
    out = []
    ident = party.get("identifier") or {}
    for i in [ident] + list(party.get("additionalIdentifiers") or []):
        if isinstance(i, dict) and i.get("id"):
            out.append((i.get("scheme") or "", str(i["id"])))
    return out


def company_no(party):
    for scheme, i in ids_of(party):
        if scheme.upper() in ("GB-COH", "COH"):
            return norm_company_number(i)
    return None


def addr_of(party):
    a = party.get("address") or {}
    parts = [a.get("streetAddress"), a.get("locality"), a.get("region"), a.get("postalCode")]
    return ", ".join(p for p in parts if p), a.get("postalCode"), a.get("locality") or a.get("region")


def stage_of(tags):
    t = {str(x).lower() for x in (tags or [])}
    if t & {"award", "awardupdate", "awardcancellation", "contract", "contractupdate", "implementation"}:
        return "award"
    if t & {"tender", "tenderupdate", "tenderamendment", "tendercancellation"}:
        return "tender"
    if t & {"planning", "planningupdate"}:
        return "planning"
    return (list(t) or ["notice"])[0]


def uuid_in(s):
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s or "")
    return m.group(0) if m else None


def parse_release(r):
    """-> (notice row dict, [supplier row dicts])"""
    tender = r.get("tender") or {}
    parties = r.get("parties") or []
    byid = {p.get("id"): p for p in parties if isinstance(p, dict)}
    buyer_ref = r.get("buyer") or {}
    buyer_party = byid.get(buyer_ref.get("id")) or next((p for p in parties if "buyer" in (p.get("roles") or [])), {})
    b_addr, b_post, b_town = addr_of(buyer_party) if buyer_party else ("", None, None)

    cpv = []
    for c in [tender.get("classification")] + list(tender.get("additionalClassifications") or []):
        if isinstance(c, dict) and c.get("id"):
            cpv.append(f"{c['id']} {c.get('description') or ''}".strip())
    region = []
    for it in tender.get("items") or []:
        for c in [it.get("classification")] + list(it.get("additionalClassifications") or []):
            if isinstance(c, dict) and c.get("id"):
                cpv.append(f"{c['id']} {c.get('description') or ''}".strip())
        for d in it.get("deliveryAddresses") or []:
            if isinstance(d, dict):
                region += [d.get("region") or d.get("locality")]
    for d in tender.get("deliveryAddresses") or []:
        if isinstance(d, dict):
            region += [d.get("region") or d.get("locality")]
    suit = tender.get("suitability") or {}

    url = ""
    for d in tender.get("documents") or []:
        if "/Notice/" in (d.get("url") or ""):
            url = d["url"]
            break
    if not url:
        u = uuid_in(str(r.get("id", ""))) or uuid_in(str(r.get("ocid", "")))
        url = f"https://www.contractsfinder.service.gov.uk/Notice/{u}" if u else ""

    sup_rows, names, total = [], [], 0.0
    for aw in r.get("awards") or []:
        aval = num((aw.get("value") or {}).get("amount"))
        total += aval or 0
        cp = aw.get("contractPeriod") or {}
        for s in aw.get("suppliers") or []:
            p = {**byid.get(s.get("id"), {}), **{k: v for k, v in s.items() if v}}
            if not p.get("identifier") and byid.get(s.get("id"), {}).get("identifier"):
                p["identifier"] = byid[s["id"]]["identifier"]
            ad, post, _ = addr_of(p)
            ids = ids_of(p)
            names.append(p.get("name") or s.get("name") or "")
            sup_rows.append({"award_id": aw.get("id"), "name": p.get("name") or s.get("name"), "scheme": ids[0][0] if ids else None,
                             "identifier": ids[0][1] if ids else None, "company_number": company_no(p), "address": ad, "postcode": post,
                             "value": aval, "currency": (aw.get("value") or {}).get("currency"), "award_date": (aw.get("date") or "")[:10] or None,
                             "status": aw.get("status"), "start": (cp.get("startDate") or "")[:10] or None, "end": (cp.get("endDate") or "")[:10] or None})
    tp, cper = tender.get("tenderPeriod") or {}, tender.get("contractPeriod") or {}
    val = tender.get("value") or {}
    row = {
        "Release ID": str(r.get("id")), "OCID": r.get("ocid"), "Published": r.get("date"), "Stage": stage_of(r.get("tag")),
        "Tags": ", ".join(map(str, r.get("tag") or [])), "Title": tender.get("title") or "", "Description": (tender.get("description") or "")[:8000],
        "Status": tender.get("status"), "Buyer": buyer_ref.get("name") or buyer_party.get("name"), "Buyer ID": buyer_ref.get("id") or buyer_party.get("id"),
        "Buyer town": b_town, "Buyer postcode": b_post,
        "Value": num(val.get("amount")) if val.get("amount") is not None else num((tender.get("minValue") or {}).get("amount")),
        "Value max": num((tender.get("maxValue") or {}).get("amount")), "Currency": val.get("currency") or (tender.get("minValue") or {}).get("currency"),
        "Procurement method": tender.get("procurementMethodDetails") or tender.get("procurementMethod"),
        "Procurement category": tender.get("mainProcurementCategory"),
        "Tender start": (tp.get("startDate") or "")[:10] or None, "Tender end": (tp.get("endDate") or "")[:10] or None,
        "Contract start": (cper.get("startDate") or "")[:10] or None, "Contract end": (cper.get("endDate") or "")[:10] or None,
        "CPV": "; ".join(dict.fromkeys(cpv)) or None, "Region": "; ".join(dict.fromkeys(x for x in region if x)) or None,
        "SME suitable": None if suit.get("sme") is None else str(suit.get("sme")), "VCSE suitable": None if suit.get("vcse") is None else str(suit.get("vcse")),
        "Awarded to": "; ".join(dict.fromkeys(n for n in names if n)) or None, "Awarded value": total or None, "url": url,
    }
    return row, sup_rows


def day_of(published):
    return (published or "")[:10] or date.today().isoformat()


def change_of(row, known_ocid):
    stage = row["Stage"]
    if stage == "award":
        return "Contract awarded"
    if known_ocid:
        return "Notice updated"
    return {"planning": "Future opportunity", "tender": "New opportunity"}.get(stage, "New opportunity")


def detail_of(row, sups):
    bits = [f"Buyer: {row['Buyer']}" if row["Buyer"] else None]
    if row["Value"] is not None:
        bits.append(f"Value: {row['Currency'] or ''} {row['Value']:,.0f}".replace("  ", " "))
    if row["Stage"] == "award":
        w = "; ".join(f"{s['name']}" + (f" ({s['company_number']})" if s["company_number"] else "") for s in sups if s["name"])
        bits.append(f"Awarded to: {w}" if w else None)
        if row["Awarded value"]:
            bits.append(f"Awarded value: {row['Awarded value']:,.0f}")
    if row["Tender end"]:
        bits.append(f"Closes: {row['Tender end']}")
    return "; ".join(b for b in bits if b)[:1000]


# ---------------------------------------------------------------- database
def connect():
    return psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)


NOTICE_COLS = [("Release ID", "text PRIMARY KEY"), ("OCID", "text"), ("Published", "timestamptz"), ("Stage", "text"), ("Tags", "text"), ("Title", "text"),
               ("Description", "text"), ("Status", "text"), ("Buyer", "text"), ("Buyer ID", "text"), ("Buyer town", "text"), ("Buyer postcode", "text"),
               ("Value", "numeric"), ("Value max", "numeric"), ("Currency", "text"), ("Procurement method", "text"), ("Procurement category", "text"),
               ("Tender start", "date"), ("Tender end", "date"), ("Contract start", "date"), ("Contract end", "date"), ("CPV", "text"), ("Region", "text"),
               ("SME suitable", "text"), ("VCSE suitable", "text"), ("Awarded to", "text"), ("Awarded value", "numeric"), ("url", "text")]


def setup(con):
    S = I(SCHEMA)
    con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(S))
    cols = sql.SQL(", ").join(sql.SQL("{} {}").format(I(n), sql.SQL(t)) for n, t in NOTICE_COLS)
    con.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {s}.notice ({c}, loaded_at timestamptz DEFAULT now())").format(s=S, c=cols))
    con.execute(sql.SQL('CREATE INDEX IF NOT EXISTS notice_ocid ON {s}.notice ("OCID")').format(s=S))
    con.execute(sql.SQL('CREATE INDEX IF NOT EXISTS notice_pub ON {s}.notice ("Published")').format(s=S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.supplier (supplier_row_id bigserial PRIMARY KEY, "Release ID" text, "OCID" text, "Award ID" text, "Supplier name" text,
        "Identifier scheme" text, "Identifier" text, company_number text, "Address" text, "Postcode" text, "Award value" numeric, "Currency" text,
        "Award date" date, "Award status" text, "Contract start" date, "Contract end" date)''').format(s=S))
    con.execute(sql.SQL('CREATE INDEX IF NOT EXISTS supplier_rel ON {s}.supplier ("Release ID")').format(s=S))
    con.execute(sql.SQL("CREATE INDEX IF NOT EXISTS supplier_co ON {s}.supplier (company_number)").format(s=S))
    con.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {s}.notice_raw ("Release ID" text PRIMARY KEY, raw jsonb)').format(s=S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.change_log (change_id bigserial PRIMARY KEY, detected_date date NOT NULL, change_type text NOT NULL,
        "Release ID" text, "OCID" text, name text, detail text)''').format(s=S))
    con.execute(sql.SQL("CREATE INDEX IF NOT EXISTS change_log_day ON {s}.change_log (detected_date)").format(s=S))
    con.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {s}.fetch_log (day date PRIMARY KEY, releases int, new int, fetched_at timestamptz DEFAULT now())").format(s=S))
    con.execute(sql.SQL("COMMENT ON TABLE {s}.notice IS 'Government > Contracts Finder notices'").format(s=S))


def save_release(con, r):
    """True when the release is new."""
    S = I(SCHEMA)
    row, sups = parse_release(r)
    if not row["Release ID"] or row["Release ID"] == "None":
        return False
    if con.execute(sql.SQL('SELECT 1 FROM {s}.notice WHERE "Release ID" = %s').format(s=S), (row["Release ID"],)).fetchone():
        return False
    known = con.execute(sql.SQL('SELECT 1 FROM {s}.notice WHERE "OCID" = %s LIMIT 1').format(s=S), (row["OCID"],)).fetchone() is not None
    with con.transaction():
        con.execute(sql.SQL("INSERT INTO {s}.notice ({c}) VALUES ({v})").format(
            s=S, c=sql.SQL(", ").join(I(n) for n, _ in NOTICE_COLS), v=sql.SQL(", ").join(sql.Placeholder() for _ in NOTICE_COLS)),
            [row[n] for n, _ in NOTICE_COLS])
        for s in sups:
            con.execute(sql.SQL('''INSERT INTO {s}.supplier ("Release ID", "OCID", "Award ID", "Supplier name", "Identifier scheme", "Identifier", company_number, "Address",
                "Postcode", "Award value", "Currency", "Award date", "Award status", "Contract start", "Contract end") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''').format(s=S),
                        (row["Release ID"], row["OCID"], s["award_id"], s["name"], s["scheme"], s["identifier"], s["company_number"], s["address"], s["postcode"],
                         s["value"], s["currency"], s["award_date"], s["status"], s["start"], s["end"]))
        if RAW_JSON:
            con.execute(sql.SQL('INSERT INTO {s}.notice_raw VALUES (%s, %s)').format(s=S), (row["Release ID"], Jsonb(clean_json(r))))
        con.execute(sql.SQL('INSERT INTO {s}.change_log (detected_date, change_type, "Release ID", "OCID", name, detail) VALUES (%s,%s,%s,%s,%s,%s)').format(s=S),
                    (day_of(row["Published"]), change_of(row, known), row["Release ID"], row["OCID"], row["Title"] or row["Buyer"], detail_of(row, sups)))
    return True


# ---------------------------------------------------------------- commands
def update_main(args):
    today = date.today()
    with connect() as con:
        setup(con)
        have = con.execute(sql.SQL("SELECT count(*) FROM {s}.fetch_log").format(s=I(SCHEMA))).fetchone()[0]
        if "--from" in args:
            d1 = datetime.strptime(args[args.index("--from") + 1], "%Y-%m-%d").date()
            d2 = datetime.strptime(args[args.index("--to") + 1], "%Y-%m-%d").date() if "--to" in args else today
        else:
            if "--days" in args:
                d1 = today - timedelta(days=int(args[args.index("--days") + 1]) - 1)
            elif have:
                d1 = today - timedelta(days=DAYS_UPDATE - 1)
            else:
                d1 = datetime.strptime(FIRST_FROM, "%Y-%m-%d").date()
            d2 = today
        say(f"Contracts Finder: reading {d1} to {d2}")
        day, problems = d1, 0
        while day <= d2:
            a = datetime.combine(day, datetime.min.time())
            seen, new = set(), 0
            try:
                for r in fetch_window(a, a + timedelta(days=1)):
                    rid = str(r.get("id"))
                    if rid in seen:
                        continue
                    seen.add(rid)
                    new += 1 if save_release(con, r) else 0
                con.execute(sql.SQL("INSERT INTO {s}.fetch_log (day, releases, new) VALUES (%s,%s,%s) ON CONFLICT (day) DO UPDATE SET releases = EXCLUDED.releases, "
                                    "new = EXCLUDED.new, fetched_at = now()").format(s=I(SCHEMA)), (day, len(seen), new))
                say(f"  {day}: {len(seen)} releases, {new} new")
            except Exception as e:
                problems += 1
                say(f"  {day}: FAILED - {e}")
            day += timedelta(days=1)
        return 1 if problems else 0


def show_main(args):
    with connect() as con:
        S = I(SCHEMA)
        try:
            days = con.execute(sql.SQL("SELECT day, releases, new FROM {s}.fetch_log ORDER BY day DESC LIMIT 10").format(s=S)).fetchall()
            tot = con.execute(sql.SQL('SELECT count(*), count(DISTINCT "OCID"), min("Published")::date, max("Published")::date FROM {s}.notice').format(s=S)).fetchone()
            chg = con.execute(sql.SQL("SELECT change_type, count(*) FROM {s}.change_log GROUP BY 1 ORDER BY 1").format(s=S)).fetchall()
            sup = con.execute(sql.SQL("SELECT count(*), count(company_number), count(DISTINCT company_number) FROM {s}.supplier").format(s=S)).fetchone()
        except psycopg.Error:
            say("Nothing loaded yet - run  python register_contracts.py")
            return 0
        say("Last days read:")
        for d, n, nw in days:
            say(f"  {d}  {n} releases, {nw} new")
        say(f"Releases stored: {tot[0]} ({tot[1]} notices), published {tot[2]} to {tot[3]}")
        for t, n in chg:
            say(f"  {t}: {n}")
        say(f"Suppliers named in awards: {sup[0]}, with a Companies House number: {sup[1]} ({sup[2]} different companies)")
    return 0


def reset_main(args):
    if input(f'This deletes the whole schema "{SCHEMA}" (every notice and supplier). Type DROP to continue: ').strip() != "DROP":
        say("Nothing deleted.")
        return 0
    with connect() as con:
        con.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(I(SCHEMA)))
    say("Deleted. Run  python register_contracts.py  to load again.")
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
        say(f"Cannot reach Contracts Finder: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
