FINANCIALS + ALL COMPANIES - replace these files, then restart

Backend (reckonant_backend folder, next to api.py) - replace all three:
  api.py            adds /api/companies/page (100 companies per call)
  rk_registers.py   companies with financials load first
  rk_build.py       financial figures (in £ millions)

UI (D:\Reckonant\reckonant-ui-live\index.html) - replace it with the one from reckonant-ui-live.
  The page loads every company, 100 at a time, with a progress message, before the app opens.

Start (PowerShell, in reckonant_backend)
  $env:RK_DATABASE_URL="postgresql://reckonant_ro:YOUR_PASSWORD@localhost:5432/postgres"
  $env:RK_DEV="1"
  $env:RK_APP_DIR="D:\Reckonant\reckonant-ui-live"
  $env:RK_MAX_COMPANIES="40000"
  python -m uvicorn api:app --port 8001
Open http://localhost:8001/app/

RK_MAX_COMPANIES sets how many companies are loaded. 40000 covers the ~32,000 companies with financials.
Companies with financials load first. The first start takes longer, because the API builds all of them.
