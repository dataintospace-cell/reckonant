"""
alert_tracker_ui.py - one local page that tracks EVERY register, every day (your "Daily tracker (Detail)").

    python alert_tracker_ui.py          then open http://127.0.0.1:8002

THREE TABS
    Users                  type a company name and/or a company registration number (either one is enough) and see every
                           entry for that company in ALL registers, plus its company-master profile.
    All registers          the list of registers: category, frequency, last entry, entries in 7 days, status.
    Daily tracker (Details) the day-by-day tracker described below.

For each register it shows its sub categories with the number of new entries on each of the last 7 days,
the CHANGE on the chosen date (compared with the day before) and the 7-day total. Click a date heading to
choose the date; click any number to see the actual records behind it. Registers and sub categories with
no data still show (zeros), so a register that has stopped updating is easy to spot.

HOW TO ADD A REGISTER (one by one)
    1. Load its data into PostgreSQL (like register_cqc.py and register_gazette.py do; add a step to daily_update.py).
    2. Add ONE block to the REGISTERS list below:  sql_register(...)  - copy the INS-01 block and change
       the code, name, category, frequency, the SQL and the list of sub categories. Also give it a  search=
       SQL (see INS-02) so the Users tab can find that register's entries by company name / number.
    2a. SHEET: add the register to REGISTERS in make_register_sheet.py (one dict, copy HOS-01) - the spreadsheet is rebuilt by daily_update and the
       script reports MISSING if you forget.
    2b. COMPANIES: make sure the register's companies reach the company master (register_company_master.py): a register with company numbers
       gets a line in SOURCES, a register with only names a reader in NAME_SOURCES (HMD-01, HOS-01, CON-01, FTS-01 are examples), and its schema is added
       to COVERAGE there. `python register_company_master.py --coverage` warns about any register not wired up (and links it automatically if
       it has an obvious company-number / company-name column). Then show the company beside each entry: use  + CO_COLS +  in the drill SELECT
       and  + co_join("CODE", 'the entry id') +  after the FROM (copy HMD-01), and the same join in search= so a company number finds the entry.
    3. Save, press F5 in the browser. A register whose table is missing shows "table not found" instead of
       breaking the page, so you can add the block first and load the data afterwards.

Your password comes from pgpass.conf. The page listens on 127.0.0.1 only (this computer).
"""
import json
import re
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import threading
import time

import psycopg
from psycopg import sql

# =====================================================================
#  Settings
# =====================================================================
DB = dict(host="localhost", port=5432, dbname="postgres", user="postgres", connect_timeout=10)   # password: pgpass.conf
HOST, PORT = "127.0.0.1", 8002
HISTORY_DAYS = 400          # how far back "last entry" and the list of sub categories look
DRILL_LIMIT = 500           # most records shown when you click a number
CACHE_SECONDS = 20          # the page re-reads the database at most this often
SEARCH_LIMIT = 200          # most entries taken from ONE register by a Users-tab search

PALETTE = ["#c0392b", "#d35400", "#b9770e", "#7d6608", "#2e86c1", "#6c3483", "#5d6d7e", "#1e8449",
           "#cb4335", "#2874a6", "#a04000", "#117a65", "#7b241c", "#4a235a", "#1b4f72", "#4d5656", "#935116"]

FREQ_LETTER = {"Daily": "D", "Weekly": "W", "Monthly": "M", "Quarterly": "Q", "Half-yearly": "H", "Annually": "A", "List based": "L"}


# =====================================================================
#  Register helpers (you normally do not edit this part)
# =====================================================================
def run_sql(con, q, params):
    """Runs q; if it fails because the 'type_label' column of the notice view does not exist yet, retries with the older group_name."""
    try:
        return con.execute(q, params)
    except psycopg.Error:
        if "type_label" not in q:
            raise
        return con.execute(q.replace("type_label", "group_name"), params)


def sql_register(code, name, category, frequency, sql, subs=None, drill=None, drill_cols=None, sub_colors=None, search=None, search_fallback=None, drill_fallback=None):
    """A register read with SQL.
    sql        returns 3 columns: day (date), sub category (text), count (int). Use %(d1)s and %(d2)s for the date range.
    subs       list of ALL sub categories in the order to show (zeros are shown for those with no entries).
    drill      SQL for the records behind a number. Use %(day)s and %(sub)s ('' = every sub category). Return any columns;
               a column called url becomes a link.
    sub_colors optional {"sub category": "#hex"}.
    search_fallback / drill_fallback   plain versions used when the first SQL fails (e.g. the company master tables do not exist yet).
    search     SQL for the Users tab. Return 6 columns IN THIS ORDER: date, type, name, company_number, detail, url.
               Use %(name)s (a LIKE pattern, '' = not typed) and %(num)s (company number, '' = not typed), e.g.
                 WHERE (%(name)s = '' OR party_name ILIKE %(name)s) AND (%(num)s = '' OR company_number = %(num)s)
    """
    def fetch(con, d1, d2):
        return run_sql(con, sql, {"d1": d1, "d2": d2}).fetchall()

    def drill_fn(con, day, sub):
        if not drill:
            return [], []
        try:
            cur = run_sql(con, drill + f" LIMIT {DRILL_LIMIT}", {"day": day, "sub": sub or ""})
        except psycopg.Error:
            if not drill_fallback:
                raise
            cur = run_sql(con, drill_fallback + f" LIMIT {DRILL_LIMIT}", {"day": day, "sub": sub or ""})    # company master not built yet
        return [c.name for c in cur.description], cur.fetchall()
    def search_fn(con, name, num):
        if not search:
            return []
        for q in (search, search_fallback):
            if not q:
                continue
            try:
                return run_sql(con, f"SELECT * FROM ({q}) s ORDER BY 1 DESC NULLS LAST LIMIT {SEARCH_LIMIT}", {"name": name, "num": num}).fetchall()
            except psycopg.Error:
                if q is search_fallback or not search_fallback:
                    raise                           # otherwise: company master not built yet, use the plain search
        return []
    return dict(code=code, name=name, category=category, frequency=frequency, fetch=fetch, drill=drill_fn,
                subs=subs or [], sub_colors=sub_colors or {}, search=search_fn)


MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"])}
CQC_NAME = re.compile(r"^(\d{1,2})_([a-z]+)_(\d{4})_(.+)$")


def cqc_name_norm(text):
    """Same normalisation as register_company_master.name_norm (so CQC names can be looked up in name_match)."""
    t = str(text or "").upper().replace("&", " AND ")
    t = re.sub(r"[^A-Z0-9 ]", " ", t)
    t = re.sub(r"\bLIMITED\b", "LTD", t)
    t = re.sub(r"^\s*THE\s+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def add_numbers(con, rows):
    """CQC has no company numbers, so its rows came out without one and showed as a separate 'company'. Fill the number in from the company master:
    the result of the name search at Companies House (name_match), else a company in the master with exactly that name."""
    need = {cqc_name_norm(r[2]): r[2] for r in rows if r[2] and not r[3]}
    if not need:
        return rows
    found = {}
    try:
        for n, cn in con.execute("SELECT name_norm, company_number FROM \"6_company_master\".name_match WHERE status = 'matched' "
                                 "AND company_number IS NOT NULL AND name_norm = ANY(%s) ORDER BY (kind = 'provider') DESC", (list(need),)).fetchall():
            found.setdefault(n, cn)
        for cn, cname in con.execute("SELECT company_number, company_name FROM \"6_company_master\".company WHERE upper(company_name) = ANY(%s)",
                                     ([v.upper() for v in need.values()],)).fetchall():
            found.setdefault(cqc_name_norm(cname), cn)
    except psycopg.Error:
        return rows                     # company master not built yet
    return [(d, t, nm, cn or found.get(cqc_name_norm(nm)), det, u) for d, t, nm, cn, det, u in rows]


def cqc_diff_register(code, name, schema, frequency="Monthly"):
    """A CQC schema, read from the diff tables made by register_cqc.py (public.cqc_diff_summary).
    Day = the date of the CURRENT table (e.g. 01_october_2026_locations -> 1 Oct 2026).
    Sub category = '<table>: Added / Changed / Removed', the count is the number of rows."""
    def parse(cur_table):
        m = CQC_NAME.match(cur_table or "")
        if not m or m.group(2) not in MONTHS:
            return None, None
        return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))), m.group(4)

    def fetch(con, d1, d2):
        out = []
        for cur_table, a, c, r in con.execute(
                "SELECT current_table, rows_added, rows_changed, rows_removed FROM public.cqc_diff_summary WHERE schema_name=%s", (schema,)):
            d, tname = parse(cur_table)
            if d and d1 <= d <= d2:
                out += [(d, f"{tname}: Added", a or 0), (d, f"{tname}: Changed", c or 0), (d, f"{tname}: Removed", r or 0)]
        return out

    def drill_fn(con, day, sub):
        """One line per changed record: the table, ADDED / CHANGED / REMOVED, the ID, the name and, for changed records,
        every changed column as  column: previous -> current."""
        rows_out = []
        for diff_table, cur_table in con.execute(
                "SELECT diff_table, current_table FROM public.cqc_diff_summary WHERE schema_name=%s", (schema,)).fetchall():
            d, tname = parse(cur_table)
            if d != day or (sub and not sub.startswith(tname + ": ")):
                continue
            kind = sub.split(": ")[-1].upper() if sub else None
            cur = con.execute(f'SELECT * FROM "{schema}"."{diff_table}"' + (" WHERE change_type = %s" if kind else "")
                              + f" LIMIT {DRILL_LIMIT}", (kind,) if kind else ())
            cols = [c.name for c in cur.description]
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                nm = next((r[c] for c in cols if re.match(r"^(location_name|provider_name|name)_curr$", c) and r[c]), None) \
                    or next((r[c] for c in cols if re.match(r"^(location_name|provider_name|name)_prev$", c) and r[c]), "")
                ch = ""
                if r.get("change_type") == "CHANGED" and r.get("changed_columns"):
                    ch = "; ".join(f"{c}: {r.get(c + '_prev') if r.get(c + '_prev') not in (None, '') else '(blank)'} -> "
                                   f"{r.get(c + '_curr') if r.get(c + '_curr') not in (None, '') else '(blank)'}"
                                   for c in [x.strip() for x in str(r["changed_columns"]).split(",")] if c + "_prev" in r)
                rows_out.append((tname, r.get("change_type"), r.get("key_value"), nm, ch))
            if len(rows_out) >= DRILL_LIMIT:
                break
        return ["table", "change", "id", "name", "what changed (previous -> current)"], rows_out[:DRILL_LIMIT]
    def search_fn(con, name, num):
        """Looks in every diff table of this schema (newest first). The name is matched on location / provider / name columns;
        the number only if the table has a Companies House style column (company_number, companies_house...)."""
        out = []
        tables = []
        for diff_table, cur_table in con.execute(
                "SELECT diff_table, current_table FROM public.cqc_diff_summary WHERE schema_name=%s", (schema,)).fetchall():
            d, tname = parse(cur_table)
            tables.append((d or date.min, diff_table, tname or diff_table))
        for d, diff_table, tname in sorted(tables, reverse=True):
            if len(out) >= SEARCH_LIMIT:
                break
            try:
                cols = [c.name for c in con.execute(f'SELECT * FROM "{schema}"."{diff_table}" LIMIT 0').description]
                q = lambda c: '"' + c.replace('"', '""') + '"'
                ncols = [c for c in cols if re.match(r"^(location_name|provider_name|name)_(curr|prev)$", c)]
                ccols = [c for c in cols if re.match(r"^.*(companies_house|company_number|company_reg|ch_number).*_(curr|prev)$", c)]
                if (name and not ncols) or (num and not ccols) or not (ncols or ccols):
                    continue
                where, par = [], {}
                if name:
                    where.append("(" + " OR ".join(f"{q(c)}::text ILIKE %(name)s" for c in ncols) + ")")
                    par["name"] = name
                if num:
                    where.append("(" + " OR ".join(f"lpad(upper(replace({q(c)}::text,' ','')),8,'0') = %(num)s" for c in ccols) + ")")
                    par["num"] = num
                sel = lambda cs: "COALESCE(" + ",".join(f"{q(c)}::text" for c in cs) + ")" if cs else "NULL::text"
                ct = q("change_type") if "change_type" in cols else "NULL::text"
                kv = q("key_value") + "::text" if "key_value" in cols else "NULL::text"
                cc = q("changed_columns") + "::text" if "changed_columns" in cols else "NULL::text"
                for ctype, key, nm, cn, chg in con.execute(
                        f'SELECT {ct}, {kv}, {sel(ncols)}, {sel(ccols)}, {cc} FROM "{schema}"."{diff_table}" WHERE ' + " AND ".join(where)
                        + f" LIMIT {SEARCH_LIMIT}", par).fetchall():
                    detail = f"ID {key}" + (f" - changed: {chg}" if chg else "")
                    out.append((d if d != date.min else None, f"{tname}: {(ctype or '').title()}", nm, cn, detail, None))
            except Exception:
                continue
        return add_numbers(con, out[:SEARCH_LIMIT])
    return dict(code=code, name=name, category="Care sector (CQC)", frequency=frequency, fetch=fetch, drill=drill_fn, subs=[],
                sub_colors={}, search=search_fn)


# =====================================================================
#  YOUR REGISTERS  (add one block per register, in the order you want them shown)
# =====================================================================
PERSON_LINK = '''LEFT JOIN LATERAL (SELECT string_agg(l.company_number, '; ' ORDER BY l.company_number) AS nums,
         string_agg(COALESCE(c.company_name, l.company_name, '') || ' (' || COALESCE(l.officer_role, '') || ', ' || l.match_level || ')', '; ' ORDER BY l.company_number) AS names
       FROM "R6_company_master".person_link l LEFT JOIN "R6_company_master".company c ON c.company_number = l.company_number
       WHERE l.source_code = 'INS-01' AND l.source_ref = {ref}) lk ON true'''


# ---- company details beside a register entry (from the company master, tied by register_company_master.py: register_link) ----
CO_COLS = ('co."Company number", co."Company name", co."Company status", co."Registered office", co."Trading address", '
           'co."Directors and officers", co."PSCs (people with significant control)", co."Latest accounts made up to", co."Turnover", '
           'co."Profit for period", co."Net assets", co."Cash", co."Employees"')


def co_join(code, key):
    """LEFT JOIN LATERAL ... co : the company (or companies) tied to ONE register entry. code = the register code used in register_company_master.py,
    key = SQL for the entry's id (the same id the master was given: Defaulter ID, Sponsor ID, Release ID ...). Use co.* or CO_COLS in the SELECT."""
    return f'''LEFT JOIN LATERAL (SELECT string_agg(DISTINCT m.company_number, '; ') AS "Company number", string_agg(DISTINCT m.company_name, '; ') AS "Company name",
             string_agg(DISTINCT m.company_status, '; ') AS "Company status", string_agg(DISTINCT m.registered_office, ' | ') AS "Registered office",
             string_agg(DISTINCT m.trading_address, ' | ') AS "Trading address", string_agg(DISTINCT m.officers_detail, ' | ') AS "Directors and officers",
             string_agg(DISTINCT m.pscs_detail, ' | ') AS "PSCs (people with significant control)",
             string_agg(DISTINCT to_char(fl.period_end, 'YYYY-MM-DD'), '; ') AS "Latest accounts made up to",
             string_agg(DISTINCT 'GBP ' || to_char(fl.turnover, 'FM999,999,999,999,999'), '; ') AS "Turnover",
             string_agg(DISTINCT 'GBP ' || to_char(fl.profit_for_period, 'FM999,999,999,999,999'), '; ') AS "Profit for period",
             string_agg(DISTINCT 'GBP ' || to_char(fl.net_assets, 'FM999,999,999,999,999'), '; ') AS "Net assets",
             string_agg(DISTINCT 'GBP ' || to_char(fl.cash, 'FM999,999,999,999,999'), '; ') AS "Cash",
             string_agg(DISTINCT to_char(fl.employees, 'FM999,999,999'), '; ') AS "Employees"
           FROM "R6_company_master".register_link rl JOIN "R6_company_master".v_company_master m ON m.company_number = rl.company_number
                LEFT JOIN "R15_company_financials".v_latest fl ON fl.company_number = m.company_number
           WHERE rl.source_code = '{code}' AND rl.source_key = ({key})::text) co ON true'''

LINK_COL = "Gazette link"
NAME_COLS = ["Person Name", "Name", "Company", "Name of Company", "Company Name", "Partnership", "Name of Bankrupt 1", "Heading"]


def notice_tables(con, schema):
    """[(table, category label, [columns in order])] of one notice schema made by register_gazette.py (tables). The label is the last part of the table comment."""
    rows = con.execute("SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       "WHERE n.nspname = %s AND c.relkind = 'r' ORDER BY 1", (schema,)).fetchall()
    if not rows:
        raise psycopg.errors.UndefinedTable(f'schema "{schema}" has no tables - run  python register_gazette.py tables')
    out = []
    for t, cm in rows:
        cols = [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, t))]
        out.append((t, (cm or t).split(" > ")[-1].strip(), cols))
    return sorted(out, key=lambda x: x[1].lower())


def notice_schema_register(code, name, schema, personal=False, category="The Gazette"):
    """One register = one notice schema made by register_gazette.py (tables). Sub category = the Gazette category (one table each). Clicking a number shows
    that category's own table (all the keys of its notices as columns, the Gazette link last). personal=True also adds the company / officer
    the person is linked to (company master) and the Bankruptcy/DRO restriction orders."""
    I = sql.Identifier
    DAY = '"Publication date"::date'

    def fetch(con, d1, d2):
        parts = [sql.SQL("SELECT {d} AS d, {lab} AS sub, count(*)::int AS n FROM {s}.{t} WHERE {d} BETWEEN %(d1)s AND %(d2)s GROUP BY 1").format(
            d=sql.SQL(DAY), lab=sql.Literal(label), s=I(schema), t=I(t)) for t, label, _ in notice_tables(con, schema)]
        rows = con.execute(sql.SQL(" UNION ALL ").join(parts), {"d1": d1, "d2": d2}).fetchall()
        if personal:
            try:
                rows += con.execute('''SELECT order_date, 'Bankruptcy/DRO ' || kind, count(*)::int FROM "R5_insolvency".restriction_order
                                    WHERE order_date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2''', {"d1": d1, "d2": d2}).fetchall()
            except psycopg.Error:
                pass
        return rows

    def run(con, q, params):
        cur = con.execute(q, params)
        return [c.name for c in cur.description], cur.fetchall()

    def drill_fn(con, day, sub):
        tabs = notice_tables(con, schema)
        par = {"day": day}
        if personal and (sub or "").startswith("Bankruptcy/DRO"):
            return run(con, '''SELECT r.court_number AS ref, r.order_date AS date, 'Bankruptcy/DRO ' || r.kind AS type, r.person_name AS name, lk.nums AS company_number,
                                     lk.names AS linked_company_and_role, r.postcode, r.url FROM "R5_insolvency".restriction_order r '''
                       + PERSON_LINK.replace("{ref}", "r.court_number") + f" WHERE r.order_date = %(day)s ORDER BY r.person_name LIMIT {DRILL_LIMIT}", par)
        if sub:
            hit = next((x for x in tabs if x[1] == sub), None)
            if not hit:
                return [], []
            t, label, cols = hit
            sel = [sql.SQL("t.{}").format(I(c)) for c in cols if c != LINK_COL]
            join = sql.SQL("")
            if personal:
                sel += [sql.SQL("lk.nums AS {}").format(I("Linked company number")), sql.SQL("lk.names AS {}").format(I("Linked company and role"))]
                join = sql.SQL(PERSON_LINK.replace("{ref}", 't."Notice ID"::text'))
            sel.append(sql.SQL("t.{} AS url").format(I(LINK_COL)))

            def q(j, s_):
                return sql.SQL("SELECT {c} FROM {s}.{t} t {j} WHERE t.{d} = %(day)s ORDER BY t.{i} LIMIT {n}").format(
                    c=sql.SQL(", ").join(s_), s=I(schema), t=I(t), j=j, d=sql.SQL('"Publication date"::date'), i=I("Notice ID"), n=sql.SQL(str(DRILL_LIMIT)))
            try:
                return run(con, q(join, sel), par)
            except psycopg.Error:
                if not personal:
                    raise
                return run(con, q(sql.SQL(""), [x for x in sel if "lk." not in x.as_string(con)]), par)       # company master not built yet
        parts = [sql.SQL("SELECT {lab} AS {cat}, t.{i} AS {ref}, t.{ty} AS {tl}, t.{pd} AS {pl}, t.{lk} AS url FROM {s}.{t} t WHERE t.{d} = %(day)s").format(
            lab=sql.Literal(label), cat=I("Category"), i=I("Notice ID"), ref=I("Notice ID"), ty=I("Notice type"), tl=I("Notice type"), pd=I("Publication date"),
            pl=I("Publication date"), lk=I(LINK_COL), s=I(schema), t=I(t), d=sql.SQL('"Publication date"::date')) for t, label, _ in tabs]
        return run(con, sql.SQL(" UNION ALL ").join(parts) + sql.SQL(f" ORDER BY 1, 2 LIMIT {DRILL_LIMIT}"), par)

    def search_fn(con, name, num):
        tabs = notice_tables(con, schema)

        def build(linked):
            parts = []
            for t, label, cols in tabs:
                have = {c.lower(): c for c in cols}
                names = [sql.SQL("NULLIF(t.{}, '')").format(I(have[n.lower()])) for n in NAME_COLS if n.lower() in have]
                nm = sql.SQL("COALESCE({}, 'Notice ' || t.{}::text)").format(sql.SQL(", ").join(names), I("Notice ID")) if names else sql.SQL("'Notice ' || t.{}::text").format(I("Notice ID"))
                cn = sql.SQL("t.{}").format(I(have["company number"])) if "company number" in have else sql.SQL("NULL::text")
                if linked:
                    cn = sql.SQL("COALESCE({}, lk.nums)").format(cn)
                    det = sql.SQL("'Notice ' || t.{i}::text || COALESCE(', ' || t.{ty}, '') || COALESCE(' - officer of ' || lk.names, '')").format(i=I("Notice ID"), ty=I("Notice type"))
                    join = sql.SQL(PERSON_LINK.replace("{ref}", 't."Notice ID"::text'))
                    cond = sql.SQL("(%(name)s = '' OR t::text ILIKE %(name)s OR lk.names ILIKE %(name)s) AND (%(num)s = '' OR t::text ILIKE '%%' || %(num)s || '%%' OR lk.nums ILIKE '%%' || %(num)s || '%%')")
                else:
                    det = sql.SQL("'Notice ' || t.{i}::text || COALESCE(', ' || t.{ty}, '')").format(i=I("Notice ID"), ty=I("Notice type"))
                    join = sql.SQL("")
                    cond = sql.SQL("(%(name)s = '' OR t::text ILIKE %(name)s) AND (%(num)s = '' OR t::text ILIKE '%%' || %(num)s || '%%')")
                parts.append(sql.SQL("SELECT t.{d} AS d, {lab}::text AS ty, {nm}::text AS nm, {cn}::text AS cn, {det}::text AS det, t.{lk}::text AS url FROM {s}.{tb} t {j} WHERE {cond}").format(
                    d=sql.SQL('"Publication date"::date'), lab=sql.Literal(label), nm=nm, cn=cn, det=det, lk=I(LINK_COL), s=I(schema), tb=I(t), j=join, cond=cond))
            return sql.SQL("SELECT * FROM (") + sql.SQL(" UNION ALL ").join(parts) + sql.SQL(f") s ORDER BY 1 DESC NULLS LAST LIMIT {SEARCH_LIMIT}")

        par = {"name": name, "num": num}
        if personal:
            try:
                return con.execute(build(True), par).fetchall()
            except psycopg.Error:
                pass                                   # company master not built yet: plain search
        return con.execute(build(False), par).fetchall()

    return dict(code=code, name=name, category=category, frequency="Daily", fetch=fetch, drill=drill_fn, subs=[], sub_colors={}, search=search_fn)


REGISTERS = [
    # ---- The Gazette notices: tables made by register_gazette.py (one table per Gazette category) ----
    notice_schema_register("INS-01", "Personal Notices (The Gazette: People > Personal insolvency)", "R8_personal_notices", personal=True, category="Insolvency"),
    notice_schema_register("INS-02", "Corporate Notices (The Gazette: Companies)", "R7_corporate_notices", category="Insolvency"),
    notice_schema_register("GAZ-01", "Other People notices (appointments, name changes, honours, wills)", "R9_other_notices"),

    # ---- CQC (diff tables made by register_cqc.py) ----
    cqc_diff_register("CQC-01", "CQC Care Directory", "R1_cqc_care_directory"),
    cqc_diff_register("CQC-02", "Care Directory With Filters", "R2_care_directory_with_filters"),
    cqc_diff_register("CQC-03", "Care Directory With Ratings", "R3_care_directory_with_ratings"),
    cqc_diff_register("CQC-04", "Deactivated Locations", "R4_deactivated_locations"),

    # ---- HMRC deliberate tax defaulters (made by register_hmrc_defaulters.py) ----
    sql_register(
        "HMD-01", "HMRC Tax defaulters", "HMRC", "Quarterly (checked daily)",
        sql='''SELECT detected_date, change_type, count(*)::int FROM "R10_hmrc_tax_defaulters".change_log
               WHERE detected_date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2''',
        subs=["Added to list", "Removed from list"],
        drill='''SELECT c.change_type AS "Change", ''' + CO_COLS + ''', c.list_month AS "List month", v.* FROM "R10_hmrc_tax_defaulters".change_log c
                  JOIN "R10_hmrc_tax_defaulters".v_defaulter v ON v."Defaulter ID" = c.defaulter_id ''' + co_join("HMD-01", 'v."Defaulter ID"') + '''
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY c.change_id''',
        drill_fallback='''SELECT c.change_type AS "Change", c.list_month AS "List month", v.* FROM "R10_hmrc_tax_defaulters".change_log c
                  JOIN "R10_hmrc_tax_defaulters".v_defaulter v ON v."Defaulter ID" = c.defaulter_id
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY c.change_id''',
        search='''SELECT c.detected_date, 'HMRC tax defaulter: ' || c.change_type, COALESCE(co."Company name", c.name), co."Company number", c.detail, v.url
                   FROM "R10_hmrc_tax_defaulters".change_log c JOIN "R10_hmrc_tax_defaulters".v_defaulter v ON v."Defaulter ID" = c.defaulter_id ''' + co_join("HMD-01", 'v."Defaulter ID"') + '''
                   WHERE (%(name)s = '' OR v::text ILIKE %(name)s OR co."Company name" ILIKE %(name)s)
                     AND (%(num)s = '' OR co."Company number" ILIKE '%%' || %(num)s || '%%')''',
        search_fallback='''SELECT c.detected_date, 'HMRC tax defaulter: ' || c.change_type, c.name, NULL::text, c.detail, v.url
                   FROM "R10_hmrc_tax_defaulters".change_log c JOIN "R10_hmrc_tax_defaulters".v_defaulter v ON v."Defaulter ID" = c.defaulter_id
                   WHERE (%(name)s = '' OR v::text ILIKE %(name)s) AND %(num)s = '' '''),
    # ---- Home Office register of licensed sponsors: workers (made by register_sponsors.py) ----
    sql_register(
        "HOS-01", "Home Office licence sponsor register", "Employment & Workforce", "Daily",
        sql='''SELECT detected_date, change_type, count(*)::int FROM "R11_sponsor_register".change_log
               WHERE detected_date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2
               UNION ALL
               SELECT "First seen", 'Initial load', count(*)::int FROM "R11_sponsor_register".sponsor s
               WHERE "First seen" BETWEEN %(d1)s AND %(d2)s
                 AND NOT EXISTS (SELECT 1 FROM "R11_sponsor_register".change_log c WHERE c.sponsor_id = s."Sponsor ID" AND c.change_type = 'Added to register')
               GROUP BY 1''',
        subs=["Initial load", "Added to register", "Removed from register", "Details changed"],
        drill='''SELECT c.change_type AS "Change", c.detail AS "What changed", ''' + CO_COLS + ''', v.* FROM "R11_sponsor_register".change_log c
                  JOIN "R11_sponsor_register".v_sponsor v ON v."Sponsor ID" = c.sponsor_id ''' + co_join("HOS-01", 'v."Sponsor ID"') + '''
                  WHERE c.detected_date = %(day)s AND %(sub)s <> 'Initial load' AND (%(sub)s = '' OR c.change_type = %(sub)s)
                  UNION ALL
                  SELECT 'Initial load', NULL, ''' + CO_COLS + ''', v.* FROM "R11_sponsor_register".v_sponsor v ''' + co_join("HOS-01", 'v."Sponsor ID"') + '''
                  WHERE v."First seen" = %(day)s AND (%(sub)s = '' OR %(sub)s = 'Initial load')
                    AND NOT EXISTS (SELECT 1 FROM "R11_sponsor_register".change_log c WHERE c.sponsor_id = v."Sponsor ID" AND c.change_type = 'Added to register')''',
        drill_fallback='''SELECT c.change_type AS "Change", c.detail AS "What changed", v.* FROM "R11_sponsor_register".change_log c
                  JOIN "R11_sponsor_register".v_sponsor v ON v."Sponsor ID" = c.sponsor_id
                  WHERE c.detected_date = %(day)s AND %(sub)s <> 'Initial load' AND (%(sub)s = '' OR c.change_type = %(sub)s)
                  UNION ALL
                  SELECT 'Initial load', NULL, v.* FROM "R11_sponsor_register".v_sponsor v
                  WHERE v."First seen" = %(day)s AND (%(sub)s = '' OR %(sub)s = 'Initial load')
                    AND NOT EXISTS (SELECT 1 FROM "R11_sponsor_register".change_log c WHERE c.sponsor_id = v."Sponsor ID" AND c.change_type = 'Added to register')''',
        search='''SELECT s.d, s.ty, COALESCE(co."Company name", s.nm), co."Company number", s.det, s.url FROM "R11_sponsor_register".v_sponsor_search s ''' + co_join("HOS-01", "s.sid") + '''
                   WHERE (%(name)s = '' OR s.nm ILIKE %(name)s OR co."Company name" ILIKE %(name)s)
                     AND (%(num)s = '' OR co."Company number" ILIKE '%%' || %(num)s || '%%')''',
        search_fallback='''SELECT d, ty, nm, cn, det, url FROM "R11_sponsor_register".v_sponsor_search
                   WHERE (%(name)s = '' OR nm ILIKE %(name)s) AND %(num)s = '' '''),
    # ---- Contracts Finder tenders and awards (made by register_contracts.py) ----
    sql_register(
        "CON-01", "Contracts Finder: tenders and contract awards", "Government contracts", "Daily",
        sql='''SELECT detected_date, change_type, count(*)::int FROM "R12_contracts_finder".change_log
               WHERE detected_date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2''',
        subs=["Future opportunity", "New opportunity", "Contract awarded", "Notice updated"],
        drill='''SELECT c.change_type AS "Change", ''' + CO_COLS + ''', n."Published", n."Title", n."Buyer", n."Value", n."Currency", n."Awarded to", n."Awarded value",
                         n."Procurement category", n."Tender end", n."Region", n."CPV", n."Release ID", n.url
                  FROM "R12_contracts_finder".change_log c JOIN "R12_contracts_finder".notice n ON n."Release ID" = c."Release ID" ''' + co_join("CON-01", 'n."Release ID"') + '''
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY n."Published", c.change_id''',
        drill_fallback='''SELECT c.change_type AS "Change", n."Published", n."Title", n."Buyer", n."Value", n."Currency", n."Awarded to", n."Awarded value",
                         n."Procurement category", n."Tender end", n."Region", n."CPV", n."Release ID", n.url
                  FROM "R12_contracts_finder".change_log c JOIN "R12_contracts_finder".notice n ON n."Release ID" = c."Release ID"
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY n."Published", c.change_id''',
        search='''SELECT c.detected_date, 'Contracts Finder: ' || c.change_type, COALESCE(co."Company name", NULLIF(sp.nm, ''), n."Buyer"), COALESCE(co."Company number", sp.cn),
                          c.name || ' - ' || c.detail, n.url
                   FROM "R12_contracts_finder".change_log c JOIN "R12_contracts_finder".notice n ON n."Release ID" = c."Release ID"
                   LEFT JOIN LATERAL (SELECT string_agg(s."Supplier name", '; ') AS nm, string_agg(s.company_number, '; ') AS cn
                                      FROM "R12_contracts_finder".supplier s WHERE s."Release ID" = n."Release ID") sp ON true ''' + co_join("CON-01", 'n."Release ID"') + '''
                   WHERE (%(name)s = '' OR n."Buyer" ILIKE %(name)s OR c.name ILIKE %(name)s OR sp.nm ILIKE %(name)s OR co."Company name" ILIKE %(name)s)
                     AND (%(num)s = '' OR sp.cn ILIKE '%%' || %(num)s || '%%' OR co."Company number" ILIKE '%%' || %(num)s || '%%')''',
        search_fallback='''SELECT c.detected_date, 'Contracts Finder: ' || c.change_type, COALESCE(NULLIF(sp.nm, ''), n."Buyer"), sp.cn, c.name || ' - ' || c.detail, n.url
                   FROM "R12_contracts_finder".change_log c JOIN "R12_contracts_finder".notice n ON n."Release ID" = c."Release ID"
                   LEFT JOIN LATERAL (SELECT string_agg(s."Supplier name", '; ') AS nm, string_agg(s.company_number, '; ') AS cn
                                      FROM "R12_contracts_finder".supplier s WHERE s."Release ID" = n."Release ID") sp ON true
                   WHERE (%(name)s = '' OR n."Buyer" ILIKE %(name)s OR c.name ILIKE %(name)s OR sp.nm ILIKE %(name)s)
                     AND (%(num)s = '' OR sp.cn ILIKE '%%' || %(num)s || '%%') '''),
    # ---- Find a Tender notices (made by register_findtender.py) ----
    sql_register(
        "FTS-01", "Find a Tender: tenders and contract awards", "Government contracts", "Daily",
        sql='''SELECT detected_date, change_type, count(*)::int FROM "R13_find_a_tender".change_log
               WHERE detected_date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2''',
        subs=["Pipeline", "Planning", "Tender", "Award", "Contract", "Termination", "Payments compliance", "Dynamic market", "Other"],
        drill='''SELECT c.change_type AS "Stage", n."Notice type", n."Notice type source", n."Notice type name", ''' + CO_COLS + ''', n."Published", n."Title", n."Buyer", n."Value", n."Currency", n."Awarded to", n."Awarded value",
                         n."Procurement category", n."Commercial tool", n."SME suitable", n."VCSE suitable", n."Tender end", n."Region", n."CPV", n."Release ID", n.url
                  FROM "R13_find_a_tender".change_log c JOIN "R13_find_a_tender".notice n ON n."Release ID" = c."Release ID" ''' + co_join("FTS-01", 'n."Release ID"') + '''
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY n."Published", c.change_id''',
        drill_fallback='''SELECT c.change_type AS "Stage", n."Notice type", n."Notice type source", n."Notice type name", n."Published", n."Title", n."Buyer", n."Value", n."Currency", n."Awarded to", n."Awarded value",
                         n."Procurement category", n."Commercial tool", n."SME suitable", n."VCSE suitable", n."Tender end", n."Region", n."CPV", n."Release ID", n.url
                  FROM "R13_find_a_tender".change_log c JOIN "R13_find_a_tender".notice n ON n."Release ID" = c."Release ID"
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY n."Published", c.change_id''',
        search='''SELECT c.detected_date, 'Find a Tender: ' || c.change_type || COALESCE(' (' || n."Notice type" || CASE WHEN n."Notice type source" LIKE 'Inferred%%' THEN ', inferred' ELSE '' END || ')', ''), COALESCE(co."Company name", NULLIF(sp.nm, ''), n."Buyer"), COALESCE(co."Company number", sp.cn),
                          c.name || ' - ' || c.detail, n.url
                   FROM "R13_find_a_tender".change_log c JOIN "R13_find_a_tender".notice n ON n."Release ID" = c."Release ID"
                   LEFT JOIN LATERAL (SELECT string_agg(s."Supplier name", '; ') AS nm, string_agg(s.company_number, '; ') AS cn
                                      FROM "R13_find_a_tender".supplier s WHERE s."Release ID" = n."Release ID") sp ON true ''' + co_join("FTS-01", 'n."Release ID"') + '''
                   WHERE (%(name)s = '' OR n."Buyer" ILIKE %(name)s OR c.name ILIKE %(name)s OR sp.nm ILIKE %(name)s OR co."Company name" ILIKE %(name)s)
                     AND (%(num)s = '' OR sp.cn ILIKE '%%' || %(num)s || '%%' OR co."Company number" ILIKE '%%' || %(num)s || '%%')''',
        search_fallback='''SELECT c.detected_date, 'Find a Tender: ' || c.change_type || COALESCE(' (' || n."Notice type" || CASE WHEN n."Notice type source" LIKE 'Inferred%%' THEN ', inferred' ELSE '' END || ')', ''), COALESCE(NULLIF(sp.nm, ''), n."Buyer"), sp.cn, c.name || ' - ' || c.detail, n.url
                   FROM "R13_find_a_tender".change_log c JOIN "R13_find_a_tender".notice n ON n."Release ID" = c."Release ID"
                   LEFT JOIN LATERAL (SELECT string_agg(s."Supplier name", '; ') AS nm, string_agg(s.company_number, '; ') AS cn
                                      FROM "R13_find_a_tender".supplier s WHERE s."Release ID" = n."Release ID") sp ON true
                   WHERE (%(name)s = '' OR n."Buyer" ILIKE %(name)s OR c.name ILIKE %(name)s OR sp.nm ILIKE %(name)s)
                     AND (%(num)s = '' OR sp.cn ILIKE '%%' || %(num)s || '%%') '''),
    # ---- Payments Transparency Register (made by register_payments.py) ----
    sql_register(
        "PTR-01", "Payments Transparency Register", "Government contracts", "Daily",
        sql='''SELECT detected_date, change_type, count(*)::int FROM "R14_payments_transparency".change_log
               WHERE detected_date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2''',
        subs=["Payment published"],
        drill='''SELECT ''' + CO_COLS + ''', p."Supplier", p."Contracting authority", p."Payment value", p."Payment date", p."Published date", p."Title", p."Contract",
                         p."OCID", p."Supplier identifiers", p.url
                  FROM "R14_payments_transparency".change_log c JOIN "R14_payments_transparency".payment p ON p."Payment ID" = c."Payment ID" ''' + co_join("PTR-01", 'p."Payment ID"') + '''
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY p."Payment value" DESC NULLS LAST, c.change_id''',
        drill_fallback='''SELECT p."Supplier", p."Contracting authority", p."Payment value", p."Payment date", p."Published date", p."Title", p."Contract",
                         p."OCID", p."Supplier identifiers", p.url
                  FROM "R14_payments_transparency".change_log c JOIN "R14_payments_transparency".payment p ON p."Payment ID" = c."Payment ID"
                  WHERE c.detected_date = %(day)s AND (%(sub)s = '' OR c.change_type = %(sub)s) ORDER BY p."Payment value" DESC NULLS LAST, c.change_id''',
        search='''SELECT c.detected_date, 'Payments Transparency: ' || c.change_type, COALESCE(co."Company name", p."Supplier"), COALESCE(co."Company number", p.company_number),
                          c.detail || '; Title: ' || COALESCE(p."Title", ''), p.url
                   FROM "R14_payments_transparency".change_log c JOIN "R14_payments_transparency".payment p ON p."Payment ID" = c."Payment ID" ''' + co_join("PTR-01", 'p."Payment ID"') + '''
                   WHERE (%(name)s = '' OR p."Supplier" ILIKE %(name)s OR p."Contracting authority" ILIKE %(name)s OR co."Company name" ILIKE %(name)s)
                     AND (%(num)s = '' OR p.company_number ILIKE '%%' || %(num)s || '%%' OR co."Company number" ILIKE '%%' || %(num)s || '%%')''',
        search_fallback='''SELECT c.detected_date, 'Payments Transparency: ' || c.change_type, p."Supplier", p.company_number,
                          c.detail || '; Title: ' || COALESCE(p."Title", ''), p.url
                   FROM "R14_payments_transparency".change_log c JOIN "R14_payments_transparency".payment p ON p."Payment ID" = c."Payment ID"
                   WHERE (%(name)s = '' OR p."Supplier" ILIKE %(name)s OR p."Contracting authority" ILIKE %(name)s)
                     AND (%(num)s = '' OR p.company_number ILIKE '%%' || %(num)s || '%%') '''),
    # ---- Company master (made by register_company_master.py from the Companies House API) ----
    sql_register(
        "COM-01", "Company master: new companies and changes", "Companies", "Daily",
        sql='''SELECT detected_date, change_type, count(*)::int FROM "R6_company_master".company_change_log
               WHERE detected_date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2''',
        subs=["Company added to master", "Director added", "Director resigned", "PSC added", "PSC ceased", "Other officer added",
              "Other officer resigned", "Company name changed", "Status changed", "Registered office changed",
              "Trading address changed", "Officer linked to personal insolvency"],
        drill='''SELECT company_number, company_name, change_type, detail, detected_at FROM "R6_company_master".company_change_log
                  WHERE detected_date = %(day)s AND (%(sub)s = '' OR change_type = %(sub)s) ORDER BY change_id''',
        search='''SELECT detected_date, change_type, company_name, company_number, detail, NULL::text
                   FROM "R6_company_master".company_change_log
                   WHERE (%(name)s = '' OR company_name ILIKE %(name)s) AND (%(num)s = '' OR company_number = %(num)s)'''),

    # ---- Company financials (made by register_financials.py: Companies House API + bulk accounts files) ----
    sql_register(
        "FIN-01", "Company financials: accounts loaded", "Companies", "Daily",
        sql='''SELECT loaded_at::date, CASE WHEN status = 'parsed' THEN 'Accounts loaded - figures read' ELSE 'Accounts loaded - no figures (PDF / paper)' END, count(*)::int
               FROM "R15_company_financials".filing WHERE status IN ('parsed', 'no_structured_data') AND loaded_at::date BETWEEN %(d1)s AND %(d2)s GROUP BY 1, 2''',
        subs=["Accounts loaded - figures read", "Accounts loaded - no figures (PDF / paper)"],
        sub_colors={"Accounts loaded - figures read": "#2e7d32", "Accounts loaded - no figures (PDF / paper)": "#9e9e9e"},
        drill='''SELECT f.company_number AS "Company number", COALESCE(f.company_name, co."Company name") AS "Company name", co."Company status", co."Registered office",
                         co."Trading address", co."Directors and officers", co."PSCs (people with significant control)",
                         f.made_up_date AS "Made up to", f.filing_date AS "Filed", COALESCE(x.accounts_type, f.description) AS "Accounts type", x.audited AS "Audited", f.source AS "Read as",
                         x.turnover AS "Turnover", x.gross_profit AS "Gross profit", x.operating_profit AS "Operating profit", x.profit_for_period AS "Profit for period",
                         x.fixed_assets AS "Fixed assets", x.current_assets AS "Current assets", x.cash AS "Cash", x.creditors_within_1yr AS "Creditors < 1 year",
                         x.net_assets AS "Net assets", x.equity AS "Equity", x.employees AS "Employees",
                         x.prior_turnover AS "Prior year turnover", x.prior_profit_for_period AS "Prior year profit", x.prior_net_assets AS "Prior year net assets",
                         'https://find-and-update.company-information.service.gov.uk/company/' || f.company_number || '/filing-history' AS url
                  FROM "R15_company_financials".filing f LEFT JOIN "R15_company_financials".financials x ON x.transaction_id = f.transaction_id
                  LEFT JOIN LATERAL (SELECT m.company_name AS "Company name", m.company_status AS "Company status", m.registered_office AS "Registered office",
                                            m.trading_address AS "Trading address", m.officers_detail AS "Directors and officers", m.pscs_detail AS "PSCs (people with significant control)"
                                     FROM "R6_company_master".v_company_master m WHERE m.company_number = f.company_number) co ON true
                  WHERE f.status IN ('parsed', 'no_structured_data') AND f.loaded_at::date = %(day)s
                    AND (%(sub)s = '' OR %(sub)s = CASE WHEN f.status = 'parsed' THEN 'Accounts loaded - figures read' ELSE 'Accounts loaded - no figures (PDF / paper)' END)
                  ORDER BY x.turnover DESC NULLS LAST, f.company_number''',
        drill_fallback='''SELECT f.company_number AS "Company number", f.company_name AS "Company name", f.made_up_date AS "Made up to", f.filing_date AS "Filed",
                         COALESCE(x.accounts_type, f.description) AS "Accounts type", f.source AS "Read as", x.turnover AS "Turnover", x.profit_for_period AS "Profit for period",
                         x.net_assets AS "Net assets", x.cash AS "Cash", x.employees AS "Employees", 'https://find-and-update.company-information.service.gov.uk/company/' || f.company_number || '/filing-history' AS url
                  FROM "R15_company_financials".filing f LEFT JOIN "R15_company_financials".financials x ON x.transaction_id = f.transaction_id
                  WHERE f.status IN ('parsed', 'no_structured_data') AND f.loaded_at::date = %(day)s
                    AND (%(sub)s = '' OR %(sub)s = CASE WHEN f.status = 'parsed' THEN 'Accounts loaded - figures read' ELSE 'Accounts loaded - no figures (PDF / paper)' END)
                  ORDER BY x.turnover DESC NULLS LAST, f.company_number''',
        search='''SELECT COALESCE(f.filing_date, f.loaded_at::date), 'Financials: accounts made up to ' || COALESCE(to_char(f.made_up_date, 'DD Mon YYYY'), '?')
                          || COALESCE(' (' || x.accounts_type || ')', ''), f.company_name, f.company_number,
                          CASE WHEN x.transaction_id IS NULL THEN 'Figures not available (PDF / paper accounts)'
                               ELSE 'Turnover ' || COALESCE('GBP ' || to_char(x.turnover, 'FM999,999,999,999,999'), 'n/a') || '; Profit ' || COALESCE('GBP ' || to_char(x.profit_for_period, 'FM999,999,999,999,999'), 'n/a')
                                    || '; Net assets ' || COALESCE('GBP ' || to_char(x.net_assets, 'FM999,999,999,999,999'), 'n/a') || '; Cash ' || COALESCE('GBP ' || to_char(x.cash, 'FM999,999,999,999,999'), 'n/a')
                                    || '; Employees ' || COALESCE(to_char(x.employees, 'FM999,999'), 'n/a') END,
                          'https://find-and-update.company-information.service.gov.uk/company/' || f.company_number || '/filing-history'
                   FROM "R15_company_financials".filing f LEFT JOIN "R15_company_financials".financials x ON x.transaction_id = f.transaction_id
                   WHERE f.status IN ('parsed', 'no_structured_data') AND (%(name)s = '' OR f.company_name ILIKE %(name)s) AND (%(num)s = '' OR f.company_number = %(num)s)'''),

    # ---- Next register: copy a block above, change the code / name / SQL, save, press F5. ----
]


# =====================================================================
#  Data for the page
# =====================================================================
def connect():
    return psycopg.connect(**DB, autocommit=True)


def iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def build_tracker():
    today = date.today()
    d_from = today - timedelta(days=HISTORY_DAYS)
    window = [(today - timedelta(days=i)).isoformat() for i in range(8)]       # today, then 7 earlier days (the 8th is only for CHANGE)
    out = []
    try:
        con = connect()
    except psycopg.OperationalError as e:
        return {"error": "Cannot connect to PostgreSQL: " + str(e).splitlines()[0], "days": window, "registers": [], "today": today.isoformat()}
    with con:
        for i, r in enumerate(REGISTERS):
            item = {"code": r["code"], "name": r["name"], "category": r["category"], "frequency": r["frequency"],
                    "letter": FREQ_LETTER.get(r["frequency"], r["frequency"][:1]), "error": None, "subs": [], "last": None}
            try:
                rows = r["fetch"](con, d_from, today)
            except Exception as e:
                msg = str(e).splitlines()[0]
                item["error"] = "table not found - load its data first" if "does not exist" in msg else msg[:160]
                item["subs"] = [{"name": s, "counts": {}, "color": r["sub_colors"].get(s, PALETTE[k % len(PALETTE)])} for k, s in enumerate(r["subs"])]
                out.append(item)
                continue
            by_sub, by_day = {}, {}
            for d, sub, n in rows:
                d = iso(d)
                if d is None or not sub:
                    continue
                by_sub.setdefault(sub, {})[d] = by_sub.get(sub, {}).get(d, 0) + int(n)
                by_day[d] = by_day.get(d, 0) + int(n)
            names = list(r["subs"]) + sorted(s for s in by_sub if s not in r["subs"])
            for k, s in enumerate(names):
                item["subs"].append({"name": s, "color": r["sub_colors"].get(s, PALETTE[k % len(PALETTE)]),
                                     "counts": {d: n for d, n in by_sub.get(s, {}).items() if d in window}})
            nz = [d for d, n in by_day.items() if n > 0]
            if nz:
                m = max(nz)
                item["last"] = {"date": m, "n": by_day[m]}
            out.append(item)
    latest = [it["last"]["date"] for it in out if it["last"] and it["last"]["date"] < today.isoformat()]
    return {"today": today.isoformat(), "days": window, "default_day": max(latest) if latest else (today - timedelta(days=1)).isoformat(),
            "generated": datetime.now().strftime("%H:%M:%S"), "registers": out, "error": None}


def norm_number(text):
    """'1234567' / '01234 567' / 'sc123456' -> '01234567' / '01234567' / 'SC123456'"""
    t = re.sub(r"[^0-9A-Za-z]", "", text or "").upper()
    return t.zfill(8) if t.isdigit() else t


def like_pattern(text):
    t = (text or "").strip()
    return "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if t else ""


PROFILE_SQL = '''SELECT company_number, company_name, company_status, registered_office, trading_address, directors, pscs,
                          registers, fetch_status, last_fetched_at
                   FROM "R6_company_master".v_company_master
                   WHERE (%(name)s = '' OR company_name ILIKE %(name)s) AND (%(num)s = '' OR company_number = %(num)s)
                   ORDER BY company_name LIMIT 5'''


def build_search(name, number):
    """Users tab: entries in ALL registers for a company name and/or number (either one is enough; both = both must match)."""
    name = (name or "").strip()
    num = norm_number(number)
    if len(name) < 2:
        name = ""
    if not name and not num:
        return {"error": "Type a company name (at least 2 letters) or a company registration number."}
    par = {"name": like_pattern(name), "num": num}
    try:
        con = connect()
    except psycopg.OperationalError as e:
        return {"error": "Cannot connect to PostgreSQL: " + str(e).splitlines()[0]}
    with con:
        profiles, per, rows = search_core(con, par["name"], num)
    return {"name": name, "number": num, "profiles": profiles, "registers": per, "rows": rows, "limit": SEARCH_LIMIT, "error": None}


def search_core(con, name_pattern, num):
    """-> (company master profiles, per-register counts, rows [register code, register name, date, type, name, company no., detail, url] newest first)."""
    rows, per, profiles = [], [], []
    par = {"name": name_pattern, "num": num}
    try:
        cur = con.execute(PROFILE_SQL, par)
        cols = [c.name for c in cur.description]
        profiles = [{k: iso(v) for k, v in zip(cols, r)} for r in cur.fetchall()]
    except Exception:
        profiles = []                  # company master not created yet
    for r in REGISTERS:
        fn = r.get("search")
        item = {"code": r["code"], "name": r["name"], "n": 0, "error": None}
        try:
            found = fn(con, name_pattern, num) if fn else []
        except Exception as e:
            msg = str(e).splitlines()[0]
            item["error"] = "table not found" if "does not exist" in msg else msg[:120]
            found = []
        for d, typ, nm, cn, detail, url in found:
            rows.append([r["code"], r["name"], iso(d), typ, nm, cn, detail, url])
        item["n"] = len(found)
        per.append(item)
    rows.sort(key=lambda x: x[2] or "", reverse=True)
    return profiles, per, rows


BULK_MAX = 200              # most companies taken from one CSV upload


def parse_day(v):
    """'2026-11-01', '01/11/2026', '1 Nov 2026' -> date, or None when empty / not a date."""
    v = (v or "").strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(v, f).date()
        except ValueError:
            pass
    return None


def build_bulk(payload):
    """CSV upload on the Users tab. payload = {rows: [{name, number, from, to}], from, to}.  The From / To of a row win; the form's From / To fill the gaps;
    with neither, every signal found is returned.  -> one table row per signal: company name, company reg, register name, date, signal, detail, link."""
    items = payload.get("rows") or []
    if not items:
        return {"error": "The file has no rows."}
    if len(items) > BULK_MAX:
        return {"error": f"The file has {len(items)} rows; the most allowed in one upload is {BULK_MAX}. Split it and upload again."}
    gf, gt = parse_day(payload.get("from")), parse_day(payload.get("to"))
    try:
        con = connect()
    except psycopg.OperationalError as e:
        return {"error": "Cannot connect to PostgreSQL: " + str(e).splitlines()[0]}
    out, summary, skipped, not_found = [], [], [], []
    with con:
        for n, it in enumerate(items, 1):
            name, num = (it.get("name") or "").strip(), norm_number(it.get("number"))
            if len(name) < 2:
                name = ""
            if not name and not num:
                skipped.append(f"row {n}: no company name or number")
                continue
            d1 = parse_day(it.get("from")) or gf
            d2 = parse_day(it.get("to")) or gt
            bad = [x for x in (it.get("from"), it.get("to")) if (x or "").strip() and not parse_day(x)]
            if bad:
                skipped.append(f"row {n}: date not understood ({', '.join(bad)}) - use YYYY-MM-DD or DD/MM/YYYY")
                continue
            profiles, per, rows = search_core(con, like_pattern(name), num)
            if not profiles and not rows:
                not_found.append(name or num)          # in neither the company master nor any register
            if d1 or d2:
                rows = [r for r in rows if r[2] and (not d1 or r[2][:10] >= d1.isoformat()) and (not d2 or r[2][:10] <= d2.isoformat())]
            prof = profiles[0] if len(profiles) == 1 else None
            in_name = (prof or {}).get("company_name") or name
            in_num = (prof or {}).get("company_number") or num
            span = ("" if not (d1 or d2) else f"{d1.isoformat() if d1 else '...'} to {d2.isoformat() if d2 else '...'}")
            summary.append({"row": n, "name": in_name, "number": in_num, "signals": len(rows), "period": span})
            if not rows:
                out.append([in_name, in_num, "", "", "No signals" + (" in this period" if span else ""), "", "", span])
            for r in rows:
                out.append([r[4] or in_name, r[5] or in_num, f"{r[0]} {r[1]}", r[2], r[3], r[6], r[7], span])
    return {"error": None, "rows": out, "summary": summary, "skipped": skipped, "limit": SEARCH_LIMIT,
            "not_found": not_found, "companies": len(summary), "with_signals": sum(1 for x in summary if x["signals"])}


_cache = {"t": 0, "v": None}
_lock = threading.Lock()


def tracker_cached():
    with _lock:
        if time.time() - _cache["t"] > CACHE_SECONDS or _cache["v"] is None:
            _cache["v"], _cache["t"] = build_tracker(), time.time()
        return _cache["v"]


def build_drill(code, day, sub):
    reg = next((r for r in REGISTERS if r["code"] == code), None)
    if not reg:
        return {"error": "unknown register"}
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return {"error": "bad date"}
    with connect() as con:
        cols, rows = reg["drill"](con, d, sub)
    return {"code": code, "name": reg["name"], "day": day, "sub": sub, "columns": cols,
            "rows": [[iso(v) if not isinstance(v, (int, float, str, type(None))) else v for v in row] for row in rows], "limit": DRILL_LIMIT}


# =====================================================================
#  The page
# =====================================================================
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alert Engine - Daily tracker</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--ink:#1c2733;--mute:#6b7785;--line:#e3e8ee;--blue:#1f5f8b;--sel:#e3eef8;--wk:#fdf1df;--red:#b3261e;--grn:#1e7a4a;--hd:#eef1f4}
@media (prefers-color-scheme:dark){:root{--bg:#12161b;--card:#1a2027;--ink:#e6ebf0;--mute:#93a0ad;--line:#2b3440;--blue:#7bb6e6;--sel:#1f3447;--wk:#3a2e1c;--red:#ff8a80;--grn:#6fd49a;--hd:#222a33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 system-ui,"Segoe UI",sans-serif}
.app{padding:12px 16px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:14px;overflow-x:auto}
.tabs button{all:unset;box-sizing:border-box;padding:10px 18px;cursor:pointer;color:var(--mute);font-weight:600;border-bottom:3px solid transparent;margin-bottom:-1px;white-space:nowrap}
.tabs button:hover{color:var(--ink)}.tabs button[aria-selected=true]{color:var(--blue);border-bottom-color:var(--blue)}
.tabs .cnt{margin-left:6px;font-size:11px;color:var(--mute);font-weight:500}
.bar{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin-bottom:12px}
.bar label.l{display:flex;flex-direction:column;gap:3px;font:600 10.5px ui-monospace,Consolas,monospace;letter-spacing:.06em;color:var(--mute);text-transform:uppercase}
.bar input[type=text],.bar select{height:34px;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:0 10px;font:13px system-ui;min-width:190px}
.chk{display:flex;gap:6px;align-items:center;color:var(--mute);font-size:12.5px}.bar .gap{flex:1}
.btn{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:6px 10px;cursor:pointer;font:12.5px system-ui}
.wrap{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:auto}
table{border-collapse:collapse;width:100%;min-width:900px}
th{position:sticky;top:0;background:var(--card);font:600 10.5px ui-monospace,Consolas,monospace;letter-spacing:.07em;color:var(--mute);text-transform:uppercase;text-align:right;padding:11px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child{text-align:left;padding-left:14px}th.day{cursor:pointer}th.day:hover{color:var(--blue)}
td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
td:first-child{text-align:left;padding-left:14px}
.wk{background:var(--wk)}.sel{background:var(--sel)}
tr.reg td{background:var(--hd);font-weight:600;padding-top:11px;padding-bottom:11px}tr.reg td.wk{background:var(--wk)}tr.reg td.sel{background:var(--sel)}
tr.reg{cursor:pointer}tr.reg .car{display:inline-block;width:14px;color:var(--mute);transition:transform .12s}tr.reg.open .car{transform:rotate(90deg)}
.code{font:700 12px ui-monospace,Consolas,monospace;color:var(--mute);margin-right:6px}.rn{font-weight:700;font-size:14.5px}
.badge{font:600 9px ui-monospace,Consolas,monospace;color:var(--mute);border:1px solid var(--line);border-radius:3px;padding:0 3px;margin-left:5px;vertical-align:super}
.meta{display:block;margin:2px 0 0 20px;font-weight:400;font-size:12px;color:var(--mute)}.err{color:var(--red)}
tr.sub td:first-child{padding-left:38px}.sq{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:9px}
.z{color:#aab4bf}@media (prefers-color-scheme:dark){.z{color:#566371}}.n{font-weight:600;cursor:pointer}.n:hover{color:var(--blue);text-decoration:underline}
.up{color:var(--red);font-weight:700}.dn{color:var(--grn);font-weight:700}.tot{font-weight:700}
.empty{padding:34px;text-align:center;color:var(--mute)}
#shade{position:fixed;inset:0;background:#0007;display:none;z-index:9}#dlg{position:fixed;z-index:10;left:50%;top:6vh;transform:translateX(-50%);width:min(1100px,94vw);max-height:86vh;background:var(--card);border:1px solid var(--line);border-radius:12px;display:none;flex-direction:column}
#dlg header{display:flex;gap:10px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line)}#dlg h3{margin:0;font-size:15px;flex:1}#dlg .body{overflow:auto;padding:0}
#dlg table{min-width:0}#dlg td,#dlg th{text-align:left}#dlg td a{color:var(--blue)}
.btn.pri{background:var(--blue);color:#fff;border-color:var(--blue);height:34px;padding:0 16px;font-weight:600}
.prof{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.card h4{margin:0 0 6px;font-size:15px}.card dl{display:grid;grid-template-columns:120px 1fr;gap:3px 10px;margin:0;font-size:13px}
.card dt{color:var(--mute)}.card dd{margin:0;overflow-wrap:anywhere}
.lk{color:var(--blue);cursor:pointer}.lk:hover{text-decoration:underline}
#vUsr td,#vUsr th{text-align:left}#vUsr .tg{font-size:15px;padding:0 4px;user-select:none}#vUsr table.in{min-width:0;width:100%;margin:2px 0}#vUsr tr.det>td{background:rgba(127,127,127,.06)}#vUsr .muted{opacity:.65}#vUsr table{min-width:760px}
#dB td{text-align:left;white-space:pre-line;max-width:460px;vertical-align:top}
</style></head><body>
<div class="app">
<div class="tabs" role="tablist" aria-label="Main">
 <button id="tUsr" role="tab" aria-selected="true">Users</button>
 <button id="tAll" role="tab" aria-selected="false">All registers<span class="cnt" id="allCnt"></span></button>
 <button id="tTrk" role="tab" aria-selected="false">Daily tracker (Details)</button>
</div>
<div id="msg"></div>
<div id="vUsr">
 <div class="bar" id="bBar">
  <label class="l">CSV file (company name and/or company reg, optional From / To)<input id="bFile" type="file" accept=".csv,text/csv"></label>
  <label class="l">From (optional)<input id="bFrom" type="date"></label>
  <label class="l">To (optional)<input id="bTo" type="date"></label>
  <button class="btn pri" id="bGo" disabled>Submit</button><button class="btn" id="bClr">Clear</button>
  <span class="chk" id="bInfo">Columns: company name, company reg, from, to (header row optional; dates YYYY-MM-DD or DD/MM/YYYY). A From / To in the file wins over the dates here.</span>
 </div>
 <div class="bar" id="pBar" style="align-items:flex-start">
  <label class="l" style="flex:1;min-width:320px">Or paste companies (one per line)<textarea id="pTxt" rows="4" placeholder="Paste company names or company numbers here, one per line" style="width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:inherit;font:inherit"></textarea></label>
  <span class="chk" style="display:flex;flex-direction:column;gap:6px;padding-top:18px">
   <label><input type="radio" name="pKind" id="pName" checked> The lines are company names</label>
   <label><input type="radio" name="pKind" id="pNum"> The lines are company numbers</label>
  </span>
 </div>
 <div id="bMsg"></div><div id="bSum" class="chk" style="margin-bottom:8px"></div>
 <div class="wrap" id="bWrap" hidden><table id="bTbl"></table></div>
 <hr style="border:0;border-top:1px solid var(--line);margin:18px 0">
 <div class="bar">
  <label class="l">Company name<input id="uName" type="text" placeholder="e.g. Acme Care" autocomplete="off"></label>
  <label class="l">Company registration number<input id="uNum" type="text" placeholder="e.g. 01234567" autocomplete="off"></label>
  <button class="btn pri" id="uGo">Search</button><button class="btn" id="uClr">Clear</button>
  <label class="l" id="uRegW" hidden>Show register<select id="uReg"></select></label>
  <span class="chk">Fill in either one, or both.</span>
 </div>
 <div id="uMsg"></div><div id="uProf" class="prof"></div><div id="uSum" class="chk" style="margin-bottom:8px"></div>
 <div class="wrap" id="uWrap" hidden><table id="uTbl"></table></div>
</div>
<div id="vTrk" hidden>
 <div class="bar">
  <label class="l">Search<input id="q" type="text" placeholder="Register or sub category"></label>
  <label class="l">Category<select id="cat"><option value="">All categories</option></select></label>
  <label class="l">Register<select id="reg"><option value="">All registers</option></select></label>
  <label class="chk"><input id="onlyEnt" type="checkbox">Only sub categories with entries in the 7 days</label>
  <label class="chk"><input id="onlyChg" type="checkbox">Only rises and falls on the chosen date</label>
  <span class="gap"></span><span id="gen" class="chk"></span>
  <button class="btn" id="exp">Expand all</button><button class="btn" id="col">Collapse all</button><button class="btn" id="ref">Refresh</button>
 </div>
 <div class="wrap"><table id="tbl"></table></div>
</div>
<div id="vAll" hidden><div class="wrap"><table id="allTbl"></table></div></div>
</div>
<div id="nfD" role="alertdialog" aria-modal="true" style="position:fixed;z-index:11;left:50%;top:14vh;transform:translateX(-50%);width:min(560px,94vw);max-height:70vh;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;display:none"><h3 style="margin:0 0 8px;font-size:15px">Not found in the current monitor database</h3><div id="nfB" style="font-size:13px"></div><div style="text-align:right;margin-top:12px"><button class="btn pri" id="nfX">OK</button></div></div><div id="shade"></div><div id="dlg" role="dialog" aria-modal="true"><header><h3 id="dT"></h3><button class="btn" id="dX">Close</button></header><div class="body" id="dB"></div></div>
<script>
const $=id=>document.getElementById(id), esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=n=>Number(n).toLocaleString("en-GB"), MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const dm=d=>d.slice(8,10)+"/"+d.slice(5,7), dmy=d=>+d.slice(8,10)+" "+MON[+d.slice(5,7)-1]+" "+d.slice(0,4);
const wkend=d=>{const w=new Date(d+"T12:00:00").getDay();return w===0||w===6};
let T=null, CHOSEN=null, OPEN=new Set(), SEEN=false;
try{const s=JSON.parse(localStorage.getItem("trkOpen")||"null");if(Array.isArray(s)){OPEN=new Set(s);SEEN=true}}catch(e){}
const saveOpen=()=>{try{localStorage.setItem("trkOpen",JSON.stringify([...OPEN]))}catch(e){}};
async function load(){
  try{const r=await fetch("/api/tracker");T=await r.json()}catch(e){$("msg").innerHTML='<div class="empty err">Cannot reach the tracker server.</div>';return}
  $("msg").innerHTML=T.error?`<div class="empty err">${esc(T.error)}</div>`:"";
  if(!CHOSEN||!T.days.includes(CHOSEN))CHOSEN=T.default_day;
  if(!SEEN&&T.registers.length){OPEN=new Set([T.registers[0].code]);SEEN=true}
  const cats=[...new Set(T.registers.map(r=>r.category))];
  const keep=[$("cat").value,$("reg").value];
  $("cat").innerHTML='<option value="">All categories</option>'+cats.map(c=>`<option>${esc(c)}</option>`).join("");
  $("reg").innerHTML='<option value="">All registers</option>'+T.registers.map(r=>`<option value="${esc(r.code)}">${esc(r.code)} ${esc(r.name)}</option>`).join("");
  $("cat").value=keep[0]||"";$("reg").value=keep[1]||"";
  $("allCnt").textContent=T.registers.length;$("gen").textContent=T.generated?"Updated "+T.generated:"";
  render();renderAll();
}
function render(){
  if(!T)return;
  const days=T.days.slice(0,7), prev=T.days[T.days.indexOf(CHOSEN)+1]||"";
  const q=$("q").value.trim().toLowerCase(), cat=$("cat").value, rg=$("reg").value, oe=$("onlyEnt").checked, oc=$("onlyChg").checked;
  const cls=d=>(wkend(d)?" wk":"")+(d===CHOSEN?" sel":"");
  let h='<thead><tr><th>Register / sub category</th>'+days.map((d,i)=>`<th class="day${cls(d)}" data-d="${d}" title="${i?"Show changes on this date":"Today"}">${i?dm(d):"Today"}</th>`).join("")+'<th>Change</th><th>7 days</th></tr></thead><tbody>';
  let shown=0;
  for(const r of T.registers){
    if(cat&&r.category!==cat)continue; if(rg&&r.code!==rg)continue;
    const reg=(r.code+" "+r.name).toLowerCase().includes(q);
    let subs=r.subs.map(s=>{const c=days.map(d=>s.counts[d]||0);const chg=(s.counts[CHOSEN]||0)-(prev?(s.counts[prev]||0):0);return{...s,c,chg,t:c.reduce((a,b)=>a+b,0)}});
    const total=subs.length; 
    if(q&&!reg)subs=subs.filter(s=>s.name.toLowerCase().includes(q));
    if(oe)subs=subs.filter(s=>s.t>0); if(oc)subs=subs.filter(s=>s.chg!==0);
    if((q&&!reg&&!subs.length)||((oe||oc)&&!subs.length))continue;
    shown++;
    const rc=days.map((d,i)=>subs.reduce((a,s)=>a+s.c[i],0)), rt=rc.reduce((a,b)=>a+b,0);
    const rchg=subs.reduce((a,s)=>a+s.chg,0), open=OPEN.has(r.code)||!!q&&!reg||oe||oc;
    const last=r.last&&r.frequency!=="Daily"?` · last entry ${dmy(r.last.date)} (${fmt(r.last.n)})`:"";
    h+=`<tr class="reg${open?" open":""}" data-c="${esc(r.code)}"><td><span class="car">▸</span><span class="code">${esc(r.code)}</span><span class="rn">${esc(r.name)}</span><span class="badge">${esc(r.letter)}</span>`
      +`<span class="meta ${r.error?"err":""}">${esc(r.error||`${r.category} · ${r.frequency} · ${subs.length} of ${total} sub categories${last}`)}</span></td>`
      +days.map((d,i)=>`<td class="${cls(d).trim()}">${rc[i]?`<span class="n" data-c="${esc(r.code)}" data-d="${d}" data-s="">${fmt(rc[i])}</span>`:'<span class="z">0</span>'}</td>`).join("")
      +`<td>${chgCell(rchg)}</td><td class="tot">${fmt(rt)}</td></tr>`;
    if(open)for(const s of subs){
      h+=`<tr class="sub"><td><span class="sq" style="background:${s.color}"></span>${esc(s.name)}</td>`
        +days.map((d,i)=>`<td class="${cls(d).trim()}">${s.c[i]?`<span class="n" data-c="${esc(r.code)}" data-d="${d}" data-s="${esc(s.name)}">${fmt(s.c[i])}</span>`:'<span class="z">0</span>'}</td>`).join("")
        +`<td>${chgCell(s.chg)}</td><td class="tot">${s.t?fmt(s.t):'<span class="z">0</span>'}</td></tr>`;
    }
  }
  if(!shown)h+=`<tr><td colspan="10" class="empty">${T.registers.length?"Nothing matches these filters.":"No registers are set up yet."}</td></tr>`;
  $("tbl").innerHTML=h+"</tbody>";
}
const chgCell=n=>n>0?`<span class="up">+${fmt(n)}</span>`:n<0?`<span class="dn">−${fmt(-n)}</span>`:'<span class="z">0</span>';
function renderAll(){
  let h='<thead><tr><th>Code</th><th style="text-align:left">Register</th><th style="text-align:left">Category</th><th style="text-align:left">Frequency</th><th>Sub categories</th><th>Last entry</th><th>Entries (7 days)</th><th style="text-align:left">Status</th></tr></thead><tbody>';
  for(const r of T.registers){
    const t=r.subs.reduce((a,s)=>a+T.days.slice(0,7).reduce((x,d)=>x+(s.counts[d]||0),0),0);
    h+=`<tr><td style="text-align:left"><span class="code">${esc(r.code)}</span></td><td style="text-align:left"><b>${esc(r.name)}</b></td><td style="text-align:left">${esc(r.category)}</td><td style="text-align:left">${esc(r.frequency)}</td><td>${r.subs.length}</td>`
      +`<td>${r.last?dmy(r.last.date)+" ("+fmt(r.last.n)+")":"-"}</td><td>${fmt(t)}</td><td style="text-align:left" class="${r.error?"err":""}">${esc(r.error||(r.last?"Receiving data":"No data yet"))}</td></tr>`;
  }
  $("allTbl").innerHTML=h+"</tbody>";
}
async function drill(c,d,s){
  $("dT").textContent=`${c} · ${s||"all sub categories"} · ${dmy(d)}`;$("dB").innerHTML='<div class="empty">Loading...</div>';$("shade").style.display="block";$("dlg").style.display="flex";
  try{const r=await (await fetch(`/api/drill?code=${encodeURIComponent(c)}&day=${d}&sub=${encodeURIComponent(s)}`)).json();
    if(r.error){$("dB").innerHTML=`<div class="empty err">${esc(r.error)}</div>`;return}
    if(!r.rows.length){$("dB").innerHTML='<div class="empty">No records to show.</div>';return}
    $("dB").innerHTML=`<table><thead><tr>${r.columns.map(x=>`<th>${esc(x==="url"?"Link":x.replace(/_/g," "))}</th>`).join("")}</tr></thead><tbody>`
     +r.rows.map(row=>"<tr>"+row.map((v,i)=>r.columns[i]==="url"&&v?`<td><a href="${esc(v)}" target="_blank" rel="noopener">Open notice</a></td>`:`<td>${esc(v)}</td>`).join("")+"</tr>").join("")+"</tbody></table>"
     +(r.rows.length>=r.limit?`<div class="empty">Showing the first ${r.limit} records.</div>`:"")}
  catch(e){$("dB").innerHTML='<div class="empty err">Could not load the records.</div>'}
}
const closeD=()=>{$("shade").style.display=$("dlg").style.display=$("nfD").style.display="none"};
$("nfX").onclick=closeD;
$("dX").onclick=closeD;$("shade").onclick=closeD;document.addEventListener("keydown",e=>{if(e.key==="Escape")closeD()});
$("tbl").addEventListener("click",e=>{
  const n=e.target.closest(".n");if(n){drill(n.dataset.c,n.dataset.d,n.dataset.s);return}
  const th=e.target.closest("th.day");if(th&&th.dataset.d!==T.days[0]){CHOSEN=th.dataset.d;render();return}
  if(th){CHOSEN=th.dataset.d;render();return}
  const tr=e.target.closest("tr.reg");if(tr){const c=tr.dataset.c;OPEN.has(c)?OPEN.delete(c):OPEN.add(c);saveOpen();render()}
});
["q","cat","reg","onlyEnt","onlyChg"].forEach(i=>$(i).addEventListener(i==="q"?"input":"change",render));
$("exp").onclick=()=>{T.registers.forEach(r=>OPEN.add(r.code));saveOpen();render()};$("col").onclick=()=>{OPEN.clear();saveOpen();render()};$("ref").onclick=load;
const TABS={usr:["tUsr","vUsr"],all:["tAll","vAll"],trk:["tTrk","vTrk"]};
function view(v){
  for(const k in TABS){$(TABS[k][1]).hidden=k!==v;$(TABS[k][0]).setAttribute("aria-selected",k===v)}
  try{localStorage.setItem("trkTab",v)}catch(e){}
  if(v==="usr")$("uName").focus();
}
for(const k in TABS)$(TABS[k][0]).onclick=()=>view(k);

let U=null;
const uMsg=(t,err)=>{$("uMsg").innerHTML=t?`<div class="empty${err?" err":""}">${esc(t)}</div>`:""};
/* ---- one consolidated row per company (shared by the Users search and the CSV upload) ---- */
const normNm=x=>(x||"").toLowerCase().replace(/[^a-z0-9]/g,"");
function consolidate(entries,profNames){
  const byName={};
  entries.forEach(e=>{if(e.num&&e.name&&!byName[normNm(e.name)])byName[normNm(e.name)]=e.num});
  const map=new Map();
  entries.forEach(e=>{
    const num=e.num||byName[normNm(e.name)]||"", key=num?"n:"+num:"m:"+normNm(e.name);
    let g=map.get(key);
    if(!g){g={num,names:{},regs:{},items:[],latest:null};map.set(key,g)}
    if(e.name)g.names[e.name]=(g.names[e.name]||0)+1;
    if(!e.none){g.items.push(e);g.regs[e.code]=(g.regs[e.code]||0)+1;
      if(e.date&&(!g.latest||e.date>g.latest.date))g.latest=e}
  });
  const out=[...map.values()].map(g=>{
    const nm=Object.keys(g.names).sort((a,b)=>g.names[b]-g.names[a]);
    g.name=(profNames&&profNames[g.num])||nm[0]||"(name not available)";
    g.items.sort((a,b)=>(b.date||"").localeCompare(a.date||""));
    return g});
  out.sort((a,b)=>((b.latest&&b.latest.date)||"").localeCompare((a.latest&&a.latest.date)||"")||a.name.localeCompare(b.name));
  return out;
}
const regList=g=>Object.keys(g.regs).sort().map(c=>c+" ("+g.regs[c]+")").join(", ");
function consHead(){return '<thead><tr><th></th><th>Company name</th><th>Company reg</th><th>Registers</th><th>Signals</th><th>Latest signal</th></tr></thead>'}
function consBody(groups,none){
  return "<tbody>"+groups.map(g=>{
    const L=g.latest, n=g.items.length;
    return `<tr class="cg"><td>${n?'<span class="lk tg" title="Show the individual entries">&#9656;</span>':""}</td><td>${esc(g.name)}</td>`
      +`<td>${g.num?`<span class="lk" data-n="${esc(g.num)}">${esc(g.num)}</span>`:"-"}</td><td>${esc(regList(g)||"-")}</td>`
      +`<td>${n?fmt(n):'<span class="muted">'+esc(none||"None")+"</span>"}</td><td>${L?dmy(L.date.slice(0,10))+" &middot; "+esc(L.type||""):"-"}</td></tr>`
      +(n?`<tr class="det" hidden><td></td><td colspan="5"><table class="in"><thead><tr><th>Register</th><th>Date</th><th>Signal</th><th>Detail</th><th></th></tr></thead><tbody>`
        +g.items.map(e=>`<tr><td><span class="code">${esc(e.code)}</span>${esc(e.regName)}</td><td>${e.date?dmy(e.date.slice(0,10)):"-"}</td><td>${esc(e.type)}</td><td>${esc(e.detail)}</td>`
          +`<td>${e.url?`<a class="lk" href="${esc(e.url)}" target="_blank" rel="noopener">Open</a>`:""}</td></tr>`).join("")
        +"</tbody></table></td></tr>":"")}).join("")+"</tbody>";
}
document.addEventListener("click",e=>{const t=e.target.closest(".tg");if(!t)return;
  const d=t.closest("tr").nextElementSibling;if(d&&d.classList.contains("det")){d.hidden=!d.hidden;t.innerHTML=d.hidden?"&#9656;":"&#9662;"}});

async function usrSearch(){
  const n=$("uName").value.trim(), c=$("uNum").value.trim();
  if(!n&&!c){usrClear(true);uMsg("Enter a company name, a company registration number, or both.");return}
  uMsg("Searching...");
  try{U=await (await fetch(`/api/search?name=${encodeURIComponent(n)}&number=${encodeURIComponent(c)}`)).json()}
  catch(e){uMsg("Cannot reach the tracker server.",true);return}
  if(U.error){$("uProf").innerHTML="";$("uSum").textContent="";$("uWrap").hidden=true;$("uRegW").hidden=true;uMsg(U.error,true);return}
  uMsg("");
  $("uReg").innerHTML='<option value="">All registers ('+fmt(U.rows.length)+')</option>'+U.registers.filter(r=>r.n).map(r=>`<option value="${esc(r.code)}">${esc(r.code)} (${fmt(r.n)})</option>`).join("");
  $("uRegW").hidden=!U.rows.length;
  renderUsr();
}
function renderUsr(){
  if(!U||U.error)return;
  $("uProf").innerHTML=U.profiles.map(p=>{
    const nf=p.fetch_status==="not_found";
    return `<div class="card"><h4>${esc(p.company_name||"(name not available)")} <span class="code">${esc(p.company_number)}</span></h4><dl>`
      +`<dt>Status</dt><dd>${nf?'<span class="err">Not found at Companies House</span>':esc(p.company_status||"-")}</dd>`
      +`<dt>Registered office</dt><dd>${esc(p.registered_office||"-")}</dd>`
      +`<dt>Trading address</dt><dd>${esc(p.trading_address||"-")}</dd>`
      +`<dt>Directors</dt><dd>${esc(p.directors||"-")}</dd>`
      +`<dt>PSC</dt><dd>${esc(p.pscs||"-")}</dd>`
      +`<dt>In registers</dt><dd>${esc(p.registers||"-")}</dd>`
      +`<dt>Master updated</dt><dd>${p.last_fetched_at?esc(String(p.last_fetched_at).slice(0,16).replace("T"," ")):"-"}</dd></dl></div>`}).join("");
  const f=$("uReg").value, rows=U.rows.filter(r=>!f||r[0]===f);
  const withData=U.registers.filter(r=>r.n).length, bad=U.registers.filter(r=>r.error);
  $("uSum").innerHTML=`${fmt(rows.length)} entr${rows.length===1?"y":"ies"} found in ${withData} of ${U.registers.length} registers`
    +(U.profiles.length?` · ${U.profiles.length} in company master`:" · not in company master")
    +(bad.length?` · <span class="err">not searched: ${bad.map(b=>esc(b.code)+" ("+esc(b.error)+")").join(", ")}</span>`:"")
    +(U.registers.some(r=>r.n>=U.limit)?` · each register shows its latest ${U.limit}`:"");
  $("uWrap").hidden=!rows.length;
  if(!rows.length){uMsg("No entries found in any register for this search.");$("uTbl").innerHTML="";return}
  uMsg("");
  const pn={};U.profiles.forEach(p=>{if(p.company_number&&p.company_name)pn[p.company_number]=p.company_name});
  const groups=consolidate(rows.map(r=>({code:r[0],regName:r[1],date:r[2],type:r[3],name:r[4],num:r[5],detail:r[6],url:r[7]})),pn);
  $("uSum").innerHTML+=` · ${fmt(groups.length)} compan${groups.length===1?"y":"ies"} (click &#9656; to see the entries)`;
  $("uTbl").innerHTML=consHead()+consBody(groups);
}
function usrClear(keepMsg){
  U=null;if(!keepMsg){$("uName").value="";$("uNum").value="";uMsg("")}
  $("uProf").innerHTML="";$("uSum").textContent="";$("uWrap").hidden=true;$("uRegW").hidden=true;$("uTbl").innerHTML="";
}
$("uGo").onclick=usrSearch;$("uClr").onclick=()=>{usrClear();$("uName").focus()};$("uReg").onchange=renderUsr;
for(const i of ["uName","uNum"])$(i).addEventListener("keydown",e=>{if(e.key==="Enter")usrSearch()});
$("uTbl").addEventListener("click",e=>{const l=e.target.closest(".lk[data-n]");if(l){$("uNum").value=l.dataset.n;$("uName").value="";usrSearch()}});
uMsg("Enter a company name, a company registration number, or both, then press Search.");

/* ---- CSV upload ---- */
let BROWS=[], BRES=null;
function parseCsv(t){
  t=t.replace(/^\uFEFF/,"");const first=(t.split(/\r?\n/,1)[0]||"");
  const sep=(first.split(";").length>first.split(",").length)?";":(first.split("\t").length>first.split(",").length?"\t":",");
  const rows=[];let row=[],cur="",q=false;
  for(let i=0;i<t.length;i++){const ch=t[i];
    if(q){if(ch==='"'){if(t[i+1]==='"'){cur+='"';i++}else q=false}else cur+=ch}
    else if(ch==='"')q=true;
    else if(ch===sep){row.push(cur);cur=""}
    else if(ch==="\n"||ch==="\r"){if(ch==="\r"&&t[i+1]==="\n")i++;row.push(cur);cur="";rows.push(row);row=[]}
    else cur+=ch}
  if(cur!==""||row.length){row.push(cur);rows.push(row)}
  return rows.filter(r=>r.some(c=>c.trim()!==""));
}
function csvToItems(rows){
  if(!rows.length)return [];
  const H=rows[0].map(h=>h.trim().toLowerCase());
  const find=f=>H.findIndex(f);
  const norm=h=>h.replace(/[_.\-]/g," ").replace(/\s+/g," ").trim();
  let iN=find(h=>/^(company |organisation |organization |business |supplier |firm )?name$|^company$/.test(norm(h)));
  let iR=find(h=>/^(company |co )?(reg|registration|registered)( (number|no|num))?$|^(company )?(number|no|num)$|^crn$|^reg no$/.test(norm(h)));
  let iF=find(h=>/^(date )?(from|start)( date)?$/.test(norm(h)));
  let iT=find(h=>/^(date )?(to|end|until)( date)?$/.test(norm(h)));
  let body=rows;
  if(iN<0&&iR<0&&iF<0&&iT<0){iN=0;iR=1;iF=2;iT=3}else body=rows.slice(1);
  const g=(r,i)=>i>=0&&i<r.length?r[i].trim():"";
  return body.map(r=>{let name=g(r,iN),number=g(r,iR);
    if(!number&&/^[A-Za-z]{0,2}\d{5,8}$/.test(name.replace(/\s/g,""))){number=name;name=""}   /* a one-column file of numbers */
    return {name,number,from:g(r,iF),to:g(r,iT)}}).filter(x=>x.name||x.number);
}
const bMsg=(t,err)=>{$("bMsg").innerHTML=t?`<div class="empty${err?" err":""}">${esc(t)}</div>`:""};
$("bFile").onchange=async e=>{
  const f=e.target.files[0];BRES=null;$("bWrap").hidden=true;$("bSum").textContent="";
  if(!f){BROWS=[];bEnable();bMsg("");return}
  try{BROWS=csvToItems(parseCsv(await f.text()))}catch(x){BROWS=[]}
  bEnable();
  bMsg(BROWS.length?`${fmt(BROWS.length)} compan${BROWS.length===1?"y":"ies"} read from ${f.name}. Press Submit.`:"No company names or numbers found in this file.",!BROWS.length);
};
function pasted(){
  const num=$("pNum").checked, t=$("pTxt").value;
  const parts=num?t.split(/[\r\n,;\t ]+/):t.split(/\r?\n/);
  const seen=new Set();
  return parts.map(x=>x.trim().replace(/^"|"$/g,"")).filter(x=>x&&!seen.has(x.toLowerCase())&&seen.add(x.toLowerCase()))
    .map(x=>num?{name:"",number:x,from:"",to:""}:{name:x,number:"",from:"",to:""});
}
function bEnable(){$("bGo").disabled=!(BROWS.length||pasted().length)}
$("pTxt").oninput=bEnable;$("pName").onchange=$("pNum").onchange=bEnable;
$("bGo").onclick=async()=>{
  const ITEMS=BROWS.concat(pasted());
  if(!ITEMS.length)return;
  $("bGo").disabled=true;bMsg("Checking "+fmt(ITEMS.length)+" companies in every register ... this can take a minute.");
  try{BRES=await (await fetch("/api/bulk",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rows:ITEMS,from:$("bFrom").value,to:$("bTo").value})})).json()}
  catch(e){bMsg("Cannot reach the tracker server.",true);$("bGo").disabled=false;return}
  bEnable();
  if(BRES.error){bMsg(BRES.error,true);$("bWrap").hidden=true;$("bSum").textContent="";return}
  bMsg("");renderBulk();
  if(BRES.not_found&&BRES.not_found.length){
    $("nfB").innerHTML=`<p style="margin:0 0 8px">${BRES.not_found.length===1?"This company was":"These companies were"} not found in the current monitor database (not in the company master and in no register):</p><ul style="margin:0;padding-left:18px">`
      +BRES.not_found.map(n=>`<li>&ldquo;${esc(n)}&rdquo;</li>`).join("")+"</ul>";
    $("shade").style.display="block";$("nfD").style.display="block";
  }
};
function bulkGroups(){
  return consolidate(BRES.rows.map(r=>({name:r[0],num:r[1],code:(r[2]||"").split(" ")[0],regName:(r[2]||"").split(" ").slice(1).join(" "),date:r[3]||"",type:r[4],detail:r[5],url:r[6],none:!r[2]})));
}
function renderBulk(){
  const R=BRES, G=bulkGroups(), nsig=R.rows.filter(r=>r[2]).length;
  $("bSum").innerHTML=`${fmt(R.companies)} compan${R.companies===1?"y":"ies"} checked · ${fmt(R.with_signals)} with signals · ${fmt(nsig)} signals`
    +(R.skipped.length?` · <span class="err">skipped: ${R.skipped.map(esc).join("; ")}</span>`:"")
    +(R.rows.length?` · <span class="lk" id="bCsv">Download summary CSV</span> · <span class="lk" id="bCsv2">Download all signals CSV</span>`:"");
  $("bWrap").hidden=!R.rows.length;
  $("bTbl").innerHTML=consHead()+consBody(G,"No signals"+(R.rows.some(r=>r[7])?" in this period":""));
  const q=v=>'"'+String(v??"").replace(/"/g,'""')+'"';
  const save=(lines,name)=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob(["\uFEFF"+lines.join("\r\n")],{type:"text/csv"}));a.download=name;a.click()};
  const b1=$("bCsv"),b2=$("bCsv2");
  if(b1)b1.onclick=()=>save([["Company name","Company reg","Registers","Signals","Latest signal date","Latest signal"].map(q).join(",")]
    .concat(G.map(g=>[g.name,g.num,regList(g),g.items.length,g.latest?g.latest.date.slice(0,10):"",g.latest?g.latest.type:""].map(q).join(","))),"signals_by_company.csv");
  if(b2)b2.onclick=()=>save([["Company name","Company reg","Register name","Date","Signal","Detail","Link","Period"].map(q).join(",")]
    .concat(R.rows.map(r=>[r[0],r[1],r[2],r[3]?r[3].slice(0,10):"",r[4]||(r[2]?"":r[5]),r[2]?r[5]:"",r[6],r[7]].map(q).join(","))),"signals_all.csv");
}
$("bTbl").addEventListener("click",e=>{const l=e.target.closest(".lk[data-n]");if(l){$("uNum").value=l.dataset.n;$("uName").value="";usrSearch()}});
$("bClr").onclick=()=>{$("pTxt").value="";$("bFile").value="";$("bFrom").value="";$("bTo").value="";BROWS=[];BRES=null;$("bGo").disabled=true;$("bWrap").hidden=true;$("bSum").textContent="";bMsg("")};
try{const t=localStorage.getItem("trkTab");if(t&&TABS[t])view(t)}catch(e){}
load();setInterval(load,120000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/tracker":
                if parse_qs(u.query).get("fresh"):
                    _cache["t"] = 0
                self._send(200, json.dumps(tracker_cached(), default=str), "application/json")
            elif u.path == "/api/search":
                q = parse_qs(u.query)
                self._send(200, json.dumps(build_search(q.get("name", [""])[0], q.get("number", [""])[0]), default=str), "application/json")
            elif u.path == "/api/drill":
                q = parse_qs(u.query)
                self._send(200, json.dumps(build_drill(q.get("code", [""])[0], q.get("day", [""])[0], q.get("sub", [""])[0]), default=str), "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e).splitlines()[0] if str(e) else "error"}), "application/json")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 3_000_000:
                self._send(413, json.dumps({"error": "File too large (limit about 3 MB)."}), "application/json")
                return
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if u.path == "/api/bulk":
                self._send(200, json.dumps(build_bulk(body), default=str), "application/json")
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:
            self._send(500, json.dumps({"error": str(e).splitlines()[0] if str(e) else "error"}), "application/json")

    def log_message(self, *a):
        pass


def ensure_financials():
    """Create the (empty) 15_company_financials tables and v_latest view if they are not there yet."""
    try:
        import register_financials as rf
        with psycopg.connect(**DB, autocommit=True) as con:
            con.execute(rf.DDL)
    except Exception as e:                              # master not built yet, or no rights: the drill-downs fall back to plain columns
        print("Note: financials tables not prepared:", str(e).splitlines()[0][:120])


def main():
    ensure_financials()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Alert Engine tracker running at {url}   (Ctrl+C to stop)", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
