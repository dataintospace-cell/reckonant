# Reckonant backend (Python)

Connects the Reckonant app to the **Signals Feeder Engine** Postgres registers. There are two parts:

| Part | File | What it is |
|---|---|---|
| **Data API** | `reckonant_backend/api.py` | FastAPI service. Reads the registers, builds every company and its signals, and serves them to the Reckonant app (`GET /api/companies`) and to dashboards (`GET /api/dashboard`). |
| **Python dashboard** | `reckonant_backend/dashboard.py` | Streamlit page with the main dashboard's numbers and lists: KPIs, top opportunities, signal mix, latest signals, risk alerts, contracts ending, open tenders and a company lookup. Use it to check the data, or as an internal view. |

The Reckonant app itself (the screens customers use after signing in) stays React. After sign-in it calls
`GET /api/companies`. **This API returns that payload in exactly the shape the app already reads**, so pointing
that one path at the API is enough to put real register data into the app. No app changes are needed.

```
 Postgres (Signals Feeder Engine)
        │  read-only
        ▼
 rk_registers.py  ── SQL per register; finds the newest dated CQC tables itself
        ▼
 rk_build.py      ── company records + dated signals + risk band + opportunity score + dashboard
        ▼
 rk_service.py    ── Dataset.load(): runs it all in batches of 500
        ▼
 api.py (port 8000) ──► /api/companies ──► Reckonant app (React, /app/)
                    ──► /api/dashboard ──► dashboard.py (Streamlit, port 8501)
```

## Files

```
reckonant_backend/
  api.py            the data API (FastAPI)
  dashboard.py      the Python dashboard (Streamlit)
  rk_service.py     loads the data set; the one entry point for both
  rk_registers.py   all SQL; schema names live in SCHEMAS at the top
  rk_build.py       turns rows into companies, signals, scores and the dashboard
  rk_mapping.py     postcode→region, SIC→sector, Companies House codes→labels, name casing, date parsing
  rk_export.py      one-off export to a JSON file, or into the current Node server's company store
tests/
  schema.sql        an exact, empty copy of the database layout (100 tables, from AlertEngine_Register_Columns.xlsx)
  seed_sample.py    10 fictional companies (numbers 99xxxxxx) covering every signal type
  test_backend.py   10 tests: data shape, every register's signals, dashboard, API, missing registers
deploy/
  reckonant-api.conf         nginx rule: data paths → Python API, behind the existing sign-in check
  reckonant-api.service      systemd unit for the API
  reckonant-dashboard.service systemd unit for the Streamlit dashboard (localhost only)
requirements.txt, .env.example
```

## Run it on a laptop (no server needed)

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

# a test database with fictional companies (any Postgres 14+)
createdb rk_demo
psql rk_demo -f tests/schema.sql
python3 tests/seed_sample.py postgresql://localhost/rk_demo

cd reckonant_backend
export RK_DATABASE_URL=postgresql://localhost/rk_demo
uvicorn api:app --port 8000                       # http://localhost:8000/docs lists every endpoint
RK_API_URL=http://localhost:8000 streamlit run dashboard.py     # http://localhost:8501
```

**The whole product on a laptop:** build the app with `build_reckonant_html.py` (it makes an `app/` folder), then start
the API with `RK_DEV=1 RK_APP_DIR=/path/to/app uvicorn api:app --port 8000` and open http://localhost:8000/app/.
`RK_DEV=1` fakes sign-in and saved state so the real React app runs against the API. Never set it in production.

## Tests

```bash
export RK_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres   # needs rights to create a database
python3 -m unittest discover -s tests -v
```

## Put it on the server (134.209.184.207)

```bash
# 1. code + Python packages
mkdir -p /opt/reckonant-backend && cp -r reckonant_backend requirements.txt /opt/reckonant-backend/
python3 -m venv /opt/reckonant-backend/venv
/opt/reckonant-backend/venv/bin/pip install -r /opt/reckonant-backend/requirements.txt

# 2. settings: a READ-ONLY database login
cp .env.example /etc/reckonant-api.env && chmod 600 /etc/reckonant-api.env && nano /etc/reckonant-api.env

# 3. services
cp deploy/reckonant-api.service deploy/reckonant-dashboard.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now reckonant-api reckonant-dashboard
curl -s localhost:8000/api/health        # {"ok":true,"companies":…}

# 4. send the data paths to Python (sign-in, saved work and emails stay on the Node server)
cp deploy/reckonant-api.conf /etc/nginx/snippets/
#    add  include snippets/reckonant-api.conf;  next to  include snippets/reckonant-app.conf;  in the site config
nginx -t && systemctl reload nginx
```

The Streamlit dashboard listens on localhost only. To open it from a Mac: `ssh -L 8501:localhost:8501 root@134.209.184.207`,
then go to http://localhost:8501.

**No API? Export files instead:** `python3 rk_export.py --node-store /var/lib/reckonant && systemctl restart reckonant`
writes the companies into the current Node server's store (keeping each company's id), so today's server serves
them with no nginx change. Run it from cron, e.g. daily.

## Endpoints

| Method and path | Returns |
|---|---|
| `GET /api/health` | `{ok, companies, builtAt, buildSeconds, error}` |
| `GET /api/companies` | `{source, datasetId, updated, companies: [Company]}`: what the app loads after sign-in |
| `GET /api/companies/{number}` | one `Company` |
| `GET /api/dashboard?days=30&sector=…&region=…` | `{totals, signalMix, bySector, byRegion, topOpportunities, latestSignals, riskAlerts, contractsEnding, openTenders, registers}`. `sector` and `region` can repeat, e.g. for an ICP |
| `GET /api/signals?days=30&polarity=opportunity\|risk\|info&key=contract_win` | `{signals: [...]}` across all companies |
| `GET /api/tenders` | open tenders from Find a Tender and Contracts Finder |
| `POST /api/refresh` | rebuild now. Needs header `X-Refresh-Token: $RK_REFRESH_TOKEN` |

The API rebuilds from the database every `RK_REFRESH_MINUTES` (default 60) and keeps serving the last good build if a
rebuild fails. Building is fast because it batches 500 companies per query round.

## The company record

**Fields the current app reads.** These match what the old Companies House import produced:

| Field | From |
|---|---|
| `num, name, status, type, incorporated, age, sic, sics, sector, address, postcode, city, region` | COM-01 `company` (region from the postcode area, sector from the first SIC code) |
| `officers [{name, role, nationality, appointed, appointedOn, resignedOn, active}]`, `directorCount` | COM-01 `company_officer` |
| `pscs [{name, kind, control, notifiedOn}]` (current owners only) | COM-01 `company_psc` |
| `riskBand, riskBandRaw, adverse` | **Reckonant's own band** from the registers: insolvent status or Gazette notice = CRITICAL; HMRC default, director insolvency, CQC Inadequate or lost sponsor licence = HIGH; Requires improvement, closures or under 2 years old = MEDIUM |
| `newDirectorRecent` | director appointed in the last 90 days |
| `newSupplier` | won a public contract in the last 180 days |
| `website` | CQC provider web address, otherwise the Companies House page |
| `turnover, employees, growth, empGrowth, netMargin, currentRatio, gearing, charges, ccj, fin, latest, lateAccounts, accountsDue, fresh` | **Not in the registers yet.** Sent as zeros/empty, the same as a company that hasn't filed accounts. They need Companies House accounts data, filing history and charges (see "Registers to fetch" in Reckonant-Data-Gaps.xlsx) |
| `decisionMakers` | always `[]`: contact details are switched off in the app |

**Extra fields** (the current app ignores these; the dashboard API and the next app version use them):
`registers` (codes), `opportunityScore` (1–99), `riskReasons`, `signals` (below), `cqc {locations, careHomes, beds, ratings, list}`,
`sponsor {onRegister, rating, route, firstSeen, lastSeen}`, `contracts [...]`, `payments {count, total, last}`, `gazette [...]`, `hmrc [...]`.

**Signals:** each is `{key, label, polarity, weight, title, detail, date, source, url}`, newest first, for the last `RK_LOOKBACK_DAYS`:

| Opportunity | Risk | Info |
|---|---|---|
| contract_win, contract_ending (next 9 months), new_location, sponsor_licence, rating_up, new_director, ownership_change, public_payment, new_company | insolvency_notice, hmrc_default, director_insolvency, rating_down, rated_inadequate, sponsor_lost, location_closed, status_change | address_change, director_left, company_change |

## Please check these assumptions against the real data

These are marked in the code. Each one is a one-line change if it's different.

1. **`register_link`**: for HMRC defaulters the code uses `source_code = 'HMD-01'` with `source_key` = Defaulter ID; for sponsors, `'HOS-01'` with Sponsor ID; for CQC, `source_code LIKE 'CQC-%'` with `source_key` = CQC provider id. (`LINK_CODES` in `rk_registers.py`.)
2. **CQC diff tables**: `change_type` values containing "add/new" mean a new location, "remov/delet" mean a location went, and anything else counts as a change. The rating change is read from `location_latest_overall_rating_prev/_curr`.
3. **Dated CQC tables** are found by name (`02_november_2026_hsca_active_locations`). The newest month is used, and diffs from the last year become signals.
4. **`company_change_log.change_type`**: matched by keyword (appoint, resign, status, address/office, psc/owner/control).
5. **Companies House codes** are expected as the API sends them (`active`, `ltd`, `director`, `ownership-of-shares-25-to-50-percent`). Readable labels also work.
6. **Company numbers** are trimmed and zero-padded to 8 characters everywhere, so `"8000 "` matches `00008000`.
7. Tables that don't exist yet are skipped with a warning, so registers can be added one at a time.

## Adding a register later

Add its schema to `SCHEMAS`, write one query function in `rk_registers.py` that returns `{company_number: [rows]}`,
add it to `load_all()`, then turn its rows into signals in `build_signals()` (`rk_build.py`) and register any new
signal key in `SIGNAL_TYPES`. Add a sample row to `tests/seed_sample.py` and a test.
