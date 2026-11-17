"""
Reckonant: one-off export, for when you'd rather not run the API.

    python3 rk_export.py --json companies.json            # the /api/companies payload, to inspect or hand over
    python3 rk_export.py --json dashboard.json --dashboard # the /api/dashboard payload
    python3 rk_export.py --node-store /var/lib/reckonant   # write into the existing Node server's company store,
                                                          # then: systemctl restart reckonant

The --node-store option lets today's server keep serving GET /api/companies with no nginx change: it writes
one JSON file per company into <dir>/companies/ (the same files the Companies House import writes), keeping
each company's permanent id ("seq") so users' saved lists stay pointed at the right companies.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from rk_service import Dataset

log = logging.getLogger("reckonant")


def write_node_store(companies: list[dict], data_dir: Path) -> int:
    co_dir = data_dir / "companies"
    co_dir.mkdir(parents=True, exist_ok=True)
    meta_file = co_dir / "_meta.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {"nextSeq": 0, "datasetId": ""}
    if not meta.get("datasetId"):
        meta["datasetId"] = "sfe-1"
    for c in companies:
        f = co_dir / f"{c['num']}.json"
        old = json.loads(f.read_text()) if f.exists() else {}
        if old.get("seq") is not None:
            c["seq"] = old["seq"]
        else:
            c["seq"] = meta["nextSeq"]; meta["nextSeq"] += 1
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(c, ensure_ascii=False, default=str))
        os.replace(tmp, f)
    meta_file.write_text(json.dumps(meta))
    return len(companies)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Export Reckonant company data from the Signals Feeder Engine registers.")
    ap.add_argument("--json", help="write the payload to this file")
    ap.add_argument("--dashboard", action="store_true", help="with --json: write the dashboard payload instead")
    ap.add_argument("--node-store", help="write into the Node server's data folder (e.g. /var/lib/reckonant)")
    ap.add_argument("--only", nargs="*", help="only these company numbers")
    ap.add_argument("--registers", help="only companies on these registers, e.g. CQC-02,CON-01")
    ap.add_argument("--max", type=int, help="at most this many companies")
    a = ap.parse_args()
    if not (a.json or a.node_store):
        ap.error("give --json FILE and/or --node-store DIR")

    ds = Dataset.load(registers=a.registers.split(",") if a.registers else None, max_companies=a.max, only=a.only)
    if a.json:
        payload = ds.dashboard() if a.dashboard else ds.api_companies()
        Path(a.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str))
        log.info("Wrote %s", a.json)
    if a.node_store:
        n = write_node_store(ds.companies, Path(a.node_store))
        log.info("Wrote %d companies to %s/companies. Now run: systemctl restart reckonant", n, a.node_store)


if __name__ == "__main__":
    main()
