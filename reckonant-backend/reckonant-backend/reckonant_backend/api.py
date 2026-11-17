"""
Reckonant data API: serves the Signals Feeder Engine registers to the Reckonant app and dashboard.

Run:
    export RK_DATABASE_URL=postgresql://user:password@host:5432/postgres
    uvicorn api:app --host 127.0.0.1 --port 8000

Endpoints (all JSON):
    GET  /api/health                      status, company count, when the data was built
    GET  /api/companies                   the payload the Reckonant app loads after sign-in (same shape as before)
    GET  /api/companies/{number}          one company, with all its signals and register details
    GET  /api/dashboard?days=30&sector=Health%20%26%20Social%20Care&region=London
                                          the main dashboard: totals, signal mix, top opportunities, latest signals,
                                          risk alerts, contracts ending, open tenders, register coverage
    GET  /api/signals?days=30&polarity=opportunity|risk|info&key=contract_win&limit=200
    GET  /api/tenders                     open public tenders
    POST /api/refresh                     rebuild now (header X-Refresh-Token: $RK_REFRESH_TOKEN)

Sign-in: in production nginx only lets signed-in users reach these paths (see README). With RK_DEV=1 the API
also fakes sign-in and saved state and serves the built app from RK_APP_DIR at /app/, so the whole product
can be run on a laptop with no Node server.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from rk_service import Dataset

logging.basicConfig(level=os.environ.get("RK_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reckonant")

REFRESH_MINUTES = float(os.environ.get("RK_REFRESH_MINUTES", "60"))
DEV = os.environ.get("RK_DEV") == "1"

@asynccontextmanager
async def lifespan(_app):
    def loop():
        while True:
            store.refresh()
            time.sleep(max(60, REFRESH_MINUTES * 60))
    threading.Thread(target=loop, daemon=True).start()
    yield


app = FastAPI(title="Reckonant data API", version="1.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2000)


class Store:
    """Holds the last good build; rebuilds in the background every RK_REFRESH_MINUTES."""

    def __init__(self):
        self.ds: Dataset | None = None
        self.error = ""
        self.lock = threading.Lock()

    def refresh(self):
        if not self.lock.acquire(blocking=False):
            return  # a rebuild is already running
        try:
            self.ds = Dataset.load()
            self.error = ""
        except Exception as e:  # keep serving the last good data
            self.error = str(e)
            log.exception("Rebuild failed")
        finally:
            self.lock.release()

    def get(self) -> Dataset:
        if self.ds is None:
            self.refresh()
        if self.ds is None:
            raise HTTPException(503, f"Data isn't available yet: {self.error or 'still loading'}")
        return self.ds


store = Store()


def _list(v: list[str] | None) -> list[str] | None:
    out = [x for item in (v or []) for x in item.split(",") if x.strip()]
    return out or None


# ── data ──────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    ds = store.ds
    return {"ok": ds is not None, "companies": len(ds.companies) if ds else 0, "builtAt": ds.built_at if ds else "",
            "buildSeconds": ds.seconds if ds else None, "error": store.error}


@app.get("/api/companies")
def companies():
    return store.get().api_companies()


@app.get("/api/companies/page")
def companies_page(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    """One page of companies, so a browser can load a large list in steps: /api/companies/page?offset=0&limit=100"""
    ds = store.get()
    return {"source": "Signals Feeder Engine", "datasetId": "sfe-1", "updated": ds.built_at, "total": len(ds.companies),
            "offset": offset, "limit": limit, "companies": ds.companies[offset:offset + limit]}


@app.get("/api/companies/{number}")
def company(number: str):
    c = store.get().company(number)
    if not c:
        raise HTTPException(404, "No company with that number")
    return c


@app.get("/api/dashboard")
def dashboard(days: int = Query(30, ge=1, le=365), sector: list[str] | None = Query(None), region: list[str] | None = Query(None)):
    return store.get().dashboard(days, _list(sector), _list(region))


@app.get("/api/signals")
def signals(days: int = Query(30, ge=1, le=365), polarity: str | None = None, key: str | None = None,
            limit: int = Query(200, ge=1, le=2000)):
    return {"signals": store.get().signals(days, polarity, key, limit)}


@app.get("/api/tenders")
def tenders():
    return {"tenders": store.get().tenders}


@app.post("/api/refresh")
def refresh(x_refresh_token: str = Header("")):
    token = os.environ.get("RK_REFRESH_TOKEN", "")
    if not token or x_refresh_token != token:
        raise HTTPException(403, "Set RK_REFRESH_TOKEN and send it in the X-Refresh-Token header")
    store.refresh()
    return health()


# ── local development only (RK_DEV=1): fake sign-in + saved state + the built app at /app/ ──
if DEV:
    from fastapi.staticfiles import StaticFiles

    _state: dict = {}

    @app.get("/api/auth/me")
    def me():
        return {"email": "dev@reckonant.local", "name": "Developer"}

    @app.post("/api/auth/logout")
    def logout():
        return {"ok": True}

    @app.get("/api/state")
    def get_state():
        return JSONResponse(_state.get("v", {}))

    @app.put("/api/state")
    async def put_state(request: Request):
        _state["v"] = await request.json()
        return {"ok": True}

    @app.get("/login.html")
    def login():
        return RedirectResponse("/app/")

    app_dir = os.environ.get("RK_APP_DIR", "")
    if app_dir and os.path.isdir(app_dir):
        app.mount("/app", StaticFiles(directory=app_dir, html=True), name="app")
