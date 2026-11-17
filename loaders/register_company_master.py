"""register_company_master.py -- the company master (Companies House).  ONE file.

  python register_company_master.py                      = update: discover companies in your registers, fetch them (daily_update.py runs this)
  python register_company_master.py --limit 1500         the same, at most 1500 companies in this run
  python register_company_master.py --backfill --limit 300   first time: every company already in the registers, 300 per run
  python register_company_master.py reset                ONE-TIME: delete the whole "R6_company_master" schema (asks you to type DROP)
  python register_company_master.py reset --recreate     delete, then create the empty tables again

All the other options (--setup, --discover, --number, --people, --no-api, --since-days, --lookup-limit ...) are listed below.

register_company_master.py - one master table of companies, kept up to date from the Companies House API.

For every company that appears in ANY of your registers it stores:
    company name, company number, registered office address, directors, PSCs (people with significant control),
    and a trading address (see the note below), and it records every change it sees.

ONLY COMPANIES THAT SHOW UP IN YOUR REGISTERS are ever fetched - never the whole of Companies House.

HOW IT WORKS (each run, normally from daily_update.py):
    1. DISCOVER   reads the company numbers from the register entries dated in the last DISCOVER_DAYS days (default 7), for every
                  register listed in SOURCES (below). A company is queued when it is new to the master, or when it appears in a
                  register again and its saved data is more than a day old. (--backfill reads every entry, not just recent ones.)
    2. FETCH      for each queued company it calls the Companies House API:
                    /company/{number}                          name, status, registered office, SIC codes
                    /company/{number}/officers                 directors and other officers
                    /company/{number}/persons-with-significant-control      PSCs
                  and saves everything. 3 calls per company, 0.55 s apart (the API allows 600 calls per 5 minutes), so about
                  35 companies a minute. LIMIT caps one run; the rest stays queued for the next day.
    3. CHANGES    when a company is already in the master and something differs from last time (new director, director resigned,
                  new PSC, PSC ceased, name, status or registered office changed) a row is added to company_change_log.
                  The first time a company is fetched one "Company added to master" row is written.
                  The tracker page can show these as a register (COM-01).

CQC (care providers) HAS NO COMPANY NUMBERS - only a Provider name and a Location name. So for the four CQC registers the script
works by NAME (steps 1b and 1c, run automatically):
    1b. NAMES     reads the newest CQC change tables (cqc_diff.py output): rows ADDED, and rows CHANGED in a name / address / postcode
                  column. Each table is read once. Provider name, location name, address and postcode go to register_name.
    1c. MATCH     each new name is searched at Companies House (1 call). It is accepted ONLY when the name is an exact match
                  (Ltd = Limited, & = and, punctuation ignored):
                    Provider name  -> exact name match; if several companies share the name, the one at a CQC location postcode.
                    Location name  -> exact name match (a trailing Ltd/PLC/LLP is ignored) AND the company's registered-office
                                      postcode equals the location postcode (a care-home name alone could belong to another company).
                  No exact match = no_match (charities, councils, sole traders, brand names) and it is tried again after 30 days.
                  Nothing is guessed: ambiguous names are left out. Results are kept in name_match, so a name costs one call, once.
                  A matched company then goes through the normal fetch (name, directors, PSC...) and is tagged with its CQC register.
    TRADING ADDRESS of a matched company = its CQC location name + address + postcode (more than one: the first, "+N more locations").
                  A change of it is logged as "Trading address changed".

PERSONAL INSOLVENCY -> COMPANIES (step 4): a personal insolvency entry (INS-01) has a PERSON, not a company. The person is matched
to company officers in two ways, using whatever the notice gives (name, date of birth, postcode, address):
    A. the officers already saved in the master, then
    B. a Companies House officer search (1-3 calls per person, only for notices that carry a date of birth): officers with the same name AND
       the same birth month + year are accepted, their appointments (companies) are read, and those companies are added to the master.
Matching uses: first name, middle name, last name, date of birth (Companies House only publishes the MONTH and YEAR of birth, so
month + year must agree), postcode and address. If the notice and the officer both have a birth month/year and they differ, it is NOT a match.
Each link gets a confidence (saved in person_link.match_level, with the evidence that agreed):
        strong     birth month/year agrees AND the postcode or address agrees
        good       birth month/year agrees  (or postcode AND address agree, when no birth date is known)
        possible   only the postcode or the address agrees with the name
        name only  only the name agrees - COMMON NAMES WILL MATCH WRONG PEOPLE, check by hand
A new link is logged as "Officer linked to personal insolvency". Run alone:  python register_company_master.py --people   (add --no-api to skip the search)

EVERY REGISTER IS LINKED TO COMPANIES  (HMRC tax defaulters, Home office sponsors, Contracts Finder and any register you add later)
    A register entry is tied to a company in one of two ways, and the link is saved in register_link (register, entry id, company number):
      - by NUMBER   when the register carries Companies House numbers (SOURCES below: Gazette notices, Contracts Finder award winners).
      - by NAME     when it only has a name (NAME_SOURCES below: HMRC defaulter companies, sponsor organisations, Contracts Finder
                    suppliers without a number). The name is searched at Companies House and accepted ONLY on an exact match
                    (Ltd = Limited, & = and, punctuation ignored); if several companies share the name, the one at the entry's postcode;
                    anything still ambiguous is left unlinked. Only names that look like companies (Ltd, Limited, PLC, LLP, CIC ...) are searched.
    For each linked company the master saves: name, number, registered office, directors, PSCs (with their nature of control = share bands),
    status, and a trading address taken from the register entry (see below). The tracker shows these next to each register entry.
    NEW REGISTERS: at the end of every run the script checks every numbered schema in the database. A schema that is not wired up
    (not in COVERAGE) is reported as a WARNING and is also linked AUTOMATICALLY if it has an obvious company-number or company-name column;
    add a proper line to SOURCES / NAME_SOURCES (and COVERAGE) when you want control. `--coverage` prints the table by itself.
    SHAREHOLDERS: Companies House does not publish a shareholder list through its API (only in confirmation-statement filings as documents),
    so full shareholder lists cannot be stored from the API. The PSC rows (people holding more than 25% of shares or votes, with the band) are saved.

TRADING ADDRESS: Companies House does NOT publish trading addresses (only the registered office). The trading_address column is
filled from your own registers: from CQC as above, or give a second column in a SOURCES query and it is saved with its source.

Tables are created in schema "R6_company_master":
    company, company_officer, company_psc, company_source, company_queue, company_change_log, and the view v_company_master
(one row per company with the current directors and PSCs joined into text, ready for your UI).

Usage:
    python register_company_master.py                  setup + discover + fetch (up to LIMIT companies)
    python register_company_master.py --setup          only create the tables
    python register_company_master.py --discover       only read the registers and queue new companies
    python register_company_master.py --number 01234567        fetch one company now and print it
    python register_company_master.py --since-days 3          only entries from the last 3 days
    python register_company_master.py --backfill --limit 300  first time: every company already in the registers, 300 per run
    python register_company_master.py --people                only match personal insolvency people to officers (saved + Companies House search)
    python register_company_master.py --people --no-api       the same, using only the officers already saved (no API calls)
    python register_company_master.py --lookup-limit 200      most CQC name searches in one run (default 500)

Your Companies House API key (free, from developer.company-information.service.gov.uk > create an application > REST key) goes in the
environment, never in the file:      setx CH_REST_KEY "your-key"      (then open a new terminal)
Database password comes from pgpass.conf, as in your other scripts.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

import psycopg


# =====================================================================
#  1. The company master  (was company_master.py)
# =====================================================================

# =====================================================================
#  Settings
# =====================================================================
DB = dict(host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"),
          dbname=os.environ.get("PGDATABASE", "postgres"), user=os.environ.get("PGUSER", "postgres"))
SCHEMA = "R6_company_master"
API = os.environ.get("CH_API_BASE", "https://api.company-information.service.gov.uk")
KEY = os.environ.get("CH_REST_KEY", "")
DELAY = float(os.environ.get("CH_DELAY", "0.55"))     # seconds between API calls (600 calls per 5 minutes allowed)
LIMIT = 1000                                          # most companies fetched in one run
DISCOVER_DAYS = 7                                     # only register entries from the last N days queue a company (7 = catches late entries)
REFRESH_DAYS = 0                                      # 0 = a company is re-fetched only when it appears in a register again.
                                                      # e.g. 30 = also re-fetch every saved company not refreshed for 30 days (more API calls)

# Where company numbers come from. One line per register: (register code, SQL).
# The SQL must return company_number as its FIRST column. An optional SECOND column is a trading address from that register,
# an optional THIRD is the entry's id in that register (so the tracker can show the company beside the entry).
# Keep the %(since)s date filter so that only RECENT register entries queue a company.
# Add a line whenever you add a register that has company numbers.
SOURCES = [
    ("INS-02", '''SELECT DISTINCT company_number FROM "R5_insolvency".gazette_notice WHERE company_number IS NOT NULL AND published_date >= %(since)s'''),
    # Contracts Finder: suppliers named in award notices. Columns: company number, trading address, entry id (the notice).
    ("CON-01", '''SELECT sp.company_number, sp."Address", sp."Release ID" FROM "R12_contracts_finder".supplier sp
                JOIN "R12_contracts_finder".notice n ON n."Release ID" = sp."Release ID"
                WHERE sp.company_number IS NOT NULL AND sp.company_number <> '' AND n."Published" >= %(since)s'''),
    # Find a Tender: same shape as Contracts Finder.
    ("FTS-01", '''SELECT sp.company_number, sp."Address", sp."Release ID" FROM "R13_find_a_tender".supplier sp
                JOIN "R13_find_a_tender".notice n ON n."Release ID" = sp."Release ID"
                WHERE sp.company_number IS NOT NULL AND sp.company_number <> '' AND n."Published" >= %(since)s'''),
    # Payments Transparency Register: the supplier's Companies House number shown on the payment.
    ("PTR-01", '''SELECT p.company_number, NULL::text, p."Payment ID" FROM "R14_payments_transparency".payment p
                WHERE p.company_number IS NOT NULL AND p.company_number <> '' AND p."Published date" >= %(since)s'''),
    # CQC has no company numbers - it is read by NAME (see CQC_NAME_SOURCES below).
]

# ---- CQC: registers that have names but no company numbers (read from the cqc_diff.py tables) ----
CQC_NAME_SOURCES = [("CQC-01", "R1_cqc_care_directory"), ("CQC-02", "R2_care_directory_with_filters"),
                    ("CQC-03", "R3_care_directory_with_ratings"), ("CQC-04", "R4_deactivated_locations")]
# Column names tried (in this order) in the diff tables, as <name>_curr. Add yours here if the script says "not found".
CQC_COLUMNS = {"provider": ["provider_name"],
               "location": ["location_name", "name"],
               "address": ["location_street_address", "location_address", "address", "street_address"],
               "town": ["location_city", "location_town_city", "town_city", "town", "city"],
               "postcode": ["location_postal_code", "location_postcode", "postcode", "postal_code"]}
# Personal insolvency: who to match against the officers. One line per register: (register code, SQL).
# The SQL returns: reference, person name, postcode, date  (keep the %(since)s filter).
PERSON_SOURCES = [
    # reference, person name, postcode, date, address, notice text (the date of birth is read from the text)
    ("INS-01", '''SELECT v.notice_id::text, v.party_name, v.postcode, v.published_date, g.address, v.body_text
                FROM "R5_insolvency".v_insolvency_notice v JOIN "R5_insolvency".gazette_notice g ON g.notice_id = v.notice_id
                WHERE v.insolvency_kind = 'personal' AND v.party_name IS NOT NULL AND v.published_date >= %(since)s'''),
    ("INS-01", '''SELECT court_number, person_name, postcode, order_date, NULL::text, conduct FROM "R5_insolvency".restriction_order
                WHERE person_name IS NOT NULL AND order_date >= %(since)s'''),
]
PEOPLE_SEARCH_PAGES = 3      # officer search pages (100 results each) read per person
PEOPLE_LOOKUP_LIMIT = 200   # most people searched at Companies House in one run
PEOPLE_ADD_COMPANIES = True # companies found through a bankrupt person's appointments are added to the master (False = link only)
PEOPLE_DAYS = 30            # personal insolvency entries of the last N days are (re)matched on every run
LOOKUP_LIMIT = 3000         # most Companies House name searches in one run (1 call each, about 0.55 s)
RECHECK_NO_MATCH_DAYS = 30  # a name with no exact match is searched again after this many days
MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"])}

DIRECTOR_WORDS = ("director", "llp-member", "llp-designated-member")

DDL = f'''
CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";

CREATE TABLE IF NOT EXISTS "{SCHEMA}".company (
    company_number          text PRIMARY KEY,
    company_name            text,
    company_status          text,
    company_type            text,
    date_of_creation        date,
    date_of_cessation       date,
    sic_codes               text[],
    registered_office       text,              -- whole address on one line
    ro_premises             text,
    ro_address_line_1       text,
    ro_address_line_2       text,
    ro_locality             text,
    ro_region               text,
    ro_postal_code          text,
    ro_country              text,
    trading_address         text,              -- from your own registers only (Companies House does not hold it)
    trading_address_source  text,
    fetch_status            text NOT NULL DEFAULT 'ok' CHECK (fetch_status IN ('ok','not_found')),
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_fetched_at         timestamptz
);
CREATE INDEX IF NOT EXISTS company_name_idx ON "{SCHEMA}".company (lower(company_name) text_pattern_ops);

CREATE TABLE IF NOT EXISTS "{SCHEMA}".company_officer (
    company_number  text NOT NULL REFERENCES "{SCHEMA}".company ON DELETE CASCADE,
    officer_key     text NOT NULL,             -- officer id | role | appointed date
    officer_name    text,
    officer_role    text,
    is_director     boolean NOT NULL DEFAULT false,
    appointed_on    date,
    resigned_on     date,
    nationality     text,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_number, officer_key)
);
ALTER TABLE "{SCHEMA}".company_officer ADD COLUMN IF NOT EXISTS officer_postcode text;
ALTER TABLE "{SCHEMA}".company_officer ADD COLUMN IF NOT EXISTS birth_month_year text;      -- 'YYYY-MM'
ALTER TABLE "{SCHEMA}".company_officer ADD COLUMN IF NOT EXISTS officer_address text;
CREATE INDEX IF NOT EXISTS company_officer_name_idx ON "{SCHEMA}".company_officer (lower(officer_name) text_pattern_ops);

CREATE TABLE IF NOT EXISTS "{SCHEMA}".company_psc (
    company_number      text NOT NULL REFERENCES "{SCHEMA}".company ON DELETE CASCADE,
    psc_key             text NOT NULL,
    psc_name            text,
    psc_kind            text,                  -- individual / corporate-entity / legal-person ...
    notified_on         date,
    ceased_on           date,
    ceased              boolean NOT NULL DEFAULT false,
    natures_of_control  text[],
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_number, psc_key)
);
CREATE INDEX IF NOT EXISTS company_psc_name_idx ON "{SCHEMA}".company_psc (lower(psc_name) text_pattern_ops);

-- which register(s) each company came from
CREATE TABLE IF NOT EXISTS "{SCHEMA}".company_source (
    company_number  text NOT NULL,
    register_code   text NOT NULL,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_number, register_code)
);

-- which register entry belongs to which company (number or exact-name match)
CREATE TABLE IF NOT EXISTS "{SCHEMA}".register_link (
    source_code     text NOT NULL,             -- HMD-01, HOS-01, CON-01 ...
    source_key      text NOT NULL,             -- the entry's id in that register (Defaulter ID, Sponsor ID, Release ID ...)
    company_number  text NOT NULL,
    how             text NOT NULL,             -- number | name
    linked_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_code, source_key, company_number)
);
CREATE INDEX IF NOT EXISTS register_link_company_idx ON "{SCHEMA}".register_link (company_number);

-- companies waiting to be fetched
CREATE TABLE IF NOT EXISTS "{SCHEMA}".company_queue (
    company_number  text PRIMARY KEY,
    queued_at       timestamptz NOT NULL DEFAULT now(),
    attempts        integer NOT NULL DEFAULT 0,
    next_try_at     timestamptz NOT NULL DEFAULT now(),
    last_error      text
);

CREATE TABLE IF NOT EXISTS "{SCHEMA}".company_change_log (
    change_id       bigserial PRIMARY KEY,
    company_number  text NOT NULL,
    company_name    text,
    change_type     text NOT NULL,
    detail          text,
    detected_at     timestamptz NOT NULL DEFAULT now(),
    detected_date   date NOT NULL DEFAULT current_date
);
CREATE INDEX IF NOT EXISTS company_change_log_date_idx ON "{SCHEMA}".company_change_log (detected_date DESC, change_type);

-- personal insolvency person  ->  officer of a company in the master
CREATE TABLE IF NOT EXISTS "{SCHEMA}".person_link (
    source_code      text NOT NULL,             -- INS-01
    source_ref       text NOT NULL,             -- notice id, or restriction-order court number
    person_name      text,
    notice_date      date,
    person_postcode  text,
    company_number   text NOT NULL,
    officer_key      text NOT NULL,
    officer_name     text,
    officer_role     text,
    resigned_on      date,
    match_level      text NOT NULL,             -- e.g. "strong: name, birth month/year, postcode"
    linked_at        timestamptz NOT NULL DEFAULT now(),
    person_dob       date,
    company_name     text,
    PRIMARY KEY (source_code, source_ref, company_number, officer_key)
);
ALTER TABLE "{SCHEMA}".person_link ADD COLUMN IF NOT EXISTS person_dob date;
ALTER TABLE "{SCHEMA}".person_link ADD COLUMN IF NOT EXISTS company_name text;
-- people already searched at Companies House
CREATE TABLE IF NOT EXISTS "{SCHEMA}".person_lookup (
    source_code text NOT NULL,
    source_ref  text NOT NULL,
    status      text NOT NULL,                  -- done | none | ambiguous | error
    links       integer,
    detail      text,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_code, source_ref)
);
CREATE INDEX IF NOT EXISTS person_link_company_idx ON "{SCHEMA}".person_link (company_number);

-- names (no company number) read from registers such as CQC
CREATE TABLE IF NOT EXISTS "{SCHEMA}".register_name (
    source_code    text NOT NULL,
    source_key     text NOT NULL,
    provider_name  text,
    provider_norm  text,
    location_name  text,
    location_norm  text,
    address        text,
    postcode       text,
    postcode_norm  text,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    seen_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_code, source_key)
);
CREATE INDEX IF NOT EXISTS register_name_prov_idx ON "{SCHEMA}".register_name (provider_norm);
CREATE INDEX IF NOT EXISTS register_name_loc_idx ON "{SCHEMA}".register_name (location_norm);

-- result of searching each name at Companies House
CREATE TABLE IF NOT EXISTS "{SCHEMA}".name_match (
    name_norm       text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('provider','location')),
    company_number  text,
    matched_title   text,
    match_postcode  text,
    status          text NOT NULL CHECK (status IN ('matched','no_match','ambiguous','error')),
    detail          text,
    checked_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (name_norm, kind)
);

-- which change tables have been read already
CREATE TABLE IF NOT EXISTS "{SCHEMA}".name_diff_done (
    schema_name text NOT NULL,
    diff_table  text NOT NULL,
    rows_read   integer,
    done_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schema_name, diff_table)
);

CREATE OR REPLACE VIEW "{SCHEMA}".v_company_master AS
SELECT c.company_number, c.company_name, c.company_status, c.registered_office, c.trading_address,
       (SELECT string_agg(o.officer_name, '; ' ORDER BY o.officer_name) FROM "{SCHEMA}".company_officer o
         WHERE o.company_number = c.company_number AND o.is_director AND o.resigned_on IS NULL) AS directors,
       (SELECT string_agg(p.psc_name, '; ' ORDER BY p.psc_name) FROM "{SCHEMA}".company_psc p
         WHERE p.company_number = c.company_number AND NOT p.ceased) AS pscs,
       (SELECT string_agg(s.register_code, ', ' ORDER BY s.register_code) FROM "{SCHEMA}".company_source s
         WHERE s.company_number = c.company_number) AS registers,
       c.fetch_status, c.last_fetched_at,
       (SELECT string_agg(o.officer_name || ' (' || COALESCE(o.officer_role, '') || CASE WHEN o.appointed_on IS NOT NULL THEN ', appointed ' || o.appointed_on ELSE '' END || ')',
                          '; ' ORDER BY o.officer_name) FROM "{SCHEMA}".company_officer o
         WHERE o.company_number = c.company_number AND o.resigned_on IS NULL) AS officers_detail,
       (SELECT string_agg(p.psc_name || ' (' || COALESCE(array_to_string(p.natures_of_control, ', '), p.psc_kind, '') || ')', '; ' ORDER BY p.psc_name)
          FROM "{SCHEMA}".company_psc p WHERE p.company_number = c.company_number AND NOT p.ceased) AS pscs_detail,
       c.company_type, c.date_of_creation
FROM "{SCHEMA}".company c;
'''


# =====================================================================
#  Companies House API
# =====================================================================
class NotFound(Exception):
    pass


def api_get(path, tries=6):
    if not KEY:
        sys.exit("CH_REST_KEY is not set. Get a free REST key at developer.company-information.service.gov.uk, then:  setx CH_REST_KEY \"your-key\"  and open a new terminal.")
    auth = "Basic " + base64.b64encode((KEY + ":").encode()).decode()
    last = None
    for i in range(tries):
        time.sleep(DELAY)
        try:
            req = urllib.request.Request(API + path, headers={"Authorization": auth, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(path)
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
    raise RuntimeError(f"{path}: {last}")


def api_pages(path):
    items, start = [], 0
    while True:
        d = api_get(f"{path}{'&' if '?' in path else '?'}items_per_page=100&start_index={start}")
        items += d.get("items") or []
        start += 100
        if start >= int(d.get("total_results") or d.get("active_count") or len(items)) or not d.get("items"):
            return items


def fetch_company(number):
    profile = api_get(f"/company/{number}")
    officers = api_pages(f"/company/{number}/officers")
    try:
        pscs = api_pages(f"/company/{number}/persons-with-significant-control")
    except NotFound:
        pscs = []                                   # companies with no PSC register answer 404
    return profile, officers, pscs


# =====================================================================
#  Saving
# =====================================================================
def norm_number(s):
    s = re.sub(r"\s+", "", str(s or "")).upper()
    if re.fullmatch(r"\d{1,8}", s):
        s = s.zfill(8)
    return s if re.fullmatch(r"\d{8}|[A-Z]{2}\d{6}", s) else None


def addr_line(a):
    return ", ".join(str(a[k]) for k in ("care_of", "po_box", "premises", "address_line_1", "address_line_2", "locality", "region", "postal_code", "country") if a.get(k))


def to_date(s):
    return s or None


def officer_key(o):
    oid = ((o.get("links") or {}).get("officer") or {}).get("appointments", "").strip("/").split("/")
    oid = oid[1] if len(oid) > 1 else (o.get("name") or "")
    return f"{oid}|{o.get('officer_role')}|{o.get('appointed_on')}"


def psc_key(p):
    return ((p.get("links") or {}).get("self") or p.get("etag") or p.get("name") or "").strip()


def log_change(con, number, name, ctype, detail):
    con.execute(f'INSERT INTO "{SCHEMA}".company_change_log (company_number, company_name, change_type, detail) VALUES (%s,%s,%s,%s)',
                (number, name, ctype, detail[:500] if detail else None))


def save_company(con, number, profile, officers, pscs, trading=None, trading_src=None):
    S = f'"{SCHEMA}"'
    ro = profile.get("registered_office_address") or {}
    name, status, ro_text = profile.get("company_name"), profile.get("company_status"), addr_line(ro)
    old = con.execute(f"SELECT company_name, company_status, registered_office FROM {S}.company WHERE company_number=%s", (number,)).fetchone()
    old_off = {r[0]: r[1] for r in con.execute(f"SELECT officer_key, resigned_on FROM {S}.company_officer WHERE company_number=%s", (number,))}
    old_psc = {r[0]: r[1] for r in con.execute(f"SELECT psc_key, ceased FROM {S}.company_psc WHERE company_number=%s", (number,))}
    con.execute(f"""
        INSERT INTO {S}.company (company_number, company_name, company_status, company_type, date_of_creation, date_of_cessation, sic_codes,
            registered_office, ro_premises, ro_address_line_1, ro_address_line_2, ro_locality, ro_region, ro_postal_code, ro_country,
            fetch_status, last_fetched_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ok',now())
        ON CONFLICT (company_number) DO UPDATE SET company_name=EXCLUDED.company_name, company_status=EXCLUDED.company_status,
            company_type=EXCLUDED.company_type, date_of_creation=EXCLUDED.date_of_creation, date_of_cessation=EXCLUDED.date_of_cessation,
            sic_codes=EXCLUDED.sic_codes, registered_office=EXCLUDED.registered_office, ro_premises=EXCLUDED.ro_premises,
            ro_address_line_1=EXCLUDED.ro_address_line_1, ro_address_line_2=EXCLUDED.ro_address_line_2, ro_locality=EXCLUDED.ro_locality,
            ro_region=EXCLUDED.ro_region, ro_postal_code=EXCLUDED.ro_postal_code, ro_country=EXCLUDED.ro_country,
            fetch_status='ok', last_fetched_at=now()""",
                (number, name, status, profile.get("type"), to_date(profile.get("date_of_creation")), to_date(profile.get("date_of_cessation")),
                 profile.get("sic_codes"), ro_text, ro.get("premises"), ro.get("address_line_1"), ro.get("address_line_2"),
                 ro.get("locality"), ro.get("region"), ro.get("postal_code"), ro.get("country")))
    if trading:
        con.execute(f"UPDATE {S}.company SET trading_address=%s, trading_address_source=%s WHERE company_number=%s", (trading[:500], trading_src, number))

    for o in officers:
        k = officer_key(o)
        role = o.get("officer_role") or ""
        isdir = any(w in role for w in DIRECTOR_WORDS)
        con.execute(f"""
            INSERT INTO {S}.company_officer (company_number, officer_key, officer_name, officer_role, is_director, appointed_on, resigned_on, nationality,
                                             officer_postcode, birth_month_year, officer_address)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (company_number, officer_key) DO UPDATE SET officer_name=EXCLUDED.officer_name, resigned_on=EXCLUDED.resigned_on,
                nationality=EXCLUDED.nationality, officer_postcode=EXCLUDED.officer_postcode, birth_month_year=EXCLUDED.birth_month_year,
                officer_address=EXCLUDED.officer_address, last_seen_at=now()""",
                    (number, k, o.get("name"), role, isdir, to_date(o.get("appointed_on")), to_date(o.get("resigned_on")), o.get("nationality"),
                     (o.get("address") or {}).get("postal_code"),
                     (lambda d: f"{d.get('year')}-{int(d.get('month')):02d}" if d.get("year") and d.get("month") else None)(o.get("date_of_birth") or {}),
                     addr_line(o.get("address") or {}) or None))
    for p in pscs:
        k = psc_key(p)
        con.execute(f"""
            INSERT INTO {S}.company_psc (company_number, psc_key, psc_name, psc_kind, notified_on, ceased_on, ceased, natures_of_control)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (company_number, psc_key) DO UPDATE SET psc_name=EXCLUDED.psc_name, ceased_on=EXCLUDED.ceased_on,
                ceased=EXCLUDED.ceased, natures_of_control=EXCLUDED.natures_of_control, last_seen_at=now()""",
                    (number, k, p.get("name"), p.get("kind"), to_date(p.get("notified_on")), to_date(p.get("ceased_on")),
                     bool(p.get("ceased") or p.get("ceased_on")), p.get("natures_of_control")))

    # ---- what changed since the last fetch ----
    if old is None:
        log_change(con, number, name, "Company added to master", f"{name} ({status})")
        return "new"
    n = 0
    if (old[0] or "") != (name or ""):
        log_change(con, number, name, "Company name changed", f"{old[0]} -> {name}"); n += 1
    if (old[1] or "") != (status or ""):
        log_change(con, number, name, "Status changed", f"{old[1]} -> {status}"); n += 1
    if (old[2] or "") != ro_text:
        log_change(con, number, name, "Registered office changed", f"{old[2]} -> {ro_text}"); n += 1
    for o in officers:
        k, isdir = officer_key(o), any(w in (o.get("officer_role") or "") for w in DIRECTOR_WORDS)
        who = "Director" if isdir else "Other officer"
        if k not in old_off:
            log_change(con, number, name, f"{who} added", f"{o.get('name')} ({o.get('officer_role')}), appointed {o.get('appointed_on')}"); n += 1
        elif old_off[k] is None and o.get("resigned_on"):
            log_change(con, number, name, f"{who} resigned", f"{o.get('name')} ({o.get('officer_role')}), resigned {o.get('resigned_on')}"); n += 1
    for p in pscs:
        k, ceased = psc_key(p), bool(p.get("ceased") or p.get("ceased_on"))
        if k not in old_psc:
            log_change(con, number, name, "PSC added" if not ceased else "PSC ceased", f"{p.get('name')}, notified {p.get('notified_on')}"); n += 1
        elif not old_psc[k] and ceased:
            log_change(con, number, name, "PSC ceased", f"{p.get('name')}, ceased {p.get('ceased_on')}"); n += 1
    return f"{n} change(s)" if n else "no change"


# =====================================================================
#  Steps
# =====================================================================
def discover(con, since, sources=None):
    S = f'"{SCHEMA}"'
    total_new = 0
    for code, query in (SOURCES if sources is None else sources):
        try:
            rows = con.execute(query, {"since": since}).fetchall()
        except psycopg.Error as e:
            print(f"  {code}: source not readable yet ({str(e).splitlines()[0]})")
            continue
        seen = {}
        bad = 0
        for r in rows:
            n = norm_number(r[0])
            if not n:
                bad += 1
                continue
            seen.setdefault(n, (r[1] if len(r) > 1 else None))
        links = {(str(r[2]), norm_number(r[0])) for r in rows if len(r) > 2 and r[2] is not None and norm_number(r[0])}
        with con.transaction():
            for key, n in links:
                con.execute(f"INSERT INTO {S}.register_link (source_code, source_key, company_number, how) VALUES (%s,%s,%s,'number') ON CONFLICT DO NOTHING", (code, key, n))
            for n, trade in seen.items():
                con.execute(f"INSERT INTO {S}.company_source (company_number, register_code) VALUES (%s,%s) ON CONFLICT DO NOTHING", (n, code))
                if trade:
                    con.execute(f"UPDATE {S}.company SET trading_address=%s, trading_address_source=%s WHERE company_number=%s AND trading_address IS DISTINCT FROM %s",
                                (str(trade)[:500], code, n, str(trade)[:500]))
            con.execute(f"""INSERT INTO {S}.company_queue (company_number)
                            SELECT n FROM unnest(%s::text[]) n
                            WHERE NOT EXISTS (SELECT 1 FROM {S}.company c WHERE c.company_number=n AND c.last_fetched_at > now() - interval '1 day')
                            ON CONFLICT DO NOTHING""", (list(seen),))
        added = con.execute(f"SELECT count(*) FROM {S}.company_queue").fetchone()[0]
        print(f"  {code}: {len(seen)} companies in entries since {since}{f', {bad} not valid UK numbers skipped' if bad else ''}; queue now {added}")
        total_new = added
    return total_new


# ---------------------------------------------------------------------
#  CQC (and any register with names but no company numbers)
# ---------------------------------------------------------------------
def name_norm(text):
    s = str(text or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\bLIMITED\b", "LTD", s)
    s = re.sub(r"^\s*THE\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def pc_norm(text):
    return re.sub(r"\s+", "", str(text or "")).upper()


def qi(c):
    return '"' + c.replace('"', '""') + '"'


def first_col(cols, options):
    return next((o for o in options if o + "_curr" in cols), None)


def read_diff(con, code, schema, diff_table, tname):
    """Reads one CQC change table; saves provider / location names. Returns the number of rows kept (None = columns not found)."""
    S = f'"{SCHEMA}"'
    cols = [c.name for c in con.execute(f'SELECT * FROM "{schema}"."{diff_table}" LIMIT 0').description]
    is_prov = tname.lower().endswith("providers")
    prov = first_col(cols, CQC_COLUMNS["provider"]) or ("name" if is_prov and "name_curr" in cols else None)
    loc = None if is_prov else first_col(cols, CQC_COLUMNS["location"])
    addr, town, pc = (first_col(cols, CQC_COLUMNS[k]) for k in ("address", "town", "postcode"))
    if not prov and not loc:
        names = [c for c in cols if "name" in c][:8]
        print(f"  {code} {tname}: no provider / location name column found (name columns here: {', '.join(names) or 'none'}) - "
              f"add yours to CQC_COLUMNS at the top of this file")
        return None
    picked = [c for c in (prov, loc, addr, town, pc) if c]
    meta = [c for c in ("key_value", "change_type", "changed_columns") if c in cols]
    cur = con.execute(f'SELECT {", ".join(qi(c) for c in meta + [p + "_curr" for p in picked])} FROM "{schema}"."{diff_table}" '
                      + ("WHERE change_type IN ('ADDED','CHANGED')" if "change_type" in cols else ""))
    keep = 0
    with con.transaction():
        for row in cur.fetchall():
            r = dict(zip(meta + picked, row))
            if r.get("change_type") == "CHANGED":
                changed = {x.strip() for x in str(r.get("changed_columns") or "").split(",")}
                if not changed & set(picked):
                    continue                       # e.g. only the rating changed: no new name
            g = lambda c: (str(r[c]).strip() if c and r.get(c) not in (None, "") else "")
            pn, ln = g(prov), g(loc)
            if not pn and not ln:
                continue
            key = f"{tname}:{r.get('key_value') or (pn + '|' + ln + '|' + g(pc))}"
            address = ", ".join(x for x in (g(addr), g(town), g(pc)) if x)
            con.execute(f"""INSERT INTO {S}.register_name (source_code, source_key, provider_name, provider_norm, location_name, location_norm,
                                                          address, postcode, postcode_norm)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (source_code, source_key) DO UPDATE SET provider_name=EXCLUDED.provider_name,
                              provider_norm=EXCLUDED.provider_norm, location_name=EXCLUDED.location_name, location_norm=EXCLUDED.location_norm,
                              address=EXCLUDED.address, postcode=EXCLUDED.postcode, postcode_norm=EXCLUDED.postcode_norm, seen_at=now()""",
                        (code, key, pn or None, name_norm(pn) if len(name_norm(pn)) >= 4 else "", ln or None,
                         name_norm(ln) if len(name_norm(ln)) >= 4 else "", address or None, g(pc) or None, pc_norm(g(pc)) or None))
            keep += 1
        con.execute(f"INSERT INTO {S}.name_diff_done (schema_name, diff_table, rows_read) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (schema, diff_table, keep))
    found = ", ".join(f"{k}={v}" for k, v in (("provider", prov), ("location", loc), ("address", addr), ("postcode", pc)) if v)
    print(f"  {code} {tname}: {keep} names read ({found})")
    return keep


def discover_names(con, backfill):
    """Reads the newest CQC change table of each CQC table (every unread one with --backfill). Each is read once."""
    S = f'"{SCHEMA}"'
    for code, schema in CQC_NAME_SOURCES:
        try:
            diffs = con.execute("SELECT diff_table, current_table FROM public.cqc_diff_summary WHERE schema_name=%s", (schema,)).fetchall()
        except psycopg.Error as e:
            print(f"  {code}: CQC change tables not available yet ({str(e).splitlines()[0]})")
            continue
        parsed = []
        for dt, ct in diffs:
            m = re.match(r"^(\d{1,2})_([a-z]+)_(\d{4})_(.+)$", ct or "")
            if m and m.group(2) in MONTHS:
                parsed.append((date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))), m.group(4), dt))
        if not backfill:
            newest = {}
            for d, tname, dt in parsed:
                if tname not in newest or d > newest[tname][0]:
                    newest[tname] = (d, tname, dt)
            parsed = list(newest.values())
        done = {r[0] for r in con.execute(f"SELECT diff_table FROM {S}.name_diff_done WHERE schema_name=%s", (schema,))}
        todo = [p for p in sorted(parsed) if p[2] not in done]
        if not todo:
            print(f"  {code}: nothing new to read")
        for d, tname, dt in todo:
            try:
                read_diff(con, code, schema, dt, tname)
            except psycopg.Error as e:
                print(f"  {code} {tname}: could not read ({str(e).splitlines()[0]})")


# ---------------------------------------------------------------------
#  Registers with NAMES only (HMRC defaulters, Home Office sponsors, Contracts Finder suppliers without a number ...)
#  Each reader returns [(entry id, name, address, postcode)] for EVERY entry; only new or altered ones are written (see discover_register_names).
# ---------------------------------------------------------------------
COMPANY_WORD = re.compile(r"\b(LTD|LIMITED|PLC|LLP|LP|CIC|CYF|CCC|INC|CORP|CORPORATION)\b\.?\)?$")
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2})\b", re.I)


def looks_like_company(name, sheet=""):
    n = re.sub(r"[^A-Za-z0-9 &.()]", " ", str(name or "")).upper().strip()
    return bool(COMPANY_WORD.search(n)) or "COMPAN" in str(sheet).upper()


def find_postcode(text):
    m = POSTCODE_RE.search(str(text or ""))
    return m.group(1).upper() if m else None


def columns_of(con, schema, rel):
    return [r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                                      (schema, rel))]


def first_nonempty(cols):
    return "COALESCE(" + ", ".join(f"NULLIF(btrim({qi(c)}::text), '')" for c in cols) + ")"


def read_hmrc_names(con, since, backfill):
    S, V = "R10_hmrc_tax_defaulters", "v_defaulter"
    cols = columns_of(con, S, V)
    if not cols:
        raise psycopg.errors.UndefinedTable("no v_defaulter yet")
    ncols = sorted((c for c in cols if "name" in c.lower() and not re.search(r"trad|previous|former|business|nature|director|partner", c.lower())),
                   key=lambda c: 0 if re.search("compan|organisation", c.lower()) else 1)
    acols = [c for c in cols if re.search(r"address|registered office", c.lower())]
    if not ncols:
        print("  HMD-01: no name column found in the defaulters list - tell Claude the column names")
        return []
    addr = "concat_ws(', ', " + ", ".join(f"NULLIF(btrim({qi(c)}::text), '')" for c in acols) + ")" if acols else "NULL::text"
    q = f'SELECT "Defaulter ID"::text, {first_nonempty(ncols)}, {addr}, "Sheet" FROM "{S}".{V}'
    return [(k, n, a, find_postcode(a)) for k, n, a, sh in con.execute(q).fetchall() if n and looks_like_company(n, sh)]


def read_sponsor_names(con, since, backfill):
    S, V = "R11_sponsor_register", "v_sponsor"
    cols = columns_of(con, S, V)
    if not cols:
        raise psycopg.errors.UndefinedTable("no v_sponsor yet")
    ncols = [c for c in cols if "organisation" in c.lower()] or [c for c in cols if "name" in c.lower()]
    acols = [c for c in cols if re.search(r"town|city|county", c.lower())]
    if not ncols:
        print("  HOS-01: no organisation name column found - tell Claude the column names")
        return []
    addr = "concat_ws(', ', " + ", ".join(f"NULLIF(btrim({qi(c)}::text), '')" for c in acols) + ")" if acols else "NULL::text"
    q = f'SELECT "Sponsor ID"::text, {first_nonempty(ncols[:1])}, {addr} FROM "{S}".{V} WHERE "On register"'
    return [(k, n, a, None) for k, n, a in con.execute(q).fetchall() if n and looks_like_company(n)]


def read_contract_supplier_names(con, since, backfill):
    q = """SELECT sp."Release ID" || '|' || sp.supplier_row_id, sp."Supplier name", sp."Address", sp."Postcode"
           FROM "R12_contracts_finder".supplier sp JOIN "R12_contracts_finder".notice n ON n."Release ID" = sp."Release ID"
           WHERE (sp.company_number IS NULL OR sp.company_number = '') AND sp."Supplier name" IS NOT NULL"""
    return [(k, n, a, pc or find_postcode(a)) for k, n, a, pc in con.execute(q).fetchall() if looks_like_company(n)]


def read_findtender_supplier_names(con, since, backfill):
    q = """SELECT sp."Release ID" || '|' || sp.supplier_row_id, sp."Supplier name", sp."Address", sp."Postcode"
           FROM "R13_find_a_tender".supplier sp JOIN "R13_find_a_tender".notice n ON n."Release ID" = sp."Release ID"
           WHERE (sp.company_number IS NULL OR sp.company_number = '') AND sp."Supplier name" IS NOT NULL"""
    return [(k, n, a, pc or find_postcode(a)) for k, n, a, pc in con.execute(q).fetchall() if looks_like_company(n)]


def read_payment_supplier_names(con, since, backfill):
    q = """SELECT p."Payment ID", p."Supplier", NULL::text, NULL::text FROM "R14_payments_transparency".payment p
           WHERE (p.company_number IS NULL OR p.company_number = '') AND p."Supplier" IS NOT NULL"""
    return [(k, n, a, pc) for k, n, a, pc in con.execute(q).fetchall() if looks_like_company(n)]


# One line per register that has names but no company number: (register code, reader).
# A reader returns [(entry id, name, address, postcode)]. The entry id is the register's own id (the part before a | is the link key).
NAME_SOURCES = [
    ("HMD-01", read_hmrc_names),
    ("HOS-01", read_sponsor_names),
    ("CON-01", read_contract_supplier_names),
    ("FTS-01", read_findtender_supplier_names),
    ("PTR-01", read_payment_supplier_names),
]

# Every numbered schema of the database and how it reaches the company master. A schema missing here is reported (and auto-linked, see auto_sources).
COVERAGE = {
    "R1_cqc_care_directory": "CQC-01, by name (cqc change tables)", "R2_care_directory_with_filters": "CQC-02, by name", "R3_care_directory_with_ratings": "CQC-03, by name",
    "R4_deactivated_locations": "CQC-04, by name", "R5_insolvency": "INS-01 people / INS-02 company numbers", "R6_company_master": None,
    "R7_corporate_notices": "INS-02 company numbers (read from 5_insolvency)", "R8_personal_notices": "INS-01 people (read from 5_insolvency)",
    "R9_other_notices": "no company link: People notices that are not about a company",
    "R10_hmrc_tax_defaulters": "HMD-01, by name", "R11_sponsor_register": "HOS-01, by name", "R12_contracts_finder": "CON-01, by number and by name",
    "R13_find_a_tender": "FTS-01, by number and by name",
    "R14_payments_transparency": "PTR-01, by number and by name",
}
NUM_COL = re.compile(r"(compan|companies).*(number|no$|num|reg)|^crn$|registered_?(number|no)|^co_?(number|no)$", re.I)
NAME_COL = re.compile(r"^(company|organisation|organization|supplier|business|employer|firm|provider|party)_?name$|^name$|^name_of_(company|organisation)$", re.I)
SKIP_TABLE = re.compile(r"change|log|queue|raw|snapshot|key|^_|link|match|lookup|fetch", re.I)


def uncovered_schemas(con):
    return [r[0] for r in con.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name ~ '^[0-9]+_' ORDER BY 1").fetchall()
            if r[0] not in COVERAGE]


def auto_sources(con):
    """A numbered schema nobody wired up: look for an obvious company-number column (-> numbers) or company-name column (-> names). Returns
    (number sources for discover(), name sources for discover_register_names()). The register code is the schema name."""
    nums, names = [], []
    for sch in uncovered_schemas(con):
        tabs = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type IN ('BASE TABLE','VIEW') ORDER BY 1", (sch,)).fetchall()
        got = False
        for (t,) in tabs:
            if SKIP_TABLE.search(t):
                continue
            cols = columns_of(con, sch, t)
            nc = next((c for c in cols if NUM_COL.search(c)), None)
            mc = next((c for c in cols if NAME_COL.search(c)), None)
            if not cols or not (nc or mc):
                continue
            key = qi(cols[0])
            if nc:
                nums.append((sch, f'SELECT {qi(nc)}::text, NULL, {key}::text FROM "{sch}"."{t}" WHERE {qi(nc)} IS NOT NULL'))
                print(f"  WARNING schema {sch} is not in COVERAGE - linked automatically by company number: {t}.{nc} (entry id = {cols[0]})")
            else:
                def reader(con, since, backfill, sch=sch, t=t, mc=mc, key=key):
                    return [(k, n, None, None) for k, n in con.execute(f'SELECT {key}::text, {qi(mc)}::text FROM "{sch}"."{t}" WHERE {qi(mc)} IS NOT NULL').fetchall()
                            if looks_like_company(n)]
                names.append((sch, reader))
                print(f"  WARNING schema {sch} is not in COVERAGE - linked automatically by company name: {t}.{mc} (entry id = {cols[0]})")
            got = True
        if not got:
            print(f"  WARNING schema {sch} is not in COVERAGE and has no obvious company number / company name column - its companies are NOT in the master. "
                  f"Add a line to SOURCES or NAME_SOURCES in register_company_master.py (ask Claude), then add the schema to COVERAGE.")
    return nums, names


def discover_register_names(con, since, backfill, sources=None):
    """Saves the names of NAME_SOURCES registers in register_name; match_names() then searches them at Companies House."""
    S = f'"{SCHEMA}"'
    for code, reader in (NAME_SOURCES if sources is None else sources):
        try:
            rows = reader(con, since, backfill)
        except psycopg.Error as e:
            print(f"  {code}: not readable yet ({str(e).splitlines()[0]})")
            continue
        data = []
        for key, name, addr, pc in rows:
            nn = name_norm(name)
            if len(nn) < 4:
                continue
            data.append((code, str(key), str(name)[:300], nn, (str(addr)[:400] if addr else None), pc, pc_norm(pc) or None))
        with con.transaction():
            con.cursor().executemany(f"""INSERT INTO {S}.register_name (source_code, source_key, provider_name, provider_norm, address, postcode, postcode_norm)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (source_code, source_key) DO UPDATE SET provider_name=EXCLUDED.provider_name,
                    provider_norm=EXCLUDED.provider_norm, address=EXCLUDED.address, postcode=EXCLUDED.postcode, postcode_norm=EXCLUDED.postcode_norm, seen_at=now()
                    WHERE {S}.register_name.provider_norm IS DISTINCT FROM EXCLUDED.provider_norm
                       OR {S}.register_name.address IS DISTINCT FROM EXCLUDED.address
                       OR {S}.register_name.postcode IS DISTINCT FROM EXCLUDED.postcode""", data)
        print(f"  {code}: {len(data)} company names read" + (f" (of {len(rows)} entries)" if len(rows) != len(data) else ""))


def apply_trading_names(con):
    """Trading address of a company matched by NAME = the address on its register entry (only where it has none, or the same register gave it)."""
    S = f'"{SCHEMA}"'
    cqc = {x for x, _ in CQC_NAME_SOURCES}
    codes = [r[0] for r in con.execute(f"SELECT DISTINCT source_code FROM {S}.register_name ORDER BY 1").fetchall() if r[0] not in cqc]
    join = JOIN_MATCH.replace("{S}", SCHEMA)
    changed = 0
    for code in codes:
        rows = con.execute(f"""SELECT DISTINCT ON (m.company_number) m.company_number, r.address FROM {S}.register_name r {join}
                               JOIN {S}.company c ON c.company_number = m.company_number AND c.fetch_status = 'ok'
                               WHERE r.source_code = %s AND r.address IS NOT NULL AND r.address <> ''
                                 AND (c.trading_address IS NULL OR c.trading_address_source = %s)
                               ORDER BY m.company_number, r.seen_at DESC""", (code, code)).fetchall()
        for n, addr in rows:
            old = con.execute(f"SELECT company_name, trading_address FROM {S}.company WHERE company_number=%s", (n,)).fetchone()
            if old and old[1] != addr[:500]:
                con.execute(f"UPDATE {S}.company SET trading_address=%s, trading_address_source=%s WHERE company_number=%s", (addr[:500], code, n))
                if old[1]:
                    log_change(con, n, old[0], "Trading address changed", f"{old[1]} -> {addr[:500]}")
                changed += 1
    if changed:
        print(f"  trading address set/updated for {changed} companies matched by name")


def coverage_report(con):
    """How many entries of each name/number register are tied to a company."""
    S = f'"{SCHEMA}"'
    print("Companies tied to register entries:")
    totals = {"HMD-01": 'SELECT count(*) FROM "R10_hmrc_tax_defaulters".defaulter', "HOS-01": 'SELECT count(*) FROM "R11_sponsor_register".sponsor WHERE "On register"',
              "CON-01": 'SELECT count(DISTINCT "Release ID") FROM "R12_contracts_finder".supplier',
              "FTS-01": 'SELECT count(DISTINCT "Release ID") FROM "R13_find_a_tender".supplier',
              "PTR-01": 'SELECT count(DISTINCT "Payment ID") FROM "R14_payments_transparency".payment'}
    for code, q in totals.items():
        try:
            tot = con.execute(q).fetchone()[0]
        except psycopg.Error:
            continue
        linked = con.execute(f"SELECT count(DISTINCT source_key), count(DISTINCT company_number) FROM {S}.register_link WHERE source_code=%s", (code,)).fetchone()
        names = con.execute(f"SELECT count(*) FROM {S}.register_name WHERE source_code=%s", (code,)).fetchone()[0]
        waiting = con.execute(f"""SELECT count(DISTINCT r.provider_norm) FROM {S}.register_name r LEFT JOIN {S}.name_match m ON m.name_norm = r.provider_norm AND m.kind='provider'
                                  WHERE r.source_code=%s AND m.name_norm IS NULL""", (code,)).fetchone()[0]
        print(f"  {code}: {tot} entries in the register, {linked[0]} tied to a company ({linked[1]} companies); {names} company-type names read, {waiting} not searched yet")


def match_one(kind, norm, raw, postcodes):
    items = api_get("/search/companies?q=" + urllib.parse.quote(raw[:160]) + "&items_per_page=20").get("items") or []
    # a care-home name rarely carries "Ltd", so for a location the company-type word is ignored (the postcode check below protects it)
    core = (lambda n: re.sub(r"\s+(LTD|PLC|LLP|CIC)$", "", n)) if kind == "location" else (lambda n: n)
    cands = [i for i in items if core(name_norm(i.get("title"))) == core(norm)]
    if not cands:
        return "no_match", None, None, None, None
    live = [i for i in cands if i.get("company_status") not in ("dissolved", "converted-closed")] or cands
    pcs = {pc_norm(p) for p in postcodes if p}
    at_pc = [i for i in live if pc_norm((i.get("address") or {}).get("postal_code")) in pcs]
    several = len(live) > 1
    if kind == "location":
        live = at_pc                                # the postcode must agree for a location name
    elif len(live) > 1 and at_pc:
        live = at_pc
    if len(live) == 1:
        i = live[0]
        # detail 'postcode' = several companies share this name and the postcode chose one: only entries at that postcode may use the match
        return "matched", i.get("company_number"), i.get("title"), pc_norm((i.get("address") or {}).get("postal_code")), ("postcode" if several else None)
    return ("ambiguous" if live else "no_match"), None, None, None, None


def match_names(con, limit):
    S = f'"{SCHEMA}"'
    rows = con.execute(f"""
        WITH want AS (
          SELECT 'provider' AS kind, provider_norm AS n, min(provider_name) AS raw, array_agg(DISTINCT postcode_norm) AS pcs, max(seen_at) AS fs
            FROM {S}.register_name WHERE provider_norm <> '' GROUP BY 2
          UNION ALL
          SELECT 'location', location_norm, min(location_name), array_agg(DISTINCT postcode_norm), max(seen_at)
            FROM {S}.register_name WHERE location_norm <> '' AND location_norm <> provider_norm GROUP BY 2)
        SELECT w.kind, w.n, w.raw, w.pcs FROM want w LEFT JOIN {S}.name_match m ON m.name_norm = w.n AND m.kind = w.kind
        WHERE m.name_norm IS NULL
           OR (m.status IN ('no_match','ambiguous') AND m.checked_at < now() - make_interval(days => %s))
           OR (m.status = 'error' AND m.checked_at < now() - interval '1 day')
        ORDER BY (w.kind = 'provider') DESC, w.fs DESC, w.n LIMIT %s""", (RECHECK_NO_MATCH_DAYS, limit)).fetchall()
    if not rows:
        print("  no new names to search.")
        return
    print(f"  searching {len(rows)} names at Companies House (about {int(len(rows) * DELAY / 60) + 1} minutes)...")
    stat = {"matched": 0, "no_match": 0, "ambiguous": 0, "error": 0}
    consecutive = 0
    for i, (kind, norm, raw, pcs) in enumerate(rows, 1):
        try:
            status, number, title, mpc, detail = match_one(kind, norm, raw, pcs or [])
            consecutive = 0
        except (RuntimeError, NotFound) as e:
            status, number, title, mpc, detail = "error", None, None, None, str(e)[:300]
            consecutive += 1
        stat[status] += 1
        con.execute(f"""INSERT INTO {S}.name_match (name_norm, kind, company_number, matched_title, match_postcode, status, detail, checked_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,now())
                        ON CONFLICT (name_norm, kind) DO UPDATE SET company_number=EXCLUDED.company_number, matched_title=EXCLUDED.matched_title,
                          match_postcode=EXCLUDED.match_postcode, status=EXCLUDED.status, detail=EXCLUDED.detail, checked_at=now()""",
                    (norm, kind, number, title, mpc, status, detail))
        if consecutive >= 5:
            print("  Five errors in a row - stopping the name searches for this run.")
            break
        if i % 100 == 0:
            print(f"  ...{i} of {len(rows)} names searched", flush=True)
    print(f"  names: {stat['matched']} matched, {stat['no_match']} no exact match, {stat['ambiguous']} ambiguous (skipped), {stat['error']} errors")


# a CQC row belongs to a matched company when its provider name matched, or its location name matched at the same postcode
JOIN_MATCH = """JOIN "{S}".name_match m ON m.status = 'matched' AND (
                  (m.kind = 'provider' AND m.name_norm = r.provider_norm)
               OR (m.kind = 'location' AND m.name_norm = r.location_norm AND m.match_postcode = r.postcode_norm))
              AND (m.detail IS DISTINCT FROM 'postcode' OR m.match_postcode = r.postcode_norm)"""


def link_names(con):
    """Matched companies: tag them with their CQC register and queue them for the normal fetch."""
    S = f'"{SCHEMA}"'
    join = JOIN_MATCH.replace("{S}", SCHEMA)
    with con.transaction():
        con.execute(f"INSERT INTO {S}.company_source (company_number, register_code) SELECT DISTINCT m.company_number, r.source_code "
                    f"FROM {S}.register_name r {join} ON CONFLICT DO NOTHING")
        con.execute(f"INSERT INTO {S}.register_link (source_code, source_key, company_number, how) SELECT DISTINCT r.source_code, split_part(r.source_key, '|', 1), "
                    f"m.company_number, 'name' FROM {S}.register_name r {join} ON CONFLICT DO NOTHING")
        con.execute(f"""INSERT INTO {S}.company_queue (company_number)
                        SELECT DISTINCT m.company_number FROM {S}.register_name r {join}
                        WHERE NOT EXISTS (SELECT 1 FROM {S}.company c WHERE c.company_number = m.company_number
                                          AND (c.fetch_status = 'not_found' OR c.last_fetched_at > now() - interval '1 day'))
                          AND (r.seen_at > now() - interval '1 day'
                               OR NOT EXISTS (SELECT 1 FROM {S}.company c2 WHERE c2.company_number = m.company_number))
                        ON CONFLICT DO NOTHING""")
    n = con.execute(f"SELECT count(*) FROM {S}.company_queue").fetchone()[0]
    m = con.execute(f"SELECT count(DISTINCT company_number) FROM {S}.name_match WHERE status='matched'").fetchone()[0]
    print(f"  {m} companies matched from register names so far; queue now {n}")


def apply_trading(con):
    """Trading address of a CQC-matched company = its CQC location(s). Runs after the companies are fetched."""
    S = f'"{SCHEMA}"'
    join = JOIN_MATCH.replace("{S}", SCHEMA)
    rows = con.execute(f"""SELECT m.company_number, r.location_name, r.address FROM {S}.register_name r {join}
                           JOIN {S}.company c ON c.company_number = m.company_number AND c.fetch_status = 'ok'
                           WHERE (r.location_name IS NOT NULL OR r.address IS NOT NULL) AND r.source_code = ANY(%s)
                           ORDER BY m.company_number, r.location_name, r.address""", ([c for c, _ in CQC_NAME_SOURCES],)).fetchall()
    by = {}
    for n, loc, addr in rows:
        by.setdefault(n, [])
        t = ", ".join(x for x in (loc, addr) if x)
        if t not in by[n]:
            by[n].append(t)
    changed = 0
    for n, locs in by.items():
        text = locs[0] + (f"  (+{len(locs) - 1} more locations)" if len(locs) > 1 else "")
        old = con.execute(f"SELECT company_name, trading_address, trading_address_source FROM {S}.company WHERE company_number=%s", (n,)).fetchone()
        if not old or old[1] == text or (old[2] not in (None, "CQC")):
            continue                                  # unchanged, or a trading address from another register stays
        con.execute(f"UPDATE {S}.company SET trading_address=%s, trading_address_source='CQC' WHERE company_number=%s", (text[:500], n))
        if old[1]:
            log_change(con, n, old[0], "Trading address changed", f"{old[1]} -> {text}")
        changed += 1
    if by:
        print(f"  trading address set/updated for {changed} of {len(by)} CQC-matched companies")


# ---------------------------------------------------------------------
#  Personal insolvency person  ->  officers (saved ones, then a Companies House search)
# ---------------------------------------------------------------------
TITLES = {"MR", "MRS", "MS", "MISS", "MX", "DR", "PROF", "SIR", "LORD", "LADY", "REV", "DAME"}
SHORT_MONTHS = {k[:3]: v for k, v in MONTHS.items()}
ADDR_STOP = {"UK", "ENGLAND", "UNITED", "KINGDOM", "WALES", "SCOTLAND", "THE", "AND", "OF"}


def person_parts(name):
    """'SMITH, John Andrew' or 'John Andrew SMITH' or 'John Smith'  ->  ('SMITH', ['JOHN', 'ANDREW'])"""
    s = re.sub(r"[()]", " ", str(name or "")).strip()
    if not s:
        return None
    if "," in s:
        sur, rest = s.split(",", 1)
        sur_t, fore = sur.split(), rest.split()
    else:
        t = s.split()
        up = [w for w in t if w.isupper() and len(w) > 1]
        if up and len(up) < len(t):                  # "John Andrew SMITH": the capitals are the surname
            sur_t, fore = up, [w for w in t if w not in up]
        else:
            sur_t, fore = t[-1:], t[:-1]
    clean = lambda w: re.sub(r"[^A-Z]", "", w.upper())
    fore = [clean(w) for w in fore if clean(w) and clean(w) not in TITLES]
    sur = "".join(clean(w) for w in sur_t)
    return (sur, fore) if sur and fore else None


def same_person(a, b):
    """first name equal, last name equal, middle names must not clash (an initial counts)"""
    if a[0] != b[0] or a[1][0] != b[1][0]:
        return False
    return not (len(a[1]) > 1 and len(b[1]) > 1 and a[1][1][0] != b[1][1][0])


def parse_dob(text):
    """Date of birth from notice text: 'Date of birth: 12/03/1980', 'Born 12 March 1980', 'DOB 1980-03-12'."""
    t = re.sub(r"\s+", " ", str(text or ""))
    for m in re.finditer(r"(?i)date of birth|birth details|d\.?o\.?b\b\.?|\bborn\b", t):
        seg = t[m.end(): m.end() + 60]
        d = mo = y = None
        x = re.search(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", seg)
        if x:
            d, mo, y = (int(v) for v in x.groups())
        elif (x := re.search(r"(\d{4})-(\d{2})-(\d{2})", seg)):
            y, mo, d = (int(v) for v in x.groups())
        elif (x := re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]{3,9})\.?,?\s+(\d{4})", seg)):
            d, mo, y = int(x.group(1)), SHORT_MONTHS.get(x.group(2).lower()[:3]), int(x.group(3))
        if d and mo and y and 1900 <= y <= date.today().year - 15:
            try:
                return date(y, mo, d)
            except ValueError:
                pass
    return None


def addr_tokens(text):
    toks = set(re.findall(r"[A-Z0-9]+", str(text or "").upper())) - ADDR_STOP
    return {t for t in toks if not (re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?", t) or re.fullmatch(r"\d[A-Z]{2}", t))}      # drop postcode parts


def address_agrees(a, b):
    shared = addr_tokens(a) & addr_tokens(b)
    return len(shared) >= 3 or (len(shared) >= 2 and any(t.isdigit() for t in shared))


def evidence(pe, op, o_dob, o_pc, o_addr):
    """pe = the person in the notice, op = officer name parts. -> (confidence, evidence text), or None if they cannot be the same person."""
    if not same_person(pe["p"], op):
        return None
    ev = ["name"]
    if len(pe["p"][1]) > 1 and len(op[1]) > 1:
        ev.append("middle name")
    dob_ok = False
    if pe["dob"] and o_dob:
        if o_dob != f"{pe['dob'].year}-{pe['dob'].month:02d}":
            return None                                       # a different birth month/year: not the same person
        ev.append("birth month/year")
        dob_ok = True
    a, b = pc_norm(pe["pc"]), pc_norm(o_pc)
    pc = None
    if len(a) > 3 and len(b) > 3:
        pc = "postcode" if a == b else ("postcode district" if a[:-3] == b[:-3] else None)
    if pc:
        ev.append(pc)
    addr = bool(pe["addr"] and o_addr and address_agrees(pe["addr"], o_addr))
    if addr:
        ev.append("address")
    if dob_ok and (pc == "postcode" or addr):
        conf = "strong"
    elif dob_ok or (pc == "postcode" and addr):
        conf = "good"
    elif pc or addr:
        conf = "possible"
    else:
        conf = "name only"
    return conf, ", ".join(ev)


def save_link(con, pe, number, cname, key, oname, role, resigned, level):
    S = f'"{SCHEMA}"'
    with con.transaction():
        ins = con.execute(f"""INSERT INTO {S}.person_link (source_code, source_ref, person_name, notice_date, person_postcode, person_dob, company_number,
                                  company_name, officer_key, officer_name, officer_role, resigned_on, match_level)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                              ON CONFLICT (source_code, source_ref, company_number, officer_key) DO UPDATE
                                SET match_level = EXCLUDED.match_level, resigned_on = EXCLUDED.resigned_on, person_dob = EXCLUDED.person_dob,
                                    company_name = COALESCE(EXCLUDED.company_name, {S}.person_link.company_name)
                                WHERE {S}.person_link.match_level IS DISTINCT FROM EXCLUDED.match_level
                                   OR {S}.person_link.resigned_on IS DISTINCT FROM EXCLUDED.resigned_on
                                   OR {S}.person_link.person_dob IS DISTINCT FROM EXCLUDED.person_dob
                              RETURNING (xmax = 0)""",
                          (pe["code"], pe["ref"], pe["name"], pe["date"], pe["pc"], pe["dob"], number, cname, key, oname, role, resigned, level)).fetchone()
        if ins and ins[0]:
            log_change(con, number, cname, "Officer linked to personal insolvency",
                       f"{oname} ({role}{', resigned ' + str(resigned) if resigned else ''}) = {pe['name']} in {pe['code']} {pe['ref']}; {level}")
            return 1
    return 0


def lookup_one(pe):
    """Companies House officer search for one person (needs a birth date). -> (status, [appointments])"""
    p, dob = pe["p"], pe["dob"]
    q = urllib.parse.quote(" ".join(p[1] + [p[0]])[:160])
    o_dob = f"{dob.year}-{dob.month:02d}"
    cands = []
    for page in range(PEOPLE_SEARCH_PAGES):
        items = api_get(f"/search/officers?q={q}&items_per_page=100&start_index={page * 100}").get("items") or []
        for it in items:
            ip = person_parts(it.get("title"))
            bd = it.get("date_of_birth") or {}
            if not ip or str(bd.get("year")) != str(dob.year) or int(bd.get("month") or 0) != dob.month:
                continue
            a = it.get("address") or {}
            ev = evidence(pe, ip, o_dob, a.get("postal_code"), addr_line(a) or it.get("address_snippet"))
            if ev:
                cands.append((it, ev))
        if len(items) < 100:
            break
    if not cands:
        return "none", []
    sure = [c for c in cands if c[1][0] == "strong"]
    if sure:
        cands = sure
    elif len(cands) > 3:
        return "ambiguous", []                 # several people share the name and birth month, none confirmed by postcode / address
    links = []
    for it, ev in cands:
        parts = ((it.get("links") or {}).get("self") or "").strip("/").split("/")
        if len(parts) < 2:
            continue
        oid = parts[1]
        for ap in api_pages(f"/officers/{oid}/appointments"):
            c = ap.get("appointed_to") or {}
            num = norm_number(c.get("company_number"))
            if num:
                links.append(dict(number=num, cname=c.get("company_name"), key=f"{oid}|{ap.get('officer_role')}|{ap.get('appointed_on')}",
                                  oname=ap.get("name") or it.get("title"), role=ap.get("officer_role"), resigned=ap.get("resigned_on"),
                                  level=f"{ev[0]}: {ev[1]} (Companies House search)"))
    return "done", links


def lookup_people_api(con, people, limit):
    S = f'"{SCHEMA}"'
    done = {(r[0], r[1]) for r in con.execute(
        f"SELECT source_code, source_ref FROM {S}.person_lookup WHERE status <> 'error' OR checked_at > now() - interval '1 day'")}
    todo = [p for p in sorted(people, key=lambda x: str(x["date"]), reverse=True) if p["dob"] and (p["code"], p["ref"]) not in done][:limit]
    if not todo:
        return 0
    print(f"  searching Companies House for {len(todo)} people with a date of birth...")
    new = errs = consecutive = nlinks = 0
    for i, pe in enumerate(todo, 1):
        try:
            status, links = lookup_one(pe)
            detail, consecutive = None, 0
        except (RuntimeError, NotFound) as e:
            status, links, detail = "error", [], str(e)[:300]
            errs += 1
            consecutive += 1
        for L in links:
            new += save_link(con, pe, L["number"], L["cname"], L["key"], L["oname"], L["role"], L["resigned"], L["level"])
            nlinks += 1
            if PEOPLE_ADD_COMPANIES:
                with con.transaction():
                    con.execute(f"INSERT INTO {S}.company_source (company_number, register_code) VALUES (%s,%s) ON CONFLICT DO NOTHING", (L["number"], pe["code"]))
                    con.execute(f"""INSERT INTO {S}.company_queue (company_number)
                                    SELECT %s WHERE NOT EXISTS (SELECT 1 FROM {S}.company c WHERE c.company_number = %s
                                                                AND (c.fetch_status = 'not_found' OR c.last_fetched_at > now() - interval '1 day'))
                                    ON CONFLICT DO NOTHING""", (L["number"], L["number"]))
        con.execute(f"""INSERT INTO {S}.person_lookup (source_code, source_ref, status, links, detail, checked_at) VALUES (%s,%s,%s,%s,%s,now())
                        ON CONFLICT (source_code, source_ref) DO UPDATE SET status=EXCLUDED.status, links=EXCLUDED.links, detail=EXCLUDED.detail, checked_at=now()""",
                    (pe["code"], pe["ref"], status, len(links), detail))
        if consecutive >= 5:
            print("  Five errors in a row - stopping the people search for this run.")
            break
        if i % 50 == 0:
            print(f"  ...{i} of {len(todo)} people searched", flush=True)
    print(f"  Companies House search: {nlinks} appointment(s) linked ({new} new), {errs} errors")
    return new


def link_people(con, days, backfill=False, use_api=True, api_limit=PEOPLE_LOOKUP_LIMIT):
    S = f'"{SCHEMA}"'
    since = date(2000, 1, 1) if backfill else date.today() - timedelta(days=days)
    index = {}
    for r in con.execute(f"""SELECT o.company_number, c.company_name, o.officer_key, o.officer_name, o.officer_role, o.resigned_on,
                                    o.officer_postcode, o.birth_month_year, o.officer_address
                             FROM {S}.company_officer o JOIN {S}.company c USING (company_number) WHERE c.fetch_status = 'ok'""").fetchall():
        p = person_parts(r[3])
        if p:
            index.setdefault((p[0], p[1][0]), []).append((p, r))
    people = []
    for code, query in PERSON_SOURCES:
        try:
            rows = con.execute(query, {"since": since}).fetchall()
        except psycopg.Error as e:
            print(f"  {code}: personal insolvency source not readable yet ({str(e).splitlines()[0]})")
            continue
        for ref, name, pcode, d, addr, body in rows:
            p = person_parts(name)
            if p:
                people.append(dict(code=code, ref=str(ref), name=name, p=p, pc=pcode, date=d, addr=addr, dob=parse_dob(body)))
    new = 0
    for pe in people:
        for op, o in index.get((pe["p"][0], pe["p"][1][0]), []):
            ev = evidence(pe, op, o[7], o[6], o[8])
            if ev is None:
                if pe["dob"]:      # the earlier, weaker link (before the birth date was known) is wrong
                    con.execute(f"DELETE FROM {S}.person_link WHERE source_code=%s AND source_ref=%s AND company_number=%s AND officer_key=%s",
                                (pe["code"], pe["ref"], o[0], o[2]))
                continue
            new += save_link(con, pe, o[0], o[1], o[2], o[3], o[4], o[5], f"{ev[0]}: {ev[1]}")
    print(f"  {len(people)} personal insolvency people ({sum(1 for x in people if x['dob'])} with a date of birth) checked against "
          f"{sum(len(v) for v in index.values())} saved officers: {new} new link(s)")
    if use_api and people:
        if not KEY:
            print("  (CH_REST_KEY is not set - Companies House officer search skipped)")
        else:
            new += lookup_people_api(con, people, api_limit)
    total = con.execute(f"SELECT count(*) FROM {S}.person_link").fetchone()[0]
    print(f"  person_link now holds {total} link(s)")
    return new


def pick(con, limit, refresh_days):
    S = f'"{SCHEMA}"'
    q = [r[0] for r in con.execute(f"SELECT company_number FROM {S}.company_queue WHERE next_try_at <= now() ORDER BY queued_at LIMIT %s", (limit,))]
    left = limit - len(q)
    if left > 0 and refresh_days > 0:
        q += [r[0] for r in con.execute(
            f"""SELECT company_number FROM {S}.company WHERE fetch_status='ok' AND last_fetched_at < now() - make_interval(days => %s)
                ORDER BY last_fetched_at LIMIT %s""", (refresh_days, left))]
    return q


def trading_for(con, number):
    r = con.execute(f'SELECT trading_address, trading_address_source FROM "{SCHEMA}".company WHERE company_number=%s', (number,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def fetch_batch(con, numbers):
    S = f'"{SCHEMA}"'
    ok = nf = err = 0
    consecutive = 0
    t0 = time.time()
    for i, n in enumerate(numbers, 1):
        try:
            profile, officers, pscs = fetch_company(n)
            with con.transaction():
                trade, src = trading_for(con, n)
                res = save_company(con, n, profile, officers, pscs, trade, src)
                con.execute(f"DELETE FROM {S}.company_queue WHERE company_number=%s", (n,))
            ok += 1; consecutive = 0
            if res != "no change":
                print(f"  {n} {profile.get('company_name')}: {res}", flush=True)
        except NotFound:
            with con.transaction():
                con.execute(f"""INSERT INTO {S}.company (company_number, fetch_status, last_fetched_at) VALUES (%s,'not_found',now())
                                ON CONFLICT (company_number) DO UPDATE SET fetch_status='not_found', last_fetched_at=now()""", (n,))
                con.execute(f"DELETE FROM {S}.company_queue WHERE company_number=%s", (n,))
            nf += 1
        except RuntimeError as e:
            err += 1; consecutive += 1
            with con.transaction():
                con.execute(f"""INSERT INTO {S}.company_queue (company_number, attempts, next_try_at, last_error) VALUES (%s,1,now()+interval '1 hour',%s)
                                ON CONFLICT (company_number) DO UPDATE SET attempts={S}.company_queue.attempts+1,
                                next_try_at=now()+make_interval(hours => {S}.company_queue.attempts+1), last_error=EXCLUDED.last_error""", (n, str(e)[:300]))
            print(f"  {n}: error, will retry later ({e})", flush=True)
            if consecutive >= 5:
                print("Five errors in a row - stopping this run; the rest stays queued.")
                break
        if i % 50 == 0:
            print(f"  ...{i} of {len(numbers)} done ({int(time.time() - t0)} s)", flush=True)
    return ok, nf, err


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true", help="only create the tables")
    ap.add_argument("--discover", action="store_true", help="only read the registers and queue new companies")
    ap.add_argument("--number", help="fetch one company now")
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--refresh-days", type=int, default=REFRESH_DAYS, help="also re-fetch companies older than this many days (0 = off)")
    ap.add_argument("--since-days", type=int, default=DISCOVER_DAYS, help="only register entries from the last N days queue a company")
    ap.add_argument("--backfill", action="store_true", help="queue every company in the registers, whatever the entry date (first run only)")
    ap.add_argument("--people", action="store_true", help="only match personal insolvency people to officers")
    ap.add_argument("--no-api", action="store_true", help="with --people: use only the officers already saved, no Companies House search")
    ap.add_argument("--people-days", type=int, default=PEOPLE_DAYS, help="match personal insolvency entries of the last N days")
    ap.add_argument("--lookup-limit", type=int, default=LOOKUP_LIMIT, help="most register-name searches in one run")
    ap.add_argument("--coverage", action="store_true", help="only print which registers are tied to companies, and warn about registers not wired up")
    a = ap.parse_args()
    try:
        con = psycopg.connect(**DB, autocommit=True)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect to PostgreSQL: {e}")
    with con:
        con.execute(DDL)
        print(f'Schema "{SCHEMA}" ready.')
        if a.setup:
            return 0
        if a.number:
            n = norm_number(a.number) or sys.exit("not a valid company number")
            ok, nf, err = fetch_batch(con, [n])
            r = con.execute(f'SELECT company_name, company_status, registered_office, directors, pscs FROM "{SCHEMA}".v_company_master WHERE company_number=%s', (n,)).fetchone()
            print(r if r else "not saved")
            return 0
        if a.coverage:
            print("Registers (numbered schemas) and how they reach the company master:")
            for sch, how in COVERAGE.items():
                print(f"  {sch}: {how or '(the master itself)'}")
            auto_sources(con)
            coverage_report(con)
            return 0
        if a.people:
            print("Matching personal insolvency people to officers:")
            link_people(con, a.people_days, a.backfill, use_api=not a.no_api)
            return 0
        print("Reading company numbers from the registers:")
        since = date(2000, 1, 1) if a.backfill else date.today() - timedelta(days=a.since_days)
        discover(con, since)
        print("Checking that every register is wired to the company master:")
        auto_nums, auto_names = auto_sources(con)
        if auto_nums:
            discover(con, since, auto_nums)
        print("Reading company names from the registers that have names only (HMRC defaulters, sponsors, Contracts Finder suppliers ...):")
        discover_register_names(con, since, a.backfill, NAME_SOURCES + auto_names)
        print("Reading names from the CQC change tables (no company numbers there):")
        discover_names(con, a.backfill)
        if a.discover:
            w = con.execute(f'SELECT count(*) FROM "{SCHEMA}".register_name').fetchone()[0]
            print(f"{w} register names saved; names are searched at Companies House when you run without --discover.")
            return 0
        print("Matching register names to Companies House:")
        match_names(con, a.lookup_limit)
        link_names(con)
        print("Matching personal insolvency people to officers (saved officers, then Companies House search):")
        link_people(con, a.people_days, a.backfill)
        todo = pick(con, a.limit, a.refresh_days)
        print(f"Fetching {len(todo)} companies from Companies House (about {int(len(todo) * 3 * DELAY / 60) + 1} minutes)...")
        ok = nf = err = 0
        if todo:
            ok, nf, err = fetch_batch(con, todo)
        else:
            print("Nothing to fetch.")
        apply_trading(con)
        apply_trading_names(con)
        coverage_report(con)
        if todo:
            print("Matching personal insolvency people again with the newly fetched officers:")
            link_people(con, a.people_days, a.backfill, use_api=False)
        left = con.execute(f'SELECT count(*) FROM "{SCHEMA}".company_queue').fetchone()[0]
        print(f"Done. {ok} saved, {nf} not found at Companies House, {err} errors. Still queued: {left}.")
        return 1 if err and not ok else 0


# =====================================================================
#  2. Reset  (was company_master_reset.py)
# =====================================================================



def reset_main():
    try:
        con = psycopg.connect(**DB, autocommit=True)
    except psycopg.OperationalError as e:
        sys.exit(f"Cannot connect to PostgreSQL: {e}")
    with con:
        tables = con.execute("SELECT table_name, table_type FROM information_schema.tables WHERE table_schema=%s ORDER BY 2, 1", (SCHEMA,)).fetchall()
        if not tables:
            print(f'Schema "{SCHEMA}" has no tables (or does not exist). Nothing to delete.')
        else:
            print(f'This will permanently delete schema "{SCHEMA}" in database "{DB["dbname"]}":')
            for t, kind in tables:
                n = con.execute(f'SELECT count(*) FROM "{SCHEMA}"."{t}"').fetchone()[0] if kind == "BASE TABLE" else None
                print(f"   {t:22} {'view' if n is None else f'{n:,} rows'}")
            if input("\nType DROP and press Enter to delete it (anything else cancels): ").strip() != "DROP":
                print("Cancelled. Nothing was deleted.")
                return 1
            con.execute(f'DROP SCHEMA "{SCHEMA}" CASCADE')
            print(f'Deleted schema "{SCHEMA}".')
    if "--recreate" in sys.argv:
        sys.argv = [sys.argv[0], "--setup"]
        return main()
    print("Next:  python company_master.py --setup   (empty tables)   then   python company_master.py --backfill --limit 300")
    return 0


# =====================================================================
#  Command line
# =====================================================================

def cli():
    if len(sys.argv) > 1 and sys.argv[1] == "reset":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        return reset_main()
    return main()


if __name__ == "__main__":
    sys.exit(cli())
