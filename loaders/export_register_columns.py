"""
export_register_columns.py - writes AlertEngine_Register_Columns.xlsx: ONE SHEET PER REGISTER listing every table / view of that register and every column
(name, type, nullable) as it really is in your PostgreSQL database today - for the front-end team.

    python export_register_columns.py                    -> AlertEngine_Register_Columns.xlsx next to this script   (override: --out path)

* Reads information_schema, so columns the loaders add from the source files (CQC, Gazette, HMRC, sponsors ...) are included.
* Tables with exactly the same columns (for example one CQC table per monthly file) are shown once, with the others listed in "Notes".
* A new numbered schema (a new register) gets its own sheet automatically; add a line to REGISTER_OF below to give it a code and name.
* daily_update.py runs this every day, so the workbook is always in step with the database.
"""
import os
import sys
from collections import OrderedDict
from datetime import date
from pathlib import Path

import psycopg
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).resolve().parent
HOST = os.environ.get("PGHOST", "localhost")
PORT = os.environ.get("PGPORT", "5432")
USER = os.environ.get("PGUSER", "postgres")
DBNAME = os.environ.get("PGDATABASE", "postgres")

# schema -> (register code, register name).   A schema that is not listed still gets a sheet.
REGISTER_OF = OrderedDict([
    ("R1_cqc_care_directory", ("CQC-01", "CQC Care Directory")),
    ("R2_care_directory_with_filters", ("CQC-02", "Care Directory With Filters")),
    ("R3_care_directory_with_ratings", ("CQC-03", "Care Directory With Ratings")),
    ("R4_deactivated_locations", ("CQC-04", "Deactivated Locations")),
    ("R5_insolvency", ("INS-00", "Gazette core tables and views")),
    ("R6_company_master", ("COM-01", "Company master")),
    ("R7_corporate_notices", ("INS-02", "Corporate notices (Gazette)")),
    ("R8_personal_notices", ("INS-01", "Personal notices (Gazette)")),
    ("R9_other_notices", ("GAZ-01", "Other personal notices (Gazette)")),
    ("R10_hmrc_tax_defaulters", ("HMD-01", "HMRC Tax defaulters")),
    ("R11_sponsor_register", ("HOS-01", "Home Office licence sponsor register")),
    ("R12_contracts_finder", ("CON-01", "Contracts Finder")),
    ("R13_find_a_tender", ("FTS-01", "Find a Tender")),
    ("R14_payments_transparency", ("PTR-01", "Payments Transparency Register")),
    ("R15_company_financials", ("FIN-01", "Company financials (Companies House accounts)")),
])
HEAD = ["Register code", "Register", "Schema", "Table / view", "Kind", "Column no.", "Column name", "Data type", "Nullable", "Notes"]
WIDTH = [12, 34, 26, 44, 8, 11, 36, 20, 10, 70]
SQL = """SELECT c.table_schema, c.table_name, t.table_type, c.ordinal_position, c.column_name,
                CASE WHEN c.data_type = 'USER-DEFINED' THEN c.udt_name WHEN c.data_type = 'ARRAY' THEN c.udt_name || '[]' ELSE c.data_type END,
                c.is_nullable, obj_description((quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass)
         FROM information_schema.columns c JOIN information_schema.tables t USING (table_schema, table_name)
         WHERE c.table_schema ~ '^[0-9]+_'
         ORDER BY split_part(c.table_schema, '_', 1)::int, c.table_name, c.ordinal_position"""


def sheet_name(code, name, used):
    base = code or name                       # the sheet is named by the register code (CQC-01, FTS-01 ...); the Index sheet gives the full names
    for ch in '[]:*?/\\':
        base = base.replace(ch, "")
    base, n = base[:31], 1
    while base in used:
        n += 1
        base = base[:28] + f" {n}"
    used.add(base)
    return base


def build(out):
    with psycopg.connect(host=HOST, port=PORT, user=USER, dbname=DBNAME, autocommit=True) as con:
        rows = con.execute(SQL).fetchall()
    tables = OrderedDict()                # schema -> table -> {kind, comment, cols}
    for sch, tab, kind, pos, col, typ, nul, com in rows:
        t = tables.setdefault(sch, OrderedDict()).setdefault(tab, {"kind": "view" if kind == "VIEW" else "table", "comment": com, "cols": []})
        t["cols"].append((pos, col, typ, nul))
    schemas = list(REGISTER_OF) + [s for s in tables if s not in REGISTER_OF]
    wb = Workbook()
    idx = wb.active
    idx.title = "Index"
    idx.append(["Register code", "Register", "Schema", "Sheet", "Tables / views", "Columns", "Status"])
    used = {"Index"}
    font = "Calibri"

    def style_head(ws, widths):
        for c in ws[1]:
            c.font = Font(name=font, size=11, bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F5F8B")
            c.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[1].height = 28.8
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    for sch in schemas:
        code, name = REGISTER_OF.get(sch, ("", sch.split("_", 1)[1].replace("_", " ").title() if "_" in sch else sch))
        ws = wb.create_sheet(sheet_name(code, name, used))
        ws.append(HEAD)
        tabs = tables.get(sch)
        if not tabs:
            ws.append([code, name, sch, "", "", None, "", "", "", "This schema is not in the database yet - run the register once, then run export_register_columns.py again."])
            idx.append([code, name, sch, ws.title, 0, 0, "not in database yet"])
        else:
            groups = OrderedDict()                          # same columns -> one block
            for tab, t in tabs.items():
                sig = (t["kind"], tuple((c[1], c[2], c[3]) for c in t["cols"]))
                groups.setdefault(sig, []).append((tab, t))
            ncol = 0
            for sig, members in groups.items():
                tab, t = members[0]
                note = (t["comment"] or "")
                if len(members) > 1:
                    others = [m[0] for m in members[1:]]
                    note = (note + "  |  " if note else "") + f"Same columns in {len(others)} more: " + ", ".join(others[:12]) + (" ..." if len(others) > 12 else "")
                for pos, col, typ, nul in t["cols"]:
                    ws.append([code, name, sch, tab, t["kind"], pos, col, typ, "yes" if nul == "YES" else "no", note if pos == t["cols"][0][0] else ""])
                    ncol += 1
            idx.append([code, name, sch, ws.title, len(tabs), ncol, "ok"])
        style_head(ws, WIDTH)
        for r in ws.iter_rows(min_row=2):
            for c in r:
                c.font = Font(name=font, size=11)
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.auto_filter.ref = ws.dimensions
    style_head(idx, [14, 40, 28, 34, 15, 10, 22])
    for r in idx.iter_rows(min_row=2):
        for c in r:
            c.font = Font(name=font, size=11)
    idx.append([])
    idx.append([f"Generated {date.today().isoformat()} from database '{DBNAME}'. One sheet per register; columns come straight from PostgreSQL."])
    wb.save(out)
    return len(schemas)


if __name__ == "__main__":
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else HERE / "AlertEngine_Register_Columns.xlsx"
    try:
        n = build(out)
    except psycopg.OperationalError as e:
        print(f"Cannot reach the database: {e}")
        sys.exit(1)
    print(f"Wrote {out}: {n} register sheets.")
