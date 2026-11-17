r"""register_cqc_api.py -- CQC data from the CQC Developer Portal API (Syndication), instead of the downloaded files.

  python register_cqc_api.py changes     pull what changed since the last run (locations and providers), then their details
  python register_cqc_api.py details     only fetch details for changed ids not yet fetched
  python register_cqc_api.py show        counts and the last run
  python register_cqc_api.py expand      fill the columns of records already stored (from their detail JSON, no API calls)

SETTINGS (environment variables - never put the key in this file):
  CQC_API_KEY    the subscription key from the portal (sign in > Products > Syndication > subscribe > primary key)
  CQC_API_BASE   optional. Default https://api.service.cqc.org.uk/public/v1 (from the Syndication spec)
  CQC_API_KEY_HEADER   optional. Default Ocp-Apim-Subscription-Key (from the Syndication spec)
  CQC_PARTNER    optional partner code (the CQC asks every user to identify themselves; higher rate limit)

HOW IT WORKS
  GET /changes/location and /changes/provider  ?startTimestamp=...&endTimestamp=...&page=n&perPage=n   (start inclusive, end exclusive)
  The first run starts 30 days back.  Each run covers from the last end timestamp to now, so no change is missed.
  The changes operation returns IDs only ("changes": [...]), so each changed ID is then fetched: GET /locations/{id} and GET /providers/{id}.
  Throttle: the portal allows 100 calls per connection every 60 seconds - the loader waits to stay under it.
  Full load: /locations and /providers list ids (50 per page max in the spec examples); the details are one call each, so a full load of
  all locations takes many hours at 100 calls a minute. Use "changes" for the daily run; use the CSV files for a first full snapshot.
  Everything is kept: the raw JSON of every page and of every detail call (schema 16_cqc_api), plus flat tables for the main fields.

TABLES (schema "R16_cqc_api"):
  checkpoint      one row per organisation type: the last timestamp covered
  change          one row per changed id per run: type, id, run time            (the daily "what changed" list)
  location        one row per location: id, name, address, postcode, status, fetched_at, detail (full JSON)
  provider        one row per provider: id, name, address, postcode, status, fetched_at, detail (full JSON)
  raw_page        every API response (url, status, body) - for audit and to re-read without calling the API again
  run_log         one row per run: started, finished, calls, changes, errors
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
import requests
from psycopg import sql
from psycopg.types.json import Jsonb

DB = dict(host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"),
          dbname=os.environ.get("PGDATABASE", "postgres"), user=os.environ.get("PGUSER", "postgres"))
SCHEMA = "R16_cqc_api"
KEY = os.environ.get("CQC_API_KEY", "")
BASE = os.environ.get("CQC_API_BASE", "https://api.service.cqc.org.uk/public/v1").rstrip("/")   # from the CQC Syndication OpenAPI spec
KEY_HEADER = os.environ.get("CQC_API_KEY_HEADER", "Ocp-Apim-Subscription-Key")   # apiKeyHeader in the spec
PARTNER = os.environ.get("CQC_PARTNER", "")
PER_PAGE = 100
MAX_PER_MINUTE = 95            # the portal allows 100 calls per minute per connection
FIRST_RUN_DAYS = 30
NOT_FOUND_RECHECK_DAYS = 30        # an id that returned 404 is tried again after this many days
MAX_PAGES = 2000               # safety stop per run

DDL = f'''
CREATE SCHEMA IF NOT EXISTS "{SCHEMA}";
CREATE TABLE IF NOT EXISTS "{SCHEMA}".checkpoint (org_type text PRIMARY KEY, covered_to timestamptz NOT NULL, updated_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS "{SCHEMA}".change (run_at timestamptz NOT NULL DEFAULT now(), org_type text NOT NULL, org_id text NOT NULL,
    PRIMARY KEY (org_type, org_id, run_at));
CREATE INDEX IF NOT EXISTS change_run_idx ON "{SCHEMA}".change (run_at);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".location (location_id text PRIMARY KEY, name text, address text, postcode text, status text,
    changed_at timestamptz, fetched_at timestamptz, detail jsonb);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".provider (provider_id text PRIMARY KEY, name text, address text, postcode text, status text,
    changed_at timestamptz, fetched_at timestamptz, detail jsonb);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".raw_page (id bigserial PRIMARY KEY, fetched_at timestamptz DEFAULT now(), url text, http_status int, body jsonb, body_text text);
CREATE TABLE IF NOT EXISTS "{SCHEMA}".not_found (org_type text NOT NULL, org_id text NOT NULL, first_seen timestamptz NOT NULL DEFAULT now(),
    last_checked timestamptz NOT NULL DEFAULT now(), times integer NOT NULL DEFAULT 1, PRIMARY KEY (org_type, org_id));
CREATE TABLE IF NOT EXISTS "{SCHEMA}".run_log (run_id bigserial PRIMARY KEY, started_at timestamptz, finished_at timestamptz, calls int, changes int, errors int, note text);
'''

_calls = []        # times of recent calls (for the throttle, last 60 s)
_total = [0]       # every call made in this run (for the run log)


def need_settings():
    if not KEY:
        sys.exit("CQC_API_KEY is not set. Subscribe to the Syndication API on the portal and copy the primary key:  setx CQC_API_KEY \"your-key\"")


def throttle():
    now = time.time()
    _calls[:] = [t for t in _calls if now - t < 60]
    if len(_calls) >= MAX_PER_MINUTE:
        time.sleep(60 - (now - _calls[0]) + 0.5)
    _calls.append(time.time())
    _total[0] += 1


def get(con, path, params=None, tries=4):
    """GET with the key.  Every response is stored in raw_page.  -> parsed JSON (or None on 404)."""
    url = f"{BASE}/{path.lstrip('/')}"
    headers = {KEY_HEADER: KEY, "Accept": "application/json"}
    if PARTNER:
        headers["partnerCode"] = PARTNER
    last = None
    for i in range(tries):
        throttle()
        try:
            r = requests.get(url, headers=headers, params=params or {}, timeout=60)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5 * (i + 1))
            continue
        text = r.text
        try:
            body = json.loads(text) if text.strip() else None
        except ValueError:
            body = None
        con.execute(sql.SQL("INSERT INTO {}.raw_page (url, http_status, body, body_text) VALUES (%s,%s,%s,%s)").format(sql.Identifier(SCHEMA)),
                    (r.url, r.status_code, json.dumps(body) if body is not None else None, None if body is not None else text[:20000]))
        if r.status_code == 404:
            return None
        if r.status_code == 401 or r.status_code == 403:
            sys.exit(f"The portal refused the key ({r.status_code}). Check CQC_API_KEY and that the Syndication API subscription is active.")
        if r.status_code == 429:
            time.sleep(60)
            last = "429 rate limit"
            continue
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            time.sleep(10 * (i + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{url}: HTTP {r.status_code} {text[:200]}")
        return body
    raise RuntimeError(f"{url}: {last}")


def find_ids(body, kind):
    """The changed ids in a changes page.  The field names are not known yet, so several are tried."""
    names = {"location": ("locationId", "locationid", "id"), "provider": ("providerId", "providerid", "id")}[kind]
    out = []
    items = body if isinstance(body, list) else (body or {}).get("items") or (body or {}).get("content") or (body or {}).get("changes") or []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for n in names:
                if it.get(n):
                    out.append(str(it[n]))
                    break
            else:
                for k, v in it.items():
                    if k.lower() == kind + "id" and v:
                        out.append(str(v))
    return out


# ---- JSON -> flat columns -----------------------------------------------------------------------------------------
# Every column holds ONE value (text).  Nested objects become separate columns:  currentRatings.overall.rating -> current_ratings_overall_rating
# Lists of simple values are joined into one column ("; ").  Lists of records get numbered columns, up to MAX_LIST items:
#   regulatedActivities[1].name -> regulated_activities_1_name ;  regulated_activities_count = how many there are.
# Items beyond MAX_LIST are not in their own columns (they stay in the "detail" column).  Column names are kept to 60 characters.
# Empty values are NULL.  New fields create new columns the first time they appear.  "detail" keeps the full JSON.
MAX_LIST = 5
_known_cols = {}     # table -> column names already in the table (per run)


def snake(name):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).replace("-", "_").replace(" ", "_").lower()


def short(name):
    if len(name) <= 60:
        return name
    import hashlib
    return name[:51] + "_" + hashlib.md5(name.encode()).hexdigest()[:8]


def flat_items(prefix, v, out):
    """Every value becomes ONE text column.  Nested objects are flattened by name.  A list becomes one column
    (items joined by "; ", the fields of one item joined by " | ") plus a <name>_count column."""
    if isinstance(v, dict):
        for k, vv in v.items():
            flat_items(snake(k) if not prefix else f"{prefix}_{snake(k)}", vv, out)
    elif isinstance(v, list):
        out[short(prefix + "_count")] = str(len(v))
        parts = []
        for item in v:
            if isinstance(item, dict):
                parts.append(" | ".join(str(x) for x in item.values() if not isinstance(x, (dict, list)) and x not in (None, "")))
            elif item not in (None, ""):
                parts.append(str(item))
        out[short(prefix)] = "; ".join(p for p in parts if p) or None
    else:
        out[short(prefix)] = None if v in (None, "") else str(v)


def columns_for(record):
    """-> {column: text value or None} for one record (every value is a single text value)."""
    out = {}
    for k, v in record.items():
        flat_items(snake(k), v, out)
    return out


def ensure_columns(con, table, cols):
    """Add any new column to the table (text).  Old jsonb columns are left until expand() drops them."""
    have = _known_cols.get(table)
    if have is None:
        have = {r[0] for r in con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                                          (SCHEMA, table)).fetchall()}
        _known_cols[table] = have
    for name in cols:
        if name in have:
            continue
        con.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} text").format(sql.Identifier(SCHEMA), sql.Identifier(table), sql.Identifier(name)))
        have.add(name)


def rebuild_if_wide(con, kind):
    """A table with too many columns (from an earlier version) is rebuilt: the stored JSON is copied aside, the table is
    recreated with its base columns, and the JSON is put back.  expand() then fills the flat columns."""
    n = con.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = %s AND table_name = %s", (SCHEMA, kind)).fetchone()[0]
    if n < 150:
        return False
    print(f"  {kind}: {n} columns from an earlier version - rebuilding the table (the stored JSON is kept)", flush=True)
    S, T, R = sql.Identifier(SCHEMA), sql.Identifier(kind), sql.Identifier(kind + "_raw")
    con.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(S, R))
    con.execute(sql.SQL("CREATE TABLE {}.{} AS SELECT * FROM {}.{}").format(S, R, S, T))
    con.execute(sql.SQL("DROP TABLE {}.{} CASCADE").format(S, T))
    con.execute(DDL)                                   # recreates the base table
    _known_cols.pop(kind, None)
    con.execute(sql.SQL("INSERT INTO {}.{} ({k}, detail, changed_at, fetched_at) SELECT {k}, detail, changed_at, fetched_at FROM {}.{}")
                .format(S, T, S, R, k=sql.Identifier(kind + "_id")))
    con.execute(sql.SQL("DROP TABLE {}.{}").format(S, R))
    print(f"  {kind}: rebuilt; now run  python register_cqc_api.py expand", flush=True)
    return True


def drop_json_columns(con, table):
    """Remove old jsonb columns (the flat columns replace them).  The detail column is kept."""
    rows = con.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s AND data_type = 'jsonb' AND column_name <> 'detail'",
                       (SCHEMA, table)).fetchall()
    for (c,) in rows:
        con.execute(sql.SQL("ALTER TABLE {}.{} DROP COLUMN {}").format(sql.Identifier(SCHEMA), sql.Identifier(table), sql.Identifier(c)))
        _known_cols.get(table, set()).discard(c)
    return [c for (c,) in rows]


def fmt_ts(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pull_changes(con, org_type):
    row = con.execute(sql.SQL("SELECT covered_to FROM {}.checkpoint WHERE org_type = %s").format(sql.Identifier(SCHEMA)), (org_type,)).fetchone()
    start = row[0] if row else datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)
    end = datetime.now(timezone.utc)
    ids, page, pages = [], 1, 0
    while True:
        body = get(con, f"changes/{org_type}", {"startTimestamp": fmt_ts(start), "endTimestamp": fmt_ts(end), "page": page, "perPage": PER_PAGE})
        got = find_ids(body, org_type) if body else []
        ids += got
        pages += 1
        if len(got) < PER_PAGE or pages >= MAX_PAGES:
            break
        page += 1
    ids = sorted(set(ids))
    with con.cursor() as cu:
        cu.executemany(sql.SQL("INSERT INTO {}.change (org_type, org_id) VALUES (%s,%s) ON CONFLICT DO NOTHING").format(sql.Identifier(SCHEMA)),
                       [(org_type, i) for i in ids])
    con.execute(sql.SQL("INSERT INTO {}.checkpoint (org_type, covered_to) VALUES (%s, %s) ON CONFLICT (org_type) DO UPDATE SET covered_to = EXCLUDED.covered_to, updated_at = now()")
                .format(sql.Identifier(SCHEMA)), (org_type, end))
    print(f"  {org_type}: {len(ids):,} changed since {fmt_ts(start)}  ({pages} page(s))", flush=True)
    return ids


def main_fields(kind, d):
    """Flat columns from the detail JSON (names tried in order; the rest is kept in detail)."""
    if not isinstance(d, dict):
        return {}
    def pick(*keys):
        for k in keys:
            for kk in d:
                if kk.lower() == k.lower() and d[kk] not in (None, ""):
                    return d[kk]
        return None
    addr = ", ".join(str(x) for x in [pick("postalAddressLine1"), pick("postalAddressLine2"), pick("postalAddressTownCity"), pick("postalAddressCounty")] if x)
    return dict(name=pick("locationName", "providerName", "name"), address=addr or pick("address"), postcode=pick("postalCode", "postcode"),
                status=pick("registrationStatus", "status"))


missing = {}
SKIPPED = {}


def fetch_details(con, kind, ids):
    table = kind
    path = {"location": "locations/{}", "provider": "providers/{}"}[kind]
    done = errs = skipped = 0
    recent = {r[0] for r in con.execute(sql.SQL("SELECT org_id FROM {}.not_found WHERE org_type = %s AND last_checked > now() - make_interval(days => %s)")
                                        .format(sql.Identifier(SCHEMA)), (kind, NOT_FOUND_RECHECK_DAYS)).fetchall()}
    for oid in ids:
        if str(oid) in recent:
            skipped += 1
            continue
        try:
            d = get(con, path.format(oid))
        except RuntimeError as e:
            errs += 1
            print("   ", str(e)[:160])
            if errs >= 25:
                print("  too many errors - stopping details")
                break
            continue
        if d is None:                       # 404: the id is not on the register - recorded, retried after NOT_FOUND_RECHECK_DAYS
            missing[kind] = missing.get(kind, 0) + 1
            con.execute(sql.SQL("INSERT INTO {}.not_found (org_type, org_id) VALUES (%s,%s) ON CONFLICT (org_type, org_id) DO UPDATE "
                                "SET last_checked = now(), times = {}.not_found.times + 1").format(sql.Identifier(SCHEMA), sql.Identifier(SCHEMA)),
                        (kind, str(oid)))
            continue
        con.execute(sql.SQL("DELETE FROM {}.not_found WHERE org_type = %s AND org_id = %s").format(sql.Identifier(SCHEMA)), (kind, str(oid)))
        f = main_fields(kind, d)
        cols = columns_for(d)
        ensure_columns(con, table, cols)
        # the id and the flat summary columns first, then every field from the record (the id column is set from the id we asked for)
        txt = lambda x: None if x in (None, "") else str(x)
        values = {kind + "_id": str(oid), "name": txt(f.get("name")), "address": txt(f.get("address")),
                  "postcode": txt(f.get("postcode")), "status": txt(f.get("status"))}
        for name, v in cols.items():
            if name not in values and name != "detail":
                values[name] = v
        values["detail"] = Jsonb(d)
        names = list(values)
        con.execute(sql.SQL("INSERT INTO {t} ({cols}, changed_at, fetched_at) VALUES ({ph}, now(), now()) "
                            "ON CONFLICT ({k}) DO UPDATE SET {upd}, changed_at = now(), fetched_at = now()").format(
                        t=sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table)),
                        cols=sql.SQL(", ").join(sql.Identifier(n) for n in names),
                        ph=sql.SQL(", ").join(sql.Placeholder() * len(names)),
                        k=sql.Identifier(kind + "_id"),
                        upd=sql.SQL(", ").join(sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(n), sql.Identifier(n)) for n in names if n != kind + "_id")),
                    [values[n] for n in names])
        done += 1
    SKIPPED[kind] = skipped
    return done, errs


def run_changes(con):
    started = datetime.now(timezone.utc)
    ids = {"location": pull_changes(con, "location"), "provider": pull_changes(con, "provider")}
    det = {}
    for kind in ("location", "provider"):
        det[kind] = fetch_details(con, kind, ids[kind])
        print(f"  {kind} details stored: {det[kind][0]:,} ({det[kind][1]} errors, {missing.get(kind, 0)} not found on the register, "
              f"{SKIPPED.get(kind, 0)} skipped as not found recently)", flush=True)
    con.execute(sql.SQL("INSERT INTO {}.run_log (started_at, finished_at, calls, changes, errors) VALUES (%s, now(), %s, %s, %s)").format(sql.Identifier(SCHEMA)),
                (started, _total[0], len(ids["location"]) + len(ids["provider"]), det["location"][1] + det["provider"][1]))


def expand(con, kind):
    """Fill the columns of every stored record from its detail JSON (no API calls); remove the old JSON columns."""
    table = kind
    dropped = drop_json_columns(con, table)
    if dropped:
        print(f"  {kind}: removed JSON columns {', '.join(dropped)}")
    rows = con.execute(sql.SQL("SELECT {k}, detail FROM {t} WHERE detail IS NOT NULL").format(
        k=sql.Identifier(kind + "_id"), t=sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table)))).fetchall()
    n = 0
    for oid, d in rows:
        cols = columns_for(d)
        ensure_columns(con, table, cols)
        names = [c for c in cols if c != "detail"]
        if not names:
            continue
        con.execute(sql.SQL("UPDATE {t} SET {set} WHERE {k} = %s").format(
            t=sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table)),
            set=sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in names),
            k=sql.Identifier(kind + "_id")), [cols[c] for c in names] + [oid])
        n += 1
    print(f"  {kind}: columns filled for {n:,} records")
    return n


def show(con):
    q = lambda s: con.execute(sql.SQL(s).format(sql.Identifier(SCHEMA))).fetchall()
    print("Checkpoints:", q("SELECT org_type || ' covered to ' || covered_to::text FROM {}.checkpoint"))
    print("Locations stored:", q("SELECT count(*) FROM {}.location")[0][0], "  Providers stored:", q("SELECT count(*) FROM {}.provider")[0][0])
    print("Changes recorded:", q("SELECT count(*) FROM {}.change")[0][0])
    print("Not on the register (404):", q("SELECT org_type || ': ' || count(*) || ' (last checked ' || max(last_checked)::date || ')' FROM {}.not_found GROUP BY org_type"))
    for r in q("SELECT run_id, started_at, finished_at, calls, changes, errors FROM {}.run_log ORDER BY run_id DESC LIMIT 5"):
        print("  run", r)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "changes"
    with psycopg.connect(**DB, password=os.environ.get("PGPASSWORD") or None, autocommit=True) as con:
        con.execute(DDL)
        for t in ("location", "provider"):          # old tables from earlier versions are rebuilt; old JSON columns are removed
            rebuild_if_wide(con, t)
            drop_json_columns(con, t)
        if cmd == "show":
            show(con)
            return 0
        if cmd == "expand":
            for kind in ("location", "provider"):
                expand(con, kind)
            return 0
        need_settings()
        if cmd == "changes":
            run_changes(con)
        elif cmd == "details":
            for kind in ("location", "provider"):
                ids = [r[0] for r in con.execute(sql.SQL("SELECT DISTINCT c.org_id FROM {c} c WHERE c.org_type = %s AND NOT EXISTS "
                                                         "(SELECT 1 FROM {t} t WHERE t.{k} = c.org_id AND t.fetched_at IS NOT NULL)")
                                                 .format(c=sql.SQL("{}.change").format(sql.Identifier(SCHEMA)), t=sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(kind)),
                                                         k=sql.Identifier(kind + "_id")), (kind,)).fetchall()]
                print(f"  {kind}: {len(ids):,} ids without details")
                fetch_details(con, kind, ids)
        else:
            print("Use: changes | details | expand | show")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
