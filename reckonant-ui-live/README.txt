FINANCIALS PATCH - two files to replace in reckonant_backend (the folder with api.py)

  rk_registers.py   adds the 15_company_financials.v_latest read (SCHEMAS entry FIN-01, function financials)
  rk_build.py       turns each company's latest accounts into turnover, profit, net assets, employees,
                    growth, margin, current ratio, gearing and the yearly figures

Steps (PowerShell)
  1. Stop the running API (Ctrl+C).
  2. Copy these two files over the ones in reckonant_backend (replace them).
  3. Start again:
       $env:RK_DATABASE_URL="postgresql://reckonant_ro:YOUR_PASSWORD@localhost:5432/postgres"
       $env:RK_DEV="1"
       $env:RK_APP_DIR="D:\Reckonant\reckonant-ui-live"
       python -m uvicorn api:app --port 8001

Rules used
  - Money is in £ millions, the same units the app's own sample data uses.
  - Employees: only whole numbers of 1 or more are used. Values like 0.94 are treated as unknown.
  - Unknown figures show as 0, with turnoverFiled / financialsFiled set to False, as before.
  - If a company has more than one row in the view, the latest period is used.
