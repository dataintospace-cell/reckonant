"""register_findtender.py -- the register "Find a tender" (UK above-threshold public procurement: tenders, awards, contracts).  ONE file.

  python register_findtender.py                 pull the last 3 days (the very first run starts on 6 Nov 2026) and load what is new (daily_update.py runs this)
  python register_findtender.py --days 30       pull the last 30 days (a backfill)
  python register_findtender.py --from 2026-09-01 [--to 2026-09-30]     pull a date range
  python register_findtender.py show            print what is in the register
  python register_findtender.py reset           ONE-TIME: delete the whole "R13_find_a_tender" schema (asks you to type DROP)

THE SOURCE
  GET https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?updatedFrom={day}T00:00:00&updatedTo={next-day}T00:00:00&limit=100
  The Cabinet Office's OCDS API for Find a Tender. Up to 100 "releases" per call; the script follows the "next" link (or cursor) until the day
  is complete, and splits the window in two if a full page comes back with no next link. No key is needed. If the service answers HTTP 429
  (too many calls) or 403 the script waits and retries; it waits 1 second between calls.

WHAT IS STORED   (schema "R13_find_a_tender")  - same shape as Contracts Finder
    notice        one row per release: published time, stage (planning / tender / award), title, description, buyer, value, procurement method and
                  category, dates, CPV codes, region, SME / VCSE suitability, who it was awarded to, and a link to the notice (the "url" column, last)
    supplier      one row per supplier named in an AWARD: name, Companies House number when given, address / postcode, award value and date
    notice_raw    the complete release exactly as the API sent it (switch off with RAW_JSON = False)
    change_log    the procurement stage of each notice = the tracker sub-categories: Pipeline, Planning, Tender, Award, Contract, Termination,
                  Payments compliance, Dynamic market, Other.  The stage comes from the notice type (UK1-UK17, F01-F25, T01-T02 - see NOTICE_TYPES and
                  STAGE_OF_TYPE); when a release does not carry its notice type the stage is worked out from the release tags.
    v_notice      the notice table plus "Open opportunity" (Yes = Tender stage, still open for bids)
  Notice type: read from the release when it states one ("Notice type source" = Stated in notice). Old-regime releases (EU directive 2014/24 etc. in
  tender.legalBasis, often republished from Public Contracts Scotland) do not state it, so F01 / F02 / F03 (and the utilities, defence and concession
  equivalents) are inferred from the directive and the tags and marked "Inferred from legal basis and tags".
  Columns added for the Find a Tender filters: Notice type, Notice type name, Commercial tool (Framework / Dynamic market or dynamic purchasing system /
  Utilities dynamic market or qualification system), CPV, Region, Value, SME suitable, VCSE suitable.
    fetch_log     which days were pulled, how many releases, how many new

LINK TO THE COMPANY MASTER
  Suppliers (and buyers' awards) with a Companies House number are queued for the company master (register FTS-01); the others are matched by exact name.

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

SCHEMA = "R13_find_a_tender"
API = os.environ.get("FT_API", "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages")
STAGES = os.environ.get("FT_STAGES", "")                  # e.g. "planning,tender,award" ; empty = the API's default (all)
LIMIT = 100
FIRST_FROM = "2026-11-06"   # the very first run starts on this date (Friday 6 Nov 2026); later runs re-read the last DAYS_UPDATE days
DAYS_UPDATE = 3             # later runs: re-read the last N days (late edits and anything missed)
DELAY = float(os.environ.get("FT_DELAY", "1"))
BLOCK_WAIT = int(os.environ.get("FT_BLOCK_WAIT", "300"))   # seconds to wait after an HTTP 403
RAW_JSON = True
UA = "Reckonant/1.0 (personal monitoring of the public Find a Tender API)"
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
            if e.code in (403, 429) and attempt < 2:
                say(f"  blocked by the API (HTTP {e.code} - too many calls); waiting {BLOCK_WAIT // 60} minutes ...")
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
    u = f"{API}?updatedFrom={stamp(a)}&updatedTo={stamp(b)}&limit={LIMIT}"
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


NOTICE_TYPES = {
    "UK1": "Pipeline notice", "UK2": "Preliminary market engagement notice", "UK3": "Planned procurement notice", "UK4": "Tender notice",
    "UK5": "Transparency notice", "UK6": "Contract award notice", "UK7": "Contract details notice", "UK9": "Contract performance notice",
    "UK10": "Contract change notice", "UK11": "Contract termination notice", "UK12": "Procurement termination notice",
    "UK13": "Dynamic market intention notice", "UK14": "Dynamic market establishment notice", "UK15": "Dynamic market modification notice",
    "UK16": "Dynamic market cessation notice", "UK17": "Payments compliance notice",
    "F01": "Prior information notice", "F02": "Contract notice", "F03": "Contract award notice", "F04": "Periodic indicative notice - utilities",
    "F05": "Contract notice - utilities", "F06": "Contract award notice - utilities", "F07": "Qualification system - utilities",
    "F08": "Notice on a buyer profile", "F12": "Design contest notice", "F13": "Results of design contest",
    "F14": "Corrigendum notice for changes or additional information", "F15": "Voluntary ex ante transparency notice",
    "F16": "Prior information notice for contracts in the field of defence and security",
    "F17": "Contract notice for contracts in the field of defence and security",
    "F18": "Contract award notice for contracts in the field of defence and security", "F19": "Subcontract notice", "F20": "Modification notice",
    "F21": "Social and other specific services - public contracts", "F22": "Social and other specific services - utilities",
    "F23": "Social and other specific services - concessions", "F24": "Concession notice", "F25": "Concession award notice",
    "T01": "Prior information notice for public service contract", "T02": "Information notice for award of public service contract",
}
# Procurement stage (the tracker sub-category) for each notice type - the groups of the Find a Tender search page.
STAGE_OF_TYPE = {}
for _stage, _codes in {
    "Pipeline": "UK1", "Planning": "UK2 UK3 F01 F04 F08 F16 T01", "Tender": "UK4 UK5 F02 F05 F07 F12 F14 F15 F17 F19 F21 F22 F23 F24",
    "Award": "UK6 F03 F06 F13 F18 F25 T02", "Contract": "UK7 UK9 UK10 F20", "Termination": "UK11 UK12", "Payments compliance": "UK17",
    "Dynamic market": "UK13 UK14 UK15 UK16",
}.items():
    for _c in _codes.split():
        STAGE_OF_TYPE[_c] = _stage
STAGES_SHOWN = ["Pipeline", "Planning", "Tender", "Award", "Contract", "Termination", "Payments compliance", "Dynamic market", "Other"]
CODE_RX = re.compile(r"^(UK(?:1[0-7]|[1-9])|F(?:0[1-8]|1[2-9]|2[0-5])|T0[12])(?![0-9])")


def stage_from_tags(tags):
    """Fallback when the release does not say which notice type it came from."""
    t = {str(x).lower() for x in (tags or [])}
    if t & {"contracttermination", "tendercancellation", "planningcancellation", "awardcancellation"}:
        return "Termination"
    if t & {"contract", "contractupdate", "contractamendment", "implementation", "implementationupdate"}:
        return "Contract"
    if t & {"award", "awardupdate"}:
        return "Award"
    if t & {"tender", "tenderupdate", "tenderamendment"}:
        return "Tender"
    if t & {"planning", "planningupdate"}:
        return "Planning"
    return "Other"


def notice_type_of(r):
    """The notice type code (UK4, F02, T01 ...) found in the release, or None.  The API does not document the field, so every short text value in the
    release is checked: values under a key with 'notice' in its name first, then document titles / types, then any value that is exactly a code."""
    best = [9, None]

    def see(prio, v):
        if isinstance(v, str) and len(v) < 90:
            m = CODE_RX.match(v.strip().upper())
            if m and prio < best[0]:
                best[0], best[1] = prio, m.group(1)

    def walk(x, key, depth=0):
        if depth > 8:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, str(k).lower(), depth + 1)
        elif isinstance(x, list):
            for v in x[:200]:
                walk(v, key, depth + 1)
        elif isinstance(x, str):
            if "notice" in key and "url" not in key:
                see(0, x)
            elif key in ("documenttype", "title", "description", "format") and depth > 1:
                see(1, x)
            elif CODE_RX.fullmatch(x.strip().upper() if len(x) < 6 else ""):
                see(2, x)

    walk(r, "")
    return best[1]


# Old-regime (F-notice) releases do not state their notice type; it follows from the EU directive named in tender.legalBasis and the release tags.
F_BY_DIRECTIVE = {"32014L0024": {"planning": "F01", "tender": "F02", "award": "F03"},        # public contracts
                  "32014L0025": {"planning": "F04", "tender": "F05", "award": "F06"},        # utilities
                  "32009L0081": {"planning": "F16", "tender": "F17", "award": "F18"},        # defence and security
                  "32014L0023": {"tender": "F24", "award": "F25"}}                           # concessions


def infer_notice_type(r):
    lb = (r.get("tender") or {}).get("legalBasis") or {}
    directive = str(lb.get("id") or "").strip().upper()
    table = F_BY_DIRECTIVE.get(directive)
    if not table:
        return None
    t = {str(x).lower() for x in (r.get("tag") or [])}
    for key, hit in (("award", t & {"award", "awardupdate"}), ("tender", t & {"tender", "tenderupdate"}), ("planning", t & {"planning", "planningupdate"})):
        if hit:
            return table.get(key)
    return None


def commercial_tool_of(r, code):
    tender = r.get("tender") or {}
    out = []
    tech = tender.get("techniques") or {}
    blob = " ".join(f"{k}" for k, v in tech.items() if v) if isinstance(tech, dict) else str(tech)
    meth = f"{tender.get('procurementMethodDetails') or ''} {tender.get('procurementMethod') or ''} {blob}".lower()
    if "framework" in meth:
        out.append("Framework")
    if "dynamic" in meth or "purchasing system" in meth:
        out.append("Dynamic market or dynamic purchasing system")
    if code in ("UK13", "UK14", "UK15", "UK16") and "Dynamic market or dynamic purchasing system" not in out:
        out.append("Dynamic market or dynamic purchasing system")
    if code == "F07":
        out.append("Utilities dynamic market or qualification system")
    return "; ".join(out) or None


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
        m = re.search(r"\b(\d{6}-\d{4})\b", str(r.get("id", "")) + " " + str(r.get("ocid", "")))
        suffix = str(r.get("ocid", "")).rsplit("-", 1)[-1]
        ref = m.group(1) if m else suffix
        url = f"https://www.find-tender.service.gov.uk/Notice/{ref}" if ref else ""

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
    code = notice_type_of(r)
    source = "Stated in notice" if code else None
    if not code:
        code = infer_notice_type(r)
        source = "Inferred from legal basis and tags" if code else None
    stage = STAGE_OF_TYPE.get(code) or stage_from_tags(r.get("tag"))
    row = {
        "Release ID": str(r.get("id")), "OCID": r.get("ocid"), "Published": r.get("date"), "Stage": stage, "Notice type": code, "Notice type source": source,
        "Notice type name": NOTICE_TYPES.get(code), "Commercial tool": commercial_tool_of(r, code), "Tags": ", ".join(map(str, r.get("tag") or [])), "Title": tender.get("title") or "", "Description": (tender.get("description") or "")[:8000],
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
    return row["Stage"] if row["Stage"] in STAGES_SHOWN else "Other"


def detail_of(row, sups, known=False):
    bits = [f"{row['Notice type']}: {row['Notice type name']}" + (" (inferred)" if row.get("Notice type source", "").startswith("Inferred") else "") if row.get("Notice type") else None,
            "Update of an earlier notice" if known else None,
            f"Buyer: {row['Buyer']}" if row["Buyer"] else None]
    if row["Value"] is not None:
        bits.append(f"Value: {row['Currency'] or ''} {row['Value']:,.0f}".replace("  ", " "))
    if row["Stage"] in ("Award", "Contract"):
        w = "; ".join(f"{s['name']}" + (f" ({s['company_number']})" if s["company_number"] else "") for s in sups if s["name"])
        bits.append(f"Awarded to: {w}" if w else None)
        if row["Awarded value"]:
            bits.append(f"Awarded value: {row['Awarded value']:,.0f}")
    if row.get("Commercial tool"):
        bits.append(f"Commercial tool: {row['Commercial tool']}")
    if row["Tender end"]:
        bits.append(f"Closes: {row['Tender end']}")
    return "; ".join(b for b in bits if b)[:1000]


# ---------------------------------------------------------------- database
def connect():
    return psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)


NOTICE_COLS = [("Release ID", "text PRIMARY KEY"), ("OCID", "text"), ("Published", "timestamptz"), ("Stage", "text"), ("Notice type", "text"), ("Notice type source", "text"), ("Notice type name", "text"), ("Commercial tool", "text"), ("Tags", "text"), ("Title", "text"),
               ("Description", "text"), ("Status", "text"), ("Buyer", "text"), ("Buyer ID", "text"), ("Buyer town", "text"), ("Buyer postcode", "text"),
               ("Value", "numeric"), ("Value max", "numeric"), ("Currency", "text"), ("Procurement method", "text"), ("Procurement category", "text"),
               ("Tender start", "date"), ("Tender end", "date"), ("Contract start", "date"), ("Contract end", "date"), ("CPV", "text"), ("Region", "text"),
               ("SME suitable", "text"), ("VCSE suitable", "text"), ("Awarded to", "text"), ("Awarded value", "numeric"), ("url", "text")]


def setup(con):
    S = I(SCHEMA)
    con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(S))
    cols = sql.SQL(", ").join(sql.SQL("{} {}").format(I(n), sql.SQL(t)) for n, t in NOTICE_COLS)
    con.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {s}.notice ({c}, loaded_at timestamptz DEFAULT now())").format(s=S, c=cols))
    cur_cols = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'notice'", (SCHEMA,)).fetchall()}
    fresh = "Notice type source" not in cur_cols
    for n, t in NOTICE_COLS:                     # a table made by an older version gets the new columns
        if n not in cur_cols and "PRIMARY KEY" not in t:
            con.execute(sql.SQL("ALTER TABLE {s}.notice ADD COLUMN IF NOT EXISTS {c} {t}").format(s=S, c=I(n), t=sql.SQL(t)))
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
    con.execute(sql.SQL("COMMENT ON TABLE {s}.notice IS 'Government > Find a Tender notices'").format(s=S))
    if fresh:
        reclassify(con)
    make_views(con)


def reclassify(con):
    """Once, when the new columns first appear: re-read every stored release and give it its stage / notice type, and relabel its change_log row."""
    S = I(SCHEMA)
    n = 0
    for rid, raw in con.execute(sql.SQL("SELECT \"Release ID\", raw FROM {s}.notice_raw").format(s=S)).fetchall():
        try:
            row, _ = parse_release(raw)
        except Exception:
            continue
        con.execute(sql.SQL('UPDATE {s}.notice SET "Stage" = %s, "Notice type" = %s, "Notice type source" = %s, "Notice type name" = %s, "Commercial tool" = %s WHERE "Release ID" = %s').format(s=S),
                    (row["Stage"], row["Notice type"], row["Notice type source"], row["Notice type name"], row["Commercial tool"], rid))
        n += 1
    con.execute(sql.SQL('''UPDATE {s}.notice SET "Stage" = CASE lower("Stage") WHEN 'planning' THEN 'Planning' WHEN 'tender' THEN 'Tender' WHEN 'award' THEN 'Award' ELSE "Stage" END
                         WHERE "Stage" IN ('planning', 'tender', 'award')''').format(s=S))
    con.execute(sql.SQL('''UPDATE {s}.change_log c SET change_type = n."Stage" FROM {s}.notice n WHERE n."Release ID" = c."Release ID"''').format(s=S))
    if n:
        say(f"  re-classified {n} stored notices by stage / notice type")


def make_views(con):
    S = I(SCHEMA)
    con.execute(sql.SQL("DROP VIEW IF EXISTS {s}.v_notice").format(s=S))
    con.execute(sql.SQL('''CREATE VIEW {s}.v_notice AS SELECT n.*,
        CASE WHEN n."Stage" IN ('Tender') AND COALESCE(lower(n."Status"), 'active') NOT IN ('cancelled', 'unsuccessful', 'withdrawn', 'complete')
             AND (n."Tender end" IS NULL OR n."Tender end" >= current_date) THEN 'Yes' ELSE 'No' END AS "Open opportunity"
        FROM {s}.notice n''').format(s=S))


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
                    (day_of(row["Published"]), change_of(row, known), row["Release ID"], row["OCID"], row["Title"] or row["Buyer"], detail_of(row, sups, known)))
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
        say(f"Find a Tender: reading {d1} to {d2}")
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
            say("Nothing loaded yet - run  python register_findtender.py")
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
    say("Deleted. Run  python register_findtender.py  to load again.")
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
        say(f"Cannot reach Find a Tender: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
