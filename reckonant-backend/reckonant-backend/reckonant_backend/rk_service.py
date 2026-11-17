"""
Reckonant: the one place that reads the registers and builds the data set.

    from rk_service import Dataset
    ds = Dataset.load()            # reads Postgres (RK_DATABASE_URL), builds every company
    ds.companies                   # list of company records, the app's shape
    ds.dashboard(days=30)          # the main dashboard's numbers and lists

Settings (environment variables):
    RK_DATABASE_URL   postgresql://user:password@host:5432/postgres   (required)
    RK_REGISTERS      only export companies on these registers, e.g. "CQC-02,CON-01" (default: all)
    RK_MAX_COMPANIES  cap on companies loaded (default 5000; the app is built for up to ~5000)
    RK_LOOKBACK_DAYS  how far back signals go (default 365)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import psycopg

import rk_registers as reg
from rk_build import build_company, build_dashboard

log = logging.getLogger("reckonant")
BATCH = 500


def settings() -> dict:
    regs = [r.strip() for r in os.environ.get("RK_REGISTERS", "").split(",") if r.strip()]
    return {
        "dsn": os.environ.get("RK_DATABASE_URL", ""),
        "registers": regs or None,
        "max": int(os.environ.get("RK_MAX_COMPANIES", "5000")),
        "lookback": int(os.environ.get("RK_LOOKBACK_DAYS", "365")),
    }


@dataclass
class Dataset:
    companies: list[dict] = field(default_factory=list)
    tenders: list[dict] = field(default_factory=list)
    built_at: str = ""
    seconds: float = 0.0
    today: date = field(default_factory=date.today)

    @classmethod
    def load(cls, dsn: str | None = None, registers: list[str] | None = None, max_companies: int | None = None,
             only: list[str] | None = None, lookback_days: int | None = None, today: date | None = None) -> "Dataset":
        cfg = settings()
        dsn = dsn or cfg["dsn"]
        if not dsn:
            raise RuntimeError("Set RK_DATABASE_URL (postgresql://user:password@host:5432/dbname)")
        today = today or date.today()
        lookback = lookback_days or cfg["lookback"]
        t0 = time.time()
        fetched_at = datetime.now(timezone.utc).isoformat()
        out: list[dict] = []
        with psycopg.connect(dsn) as conn:
            conn.read_only = True
            cat = reg.Catalog(conn)
            nums = reg.company_numbers(conn, cat, registers or cfg["registers"], max_companies or cfg["max"], only)
            log.info("Building %d companies", len(nums))
            for i in range(0, len(nums), BATCH):
                part = [reg.norm_num(n) for n in nums[i:i + BATCH]]
                raw = reg.load_all(conn, part, cat, today, lookback)
                out += [c for c in (build_company(n, raw, today, fetched_at, lookback) for n in part) if c]
            tenders = reg.open_tenders(conn, cat, today)
        out.sort(key=lambda c: c["name"])
        ds = cls(companies=out, tenders=tenders, built_at=fetched_at, seconds=round(time.time() - t0, 2), today=today)
        log.info("Built %d companies and %d open tenders in %.1fs", len(out), len(tenders), ds.seconds)
        return ds

    # ── views ──
    def api_companies(self) -> dict:
        """GET /api/companies: the exact payload the Reckonant app loads after sign-in."""
        return {"source": "Signals Feeder Engine", "datasetId": "sfe-1", "updated": self.built_at, "companies": self.companies}

    def company(self, num: str) -> dict | None:
        n = reg.norm_num(num)
        return next((c for c in self.companies if c["num"] == n), None)

    def dashboard(self, days: int = 30, sectors: list[str] | None = None, regions: list[str] | None = None) -> dict:
        return build_dashboard(self.companies, self.tenders, self.today, days, sectors, regions)

    def signals(self, days: int = 30, polarity: str | None = None, key: str | None = None, limit: int = 200) -> list[dict]:
        rows = []
        for c in self.companies:
            for s in c["signals"]:
                age = (self.today - date.fromisoformat(s["date"])).days if s["date"] else None
                if age is None or not (-366 <= age <= days):
                    continue
                if (polarity and s["polarity"] != polarity) or (key and s["key"] != key):
                    continue
                rows.append({"num": c["num"], "name": c["name"], "sector": c["sector"], "region": c["region"], **s})
        return sorted(rows, key=lambda r: r["date"], reverse=True)[:limit]
