"""
Reckonant: reads the Signals Feeder Engine's Postgres registers.

Every function takes an open psycopg connection and a list of company numbers, and returns
{company_number: [rows]} with each row as a dict keyed by the database's own column names.
Tables that don't exist yet are skipped with a warning, so the export keeps working while
registers are being added.

Schema and table names come from AlertEngine_Register_Columns.xlsx (generated 2026-11-10).
If a register is renamed or moved, change it in SCHEMAS below and nothing else.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

log = logging.getLogger("reckonant")

# ── where each register lives ─────────────────────────────────────────────────
SCHEMAS = {
    "COM-01": "6_company_master",
    "CQC-01": "1_cqc_care_directory",
    "CQC-02": "2_care_directory_with_filters",
    "CQC-03": "3_care_directory_with_ratings",
    "CQC-04": "4_deactivated_locations",
    "INS-00": "5_insolvency",
    "INS-02": "7_corporate_notices",
    "INS-01": "8_personal_notices",
    "GAZ-01": "9_other_notices",
    "HMD-01": "10_hmrc_tax_defaulters",
    "HOS-01": "11_sponsor_register",
    "CON-01": "12_contracts_finder",
    "FTS-01": "13_find_a_tender",
    "PTR-01": "14_payments_transparency",
    "FIN-01": "15_company_financials",   # view v_latest: latest accounts per company
    "CQC-API": "16_cqc_api",             # CQC API: provider, location, change, checkpoint, run_log
}
# register_link.source_code values used to match registers that have no company number.
# ASSUMPTION (check with the data team): source_key holds that register's own id.
LINK_CODES = {"HMD-01": "HMD-01", "HOS-01": "HOS-01", "CQC": "CQC-%"}

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}


def Q(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def norm_num_sql(col: sql.Composable) -> sql.Composed:
    """Company numbers as Companies House writes them: upper case, digits zero-padded to 8."""
    return sql.SQL("(CASE WHEN trim({c}) ~ '^[0-9]+$' THEN lpad(trim({c}), 8, '0') ELSE upper(trim({c})) END)").format(c=col)


def norm_num(n) -> str:
    s = str(n or "").strip().upper()
    return s.zfill(8) if s.isdigit() else s


# ── table discovery ───────────────────────────────────────────────────────────
class Catalog:
    """Knows which tables exist, and finds the newest dated snapshot or diff of a CQC file."""

    def __init__(self, conn: psycopg.Connection):
        with conn.cursor() as cur:
            cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                        "WHERE table_schema ~ '^[0-9]+_'")
            self.tables: dict[str, set[str]] = {}
            for s, t in cur.fetchall():
                self.tables.setdefault(s, set()).add(t)

    def has(self, schema: str, table: str) -> bool:
        return table in self.tables.get(schema, set())

    @staticmethod
    def _date(day: str, month: str, year: str) -> date | None:
        try:
            return date(int(year), MONTHS[month.lower()], int(day))
        except (KeyError, ValueError):
            return None

    def latest_snapshot(self, schema: str, suffix: str) -> tuple[str, date] | None:
        """'02_november_2026_hsca_active_locations' beats '01_october_2026_hsca_active_locations'."""
        best = None
        for t in self.tables.get(schema, set()):
            m = re.match(r"^(\d{1,2})_([a-z]+)_(\d{4})_(.+)$", t)
            if m and m.group(4) == suffix:
                d = self._date(*m.group(1, 2, 3))
                if d and (best is None or d > best[1]):
                    best = (t, d)
        return best

    def diffs(self, schema: str, suffix: str, since: date) -> list[tuple[str, date]]:
        """Month-to-month change tables ending on or after `since`, oldest first.
        Postgres cuts names at 63 characters, so the suffix is matched by its start."""
        out = []
        for t in self.tables.get(schema, set()):
            m = re.match(r"^diff_\d{1,2}_[a-z]+_\d{4}_(\d{1,2})_([a-z]+)_(\d{4})_(.+)$", t)
            if m and suffix.startswith(m.group(4)):
                d = self._date(*m.group(1, 2, 3))
                if d and d >= since:
                    out.append((t, d))
        return sorted(out, key=lambda x: x[1])


def _rows(conn, query, params) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _group(rows: list[dict], key: str = "company_number") -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(norm_num(r.get(key)), []).append(r)
    return out


_WARNED: set[str] = set()


def _skip(code: str, what: str) -> dict:
    if (code, what) not in _WARNED:  # once per run, not once per batch
        _WARNED.add((code, what))
        log.warning("%s: %s not found, skipped", code, what)
    return {}


# ── Companies House (COM-01) ──────────────────────────────────────────────────
def company_numbers(conn, cat: Catalog, registers: list[str] | None = None, limit: int | None = None,
                    only: list[str] | None = None) -> list[str]:
    """Which companies to export. All of COM-01 by default; `registers` keeps only companies that
    appear on those registers (company_source.register_code), `only` picks exact numbers."""
    s = SCHEMAS["COM-01"]
    q = sql.SQL("SELECT c.company_number FROM {co} c").format(co=Q(s, "company"))
    where, params = [], []
    if only:
        where.append(sql.SQL("c.company_number = ANY(%s)")); params.append([norm_num(x) for x in only])
    if registers and cat.has(s, "company_source"):
        where.append(sql.SQL("EXISTS (SELECT 1 FROM {src} x WHERE x.company_number = c.company_number "
                             "AND x.register_code = ANY(%s))").format(src=Q(s, "company_source")))
        params.append(registers)
    if where:
        q += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where)
    # companies with financials come first, so the cap keeps the ones the accounts can be shown for
    fin = SCHEMAS["FIN-01"]
    if not only and cat.has(fin, "v_latest"):
        q += sql.SQL(" ORDER BY EXISTS (SELECT 1 FROM {f} f WHERE f.company_number = c.company_number) DESC, "
                     "c.company_number").format(f=Q(fin, "v_latest"))
    else:
        q += sql.SQL(" ORDER BY c.company_number")
    if limit:
        q += sql.SQL(" LIMIT %s"); params.append(limit)
    return [r["company_number"] for r in _rows(conn, q, params)]


def companies(conn, cat: Catalog, nums: list[str]) -> dict[str, dict]:
    s = SCHEMAS["COM-01"]
    rows = _rows(conn, sql.SQL("SELECT * FROM {} WHERE company_number = ANY(%s)").format(Q(s, "company")), [nums])
    return {norm_num(r["company_number"]): r for r in rows}


def officers(conn, cat, nums):
    s = SCHEMAS["COM-01"]
    if not cat.has(s, "company_officer"):
        return _skip("COM-01", "company_officer")
    return _group(_rows(conn, sql.SQL(
        "SELECT company_number, officer_key, officer_name, officer_role, is_director, appointed_on, resigned_on, "
        "nationality FROM {} WHERE company_number = ANY(%s) ORDER BY appointed_on DESC NULLS LAST"
    ).format(Q(s, "company_officer")), [nums]))


def pscs(conn, cat, nums):
    s = SCHEMAS["COM-01"]
    if not cat.has(s, "company_psc"):
        return _skip("COM-01", "company_psc")
    return _group(_rows(conn, sql.SQL(
        "SELECT company_number, psc_key, psc_name, psc_kind, notified_on, ceased_on, ceased, natures_of_control "
        "FROM {} WHERE company_number = ANY(%s) ORDER BY notified_on DESC NULLS LAST"
    ).format(Q(s, "company_psc")), [nums]))


def change_log(conn, cat, nums, since: date):
    s = SCHEMAS["COM-01"]
    if not cat.has(s, "company_change_log"):
        return _skip("COM-01", "company_change_log")
    return _group(_rows(conn, sql.SQL(
        "SELECT company_number, change_type, detail, detected_date FROM {} "
        "WHERE company_number = ANY(%s) AND detected_date >= %s ORDER BY detected_date DESC"
    ).format(Q(s, "company_change_log")), [nums, since]))


def register_codes(conn, cat, nums):
    s = SCHEMAS["COM-01"]
    if not cat.has(s, "company_source"):
        return _skip("COM-01", "company_source")
    return _group(_rows(conn, sql.SQL(
        "SELECT company_number, register_code, first_seen_at FROM {} WHERE company_number = ANY(%s)"
    ).format(Q(s, "company_source")), [nums]))


def _linked_keys(conn, cat, nums, code_pattern: str) -> dict[str, list[str]]:
    """register_link: (source_code, source_key) -> company_number, for registers without company numbers."""
    s = SCHEMAS["COM-01"]
    if not cat.has(s, "register_link"):
        return {}
    rows = _rows(conn, sql.SQL(
        "SELECT company_number, source_key, how FROM {} WHERE company_number = ANY(%s) AND source_code LIKE %s"
    ).format(Q(s, "register_link")), [nums, code_pattern])
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(str(r["source_key"]), []).append(norm_num(r["company_number"]))
    return out


# ── CQC (CQC-02 locations + monthly changes, CQC-04 closures) ─────────────────
CQC_LOCATION_COLS = ["location_id", "location_name", "care_home", "care_homes_beds", "dormant_y_n",
                     "location_type_sector", "location_primary_inspection_category",
                     "location_latest_overall_rating", "publication_date", "location_hsca_start_date",
                     "location_city", "location_postal_code", "location_local_authority", "location_region",
                     "provider_id", "provider_name", "provider_web_address", "provider_companies_house_number"]


def cqc_locations(conn, cat, nums) -> tuple[dict, str]:
    s = SCHEMAS["CQC-02"]
    snap = cat.latest_snapshot(s, "hsca_active_locations")
    if not snap:
        return _skip("CQC-02", "a dated hsca_active_locations table"), ""
    table, _ = snap
    links = _linked_keys(conn, cat, nums, LINK_CODES["CQC"])
    num_expr = norm_num_sql(sql.Identifier("provider_companies_house_number"))
    cols = sql.SQL(", ").join(sql.Identifier(c) for c in CQC_LOCATION_COLS)
    rows = _rows(conn, sql.SQL("SELECT {cols}, {n} AS company_number FROM {t} WHERE {n} = ANY(%s) OR provider_id = ANY(%s)")
                 .format(cols=cols, n=num_expr, t=Q(s, table)), [nums, list(links)])
    for r in rows:  # providers linked by register_link instead of their own column
        if r["company_number"] not in nums and str(r["provider_id"]) in links:
            r["company_number"] = links[str(r["provider_id"])][0]
    return _group(rows), table


def cqc_changes(conn, cat, nums, since: date) -> dict:
    """Location added / removed / rating changed, from the month-to-month diff tables."""
    s = SCHEMAS["CQC-02"]
    diffs = cat.diffs(s, "hsca_active_locations", since)
    if not diffs:
        return _skip("CQC-02", "diff_..._hsca_active_locations tables")
    out: dict[str, list[dict]] = {}
    for table, end in diffs:
        n_curr = norm_num_sql(sql.Identifier("provider_companies_house_number_curr"))
        n_prev = norm_num_sql(sql.Identifier("provider_companies_house_number_prev"))
        rows = _rows(conn, sql.SQL(
            "SELECT change_type, key_value, changed_columns, location_name_curr, location_name_prev, "
            "location_latest_overall_rating_prev, location_latest_overall_rating_curr, care_homes_beds_curr, "
            "location_city_curr, location_city_prev, COALESCE({nc}, {np}) AS company_number "
            "FROM {t} WHERE {nc} = ANY(%s) OR {np} = ANY(%s)"
        ).format(nc=n_curr, np=n_prev, t=Q(s, table)), [nums, nums])
        for r in rows:
            r["diff_date"] = end
            out.setdefault(norm_num(r["company_number"]), []).append(r)
    return out


def cqc_closed(conn, cat, nums) -> dict:
    s = SCHEMAS["CQC-04"]
    snap = cat.latest_snapshot(s, "deactivated_locations")
    if not snap:
        return _skip("CQC-04", "a dated deactivated_locations table")
    n = norm_num_sql(sql.Identifier("provider_companies_house_number"))
    return _group(_rows(conn, sql.SQL(
        "SELECT location_id, location_name, location_hsca_end_date, care_homes_beds_at_point_location_de_activated, "
        "location_city, location_latest_overall_rating, {n} AS company_number FROM {t} WHERE {n} = ANY(%s)"
    ).format(n=n, t=Q(s, snap[0])), [nums]))


# ── Gazette (INS-00 view) and directors' personal insolvency (COM-01 person_link) ──
def gazette(conn, cat, nums):
    s = SCHEMAS["INS-00"]
    if not cat.has(s, "v_insolvency_notice"):
        return _skip("INS-00", "v_insolvency_notice")
    return _group(_rows(conn, sql.SQL(
        "SELECT notice_id, published_date, insolvency_kind, group_name, notice_code, type_label, title, url, "
        "{n} AS company_number FROM {t} WHERE {n} = ANY(%s) ORDER BY published_date DESC"
    ).format(n=norm_num_sql(sql.Identifier("company_number")), t=Q(s, "v_insolvency_notice")), [nums]))


def director_insolvency(conn, cat, nums):
    s = SCHEMAS["COM-01"]
    if not cat.has(s, "person_link"):
        return _skip("COM-01", "person_link")
    return _group(_rows(conn, sql.SQL(
        "SELECT company_number, source_code, source_ref, person_name, officer_name, officer_role, resigned_on, "
        "notice_date, match_level FROM {} WHERE company_number = ANY(%s) ORDER BY notice_date DESC NULLS LAST"
    ).format(Q(s, "person_link")), [nums]))


# ── HMRC defaulters (HMD-01) and Home Office sponsors (HOS-01): matched by register_link ──
def _by_link(conn, cat, nums, code, schema, view, id_col) -> dict:
    if not cat.has(schema, view):
        return _skip(code, view)
    links = _linked_keys(conn, cat, nums, LINK_CODES[code])
    if not links:
        return {}
    rows = _rows(conn, sql.SQL("SELECT * FROM {t} WHERE {id}::text = ANY(%s)")
                 .format(t=Q(schema, view), id=sql.Identifier(id_col)), [list(links)])
    out: dict[str, list[dict]] = {}
    for r in rows:
        for n in links.get(str(r[id_col]), []):
            out.setdefault(n, []).append(r)
    return out


def hmrc_defaults(conn, cat, nums):
    return _by_link(conn, cat, nums, "HMD-01", SCHEMAS["HMD-01"], "v_defaulter", "Defaulter ID")


def sponsors(conn, cat, nums):
    return _by_link(conn, cat, nums, "HOS-01", SCHEMAS["HOS-01"], "v_sponsor", "Sponsor ID")


def sponsor_changes(conn, cat, nums, since: date):
    s = SCHEMAS["HOS-01"]
    if not cat.has(s, "change_log"):
        return _skip("HOS-01", "change_log")
    links = _linked_keys(conn, cat, nums, LINK_CODES["HOS-01"])
    if not links:
        return {}
    rows = _rows(conn, sql.SQL("SELECT sponsor_id, change_type, detail, detected_date FROM {} "
                               "WHERE sponsor_id::text = ANY(%s) AND detected_date >= %s ORDER BY detected_date DESC")
                 .format(Q(s, "change_log")), [list(links), since])
    out: dict[str, list[dict]] = {}
    for r in rows:
        for n in links.get(str(r["sponsor_id"]), []):
            out.setdefault(n, []).append(r)
    return out


# ── public contracts (CON-01, FTS-01) and payments (PTR-01) ───────────────────
def contracts(conn, cat, nums) -> dict:
    out: dict[str, list[dict]] = {}
    for code, notice in (("CON-01", "notice"), ("FTS-01", "v_notice")):
        s = SCHEMAS[code]
        if not cat.has(s, "supplier"):
            _skip(code, "supplier"); continue
        nt = notice if cat.has(s, notice) else ("notice" if cat.has(s, "notice") else None)
        join = sql.SQL(" LEFT JOIN {n} n ON n.{rid} = s.{rid}").format(n=Q(s, nt), rid=sql.Identifier("Release ID")) if nt else sql.SQL("")
        extra = sql.SQL(', n."Title", n."Buyer", n."Procurement category", n."CPV", n."url"') if nt else sql.SQL("")
        rows = _rows(conn, sql.SQL(
            'SELECT s."Release ID", s."OCID", s."Award ID", s."Supplier name", s."Award value", s."Currency", '
            's."Award date", s."Award status", s."Contract start", s."Contract end"{extra}, {n} AS company_number '
            "FROM {sup} s{join} WHERE {n} = ANY(%s)"
        ).format(extra=extra, n=norm_num_sql(sql.SQL("s.company_number")), sup=Q(s, "supplier"), join=join), [nums])
        for r in rows:
            r["register"] = code
            out.setdefault(norm_num(r["company_number"]), []).append(r)
    return out


def payments(conn, cat, nums):
    s = SCHEMAS["PTR-01"]
    if not cat.has(s, "payment"):
        return _skip("PTR-01", "payment")
    return _group(_rows(conn, sql.SQL(
        'SELECT "Payment ID", "Title", "Contracting authority", "Payment value", "Payment date", "url", '
        "{n} AS company_number FROM {t} WHERE {n} = ANY(%s) ORDER BY \"Payment date\" DESC NULLS LAST"
    ).format(n=norm_num_sql(sql.Identifier("company_number")), t=Q(s, "payment")), [nums]))


def open_tenders(conn, cat, today: date, limit: int = 500) -> list[dict]:
    """Tenders still open for bids, newest first (not tied to one company)."""
    out: list[dict] = []
    for code, notice in (("FTS-01", "v_notice"), ("CON-01", "notice")):
        s = SCHEMAS[code]
        if not cat.has(s, notice):
            _skip(code, notice); continue
        open_test = (sql.SQL('(lower(coalesce("Open opportunity", \'\')) IN (\'true\', \'yes\', \'y\', \'1\') OR ')
                     if notice == "v_notice" else sql.SQL("("))
        rows = _rows(conn, sql.SQL(
            'SELECT "Release ID", "Title", "Buyer", "Buyer town", "Value", "Value max", "Currency", "Tender end", '
            '"Region", "CPV", "Procurement category", "SME suitable", "url", "Published" FROM {t} WHERE '
            '{open} ("Tender end" >= %s AND lower(coalesce("Stage", \'\')) LIKE \'%%tender%%\')) '
            'ORDER BY "Published" DESC NULLS LAST LIMIT %s'
        ).format(t=Q(s, notice), open=open_test), [today, limit])
        for r in rows:
            r["register"] = code
        out += rows
    return out[:limit]


def financials(conn, cat, nums) -> dict[str, dict]:
    """Latest accounts per company from 15_company_financials.v_latest (one row per company)."""
    s = SCHEMAS["FIN-01"]
    if not cat.has(s, "v_latest"):
        return _skip("FIN-01", "v_latest")
    rows = _rows(conn, sql.SQL("SELECT * FROM {} WHERE company_number = ANY(%s)").format(Q(s, "v_latest")), [nums])
    out: dict[str, dict] = {}
    for r in rows:  # if a company has more than one row, keep the latest period
        k = norm_num(r["company_number"])
        if k not in out or str(r.get("period_end") or "") > str(out[k].get("period_end") or ""):
            out[k] = r
    return out


# ── CQC API register (16_cqc_api): providers, locations, changes and run status ──
def cqc_api_providers(conn, cat, nums) -> dict[str, list[dict]]:
    s = SCHEMAS["CQC-API"]
    if not cat.has(s, "provider"):
        return _skip("CQC-API", "provider")
    n = norm_num_sql(sql.Identifier("companies_house_number"))
    return _group(_rows(conn, sql.SQL("SELECT *, {n} AS company_number FROM {t} WHERE {n} = ANY(%s)")
                        .format(n=n, t=Q(s, "provider")), [nums]))


def cqc_api_locations(conn, cat, providers: dict) -> dict[str, list[dict]]:
    s = SCHEMAS["CQC-API"]
    if not cat.has(s, "location"):
        return _skip("CQC-API", "location")
    pid_to_co = {str(p["provider_id"]): co for co, rows in providers.items() for p in rows if p.get("provider_id")}
    if not pid_to_co:
        return {}
    rows = _rows(conn, sql.SQL("SELECT * FROM {t} WHERE provider_id = ANY(%s)").format(t=Q(s, "location")),
                 [list(pid_to_co)])
    for r in rows:
        r["company_number"] = pid_to_co.get(str(r.get("provider_id")))
    return _group(rows)


def cqc_api_changes(conn, cat, providers: dict, locations: dict, since: date) -> dict[str, list[dict]]:
    s = SCHEMAS["CQC-API"]
    if not cat.has(s, "change"):
        return _skip("CQC-API", "change")
    id_to_co = {str(p["provider_id"]): co for co, rows in providers.items() for p in rows if p.get("provider_id")}
    id_to_co.update({str(l["location_id"]): co for co, rows in locations.items() for l in rows if l.get("location_id")})
    if not id_to_co:
        return {}
    rows = _rows(conn, sql.SQL("SELECT run_at, org_type, org_id FROM {t} WHERE run_at >= %s AND org_id = ANY(%s)")
                 .format(t=Q(s, "change")), [since, list(id_to_co)])
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(id_to_co[str(r["org_id"])], []).append(r)
    return out


def cqc_api_status(conn, cat) -> dict:
    """Latest run_log row and checkpoint rows, so the export can say how fresh the API data is."""
    s = SCHEMAS["CQC-API"]
    out = {}
    if cat.has(s, "run_log"):
        rows = _rows(conn, sql.SQL("SELECT * FROM {t} ORDER BY run_id DESC LIMIT 1").format(t=Q(s, "run_log")), [])
        out["run_log"] = rows[0] if rows else {}
    if cat.has(s, "checkpoint"):
        out["checkpoint"] = _rows(conn, sql.SQL("SELECT * FROM {t}").format(t=Q(s, "checkpoint")), [])
    return out


def load_all(conn, nums: list[str], cat: Catalog, today: date, lookback_days: int = 365) -> dict:
    """Everything the export needs for one batch of companies."""
    since = today - timedelta(days=lookback_days)
    cqc, cqc_table = cqc_locations(conn, cat, nums)
    api_providers = cqc_api_providers(conn, cat, nums)
    api_locations = cqc_api_locations(conn, cat, api_providers)
    return {
        "companies": companies(conn, cat, nums),
        "officers": officers(conn, cat, nums),
        "pscs": pscs(conn, cat, nums),
        "changes": change_log(conn, cat, nums, since),
        "registers": register_codes(conn, cat, nums),
        "cqc": cqc, "cqc_table": cqc_table,
        "cqc_changes": cqc_changes(conn, cat, nums, since),
        "cqc_closed": cqc_closed(conn, cat, nums),
        "gazette": gazette(conn, cat, nums),
        "director_insolvency": director_insolvency(conn, cat, nums),
        "hmrc": hmrc_defaults(conn, cat, nums),
        "sponsor": sponsors(conn, cat, nums),
        "sponsor_changes": sponsor_changes(conn, cat, nums, since),
        "contracts": contracts(conn, cat, nums),
        "payments": payments(conn, cat, nums),
        "fin": financials(conn, cat, nums),
        "cqc_api_providers": api_providers,
        "cqc_api_locations": api_locations,
        "cqc_api_changes": cqc_api_changes(conn, cat, api_providers, api_locations, since),
        "cqc_api_status": cqc_api_status(conn, cat),
    }
