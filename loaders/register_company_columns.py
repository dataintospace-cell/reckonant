r"""register_company_columns.py -- every register gets the company registration number and company name as COLUMNS.

  python register_company_columns.py            (re)build the mapped views  - daily_update.py runs it after the company master
  python register_company_columns.py list       show every register and its view, and how many entries are mapped

WHAT IT MAKES
  For each register a view  "<schema>"."v_<table>_mapped"  =  ALL the register's own columns  +
        mapped_company_number   the Companies House number(s) of the company the entry belongs to   (several are joined with "; ")
        mapped_company_name     that company's name from the company master
        mapped_how              number (the register states the number) | name (matched by exact name) | number; name
  The views are live: whatever the loaders and the company master add or change shows at once.  Your front end reads these views.

      Register                       View
      INS-01 / INS-02 / GAZ-01       "R5_insolvency".v_gazette_notice_mapped
      HMD-01 HMRC defaulters         "R10_hmrc_tax_defaulters".v_defaulter_mapped
      HOS-01 Sponsors                "R11_sponsor_register".v_sponsor_mapped
      CON-01 Contracts Finder        "R12_contracts_finder".v_notice_mapped
      FTS-01 Find a Tender           "R13_find_a_tender".v_notice_mapped
      PTR-01 Payments Transparency   "R14_payments_transparency".v_payment_mapped
      CQC-01 .. CQC-04               "R6_company_master".v_cqc_mapped     (CQC has no company numbers: matched by provider / location name)
      COM-01 company master          is the company master itself (company_number, company_name)
      FIN-01 financials              has company_number and company_name columns in every table

ADDING A REGISTER IN FUTURE
  1. add its tracker block in alert_tracker_ui.py (as before),
  2. add one line to SOURCES in register_company_master.py (how its entries reach a company number or name),
  3. add one dict to MAPPED below (its table, its entry id column, and any column that already holds a company number).
  make_register_sheet.py stops with "NOT MAPPED" when a tracker register has no line here, so a register cannot be forgotten.
"""
import os
import sys

import psycopg
from psycopg import sql

DB = dict(host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"),
          dbname=os.environ.get("PGDATABASE", "postgres"), user=os.environ.get("PGUSER", "postgres"))
MASTER = "R6_company_master"

# codes: register codes sharing the table | schema, table | key: SQL for the entry id, as the company master stored it in register_link.source_key
# direct_sql: optional - a sub-select (t = the entry row) returning company numbers the register itself states
MAPPED = [
    dict(codes=["INS-01", "INS-02", "GAZ-01"], schema="R5_insolvency", table="gazette_notice", key="t.notice_id",
         direct_sql="SELECT t.company_number WHERE t.company_number IS NOT NULL AND t.company_number <> ''"),
    dict(codes=["HMD-01"], schema="R10_hmrc_tax_defaulters", table="v_defaulter", key='t."Defaulter ID"'),
    dict(codes=["HOS-01"], schema="R11_sponsor_register", table="v_sponsor", key='t."Sponsor ID"'),
    dict(codes=["CON-01"], schema="R12_contracts_finder", table="notice", key='t."Release ID"',
         direct_sql='''SELECT s.company_number FROM "R12_contracts_finder".supplier s WHERE s."Release ID" = t."Release ID" AND s.company_number IS NOT NULL AND s.company_number <> '' '''),
    dict(codes=["FTS-01"], schema="R13_find_a_tender", table="notice", key='t."Release ID"',
         direct_sql='''SELECT s.company_number FROM "R13_find_a_tender".supplier s WHERE s."Release ID" = t."Release ID" AND s.company_number IS NOT NULL AND s.company_number <> '' '''),
    dict(codes=["PTR-01"], schema="R14_payments_transparency", table="payment", key='t."Payment ID"',
         direct_sql="SELECT t.company_number WHERE t.company_number IS NOT NULL AND t.company_number <> ''"),
]
# registers that ARE company data (nothing to map) or are mapped by name in v_cqc_mapped
CQC_CODES = ["CQC-01", "CQC-02", "CQC-03", "CQC-04"]
SELF_MAPPED = ["COM-01", "FIN-01"]


def covered_codes():
    return {c for m in MAPPED for c in m["codes"]} | set(CQC_CODES) | set(SELF_MAPPED)


def view_sql(m):
    S, M = sql.Identifier(m["schema"]), sql.Identifier(MASTER)
    codes = sql.SQL(", ").join(sql.Literal(c) for c in m["codes"])
    direct = sql.SQL(" UNION SELECT d.cn, 'number' FROM (" + m["direct_sql"] + ") d(cn)") if m.get("direct_sql") else sql.SQL("")
    return sql.SQL('''CREATE VIEW {s}.{v} AS
SELECT t.*, x.cn AS mapped_company_number, x.nm AS mapped_company_name, x.how AS mapped_how
FROM {s}.{t} t
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT u.cn, '; ' ORDER BY u.cn) AS cn, string_agg(DISTINCT c.company_name, '; ' ORDER BY c.company_name) AS nm,
           string_agg(DISTINCT u.how, '; ' ORDER BY u.how) AS how
    FROM (SELECT rl.company_number AS cn, rl.how FROM {m}.register_link rl WHERE rl.source_code IN ({codes}) AND rl.source_key = ({key})::text
          {direct}) u
    LEFT JOIN {m}.company c ON c.company_number = u.cn) x ON true''').format(
        s=S, t=sql.Identifier(m["table"]), v=sql.Identifier(view_name(m)), m=M, codes=codes, key=sql.SQL(m["key"]), direct=direct)


def view_name(m):
    t = m["table"]
    return "v_" + (t[2:] if t.startswith("v_") else t) + "_mapped"


CQC_VIEW = f'''CREATE VIEW "{MASTER}".v_cqc_mapped AS
SELECT r.source_code AS register_code, split_part(r.source_key, '|', 1) AS entry_key, r.provider_name, r.location_name, r.address, r.postcode,
       x.cn AS mapped_company_number, x.nm AS mapped_company_name, x.how AS mapped_how
FROM "{MASTER}".register_name r
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT m.company_number, '; ' ORDER BY m.company_number) AS cn, string_agg(DISTINCT c.company_name, '; ' ORDER BY c.company_name) AS nm, 'name'::text AS how
    FROM "{MASTER}".name_match m LEFT JOIN "{MASTER}".company c ON c.company_number = m.company_number
    WHERE m.status = 'matched' AND ((m.kind = 'provider' AND m.name_norm = r.provider_norm)
                                OR (m.kind = 'location' AND m.name_norm = r.location_norm AND m.match_postcode = r.postcode_norm))
      AND (m.detail IS DISTINCT FROM 'postcode' OR m.match_postcode = r.postcode_norm)) x ON true'''


def exists(con, schema, table):
    return con.execute("SELECT to_regclass(%s)", (f'"{schema}"."{table}"',)).fetchone()[0] is not None


def build(con):
    done = []
    for m in MAPPED:
        v = view_name(m)
        if not exists(con, m["schema"], m["table"]) or not exists(con, MASTER, "register_link"):
            print(f"  {m['codes'][0]:<7} skipped: {m['schema']}.{m['table']} (or the company master) does not exist yet")
            continue
        con.execute(sql.SQL("DROP VIEW IF EXISTS {}.{}").format(sql.Identifier(m["schema"]), sql.Identifier(v)))
        con.execute(view_sql(m))
        done.append((", ".join(m["codes"]), m["schema"], v))
    if exists(con, MASTER, "register_name") and exists(con, MASTER, "name_match"):
        con.execute(f'DROP VIEW IF EXISTS "{MASTER}".v_cqc_mapped')
        con.execute(CQC_VIEW)
        done.append((", ".join(CQC_CODES), MASTER, "v_cqc_mapped"))
    return done


def show(con, done):
    print(f"{'Register':<22}{'View':<52}{'Entries':>10}{'Mapped':>10}")
    for codes, schema, v in done:
        total, mapped = con.execute(sql.SQL("SELECT count(*), count(mapped_company_number) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(v))).fetchone()
        print(f"{codes:<22}{schema + '.' + v:<52}{total:>10,}{mapped:>10,}")


def main():
    with psycopg.connect(**DB, password=os.environ.get("PGPASSWORD") or None, autocommit=True) as con:
        done = build(con)
        print(f"Company number / name columns: {len(done)} views built.")
        if len(sys.argv) > 1 and sys.argv[1] == "list":
            show(con, done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
