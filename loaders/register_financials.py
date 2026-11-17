r"""register_financials.py -- company financials (FIN-01) from the FREE Companies House API.  ONE file.

  python register_financials.py                  daily: check the next companies of the company master, load any new accounts
  python register_financials.py --limit 800      the same, at most 800 companies in this run (default 600)
  python register_financials.py --number 09266748   one company now, and print what was found
  python register_financials.py --zip Accounts_Monthly_Data-October2026.zip     load a Companies House bulk accounts file (fast back-fill)
  python register_financials.py show             counts and the latest figures
  python register_financials.py --setup          only create the tables

WHICH COMPANIES: every company in 6_company_master.company (so every company that appears in any of your registers, now and in future -
the company master step adds new ones each day).  Run register_company_master.py first (daily_update.py does).

HOW IT WORKS (the free REST API has no "financials" endpoint - the figures are inside each filed set of accounts, so):
    1. GET /company/{n}/filing-history?category=accounts          the accounts filings of the company (date, made-up date, type)
    2. GET <document_metadata link>                               tells which formats exist (iXBRL = machine readable, PDF = image)
    3. GET <document>/content  (Accept: application/xhtml+xml)    the iXBRL accounts; the tagged numbers are read (turnover, profit,
                                                                   assets, creditors, net assets, equity, cash, employees ...)
  Only filings not seen before are downloaded.  A company is checked again every RECHECK_DAYS days (dissolved ones are not).
  3 calls per new set of accounts; calls are 0.55 s apart (the API allows 600 per 5 minutes), so ~600 companies a run is about 30 minutes.
  Small companies that file PDF / paper accounts only have no tagged numbers: they are recorded as "no structured data".
  Full filing detail for what could not be read stays visible in "R15_company_financials".filing.

FILE FORMATS READ (API documents and bulk zips alike, detected from the content, not the file name):
    inline XBRL  (.html / .xhtml)      the tagged numbers                              source "ixbrl"
    XBRL         (.xml)                the tagged numbers (older / non-inline files)   source "xbrl-xml"
    ZIP of iXBRL (.zip inside a zip)   opened, every file inside is read as above       (same sources)
    PDF with text                      figures found by their labels (Turnover, Fixed assets, Net assets ...); current and prior year
                                       columns.  Less exact than XBRL - check source = "pdf-text"
    PDF that is a scan (image)         read by OCR when tesseract + pdftoppm are installed (source "pdf-ocr", least exact);
                                       otherwise recorded as "no structured data"
  PDF needs   pip install pypdf   (or the pdftotext program).  OCR needs Tesseract + Poppler on the PATH (optional).
  Where a company files BOTH an iXBRL and a PDF, the iXBRL is used.

BULK DOWNLOAD (free, whole-market, recommended for the first load and every day after):
    python register_financials.py bulk                       download every new Companies House accounts zip into  D:\Reckonant\data\Accounts ,
                                                             index it, and load the accounts of companies in YOUR company master
    python register_financials.py bulk --list                only show what would be downloaded
    python register_financials.py bulk --archive-from 2022   also the older monthly archive files from 2022 (big: several GB each)
    python register_financials.py bulk --max-files 12        stop after 12 new downloads (daily_update.py uses this)
    options: --folder D:\Reckonant\data\Accounts   --all-daily   --no-download   --delete-after
  Files used: monthly files (page en_monthlyaccountsdata.html, last 13 months), daily files (page en_accountsdata.html, last 60 days -
  only for months that have no monthly file yet), and with --archive-from the archive page historicmonthlyaccountsdata.html.
  Every file name inside every zip is indexed in table bulk_member, so when a NEW company joins your company master its accounts are loaded
  from the zips already on disk on the next run (keep the zip files - do not use --delete-after unless disk space is short).
  Interrupted downloads resume.  A filing already loaded through the API (same company and made-up date) is not loaded twice.

OTHER (free, much faster for the first load): Companies House "Accounts data product" - one zip per day / month of every
  iXBRL accounts filed, https://download.companieshouse.gov.uk/en_accountsdata.html .  Download a zip, then:  python register_financials.py --zip <file>
  Only companies in your company master are loaded (add --all to load every company in the file).

TABLES (schema "R15_company_financials"):
    filing        one row per set of accounts: company number, filing date, made-up date, type, description, formats, status
    financials    one row per readable filing: period start / end + the key figures (current year, and prior year for the main ones)
    fact          every tagged number read (concept, period, value, unit, dimension) for anything not in the key columns
    company_check when a company was last checked and what was found
    change_log    "Accounts filed" (detected_date, change_type) for accounts filed in the last 30 days
    v_latest      the latest accounts of each company with the key figures - use this one for the front end

Needs the same key as the company master:   setx CH_REST_KEY "your-key"   (then open a new terminal)
"""
import argparse
import base64
import html as htmllib
import socket
import io
import shutil
import subprocess
import tempfile
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg import sql

DB = dict(host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"),
          dbname=os.environ.get("PGDATABASE", "postgres"), user=os.environ.get("PGUSER", "postgres"))
SCHEMA = "R15_company_financials"
MASTER = "R6_company_master"
API = os.environ.get("CH_API_BASE", "https://api.company-information.service.gov.uk")
KEY = os.environ.get("CH_REST_KEY", "")
DELAY = float(os.environ.get("CH_DELAY", "0.55"))
LIMIT = 600                 # most companies checked in one run
RECHECK_DAYS = 30           # a live company is checked again after this many days
YEARS = 3                   # on the first check, read accounts made up in the last N years (0 = all of them)
BULK_BASE = os.environ.get("CH_BULK_BASE", "https://download.companieshouse.gov.uk")
ACCOUNTS_DIR = os.environ.get("ACCOUNTS_DIR") or r"D:\Reckonant\data\Accounts"      # the bulk zip files (tens of GB) live on drive E:
MAX_FACTS = 600             # most numbers kept per filing

# key figure -> concept names (local part, without prefix), best first
KEY_CONCEPTS = {
    "turnover": ["Turnover", "TurnoverRevenue", "Revenue"],
    "gross_profit": ["GrossProfitLoss"],
    "operating_profit": ["OperatingProfitLoss"],
    "profit_before_tax": ["ProfitLossBeforeTax", "ProfitLossOnOrdinaryActivitiesBeforeTax"],
    "profit_for_period": ["ProfitLoss", "ProfitLossForPeriod", "ProfitLossForPeriodAttributableToOwnersParent"],
    "fixed_assets": ["FixedAssets"],
    "intangible_assets": ["IntangibleAssets", "IntangibleAssetsIncludingGoodwill"],
    "tangible_assets": ["PropertyPlantEquipment"],
    "current_assets": ["CurrentAssets"],
    "stocks": ["Inventories", "Stocks"],
    "debtors": ["Debtors"],
    "cash": ["CashBankOnHand", "CashBankInHand", "CashCashEquivalents"],
    "creditors_within_1yr": ["CreditorsDueWithinOneYear"],
    "creditors_after_1yr": ["CreditorsDueAfterOneYear"],
    "net_current_assets": ["NetCurrentAssetsLiabilities"],
    "total_assets_less_current_liabilities": ["TotalAssetsLessCurrentLiabilities"],
    "net_assets": ["NetAssetsLiabilities", "NetAssetsLiabilitiesIncludingPensionAssetLiability"],
    "equity": ["Equity", "ShareholderFunds"],
    "share_capital": ["CalledUpShareCapital", "IssuedShareCapital"],
    "retained_earnings": ["ProfitLossAccountReserve", "RetainedEarningsAccumulatedLosses"],
    "employees": ["AverageNumberEmployeesDuringPeriod"],
}
PRIOR_OF = ["turnover", "profit_for_period", "net_assets", "cash", "employees"]     # also stored for the previous year

DDL = f'''
CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";
CREATE TABLE IF NOT EXISTS "{SCHEMA}".filing (
    transaction_id   text PRIMARY KEY,                -- Companies House filing id (zip files: "zip:<file name>")
    company_number   text NOT NULL,
    filing_date      date,
    made_up_date     date,
    filing_type      text,                            -- AA = accounts
    description      text,                            -- e.g. accounts-with-accounts-type-micro-entity
    formats          text,                            -- formats offered: application/xhtml+xml, application/pdf ...
    status           text NOT NULL,                   -- parsed | no_structured_data | error
    source           text,                            -- ixbrl | xbrl-xml | pdf-text | pdf-ocr
    note             text,
    document_url     text,
    loaded_at        timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE "{SCHEMA}".filing ADD COLUMN IF NOT EXISTS source text;
CREATE INDEX IF NOT EXISTS filing_company_idx ON "{SCHEMA}".filing (company_number, made_up_date DESC);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".financials (
    transaction_id   text PRIMARY KEY REFERENCES "{SCHEMA}".filing (transaction_id) ON DELETE CASCADE,
    company_number   text NOT NULL,
    period_start     date,
    period_end       date,
    accounts_type    text,
    audited          text,
    {", ".join(f"{k} numeric" for k in KEY_CONCEPTS)},
    {", ".join(f"prior_{k} numeric" for k in PRIOR_OF)}
);
CREATE INDEX IF NOT EXISTS financials_company_idx ON "{SCHEMA}".financials (company_number, period_end DESC);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".fact (
    transaction_id   text NOT NULL REFERENCES "{SCHEMA}".filing (transaction_id) ON DELETE CASCADE,
    concept          text NOT NULL,
    period_start     date,
    period_end       date,
    value            numeric,
    unit             text,
    dimension        text
);
CREATE INDEX IF NOT EXISTS fact_filing_idx ON "{SCHEMA}".fact (transaction_id);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".company_check (
    company_number   text PRIMARY KEY,
    last_checked_at  timestamptz NOT NULL,
    filings_seen     integer,
    latest_made_up   date,
    status           text,                            -- ok | not_found | error
    note             text
);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".change_log (
    id               bigserial PRIMARY KEY,
    detected_date    date NOT NULL DEFAULT current_date,
    change_type      text NOT NULL,                   -- Accounts filed
    company_number   text NOT NULL,
    transaction_id   text,
    made_up_date     date,
    detail           text
);
CREATE INDEX IF NOT EXISTS change_log_date_idx ON "{SCHEMA}".change_log (detected_date);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".bulk_file (
    file_name        text PRIMARY KEY,                -- Accounts_Bulk_Data-2026-11-13.zip / Accounts_Monthly_Data-October2026.zip
    kind             text NOT NULL,                   -- daily | monthly | archive
    url              text,
    size_bytes       bigint,
    downloaded_at    timestamptz,
    indexed_at       timestamptz,
    members          integer
);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".bulk_member (
    file_name        text NOT NULL,
    member_name      text NOT NULL,
    company_number   text NOT NULL,
    made_up_date     date,
    loaded           boolean NOT NULL DEFAULT false,
    PRIMARY KEY (file_name, member_name)
);
CREATE INDEX IF NOT EXISTS bulk_member_company_idx ON "{SCHEMA}".bulk_member (company_number) WHERE NOT loaded;
ALTER TABLE "{SCHEMA}".filing ADD COLUMN IF NOT EXISTS company_name text;
ALTER TABLE "{SCHEMA}".financials ADD COLUMN IF NOT EXISTS company_name text;
ALTER TABLE "{SCHEMA}".company_check ADD COLUMN IF NOT EXISTS company_name text;
ALTER TABLE "{SCHEMA}".change_log ADD COLUMN IF NOT EXISTS company_name text;
CREATE OR REPLACE VIEW "{SCHEMA}".v_latest AS
SELECT DISTINCT ON (f.company_number) f.company_number, coalesce(c.company_name, f.company_name) AS company_name, f.period_start, f.period_end, fi.filing_date, f.accounts_type, f.audited,
       {", ".join("f." + k for k in KEY_CONCEPTS)}, {", ".join("f.prior_" + k for k in PRIOR_OF)}, f.transaction_id
FROM "{SCHEMA}".financials f
JOIN "{SCHEMA}".filing fi ON fi.transaction_id = f.transaction_id
LEFT JOIN "{MASTER}".company c ON c.company_number = f.company_number
ORDER BY f.company_number, f.period_end DESC NULLS LAST, fi.filing_date DESC NULLS LAST;
'''


# =====================================================================
#  iXBRL reader (standard library only)
# =====================================================================
TAG = re.compile(r"<[^>]+>")
CTX = re.compile(r"<(?:\w+:)?context\b[^>]*?\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</(?:\w+:)?context>", re.I | re.S)
DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
MEMBER = re.compile(r"<(?:\w+:)?(?:explicitMember|typedMember)\b[^>]*?dimension\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</(?:\w+:)?(?:explicitMember|typedMember)>", re.I | re.S)
NONFRAC = re.compile(r"<ix:nonFraction\b([^>]*?)(/>|>(.*?)</ix:nonFraction>)", re.I | re.S)
NONNUM = re.compile(r"<ix:nonNumeric\b([^>]*)>(.*?)</ix:nonNumeric>", re.I | re.S)
ATTR = re.compile(r"([\w:.-]+)\s*=\s*(\"[^\"]*\"|'[^']*')", re.S)


def attrs(s):
    return {k.lower(): htmllib.unescape(v[1:-1]) for k, v in ATTR.findall(s)}


def local(name):
    return (name or "").split(":")[-1]


def dec(text, a):
    """Number from the text of an ix:nonFraction, honouring format / scale / sign.  None when it is not a number."""
    fmt = (a.get("format") or "").lower()
    t = htmllib.unescape(TAG.sub("", text or "")).strip()
    if a.get("xsi:nil") == "true":
        return None
    if "zerodash" in fmt or "fixed-zero" in fmt or "zero" in fmt and not re.search(r"\d", t):
        v = Decimal(0)
    else:
        t = t.replace("\xa0", "").replace(" ", "")
        if "commadecimal" in fmt:
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
        t = re.sub(r"[^0-9.\-]", "", t)
        if not t or t in (".", "-"):
            return None
        try:
            v = Decimal(t)
        except InvalidOperation:
            return None
    try:
        v = v * (Decimal(10) ** int(a.get("scale") or 0))
    except (ValueError, InvalidOperation):
        pass
    if a.get("sign") == "-":
        v = -v
    if abs(v) >= Decimal("1e15"):
        return None
    return v


def parse_ixbrl(doc):
    """-> dict(facts=[(concept, start, end, value, unit, dimension)], period_start, period_end, accounts_type, audited, company)"""
    ctx = {}
    for cid, body in CTX.findall(doc):
        dates = DATE.findall(body)
        if "instant" in body.lower() and len(dates) >= 1 and not re.search(r"startDate", body, re.I):
            start, end = None, dates[0]
        elif len(dates) >= 2:
            start, end = dates[0], dates[1]
        else:
            continue
        dims = ";".join(f"{local(d)}={local(htmllib.unescape(TAG.sub('', m)).strip())}" for d, m in MEMBER.findall(body))
        ctx[cid] = (start, end, dims or None)
    facts = []
    for m in NONFRAC.finditer(doc):
        a = attrs(m.group(1))
        c = ctx.get(a.get("contextref"))
        if not c or not a.get("name"):
            continue
        v = dec(m.group(3) or "", a)
        if v is None:
            continue
        facts.append((local(a["name"]), c[0], c[1], v, local(a.get("unitref") or "") or None, c[2]))
    text = {}
    for m in NONNUM.finditer(doc):
        a = attrs(m.group(1))
        text.setdefault(local(a.get("name")), htmllib.unescape(TAG.sub("", m.group(2))).strip()[:200])
    return finish_parse(facts, text)


def finish_parse(facts, text):
    plain = [f for f in facts if f[5] is None]
    ends = sorted({f[2] for f in plain}, reverse=True)
    cur = ends[0] if ends else None
    # the reporting period = the duration ending on the latest date (a very long one is replaced by the shortest)
    starts = [f[1] for f in plain if f[2] == cur and f[1]]
    p_start = min(starts) if starts else None
    try:
        if cur and p_start and (date.fromisoformat(cur) - date.fromisoformat(p_start)).days > 800:
            p_start = max(starts)
    except ValueError:
        pass
    at = next((v for k, v in text.items() if "accountstype" in k.lower() or k.lower() == "accountingstandardsapplied"), None)
    aud = next((v for k, v in text.items() if "auditedorunaudited" in k.lower()), None)
    return dict(facts=facts, period_start=p_start, period_end=cur, accounts_type=at, audited=aud,
                company=text.get("UKCompaniesHouseRegisteredNumber"),
                company_name=next((v for k, v in text.items() if k.lower() in ("entitycurrentlegalorregisteredname", "companyname")), None))

# ---- XBRL instance (.xml) ----
XFACT = re.compile(r"<([\w.-]+):([\w.-]+)\b([^>]*?\bcontextRef\s*=[^>]*?)(?:/>|>(.*?)</\1:\2>)", re.I | re.S)


def parse_xbrl_xml(doc):
    """Plain XBRL instance document (not inline): facts are elements carrying contextRef / unitRef."""
    ctx = {}
    for cid, body in CTX.findall(doc):
        dates = DATE.findall(body)
        if re.search(r"instant", body, re.I) and not re.search(r"startDate", body, re.I) and dates:
            start, end = None, dates[0]
        elif len(dates) >= 2:
            start, end = dates[0], dates[1]
        else:
            continue
        dims = ";".join(f"{local(d)}={local(htmllib.unescape(TAG.sub('', m)).strip())}" for d, m in MEMBER.findall(body))
        ctx[cid] = (start, end, dims or None)
    facts, text = [], {}
    for pre, name, at, body in XFACT.findall(doc):
        if pre.lower() in ("xbrli", "link", "xbrldi", "xlink", "xsi"):
            continue
        a = attrs(at)
        c = ctx.get(a.get("contextref"))
        val = htmllib.unescape(TAG.sub("", body or "")).strip()
        if not a.get("unitref"):
            if val:
                text.setdefault(name, val[:200])
            continue
        if not c or a.get("xsi:nil") == "true":
            continue
        try:
            v = Decimal(val.replace(",", ""))
        except InvalidOperation:
            continue
        if abs(v) >= Decimal("1e15"):
            continue
        facts.append((name, c[0], c[1], v, local(a["unitref"]) or None, c[2]))
    return finish_parse(facts, text)


# ---- PDF ----
NUM = re.compile(r"\(?-?\s?£?\s?\d[\d,]*(?:\.\d+)?\)?")
PDF_LABELS = [   # key, regex on the start of a line (lower case), best first; the first label that matches a line with numbers wins
    ("turnover", r"(turnover|revenue|sales)\b"),
    ("gross_profit", r"gross (profit|loss)"),
    ("operating_profit", r"operating (profit|loss)|(loss|profit)/?\(?(loss|profit)?\)? from operations"),
    ("profit_before_tax", r"(profit|loss)( or loss)?( on ordinary activities)? before tax"),
    ("profit_for_period", r"(profit|loss)(\s*/\s*\(?loss\)?|\s+or\s+loss)? for the (financial )?(year|period)|(profit|loss) and total comprehensive|retained (profit|loss) for the"),
    ("intangible_assets", r"intangible (fixed )?assets"),
    ("tangible_assets", r"tangible (fixed )?assets|property,? plant"),
    ("fixed_assets", r"(total )?fixed assets"),
    ("stocks", r"(stocks?|inventor(y|ies))\b"),
    ("debtors", r"(trade )?debtors\b"),
    ("cash", r"cash (at bank|and cash equivalents|in hand)|cash$"),
    ("current_assets", r"(total )?current assets"),
    ("creditors_within_1yr", r"creditors:? ?amounts falling due within one year|amounts falling due within one year"),
    ("creditors_after_1yr", r"creditors:? ?amounts falling due after (more than )?one year|amounts falling due after (more than )?one year"),
    ("net_current_assets", r"net current (assets|liabilities)"),
    ("total_assets_less_current_liabilities", r"total assets less current liabilities"),
    ("net_assets", r"(total )?net (assets|liabilities)|net assets/\(liabilities\)"),
    ("share_capital", r"called up share capital|share capital"),
    ("retained_earnings", r"profit and loss (account|reserve)|retained earnings"),
    ("equity", r"(total )?(shareholders.? funds|equity|capital and reserves)"),
    ("employees", r"average (monthly )?number of (persons|employees)|average number of staff"),
]
MONTHS_RX = "january|february|march|april|may|june|july|august|september|october|november|december"


def pdf_numbers(line):
    out = []
    for m in NUM.finditer(line):
        t = m.group(0)
        neg = t.strip().startswith("(") and t.strip().endswith(")") or "-" in t
        d = re.sub(r"[^0-9.]", "", t)
        if not d or d == ".":
            continue
        try:
            v = Decimal(d)
        except InvalidOperation:
            continue
        out.append((-v if neg else v, "," in t))
    return out


def parse_pdf_text(text, source="pdf-text"):
    """Figures found by their labels in the text of a set of accounts.  Returns None when too little is found."""
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    low = text.lower()
    scale = 1000 if re.search(r"£\s?'?000|in thousands|£k\b", low) else 1
    row, prior = {}, {}
    for i, l in enumerate(lines):
        ll = l.lower()
        for key, rx in PDF_LABELS:
            if key in row:
                continue
            m = re.match(rx, ll)
            if not m:
                continue
            rest = l[m.end():]
            nums = pdf_numbers(rest)
            if not nums and i + 1 < len(lines) and re.fullmatch(r"[\s£\d,.()\-]+", lines[i + 1]):     # figures alone on the next line
                nums = pdf_numbers(lines[i + 1])
            if key != "employees" and len(nums) >= 3 and not nums[0][1] and abs(nums[0][0]) <= 40:
                nums = nums[1:]                                                  # first figure is the note number
            if not nums:
                continue
            f = Decimal(1) if key == "employees" else Decimal(scale)
            sgn = -1 if key.startswith("creditors") else 1            # creditors are shown in brackets in PDFs; stored positive like the XBRL figures
            row[key] = nums[0][0] * f * (sgn if nums[0][0] < 0 else 1)
            if len(nums) > 1 and key in PRIOR_OF:
                prior[key] = nums[1][0] * f
            break
    if len(row) < 3:
        return None
    pe = None
    m = re.search(r"(?:year|period) ended\s+(\d{1,2})(?:st|nd|rd|th)?\s+(" + MONTHS_RX + r")\s+(\d{4})", low) or \
        re.search(r"(?:at|as at)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(" + MONTHS_RX + r")\s+(\d{4})", low)
    if m:
        try:
            pe = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y").date().isoformat()
        except ValueError:
            pass
    full = {k: row.get(k) for k in KEY_CONCEPTS}
    full.update({"prior_" + k: prior.get(k) for k in PRIOR_OF})
    return dict(facts=[], period_start=None, period_end=pe, accounts_type=None, audited=None, company=None, row=full, source=source)


def pdf_to_text(data):
    """Text of a PDF: pypdf, then the pdftotext program."""
    text = ""
    try:
        from pypdf import PdfReader
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        text = ""
    if len(text.strip()) < 200 and shutil.which("pdftotext"):
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "a.pdf")
            open(fp, "wb").write(data)
            r = subprocess.run(["pdftotext", "-layout", fp, "-"], capture_output=True, timeout=120)
            text = r.stdout.decode("utf-8", "replace") or text
    return text


def pdf_ocr(data, max_pages=12):
    """OCR for scanned PDFs (needs pdftoppm + tesseract on the PATH).  '' when they are missing."""
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return ""
    out = []
    with tempfile.TemporaryDirectory() as d:
        fp = os.path.join(d, "a.pdf")
        open(fp, "wb").write(data)
        subprocess.run(["pdftoppm", "-r", "200", "-l", str(max_pages), "-png", fp, os.path.join(d, "p")], capture_output=True, timeout=300)
        for img in sorted(x for x in os.listdir(d) if x.endswith(".png")):
            r = subprocess.run(["tesseract", os.path.join(d, img), "stdout", "--psm", "6"], capture_output=True, timeout=180)
            out.append(r.stdout.decode("utf-8", "replace"))
    return "\n".join(out)


def read_pdf(data):
    """-> parsed dict or None"""
    text = pdf_to_text(data)
    if len(text.strip()) >= 200:
        p = parse_pdf_text(text, "pdf-text")
        if p:
            return p
        return None
    text = pdf_ocr(data)
    return parse_pdf_text(text, "pdf-ocr") if text.strip() else None


def read_document(data, name=""):
    """Any accounts file -> parsed dict (or None).  Format is detected from the content: iXBRL, XBRL xml, PDF, nested zip (first readable file)."""
    head = data[:2000]
    if head[:4] == b"%PDF":
        return read_pdf(data)
    if head[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            for n in z.namelist():
                if n.endswith("/"):
                    continue
                p = read_document(z.read(n), n)
                if p:
                    return p
        except zipfile.BadZipFile:
            pass
        return None
    doc = data.decode("utf-8", "replace")
    if re.search(r"<ix:nonFraction", doc, re.I):
        p = parse_ixbrl(doc)
        p["source"] = "ixbrl"
    elif re.search(r"contextRef\s*=", doc, re.I):
        p = parse_xbrl_xml(doc)
        p["source"] = "xbrl-xml"
    else:
        return None
    return p if p["facts"] else None


def pick(facts, names, period_end, prior=False, dim_like=None):
    """One figure: first concept (best first) with a value at the reporting period end (or the year before)."""
    ends = sorted({f[2] for f in facts if f[5] is None}, reverse=True)
    if prior:
        # the previous balance-sheet date: the latest end date older than the current one by about a year
        older = [e for e in ends if e < (period_end or "") and (date.fromisoformat(period_end) - date.fromisoformat(e)).days > 200] if period_end else []
        target = older[0] if older else None
    else:
        target = period_end
    if not target:
        return None
    for n in names:
        rows = [f for f in facts if f[0] == n and f[2] == target and (f[5] is None if not dim_like else (f[5] and dim_like.search(f[5])))]
        if rows:
            # for durations take the longest one
            rows.sort(key=lambda f: (f[1] or "9999"))
            return rows[0][3]
    return None


def summarise(p):
    f, pe = p["facts"], p["period_end"]
    row = {k: pick(f, n, pe) for k, n in KEY_CONCEPTS.items()}
    # creditors are often tagged "Creditors" with a maturity dimension
    if row["creditors_within_1yr"] is None:
        row["creditors_within_1yr"] = pick(f, ["Creditors"], pe, dim_like=re.compile(r"WithinOneYear|Within", re.I))
    if row["creditors_after_1yr"] is None:
        row["creditors_after_1yr"] = pick(f, ["Creditors"], pe, dim_like=re.compile(r"AfterOneYear|MoreThanOneYear|After", re.I))
    if row["net_assets"] is None:
        row["net_assets"] = row["equity"]
    for k in PRIOR_OF:
        row["prior_" + k] = pick(f, KEY_CONCEPTS[k], pe, prior=True)
    return row


# =====================================================================
#  Companies House API
# =====================================================================
class NotFound(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def auth_header():
    if not KEY:
        sys.exit("CH_REST_KEY is not set. Get a free REST key at developer.company-information.service.gov.uk, then:  setx CH_REST_KEY \"your-key\"  and open a new terminal.")
    return "Basic " + base64.b64encode((KEY + ":").encode()).decode()


def http(url, accept="application/json", tries=6, raw=False):
    """GET with the key.  A redirect (documents live on S3) is followed WITHOUT the key."""
    last = None
    opener = urllib.request.build_opener(_NoRedirect)
    for i in range(tries):
        time.sleep(DELAY)
        try:
            req = urllib.request.Request(url, headers={"Authorization": auth_header(), "Accept": accept})
            try:
                with opener.open(req, timeout=90) as r:
                    data = r.read()
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    with urllib.request.urlopen(urllib.request.Request(e.headers["Location"]), timeout=120) as r2:
                        data = r2.read()
                else:
                    raise
            return data if raw else json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(url)
            if e.code == 401:
                sys.exit("Companies House refused the key (401). Check CH_REST_KEY is a REST key, not a streaming key.")
            if e.code == 429:
                wait = 60 * (i + 1)
                print(f"  rate limit reached, waiting {wait}s", flush=True)
                time.sleep(wait)
                last = "429 rate limit"
                continue
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = str(e)
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def filing_history(number):
    items, start = [], 0
    while True:
        d = http(f"{API}/company/{number}/filing-history?category=accounts&items_per_page=100&start_index={start}")
        items += d.get("items") or []
        start += 100
        if start >= int(d.get("total_count") or 0) or not d.get("items"):
            return items


def document_data(meta_url):
    """-> (formats text, bytes or None).  Best machine-readable format first: iXBRL, XBRL xml, then PDF."""
    meta = http(meta_url)
    res = meta.get("resources") or {}
    fmts = ", ".join(sorted(res))
    for want in ("application/xhtml+xml", "application/xml", "application/pdf"):
        if want in res:
            content = (meta.get("links") or {}).get("document") or meta_url.rstrip("/") + "/content"
            return fmts, http(content, accept=want, raw=True)
    return fmts, None


# =====================================================================
#  Database
# =====================================================================
def connect():
    return psycopg.connect(**DB, password=os.environ.get("PGPASSWORD") or None, autocommit=True)


def pdate(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def save_filing(con, number, tid, filing_date, made_up, ftype, desc, fmts, url, parsed, note=None, status=None):
    """Store one filing (and its figures when parsed).  Returns True when it was new."""
    status = status or ("parsed" if parsed else ("no_structured_data" if not note else "error"))
    src = parsed.get("source") if parsed else None
    cur = con.execute(sql.SQL('INSERT INTO {}.filing (transaction_id, company_number, filing_date, made_up_date, filing_type, description, formats, status, note, document_url, source, company_name) '
                              'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (transaction_id) DO UPDATE SET status = EXCLUDED.status, note = EXCLUDED.note, source = EXCLUDED.source, company_name = coalesce(EXCLUDED.company_name, {}.filing.company_name), '
                              'formats = EXCLUDED.formats, loaded_at = now() RETURNING (xmax = 0)').format(sql.Identifier(SCHEMA), sql.Identifier(SCHEMA)),
                      (tid, number, pdate(filing_date), pdate(made_up), ftype, desc, fmts, status, note, url, src, (parsed or {}).get("company_name")))
    new = cur.fetchone()[0]
    if parsed:
        row = parsed.get("row") or summarise(parsed)
        cols = ["transaction_id", "company_number", "period_start", "period_end", "accounts_type", "audited"] + list(row)
        vals = [tid, number, pdate(parsed["period_start"]), pdate(parsed["period_end"]) or pdate(made_up), parsed["accounts_type"], parsed["audited"]] + list(row.values())
        con.execute(sql.SQL('DELETE FROM {}.financials WHERE transaction_id = %s').format(sql.Identifier(SCHEMA)), (tid,))
        con.execute(sql.SQL('INSERT INTO {}.financials ({}) VALUES ({})').format(sql.Identifier(SCHEMA), sql.SQL(", ").join(map(sql.Identifier, cols)),
                                                                                  sql.SQL(", ").join(sql.Placeholder() * len(cols))), vals)
        con.execute(sql.SQL('DELETE FROM {}.fact WHERE transaction_id = %s').format(sql.Identifier(SCHEMA)), (tid,))
        with con.cursor() as cu:
            cu.executemany(sql.SQL('INSERT INTO {}.fact (transaction_id, concept, period_start, period_end, value, unit, dimension) VALUES (%s,%s,%s,%s,%s,%s,%s)').format(sql.Identifier(SCHEMA)),
                           [(tid, c, pdate(s), pdate(e), v, u, d) for c, s, e, v, u, d in parsed["facts"][:MAX_FACTS]])
    return new


def fill_names(con, number=None):
    """Company name into every table that has a company number: from the company master, else from the accounts document itself."""
    S, M = sql.Identifier(SCHEMA), sql.Identifier(MASTER)
    w = sql.SQL(" AND {}.company_number = %s") if number else sql.SQL("")
    prm = (number,) if number else ()
    q = lambda t, extra: sql.SQL("UPDATE {s}.{t} x SET company_name = m.company_name FROM {m}.company m WHERE m.company_number = x.company_number "
                                 "AND m.company_name IS NOT NULL AND x.company_name IS DISTINCT FROM m.company_name").format(s=S, t=sql.Identifier(t), m=M) + (sql.SQL(" AND x.company_number = %s") if number else sql.SQL(""))
    for t in ("filing", "company_check", "change_log"):
        con.execute(q(t, ""), prm)
    con.execute(sql.SQL("UPDATE {s}.financials x SET company_name = f.company_name FROM {s}.filing f WHERE f.transaction_id = x.transaction_id "
                        "AND f.company_name IS NOT NULL AND x.company_name IS DISTINCT FROM f.company_name").format(s=S) + (sql.SQL(" AND x.company_number = %s") if number else sql.SQL("")), prm)


def figures_exist(con, number, made_up):
    """True when figures for this company and made-up date are already loaded (from the API or a bulk file)."""
    d = pdate(made_up)
    return bool(d and con.execute(sql.SQL("SELECT 1 FROM {}.filing WHERE company_number = %s AND made_up_date = %s AND status = 'parsed' LIMIT 1").format(sql.Identifier(SCHEMA)), (number, d)).fetchone())


def log_filed(con, number, tid, filing_date, made_up, desc):
    fd = pdate(filing_date)
    if fd and fd >= date.today() - timedelta(days=30):
        con.execute(sql.SQL('INSERT INTO {}.change_log (change_type, company_number, transaction_id, made_up_date, detail) VALUES (%s,%s,%s,%s,%s)').format(sql.Identifier(SCHEMA)),
                    ("Accounts filed", number, tid, pdate(made_up), desc))


def check_company(con, number, years=YEARS, verbose=False):
    """Read the accounts filings of one company, load the new ones.  -> (new filings, parsed, status)"""
    known = {r[0] for r in con.execute(sql.SQL('SELECT transaction_id FROM {}.filing WHERE company_number = %s').format(sql.Identifier(SCHEMA)), (number,)).fetchall()}
    first = not known
    try:
        items = filing_history(number)
    except NotFound:
        con.execute(sql.SQL('INSERT INTO {}.company_check VALUES (%s, now(), 0, NULL, %s, %s) ON CONFLICT (company_number) DO UPDATE SET last_checked_at = now(), status = EXCLUDED.status, note = EXCLUDED.note')
                    .format(sql.Identifier(SCHEMA)), (number, "not_found", "no filing history"))
        return 0, 0, "not_found"
    items = [i for i in items if (i.get("links") or {}).get("document_metadata")]
    cutoff = date.today() - timedelta(days=365 * years) if years else None
    new = parsed = 0
    latest = None
    for it in items:
        tid = it.get("transaction_id")
        mu = (it.get("description_values") or {}).get("made_up_date")
        latest = max([x for x in (latest, pdate(mu)) if x], default=None)
        if not tid or tid in known:
            continue
        if cutoff and pdate(mu or it.get("date")) and pdate(mu or it.get("date")) < cutoff:
            continue
        url = it["links"]["document_metadata"]
        if figures_exist(con, number, mu):          # already loaded from a bulk file - do not download it again
            save_filing(con, number, tid, it.get("date"), mu, it.get("type"), it.get("description"), None, url, None, "figures already loaded from a bulk file", status="duplicate")
            continue
        try:
            fmts, data = document_data(url)
            p = read_document(data) if data else None
            save_filing(con, number, tid, it.get("date"), mu, it.get("type"), it.get("description"), fmts, url, p, None)
            if p:
                parsed += 1
        except NotFound:
            save_filing(con, number, tid, it.get("date"), mu, it.get("type"), it.get("description"), None, url, None, "document not found")
        except RuntimeError as e:
            save_filing(con, number, tid, it.get("date"), mu, it.get("type"), it.get("description"), None, url, None, str(e)[:200])
        new += 1
        if not first:
            log_filed(con, number, tid, it.get("date"), mu, it.get("description"))
        if verbose:
            print(f"   {it.get('date')}  made up {mu}  {it.get('description')}  -> {('figures read (' + p['source'] + ')') if p else 'no figures (no readable data)'}")
    con.execute(sql.SQL('INSERT INTO {}.company_check VALUES (%s, now(), %s, %s, %s, NULL) ON CONFLICT (company_number) DO UPDATE SET last_checked_at = now(), '
                        'filings_seen = EXCLUDED.filings_seen, latest_made_up = EXCLUDED.latest_made_up, status = EXCLUDED.status, note = NULL').format(sql.Identifier(SCHEMA)),
                (number, len(items), latest, "ok"))
    fill_names(con, number)
    return new, parsed, "ok"


def queue(con, limit, recheck_days):
    """Never-checked companies first (newest in the master first), then the oldest checks.  Dissolved companies are checked once."""
    return [r[0] for r in con.execute(sql.SQL('''
        SELECT c.company_number
        FROM {m}.company c LEFT JOIN {s}.company_check k ON k.company_number = c.company_number
        WHERE c.fetch_status = 'ok'
          AND (k.company_number IS NULL
               OR (k.last_checked_at < now() - make_interval(days => %s)
                   AND coalesce(lower(c.company_status), '') NOT IN ('dissolved', 'closed', 'converted-closed')))
        ORDER BY (k.company_number IS NULL) DESC, k.last_checked_at NULLS FIRST, c.first_seen_at DESC
        LIMIT %s''').format(m=sql.Identifier(MASTER), s=sql.Identifier(SCHEMA)), (recheck_days, limit)).fetchall()]


# =====================================================================
#  Bulk zip (Companies House accounts data product)
# =====================================================================
ZIP_NAME = re.compile(r"^(?:.*[\\/])?(?:Prod\d+_\d+_)?([A-Z0-9]{8})_(\d{8})\.(?:html|htm|xhtml|xml|zip)$", re.I)


def find_zips(path):
    """A zip file (".zip" may be left off), or a folder: every .zip in it."""
    cands = [path, path + ".zip"]
    for c in cands:
        if os.path.isfile(c):
            return [c]
    if os.path.isdir(path):
        z = sorted(os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".zip"))
        if z:
            return z
    here = os.path.dirname(os.path.abspath(__file__))
    near = sorted(f for f in os.listdir(here) if f.lower().endswith(".zip") and "account" in f.lower())
    sys.exit(f"Cannot find the file '{path}' in {os.getcwd()}.\n"
             "Download the zip from https://download.companieshouse.gov.uk/en_accountsdata.html (Save as ... Accounts_Bulk_Data-2026-11-13.zip) into this folder,\n"
             "or give the full path, e.g.  --zip \"C:\\Users\\you\\Downloads\\Accounts_Bulk_Data-2026-11-13.zip\"   (or a folder of zips).\n"
             + ("Zip files that look like accounts data in the script folder: " + ", ".join(near) if near else "No accounts zip was found next to the script."))


def load_zip(con, path, everything=False):
    for fp in find_zips(path):
        load_one_zip(con, fp, everything)


def load_one_zip(con, path, everything=False):
    master = {r[0] for r in con.execute(sql.SQL('SELECT company_number FROM {}.company').format(sql.Identifier(MASTER))).fetchall()}
    z = zipfile.ZipFile(path)
    names = [n for n in z.namelist() if not n.endswith("/")]
    seen = loaded = skipped = bad = unread = already = 0
    for i, n in enumerate(names, 1):
        m = ZIP_NAME.match(n)
        if not m:
            continue
        number, made = m.group(1).upper(), m.group(2)
        seen += 1
        if not everything and number not in master:
            skipped += 1
            continue
        tid = f"zip:{os.path.basename(n)}"
        if con.execute(sql.SQL('SELECT 1 FROM {}.filing WHERE transaction_id = %s').format(sql.Identifier(SCHEMA)), (tid,)).fetchone():
            already += 1
            continue
        try:
            p = read_document(z.read(n), n)             # .html, .xml, .zip (of iXBRL) - detected from the content
            made_d = f"{made[:4]}-{made[4:6]}-{made[6:]}"
            fmt = {"html": "application/xhtml+xml", "htm": "application/xhtml+xml", "xhtml": "application/xhtml+xml", "xml": "application/xml", "zip": "application/zip"}.get(n.rsplit(".", 1)[-1].lower())
            save_filing(con, number, tid, None, made_d, "AA", "bulk accounts data", fmt, None, p, None)
            loaded += 1 if p else 0
            unread += 0 if p else 1
        except Exception as e:
            bad += 1
            print(f"  {n}: {str(e)[:120]}")
        if i % 2000 == 0:
            print(f"  {i:,} of {len(names):,} files ...", flush=True)
    print(f"Zip {os.path.basename(path)}: {seen:,} accounts files, {loaded:,} loaded, {unread:,} with no readable figures, {already:,} already loaded before, {skipped:,} not in your company master, {bad} failed.")



# =====================================================================
#  Bulk accounts files (download, index, load)
# =====================================================================
MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
HREF = re.compile(r"""href\s*=\s*["']([^"']*Accounts_(?:Bulk|Monthly)_Data-[^"']+?\.zip)["']""", re.I)
PAGES = {"daily": "/en_accountsdata.html", "monthly": "/en_monthlyaccountsdata.html", "archive": "/historicmonthlyaccountsdata.html"}


def web_get(url, tries=4, binary=False):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Reckonant/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            return data if binary else data.decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def file_period(name):
    """-> (year, month or None, day or None) from the file name."""
    m = re.search(r"Bulk_Data-(\d{4})-(\d{2})-(\d{2})\.zip", name, re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"Monthly_Data-([A-Za-z]+?)(\d{4})\.zip", name, re.I)
    if m:
        mo = m.group(1).lower()
        if mo in MONTH_NAMES:
            return int(m.group(2)), MONTH_NAMES.index(mo) + 1, None
        return int(m.group(2)), None, None             # JanuaryToDecember2008 (a whole year)
    return None


def list_bulk(kind):
    """Files offered on one of the Companies House pages: [(file_name, url, kind, year, month, day)]"""
    page = BULK_BASE + PAGES[kind]
    html_text = web_get(page)
    out, seen = [], set()
    for h in HREF.findall(html_text):
        url = urllib.parse.urljoin(page, h)
        name = url.rsplit("/", 1)[-1]
        per = file_period(name)
        if name in seen or not per:
            continue
        seen.add(name)
        out.append((name, url, kind) + per)
    return out


def download(url, dest, tries=6):
    """Download with resume (.part file); the finished file must be a valid zip."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    for i in range(tries):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Reckonant/1.0", **({"Range": f"bytes={have}-"} if have else {})})
            with urllib.request.urlopen(req, timeout=120) as r:
                if have and r.status != 206:
                    have = 0                                       # server ignored the range: start again
                total = int(r.headers.get("Content-Length") or 0) + have
                t0, last = time.time(), time.time()
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        if time.time() - last > 30:
                            got = f.tell()
                            print(f"    {got / 2**20:,.0f} of {total / 2**20:,.0f} MB ({int(100 * got / total) if total else 0}%)", flush=True)
                            last = time.time()
            if total and os.path.getsize(part) < total:
                raise OSError("connection closed early")
            if not zipfile.is_zipfile(part):
                raise OSError("downloaded file is not a valid zip")
            os.replace(part, dest)
            return os.path.getsize(dest)
        except (urllib.error.URLError, TimeoutError, OSError, socket.error) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                raise NotFound(url)
            print(f"    download problem ({e}); retrying ...", flush=True)
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"could not download {url}")


def index_zip(con, path, file_name):
    """Record every accounts file name (company number + made-up date) of a zip in bulk_member."""
    z = zipfile.ZipFile(path)
    rows = []
    for n in z.namelist():
        m = ZIP_NAME.match(n) if not n.endswith("/") else None
        if m:
            d = m.group(2)
            rows.append((file_name, n, m.group(1).upper(), f"{d[:4]}-{d[4:6]}-{d[6:]}"))
    con.execute("CREATE TEMP TABLE IF NOT EXISTS _bm (file_name text, member_name text, company_number text, made_up_date text)")
    con.execute("TRUNCATE _bm")
    with con.cursor() as cu:
        with cu.copy("COPY _bm (file_name, member_name, company_number, made_up_date) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
    con.execute(sql.SQL("INSERT INTO {}.bulk_member (file_name, member_name, company_number, made_up_date) "
                        "SELECT file_name, member_name, company_number, CASE WHEN made_up_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}$' THEN made_up_date::date END FROM _bm "
                        "ON CONFLICT DO NOTHING").format(sql.Identifier(SCHEMA)))
    con.execute(sql.SQL("UPDATE {}.bulk_file SET indexed_at = now(), members = %s WHERE file_name = %s").format(sql.Identifier(SCHEMA)), (len(rows), file_name))
    return len(rows)


def load_pending(con, folder, only_file=None):
    """Read, from the zips on disk, every indexed accounts file of a company in the company master that is not loaded yet."""
    where = sql.SQL("AND m.file_name = %s") if only_file else sql.SQL("")
    params = (only_file,) if only_file else ()
    files = con.execute(sql.SQL("SELECT m.file_name, count(*) FROM {s}.bulk_member m JOIN {m}.company c ON c.company_number = m.company_number "
                                "WHERE NOT m.loaded {w} GROUP BY 1 ORDER BY 1").format(s=sql.Identifier(SCHEMA), m=sql.Identifier(MASTER), w=where), params).fetchall()
    total = loaded = unread = dup = 0
    for fname, n in files:
        path = os.path.join(folder, fname)
        if not os.path.exists(path):
            print(f"  {fname}: {n:,} accounts of your companies are waiting, but the file is not in {folder} (download it again with: bulk)")
            continue
        z = zipfile.ZipFile(path)
        rows = con.execute(sql.SQL("SELECT m.member_name, m.company_number, m.made_up_date FROM {s}.bulk_member m JOIN {mm}.company c ON c.company_number = m.company_number "
                                   "WHERE NOT m.loaded AND m.file_name = %s").format(s=sql.Identifier(SCHEMA), mm=sql.Identifier(MASTER)), (fname,)).fetchall()
        done = []
        for member, number, made in rows:
            tid = f"zip:{os.path.basename(member)}"
            total += 1
            if con.execute(sql.SQL("SELECT 1 FROM {}.filing WHERE transaction_id = %s").format(sql.Identifier(SCHEMA)), (tid,)).fetchone() or figures_exist(con, number, made):
                dup += 1
            else:
                try:
                    p = read_document(z.read(member), member)
                    ext = member.rsplit(".", 1)[-1].lower()
                    fmt = {"xml": "application/xml", "zip": "application/zip"}.get(ext, "application/xhtml+xml")
                    save_filing(con, number, tid, None, made, "AA", "bulk accounts data: " + fname, fmt, None, p, None)
                    loaded += 1 if p else 0
                    unread += 0 if p else 1
                except Exception as e:
                    print(f"    {member}: {str(e)[:100]}")
                    continue                                       # leave not loaded: tried again next run
            done.append((fname, member))
        with con.cursor() as cu:
            cu.executemany(sql.SQL("UPDATE {}.bulk_member SET loaded = true WHERE file_name = %s AND member_name = %s").format(sql.Identifier(SCHEMA)), done)
        print(f"  {fname}: {len(rows):,} accounts of your companies - read {len(done):,}", flush=True)
    return total, loaded, unread, dup


def bulk(con, folder, archive_from=None, max_files=None, only_list=False, no_download=False, delete_after=False, all_daily=False):
    folder = os.path.abspath(folder)
    os.makedirs(folder, exist_ok=True)
    plan = []
    acc = [0, 0, 0, 0]
    if not no_download:
        monthly = list_bulk("monthly")
        daily = list_bulk("daily")
        archive = [f for f in list_bulk("archive") if archive_from and f[3] >= archive_from] if archive_from else []
        have_month = {(f[3], f[4]) for f in monthly}
        daily_needed = [f for f in daily if all_daily or (f[3], f[4]) not in have_month]
        plan = sorted(archive, key=lambda f: (f[3], f[4] or 0)) + sorted(monthly, key=lambda f: (f[3], f[4] or 0)) + sorted(daily_needed, key=lambda f: (f[3], f[4], f[5]))
        known = {r[0] for r in con.execute(sql.SQL("SELECT file_name FROM {}.bulk_file WHERE downloaded_at IS NOT NULL").format(sql.Identifier(SCHEMA))).fetchall()}
        # a file that is registered but missing from the folder is only fetched again when it is also not indexed
        todo = [f for f in plan if f[0] not in known]
        print(f"Companies House bulk accounts: {len(monthly)} monthly, {len(daily)} daily files listed"
              f"{', ' + str(len(archive)) + ' archive files from ' + str(archive_from) if archive_from else ''}; "
              f"{len(daily) - len(daily_needed)} daily files skipped (their month has a monthly file); {len(todo)} to download.", flush=True)
        if only_list:
            for f in todo:
                print("  ", f[2], f[0], f[1])
            return 0
        n_new = 0
        for name, url, kind, y, mo, dd in todo:
            if max_files and n_new >= max_files:
                print(f"Stopped after {max_files} downloads (--max-files); the rest follow on the next run.")
                break
            dest = os.path.join(folder, name)
            try:
                if not os.path.exists(dest):
                    print(f"{datetime.now():%H:%M:%S} downloading {name} ...", flush=True)
                    size = download(url, dest)
                else:
                    size = os.path.getsize(dest)
            except NotFound:
                print(f"  {name}: not found on the server (no longer offered?) - skipped")
                continue
            except RuntimeError as e:
                print(f"  {name}: {e} - will try again next run")
                continue
            con.execute(sql.SQL("INSERT INTO {}.bulk_file (file_name, kind, url, size_bytes, downloaded_at) VALUES (%s,%s,%s,%s, now()) "
                                "ON CONFLICT (file_name) DO UPDATE SET downloaded_at = now(), size_bytes = EXCLUDED.size_bytes").format(sql.Identifier(SCHEMA)), (name, kind, url, size))
            n_new += 1
            k = index_zip(con, dest, name)
            print(f"  {name}: {size / 2**20:,.0f} MB, {k:,} accounts files indexed", flush=True)
            acc = [x + y for x, y in zip(acc, load_pending(con, folder, name))]
            fill_names(con)
            if delete_after:
                try:
                    os.remove(dest)
                except OSError:
                    pass
    else:                                          # --no-download: index and read the zips already in the folder
        for fn in sorted(os.listdir(folder)):
            if fn.lower().endswith(".zip") and file_period(fn):
                done = con.execute(sql.SQL("SELECT indexed_at FROM {}.bulk_file WHERE file_name = %s").format(sql.Identifier(SCHEMA)), (fn,)).fetchone()
                if not done or not done[0]:
                    con.execute(sql.SQL("INSERT INTO {}.bulk_file (file_name, kind, size_bytes, downloaded_at) VALUES (%s,%s,%s, now()) ON CONFLICT (file_name) DO NOTHING").format(sql.Identifier(SCHEMA)),
                                (fn, "daily" if "Bulk" in fn else "monthly", os.path.getsize(os.path.join(folder, fn))))
                    print(f"  {fn}: {index_zip(con, os.path.join(folder, fn), fn):,} accounts files indexed")
    # companies that joined the master since an earlier run: read their accounts from the zips already indexed
    total, loaded, unread, dup = [x + y for x, y in zip(acc, load_pending(con, folder))]
    fill_names(con)
    print(f"Done. Accounts of your companies read in this step: {total:,} ({loaded:,} with figures, {unread:,} with no readable figures, {dup:,} already loaded).")
    return 0


# =====================================================================
def show(con):
    q = lambda s, *a: con.execute(sql.SQL(s).format(sql.Identifier(SCHEMA)), a).fetchall()
    print("Companies checked :", q("SELECT count(*) FROM {}.company_check")[0][0])
    for st, n in q("SELECT status, count(*) FROM {}.filing GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  filings {st:<20} {n:,}")
    print("Companies with figures:", q("SELECT count(DISTINCT company_number) FROM {}.financials")[0][0])
    left = con.execute(sql.SQL('SELECT count(*) FROM {m}.company c LEFT JOIN {s}.company_check k USING (company_number) WHERE c.fetch_status = %s AND k.company_number IS NULL')
                       .format(m=sql.Identifier(MASTER), s=sql.Identifier(SCHEMA)), ("ok",)).fetchone()[0]
    print("Companies in the master not yet checked:", left)
    print("\nLatest figures (5 most recent):")
    for r in q("SELECT company_number, company_name, period_end, turnover, profit_for_period, net_assets FROM {}.v_latest ORDER BY filing_date DESC NULLS LAST LIMIT 5"):
        print("  ", r)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["show", "bulk"], help="show = counts and latest figures; bulk = download Companies House accounts zips and load them")
    ap.add_argument("--setup", action="store_true", help="only create the tables")
    ap.add_argument("--number", help="check one company now")
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--recheck-days", type=int, default=RECHECK_DAYS)
    ap.add_argument("--years", type=int, default=YEARS, help="read accounts made up in the last N years (0 = all)")
    ap.add_argument("--zip", help="load a Companies House bulk accounts zip")
    ap.add_argument("--folder", default=ACCOUNTS_DIR, help="bulk: where the zip files are kept (default: D:\\Reckonant\\data\\Accounts)")
    ap.add_argument("--archive-from", type=int, help="bulk: also the archive monthly files from this year (big downloads)")
    ap.add_argument("--max-files", type=int, help="bulk: stop after this many new downloads")
    ap.add_argument("--list", action="store_true", help="bulk: only list what would be downloaded")
    ap.add_argument("--no-download", action="store_true", help="bulk: only index and read zips already in the folder")
    ap.add_argument("--delete-after", action="store_true", help="bulk: delete each zip after loading it (new companies then cannot be back-filled from it)")
    ap.add_argument("--all-daily", action="store_true", help="bulk: also daily files whose month has a monthly file")
    ap.add_argument("--all", action="store_true", help="with --zip: every company in the file, not only your company master")
    a = ap.parse_args()
    with connect() as con:
        con.execute(DDL)
        if a.setup:
            fill_names(con)                       # also adds company names to rows loaded earlier
            print("Tables ready in", SCHEMA, "- company names filled from the company master")
            return 0
        if a.cmd == "show":
            show(con)
            return 0
        if a.cmd == "bulk":
            return bulk(con, a.folder, a.archive_from, a.max_files, a.list, a.no_download, a.delete_after, a.all_daily)
        if a.zip:
            load_zip(con, a.zip, a.all)
            fill_names(con)
            return 0
        if a.number:
            n = a.number.strip().upper().zfill(8) if a.number.strip().isdigit() else a.number.strip().upper()
            new, parsed, st = check_company(con, n, a.years, verbose=True)
            print(f"{n}: {st}; {new} new set(s) of accounts, {parsed} with figures.")
            for r in con.execute(sql.SQL('SELECT period_end, turnover, profit_for_period, net_assets, cash, employees FROM {}.financials WHERE company_number = %s ORDER BY period_end DESC').format(sql.Identifier(SCHEMA)), (n,)).fetchall():
                print("  period end %s  turnover %s  profit %s  net assets %s  cash %s  employees %s" % r)
            return 0
        todo = queue(con, a.limit, a.recheck_days)
        print(f"Company financials: {len(todo)} companies to check (limit {a.limit}).", flush=True)
        t0 = time.time()
        tot_new = tot_parsed = errors = 0
        for i, n in enumerate(todo, 1):
            try:
                new, parsed, st = check_company(con, n, a.years)
                tot_new += new
                tot_parsed += parsed
            except RuntimeError as e:
                errors += 1
                con.execute(sql.SQL('INSERT INTO {}.company_check VALUES (%s, now(), NULL, NULL, %s, %s) ON CONFLICT (company_number) DO UPDATE SET last_checked_at = now(), status = %s, note = %s')
                            .format(sql.Identifier(SCHEMA)), (n, "error", str(e)[:200], "error", str(e)[:200]))
                if errors >= 25:
                    print("Too many errors in a row - stopping. Last:", e)
                    break
            if i % 50 == 0:
                print(f"  {i} of {len(todo)} companies, {tot_new} new accounts, {tot_parsed} with figures ({int(time.time() - t0)} s)", flush=True)
        fill_names(con)
        print(f"Done. {len(todo)} companies checked, {tot_new} new sets of accounts, {tot_parsed} with figures, {errors} errors.")
        return 1 if errors and errors >= 25 else 0


if __name__ == "__main__":
    sys.exit(main())
