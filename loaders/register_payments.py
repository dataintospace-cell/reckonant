"""register_payments.py -- the register "Payments Transparency Register" (payments made by public bodies to suppliers on contracts since 31 March 2026).  ONE file.

  python register_payments.py                  read the last 3 days of PUBLISHED payments (the very first run starts on 1 Oct 2026) and load what is new
  python register_payments.py --days 30        read the last 30 days (a backfill)
  python register_payments.py --from 2026-10-01 [--to 2026-10-31]     read a date range
  python register_payments.py --debug          also save the first list page and the first detail page next to this script (ptr_debug_*.html)
  python register_payments.py --file page.html parse a saved page and print what was found (no database)
  python register_payments.py show             print what is in the register
  python register_payments.py reset            ONE-TIME: delete the whole "R14_payments_transparency" schema (asks you to type DROP)

THE SOURCE
  https://payments-transparency-register.supplier-information.find-tender.service.gov.uk/?publishedDateFromDay=..&publishedDateToDay=..&page=N&pageSize=P
  It is a web page, not an API: every payment is a "card" (title, contracting authority, contract, supplier, payment value excl. VAT, date paid, date
  published, OCID) linked to a detail page (/Home/Details/{id}) that also names the supplier's identifier (Companies House number, PPON or charity number).
  The script reads one published day at a time, page after page, then opens the detail page of each NEW payment (1 second apart).

WHAT IS STORED   (schema "R14_payments_transparency")
    payment       one row per payment: Payment ID, Title, Contracting authority, Contract, Supplier, Payment value, Payment date, Published date, OCID,
                  Supplier identifiers (as shown), company_number (when a Companies House number is shown), "Detail text" (the whole detail page as text), url
    change_log    "Payment published" (the tracker sub-category)
    fetch_log     which days were read, how many payments, how many new

LINK TO THE COMPANY MASTER
  Suppliers with a Companies House number are queued for the company master (register PTR-01); the others are matched by exact name.

The page layout was read from a description, not from the raw HTML.  If the script says the layout was not recognised, run it with --debug and send me
the two saved files.   Database password comes from pgpass.conf (as in your other scripts).
"""
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import psycopg
from psycopg import sql

HOST = os.environ.get("PGHOST", "localhost")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
DBNAME = os.environ.get("PGDATABASE", "postgres")

SCHEMA = "R14_payments_transparency"
BASE = os.environ.get("PTR_BASE", "https://payments-transparency-register.supplier-information.find-tender.service.gov.uk/")
FIRST_FROM = "2026-10-01"      # the very first run starts here; later runs re-read the last DAYS_UPDATE days
DAYS_UPDATE = 3
PAGE_SIZE = int(os.environ.get("PTR_PAGE_SIZE", "50"))      # falls back to 10 if the site refuses a bigger page
DELAY = float(os.environ.get("PTR_DELAY", "1"))
BLOCK_WAIT = int(os.environ.get("PTR_BLOCK_WAIT", "300"))
MAX_PAGES = 400
UA = "Reckonant/1.0 (personal monitoring of the public Payments Transparency Register)"
HERE = Path(__file__).resolve().parent
DEBUG = "--debug" in sys.argv
I = sql.Identifier


def say(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- web
def get_html(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:
                say(f"  blocked by the site (HTTP {e.code}); waiting {BLOCK_WAIT // 60} minutes ...")
                time.sleep(BLOCK_WAIT)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(15)
                continue
            raise


def list_url(day, page, size):
    d, m, y = f"{day.day:02d}", f"{day.month:02d}", str(day.year)
    return (f"{BASE}?paymentValueFrom=&paymentValueTo=&paymentDateFromDay=&paymentDateFromMonth=&paymentDateFromYear="
            f"&paymentDateToDay=&paymentDateToMonth=&paymentDateToYear="
            f"&publishedDateFromDay={d}&publishedDateFromMonth={m}&publishedDateFromYear={y}"
            f"&publishedDateToDay={d}&publishedDateToMonth={m}&publishedDateToYear={y}&page={page}&pageSize={size}&sortField=&sortDir=")


# ---------------------------------------------------------------- reading the page
class Tokens(HTMLParser):
    """Turns a page into a flat list: ("A", href) for each link and ("T", text) for each piece of text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag == "a":
            self.out.append(("A", dict(attrs).get("href") or ""))
        elif tag in ("br", "p", "div", "li", "dt", "dd", "tr", "td", "th", "h1", "h2", "h3", "h4"):
            self.out.append(("B", ""))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        t = re.sub(r"\s+", " ", data).strip()
        if t and not self.skip:
            self.out.append(("T", t))


def tokens_of(html):
    p = Tokens()
    p.feed(html)
    return [x for x in p.out if x[0] != "B"]


LABELS = {"contracting authority": "authority", "contract": "contract", "supplier": "supplier",
          "payment value (excluding vat)": "value", "payment value": "value", "date payment was made": "paid",
          "published payment date": "published", "ocid": "ocid"}
DETAIL_RX = re.compile(r"/Home/Details/([^?#/]+)", re.I)


def money(t):
    t = re.sub(r"[^0-9.]", "", t or "")
    try:
        return float(t) if t else None
    except ValueError:
        return None


def day_of(t):
    t = (t or "").strip()
    for f in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            pass
    return None


def parse_list(html):
    """-> (total results or None, [payment dicts])"""
    toks = tokens_of(html)
    total = None
    for k, t in toks:
        if k == "T":
            m = re.fullmatch(r"([\d,]+)\s+results?(?: found)?", t, re.I)
            if m:
                total = int(m.group(1).replace(",", ""))
                break
    cards, cur = [], None
    for k, t in toks:
        if k == "A":
            m = DETAIL_RX.search(t)
            if m:
                if cur is None or cur["id"] != m.group(1):
                    cur = {"id": m.group(1), "href": t, "tok": []}
                    cards.append(cur)
                continue
        if k == "T" and cur is not None:
            cur["tok"].append(t)
    out = []
    for c in cards:
        tk = c["tok"]
        low = [x.rstrip(":").strip().lower() for x in tk]
        rec = {"id": c["id"], "url": urljoin(BASE, c["href"].split("?")[0]), "title": tk[0] if tk else None, "authority": None, "contract": None,
               "supplier": None, "value": None, "paid": None, "published": None, "ocid": None, "text": " | ".join(tk)}
        for i, lab in enumerate(low):
            key = LABELS.get(lab)
            if key and rec[key] is None and i + 1 < len(tk):
                rec[key] = tk[i + 1]
            elif ":" in tk[i] and key is None:                       # "Supplier: BOXXE LIMITED" in one piece of text
                a, _, b = tk[i].partition(":")
                key = LABELS.get(a.strip().lower())
                if key and rec[key] is None and b.strip():
                    rec[key] = b.strip()
        if rec["authority"] is None and len(tk) > 1 and tk[1].rstrip(":").lower() not in LABELS:
            rec["authority"] = tk[1]
        out.append(rec)
    return total, out


COMPANY_RX = re.compile(r"(?:companies house|company (?:registration )?number|crn)(?:\s*(?:registration |reg\.? )?(?:number|no\.?|id))?[^A-Za-z0-9]{0,25}((?:SC|NI|OC|SO|NC|R0|FC|LP|GE|IP|SP|NL|NP|RC|SL|SF|NF|AC|CE|CS|IC|PC|RS|SE|ZC)?\d{6,8})\b", re.I)
PPON_RX = re.compile(r"\b([A-Z]{4}-\d{4}-[A-Z]{4})\b")
CHARITY_RX = re.compile(r"charity[^0-9]{0,25}(\d{5,8})", re.I)


def parse_detail(html):
    text = " | ".join(t for k, t in tokens_of(html) if k == "T")
    ids, cn = [], None
    m = COMPANY_RX.search(text)
    if m:
        cn = m.group(1).upper()
        cn = cn.zfill(8) if cn.isdigit() else cn
        ids.append("Companies House " + cn)
    for x in PPON_RX.findall(text):
        ids.append("PPON " + x)
    m = CHARITY_RX.search(text)
    if m:
        ids.append("Charity " + m.group(1))
    return text[:20000], "; ".join(dict.fromkeys(ids)) or None, cn


# ---------------------------------------------------------------- database
def connect():
    return psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True)


def setup(con):
    S = I(SCHEMA)
    con.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.payment ("Payment ID" text PRIMARY KEY, "Title" text, "Contracting authority" text, "Contract" text,
        "Supplier" text, "Payment value" numeric, "Payment date" date, "Published date" date, "OCID" text, "Supplier identifiers" text, company_number text,
        "Detail text" text, url text, loaded_at timestamptz DEFAULT now())''').format(s=S))
    con.execute(sql.SQL('CREATE INDEX IF NOT EXISTS payment_pub ON {s}.payment ("Published date")').format(s=S))
    con.execute(sql.SQL("CREATE INDEX IF NOT EXISTS payment_co ON {s}.payment (company_number)").format(s=S))
    con.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {s}.change_log (change_id bigserial PRIMARY KEY, detected_date date NOT NULL, change_type text NOT NULL,
        "Payment ID" text, name text, detail text)''').format(s=S))
    con.execute(sql.SQL("CREATE INDEX IF NOT EXISTS change_log_day ON {s}.change_log (detected_date)").format(s=S))
    con.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {s}.fetch_log (day date PRIMARY KEY, payments int, new int, fetched_at timestamptz DEFAULT now())").format(s=S))
    con.execute(sql.SQL("COMMENT ON TABLE {s}.payment IS 'Government > Payments Transparency Register'").format(s=S))


def save_payment(con, rec, detail_html):
    S = I(SCHEMA)
    if con.execute(sql.SQL('SELECT 1 FROM {s}.payment WHERE "Payment ID" = %s').format(s=S), (rec["id"],)).fetchone():
        return False
    dtext, idents, cn = parse_detail(detail_html) if detail_html else (None, None, None)
    pub, paid, val = day_of(rec["published"]), day_of(rec["paid"]), money(rec["value"])
    with con.transaction():
        con.execute(sql.SQL('''INSERT INTO {s}.payment ("Payment ID", "Title", "Contracting authority", "Contract", "Supplier", "Payment value", "Payment date",
            "Published date", "OCID", "Supplier identifiers", company_number, "Detail text", url) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''').format(s=S),
                    (rec["id"], rec["title"], rec["authority"], rec["contract"], rec["supplier"], val, paid, pub, rec["ocid"], idents, cn, dtext, rec["url"]))
        bits = [f"Paid by: {rec['authority']}" if rec["authority"] else None,
                f"Payment: GBP {val:,.2f} excl. VAT" if val is not None else None,
                f"Paid on: {paid}" if paid else None, f"Contract: {rec['contract']}" if rec["contract"] else None,
                f"OCID: {rec['ocid']}" if rec["ocid"] else None]
        con.execute(sql.SQL('INSERT INTO {s}.change_log (detected_date, change_type, "Payment ID", name, detail) VALUES (%s,%s,%s,%s,%s)').format(s=S),
                    (pub or date.today(), "Payment published", rec["id"], rec["supplier"] or rec["title"], "; ".join(b for b in bits if b)[:1000]))
    return True


def read_day(day):
    """All payment cards published on `day` (page after page)."""
    size, seen, out, total = PAGE_SIZE, set(), [], None
    for page in range(1, MAX_PAGES + 1):
        try:
            html = get_html(list_url(day, page, size))
        except urllib.error.HTTPError as e:
            if e.code == 400 and size != 10:
                size, page = 10, 1
                seen.clear(); out.clear()
                html = get_html(list_url(day, 1, 10))
            else:
                raise
        if DEBUG and page == 1 and not (HERE / "ptr_debug_list.html").exists():
            (HERE / "ptr_debug_list.html").write_text(html, encoding="utf-8")
        t, cards = parse_list(html)
        total = t if t is not None else total
        fresh = [c for c in cards if c["id"] not in seen]
        if not fresh:
            if page == 1 and total:
                say(f"  {day}: the site says {total} results but no payment cards were recognised - the page layout is not what I expected."
                    f"  Run  python register_payments.py --debug  and send me ptr_debug_list.html and ptr_debug_detail.html")
            break
        for c in fresh:
            seen.add(c["id"]); out.append(c)
        if total is not None and len(out) >= total:
            break
        time.sleep(DELAY)
    return out, total


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
        say(f"Payments Transparency Register: reading payments published {d1} to {d2}")
        day, problems, first_detail = d1, 0, True
        while day <= d2:
            try:
                cards, total = read_day(day)
                new = 0
                for c in cards:
                    if con.execute(sql.SQL('SELECT 1 FROM {s}.payment WHERE "Payment ID" = %s').format(s=I(SCHEMA)), (c["id"],)).fetchone():
                        continue
                    time.sleep(DELAY)
                    try:
                        dh = get_html(c["url"])
                    except Exception as e:
                        say(f"    detail page failed for {c['id']}: {e} (saved without it)")
                        dh = None
                    if DEBUG and dh and first_detail:
                        (HERE / "ptr_debug_detail.html").write_text(dh, encoding="utf-8")
                        first_detail = False
                    new += 1 if save_payment(con, c, dh) else 0
                con.execute(sql.SQL("INSERT INTO {s}.fetch_log (day, payments, new) VALUES (%s,%s,%s) ON CONFLICT (day) DO UPDATE SET payments = EXCLUDED.payments, "
                                    "new = EXCLUDED.new, fetched_at = now()").format(s=I(SCHEMA)), (day, len(cards), new))
                say(f"  {day}: {len(cards)} payments" + (f" (site says {total})" if total is not None and total != len(cards) else "") + f", {new} new")
            except Exception as e:
                problems += 1
                say(f"  {day}: FAILED - {e}")
            day += timedelta(days=1)
    return 1 if problems else 0


def file_main(args):
    html = Path(args[0]).read_text(encoding="utf-8", errors="replace")
    total, cards = parse_list(html)
    if cards:
        say(f"Results line: {total}; payment cards found: {len(cards)}")
        for c in cards[:5]:
            say({k: v for k, v in c.items() if k != "text"})
    else:
        dtext, idents, cn = parse_detail(html)
        say("No payment cards - read as a detail page:")
        say({"identifiers": idents, "company_number": cn, "text": dtext[:600]})
    return 0


def show_main(args):
    with connect() as con:
        S = I(SCHEMA)
        try:
            days = con.execute(sql.SQL("SELECT day, payments, new FROM {s}.fetch_log ORDER BY day DESC LIMIT 10").format(s=S)).fetchall()
            tot = con.execute(sql.SQL('SELECT count(*), count(company_number), count(DISTINCT company_number), sum("Payment value"), min("Published date"), max("Published date") FROM {s}.payment').format(s=S)).fetchone()
        except psycopg.Error:
            say("Nothing loaded yet - run  python register_payments.py")
            return 0
        say("Last days read:")
        for d, n, nw in days:
            say(f"  {d}  {n} payments, {nw} new")
        say(f"Payments stored: {tot[0]} (total GBP {tot[3] or 0:,.2f}), published {tot[4]} to {tot[5]}")
        say(f"With a Companies House number: {tot[1]} ({tot[2]} different companies)")
    return 0


def reset_main(args):
    if input(f'This deletes the whole schema "{SCHEMA}" (every payment). Type DROP to continue: ').strip() != "DROP":
        say("Nothing deleted.")
        return 0
    with connect() as con:
        con.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(I(SCHEMA)))
    say("Deleted. Run  python register_payments.py  to load again.")
    return 0


def cli():
    args = sys.argv[1:]
    if "--file" in args:
        return file_main([args[args.index("--file") + 1]])
    cmd = args[0] if args and not args[0].startswith("-") else "update"
    rest = args[1:] if args and not args[0].startswith("-") else args
    fn = {"update": update_main, "show": show_main, "reset": reset_main}.get(cmd)
    if not fn:
        say("Commands: update (default), show, reset, --file page.html")
        return 2
    try:
        return fn(rest) or 0
    except psycopg.OperationalError as e:
        say(f"Cannot reach the database: {e}")
        return 1
    except urllib.error.URLError as e:
        say(f"Cannot reach the Payments Transparency Register: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(cli())
