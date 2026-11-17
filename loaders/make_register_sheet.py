"""
make_register_sheet.py - writes the Alert Engine register/API spreadsheet (same layout as Signals_Feeder_Engine_All_APIs).

    python make_register_sheet.py            -> AlertEngine_All_Registers.xlsx next to this script (override: --out path)

ONE ROW PER REQUEST the code makes, register-level columns repeated on each row.
TO ADD A REGISTER: add one dict to REGISTERS below (copy HOS-01).  daily_update.py runs this script every day and it
CHECKS alert_tracker_ui.py: if a register code is in the tracker but not in REGISTERS, it prints a MISSING warning and exits 1,
so the daily e-mail shows the sheet is behind the code.
"""
import re
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).resolve().parent
TODAY = date.today().isoformat()
GAZ = "https://www.thegazette.co.uk/all-notices/notice/data.feed"
CH = "https://api.company-information.service.gov.uk"
FTS_API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
OCDS = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"

HEAD = ["Code", "Register (admin name)", "User-facing name", "Category", "Visible to", "Authority", "Jurisdiction", "Collection mode",
        "Format", "Key needed", "Source publishes", "History kept", "Data kept", "Official source", "Request no.", "Method",
        "Request (endpoint or URL, as built)", "Notes on this request", "Last refreshed"]
WIDTH = [9, 34, 26, 18, 15, 26, 20, 20, 18, 22, 18, 16, 40, 40, 9, 10, 90, 60, 13]

def gaz(code, name, ui, cat, schema, personal):
    r = dict(code=code, name=name, ui=ui, cat=cat, visible="Users and admin", auth="The Gazette", juris="England, Wales, NI" if personal else "UK",
             mode="News feed (RSS/Atom)", fmt="Atom + web pages", key="No", pub="Daily", hist="Rolling window (--days, default 7 per run); notices kept",
             data=f"Notice type, name, locality, court/case number, publication date, notice text; company link via company master. Schema {schema}",
             src="https://www.thegazette.co.uk/all-notices/notice")
    r["requests"] = [
        ("GET", GAZ + "?categorycode={code}&start-publish-date={start}&end-publish-date={today}&results-page-size=100&sort-by=latest-date", "Once per Gazette category code; start = today minus --days"),
        ("GET", GAZ + "?...&results-page={n}", "Next pages until the feed is exhausted"),
        ("GET", "https://www.thegazette.co.uk/notice/{notice_id}", "Notice page opened once per notice, 10 s apart, for the full text"),
    ]
    return r

def cqc(code, name, ui, schema, kind):
    return dict(code=code, name=name, ui=ui, cat="Care sector", visible="Users and admin", auth="Care Quality Commission", juris="England",
                mode="Downloaded file", fmt=kind, key="No", pub="Monthly (checked daily)", hist="History folder, 14 files",
                data=f"Full directory snapshot; daily diff of added / removed / changed locations. Schema {schema}",
                src="https://www.cqc.org.uk/about-us/transparency/using-cqc-data",
                requests=[("GET", "https://www.cqc.org.uk/about-us/transparency/using-cqc-data", "Page read to find the newest file link"),
                          ("HEAD", "{file link found on that page}", "Size/date signature: unchanged = skip"),
                          ("GET", "{file link found on that page}", "Downloaded only when the signature changed; backup: Google Drive folder")])

REGISTERS = [
    gaz("INS-01", "Personal Notices (The Gazette: People > Personal insolvency)", "Personal insolvency", "Insolvency", "R8_personal_notices", True),
    gaz("INS-02", "Corporate Notices (The Gazette: Companies)", "Corporate insolvency", "Insolvency", "R7_corporate_notices", False),
    gaz("GAZ-01", "Other People notices (appointments, name changes, honours, wills)", "Other personal notices", "Notices", "R9_other_notices", True),
    cqc("CQC-01", "CQC Care Directory", "CQC care directory", "R1_cqc_care_directory", "CSV"),
    cqc("CQC-02", "Care Directory With Filters", "Care directory with filters", "R2_care_directory_with_filters", "ODS"),
    cqc("CQC-03", "Care Directory With Ratings", "Care directory with ratings", "R3_care_directory_with_ratings", "ODS"),
    cqc("CQC-04", "Deactivated Locations", "Deactivated locations", "R4_deactivated_locations", "ODS"),
    dict(code="COM-01", name="Company master: new companies and changes", ui="Companies", cat="Companies", visible="Users and admin",
         auth="Companies House", juris="UK", mode="API", fmt="JSON", key="Yes, Companies House REST key (CH_REST_KEY)", pub="Real time",
         hist="Full (changes logged)", data="Name, number, status, type, incorporation date, registered address, trading addresses, officers (directors), PSCs with share bands (no shareholder endpoint exists). Schema 6_company_master; every register links to it by number, then exact name (postcode to disambiguate)",
         src="https://developer.company-information.service.gov.uk",
         requests=[("GET", CH + "/company/{number}", "Profile; limit 600 calls / 5 minutes, handled with a delay and retries"),
                   ("GET", CH + "/company/{number}/officers", "Directors and secretaries (paged)"),
                   ("GET", CH + "/company/{number}/persons-with-significant-control", "PSCs incl. share-holding bands (paged)"),
                   ("GET", CH + "/search/companies?q={name}&items_per_page=20", "Exact-name match for register entries with no number; up to 3000 searches a run"),
                   ("GET", CH + "/search/officers?q={name}&items_per_page=100&start_index={n}", "Officer search"),
                   ("GET", CH + "/officers/{officer_id}/appointments", "Other appointments of an officer")]),
    dict(code="HMD-01", name="HMRC Tax defaulters", ui="HMRC deliberate tax defaulters", cat="HMRC", visible="Users and admin", auth="HMRC",
         juris="UK", mode="Downloaded file", fmt="ODS", key="No", pub="Quarterly (checked daily)", hist="From Sep 2026; all lists kept",
         data="Defaulter, trading name, address, amount of tax, penalty, period; first/last listed. Schema 10_hmrc_tax_defaulters",
         src="https://www.gov.uk/government/publications/publishing-details-of-deliberate-tax-defaulters-pddd",
         requests=[("GET", "https://www.gov.uk/api/content/government/publications/publishing-details-of-deliberate-tax-defaulters-pddd", "details.attachments lists the list files; HTML page is the fallback"),
                   ("HEAD", "{attachment url}", "Size/date signature: unchanged = skip"),
                   ("GET", "{attachment url}", "Each new monthly list from 2026-09 onward")]),
    dict(code="HOS-01", name="Home Office licence sponsor register", ui="Licensed sponsors (workers)", cat="Employment & Workforce",
         visible="Users and admin", auth="Home Office (UKVI)", juris="UK", mode="Downloaded file", fmt="CSV", key="No", pub="Daily",
         hist="Last 14 files kept; changes logged", data="Organisation, town, county, type & rating, route; added / removed / changed. Schema 11_sponsor_register",
         src="https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
         requests=[("GET", "https://www.gov.uk/api/content/government/publications/register-of-licensed-sponsors-workers", "Find the latest CSV attachment"),
                   ("HEAD", "{attachment url}", "Size/date signature: unchanged = skip"),
                   ("GET", "{attachment url}", "Always the latest file; a drop of more than 50% in rows is refused")]),
    dict(code="CON-01", name="Contracts Finder: tenders and contract awards", ui="Contracts Finder", cat="Government contracts",
         visible="Users and admin", auth="Crown Commercial Service", juris="England, UK-wide notices", mode="API", fmt="JSON (OCDS)", key="No",
         pub="Daily", hist="From 2026-11-06; full releases kept (notice_raw)",
         data="Release id, OCID, published date, stage, title, buyer, value, suppliers (with company number, address). Schema 12_contracts_finder",
         src="https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search",
         requests=[("GET", OCDS + "?publishedFrom={day}T00:00:00&publishedTo={next-day}T00:00:00&limit=100", "One call per day window (first run from 2026-11-06, then the last 3 days); a window that overflows is split in halves"),
                   ("GET", "{links.next from the previous response}", "Next page; 1 s delay; on HTTP 403 wait 300 s")]),
    dict(code="FTS-01", name="Find a Tender: tenders and contract awards", ui="Find a Tender", cat="Government contracts",
         visible="Users and admin", auth="Cabinet Office (Find a Tender Service)", juris="UK", mode="API", fmt="JSON (OCDS)", key="No",
         pub="Daily", hist="From 2026-11-06; full releases kept (notice_raw)",
         data="Release id, OCID, published date, stage (Pipeline, Planning, Tender, Award, Contract, Termination, Payments compliance, Dynamic market), notice type UK1-UK17 / F01-F25 / T01-T02, commercial tool, CPV, region, value, SME / VCSE, title, buyer, suppliers (with company number, address). Schema 13_find_a_tender",
         src=FTS_API,
         requests=[("GET", FTS_API + "?updatedFrom={day}T00:00:00&updatedTo={next-day}T00:00:00&limit=100", "One call per day window (first run from 2026-11-06, then the last 3 days); a window that overflows is split in halves"),
                   ("GET", "{links.next from the previous response}", "Next page; 1 s delay; on HTTP 403/429 wait 300 s")]),
    dict(code="PTR-01", name="Payments Transparency Register", ui="Payments Transparency Register", cat="Government contracts",
         visible="Users and admin", auth="Cabinet Office (Find a Tender Service)", juris="UK", mode="Web page (HTML)", fmt="HTML pages", key="No",
         pub="Daily", hist="From 2026-10-01; every payment kept",
         data="Payment id, title, contracting authority, contract, supplier, payment value (excl. VAT), date paid, date published, OCID, supplier identifiers (Companies House / PPON / charity number), full detail text. Schema 14_payments_transparency",
         src="https://payments-transparency-register.supplier-information.find-tender.service.gov.uk/",
         requests=[("GET", "https://payments-transparency-register.supplier-information.find-tender.service.gov.uk/?publishedDateFromDay={dd}&publishedDateFromMonth={mm}&publishedDateFromYear={yyyy}&publishedDateToDay={dd}&publishedDateToMonth={mm}&publishedDateToYear={yyyy}&page={n}&pageSize=50", "One published day at a time (first run from 2026-10-01, then the last 3 days); pages until all results are read; falls back to pageSize=10 if refused"),
                   ("GET", "https://payments-transparency-register.supplier-information.find-tender.service.gov.uk/Home/Details/{payment id}", "Detail page of each NEW payment, 1 s apart; gives the supplier identifiers")]),
    dict(code="FIN-01", name="Company financials (Companies House accounts)", ui="Tracker (daily counts + drill-down), Users search, and financial columns in every register drill-down", cat="Companies",
         visible="Users and admin", auth="Companies House (free REST API key, CH_REST_KEY)", juris="UK", mode="REST API (JSON) + iXBRL documents", fmt="JSON; documents: iXBRL (html), XBRL (xml), zip of iXBRL, PDF (text or scanned - OCR)", key="Yes (free REST key)",
         pub="As companies file (checked daily, 600 companies a run, each company every 30 days)", hist="Last 3 years of accounts on first check; bulk zip back-fill available",
         data="Per filing: company number, filing date, made-up date, accounts type, formats. Per readable filing: turnover, gross / operating / pre-tax profit, profit for the period, fixed / current assets, stocks, debtors, cash, creditors (within / after 1 year), net current assets, net assets, equity, share capital, retained earnings, employees (+ prior year of the main ones). Every tagged number in table fact. Schema 15_company_financials",
         src="https://developer-specs.company-information.service.gov.uk/",
         requests=[("GET", "https://api.company-information.service.gov.uk/company/{number}/filing-history?category=accounts&items_per_page=100", "Accounts filings of one company; Basic auth, key as user name; 0.55 s apart (600 calls / 5 min)"),
                   ("GET", "{links.document_metadata from the filing}", "Which formats exist (iXBRL = readable figures, PDF only = none)"),
                   ("GET", "{document metadata links.document}  (Accept: application/xhtml+xml)", "Best format offered (iXBRL, then XBRL xml, then PDF); 302 redirect to a signed file URL, followed WITHOUT the key; numbers read from the tags, or from PDF text / OCR"),
                   ("GET", "https://download.companieshouse.gov.uk/en_accountsdata.html  (Accounts_Bulk_Data / Accounts_Monthly_Data zip)", "Daily bulk files (last 60 days, one per day Tue-Sat): Accounts_Bulk_Data-YYYY-MM-DD.zip - listing parsed for hrefs; downloaded with resume to the Accounts folder"),
                   ("GET", "https://download.companieshouse.gov.uk/en_monthlyaccountsdata.html  ->  Accounts_Monthly_Data-<MonthYYYY>.zip", "Monthly bulk files (last 13 months, 2-4 GB each); a daily file is skipped when its month has a monthly file"),
                   ("GET", "https://download.companieshouse.gov.uk/historicmonthlyaccountsdata.html  ->  /archive/Accounts_Monthly_Data-<MonthYYYY>.zip", "Archive monthly files 2008-2025; only with: python register_financials.py bulk --archive-from <year>")]),
]

def check_drift():
    ui = HERE / "alert_tracker_ui.py"
    if not ui.exists():
        return []
    codes = set(re.findall(r'(?:sql_register|notice_schema_register|cqc_diff_register)\(\s*"([A-Z]{3}-\d{2})"', ui.read_text(encoding="utf-8", errors="ignore")))
    return sorted(codes - {r["code"] for r in REGISTERS})

def check_mapping():
    """Tracker registers that have no company number / name columns (add them to MAPPED in register_company_columns.py)."""
    ui = HERE / "alert_tracker_ui.py"
    try:
        import register_company_columns as rcc
    except ImportError:
        return []
    if not ui.exists():
        return []
    codes = set(re.findall(r'(?:sql_register|notice_schema_register|cqc_diff_register)\(\s*"([A-Z]{3}-\d{2})"', ui.read_text(encoding="utf-8", errors="ignore")))
    return sorted(codes - rcc.covered_codes())

def build(out):
    wb = Workbook(); ws = wb.active; ws.title = "All APIs"
    font = "Calibri"
    ws.append(HEAD)
    for c in ws[1]:
        c.font = Font(name=font, size=11, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F5F8B")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 28.8
    for r in REGISTERS:
        for i, (m, url, note) in enumerate(r["requests"], 1):
            ws.append([r["code"], r["name"], r["ui"], r["cat"], r["visible"], r["auth"], r["juris"], r["mode"], r["fmt"], r["key"],
                       r["pub"], r["hist"], r["data"], r["src"], i, m, url, note, TODAY])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = Font(name=font, size=11); c.alignment = Alignment(wrap_text=True, vertical="top")
    for i, w in enumerate(WIDTH, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C2"; ws.auto_filter.ref = ws.dimensions
    rd = wb.create_sheet("Read me")
    for line in ["Alert Engine - registers and the requests the code makes.",
                 "One row per request; register-level columns are repeated on each row. Layout copied from the Signals Feeder 'All APIs' sheet.",
                 "Generated by make_register_sheet.py from its REGISTERS list; daily_update.py runs it daily. Do not edit by hand - edit REGISTERS.",
                 "When a register is added to alert_tracker_ui.py without a REGISTERS entry the script prints MISSING and exits 1.",
                 f"Last generated: {TODAY}"]:
        rd.append([line])
    rd.column_dimensions["A"].width = 130
    for r in rd.iter_rows():
        r[0].font = Font(name=font, size=11, bold=(r[0].row == 1))
    wb.save(out)

if __name__ == "__main__":
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else HERE / "AlertEngine_All_Registers.xlsx"
    build(out)
    n = sum(len(r["requests"]) for r in REGISTERS)
    print(f"Wrote {out}: {len(REGISTERS)} registers, {n} request rows.")
    miss = check_drift()
    if miss:
        print("MISSING from the sheet (add to REGISTERS in make_register_sheet.py): " + ", ".join(miss)); sys.exit(1)
    nm = check_mapping()
    if nm:
        print("NOT MAPPED (add to MAPPED in register_company_columns.py so the register gets company number / name columns): " + ", ".join(nm)); sys.exit(1)
