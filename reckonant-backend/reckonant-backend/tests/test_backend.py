"""
Tests for the Reckonant backend. Builds a throw-away database from tests/schema.sql (an exact, empty copy of
the Signals Feeder Engine layout), fills it with fictional companies, then checks the API and the data shapes.

    export RK_TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres   # any Postgres you can create databases on
    python3 -m unittest discover -s tests -v

The test database is called rk_test_<random> and is dropped at the end.
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "reckonant_backend"))
sys.path.insert(0, str(HERE))

ADMIN_DSN = os.environ.get("RK_TEST_DATABASE_URL", "")

# Fields the Reckonant app reads from every company (from the app's live-data mode).
APP_FIELDS = {"num": str, "name": str, "sector": str, "sic": str, "region": str, "city": str, "postcode": str, "address": str,
              "status": str, "type": str, "incorporated": str, "age": int, "turnover": (int, float), "employees": int,
              "growth": (int, float), "empGrowth": (int, float), "netMargin": (int, float), "currentRatio": (int, float),
              "gearing": (int, float), "charges": int, "ccj": int, "adverse": bool, "lateAccounts": bool, "officers": list,
              "pscs": list, "fin": list, "latest": dict, "decisionMakers": list, "directorCount": int, "fresh": bool,
              "newDirectorRecent": bool, "riskBand": str, "riskBandRaw": str, "website": str, "fetchedAt": str}


@unittest.skipUnless(ADMIN_DSN, "set RK_TEST_DATABASE_URL to run the database tests")
class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = "rk_test_" + uuid.uuid4().hex[:8]
        with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{cls.db}"')
        info = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
        info["dbname"] = cls.db
        cls.dsn = psycopg.conninfo.make_conninfo(**info)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute((HERE / "schema.sql").read_text())
        import seed_sample
        seed_sample.seed(cls.dsn)
        os.environ["RK_DATABASE_URL"] = cls.dsn
        from rk_service import Dataset
        cls.ds = Dataset.load(dsn=cls.dsn)
        cls.by = {c["num"]: c for c in cls.ds.companies}

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{cls.db}" WITH (FORCE)')

    def keys(self, num):
        return [s["key"] for s in self.by[num]["signals"]]

    # ── shape the app needs ──
    def test_every_company_has_the_fields_the_app_reads(self):
        self.assertEqual(len(self.ds.companies), 10)
        for c in self.ds.companies:
            for f, t in APP_FIELDS.items():
                self.assertIn(f, c, f"{c['num']} is missing {f}")
                self.assertIsInstance(c[f], t, f"{c['num']}.{f} has the wrong type")
            self.assertEqual(c["decisionMakers"], [], "contact details must stay off")
            self.assertIn(c["riskBand"], {"LOW", "MEDIUM", "HIGH", "CRITICAL"})
            self.assertGreaterEqual(len(c["fin"]), 2)

    def test_api_payload_shape(self):
        p = self.ds.api_companies()
        self.assertEqual(set(p), {"source", "datasetId", "updated", "companies"})

    # ── mapping ──
    def test_company_basics(self):
        c = self.by["99000001"]
        self.assertEqual(c["name"], "Brightwater Care Homes Limited")
        self.assertEqual((c["region"], c["sector"], c["status"]), ("South West", "Health & Social Care", "Active"))
        self.assertEqual(c["officers"][0]["name"], "Daniel Okafor")  # newest appointment first, "SURNAME, First" flipped
        self.assertTrue(c["website"].startswith("https://www.brightwatercare"))

    # ── signals from each register ──
    def test_cqc_signals(self):
        self.assertIn("new_location", self.keys("99000001"))
        self.assertIn("rating_up", self.keys("99000008"))
        self.assertIn("rating_down", self.keys("99000004"))
        self.assertIn("location_closed", self.keys("99000002"))
        self.assertEqual(self.by["99000008"]["cqc"]["locations"], 2, "company number with a trailing space should still match")
        self.assertEqual(self.by["99000010"]["cqc"]["locations"], 1, "provider matched through register_link")

    def test_gazette_hmrc_sponsor_signals(self):
        self.assertIn("insolvency_notice", self.keys("99000004"))
        self.assertEqual(self.by["99000004"]["riskBand"], "CRITICAL")
        self.assertIn("hmrc_default", self.keys("99000009"))
        self.assertIn("director_insolvency", self.keys("99000009"))
        self.assertEqual(self.by["99000009"]["riskBand"], "HIGH")
        self.assertIn("sponsor_licence", self.keys("99000005"))
        self.assertIn("sponsor_lost", self.keys("99000002"))

    def test_contract_and_company_house_signals(self):
        k = self.keys("99000003")
        for key in ("contract_win", "public_payment", "ownership_change", "address_change"):
            self.assertIn(key, k)
        self.assertIn("contract_ending", self.keys("99000006"), "padded/space-wrapped supplier number should match")
        self.assertIn("new_company", self.keys("99000006"))
        self.assertIn("new_director", self.keys("99000001"))
        self.assertIn("director_left", self.keys("99000002"))

    # ── dashboard ──
    def test_dashboard(self):
        d = self.ds.dashboard(days=90)
        self.assertEqual(d["totals"]["companies"], 10)
        self.assertEqual(d["totals"]["openTenders"], 2)
        self.assertGreater(d["totals"]["signals90d"], 5)
        self.assertEqual(d["topOpportunities"][0]["num"], "99000003")
        self.assertNotIn("99000004", [x["num"] for x in d["topOpportunities"]], "high-risk companies aren't top opportunities")
        self.assertEqual({r["num"] for r in d["riskAlerts"]}, {"99000004", "99000002", "99000009"})
        focused = self.ds.dashboard(days=90, sectors=["Health & Social Care"])
        self.assertEqual(focused["totals"]["companies"], 6)

    def test_api_endpoints(self):
        from fastapi.testclient import TestClient
        import api
        api.store.ds = self.ds
        cl = TestClient(api.app)
        self.assertEqual(cl.get("/api/companies").json()["companies"][0]["num"], self.ds.companies[0]["num"])
        self.assertEqual(cl.get("/api/companies/99000003").json()["name"], "Kestrel Facilities Services Limited")
        self.assertEqual(cl.get("/api/companies/nope").status_code, 404)
        self.assertEqual(cl.get("/api/dashboard", params={"days": 90, "region": "London"}).json()["totals"]["companies"], 1)
        self.assertTrue(all(s["polarity"] == "risk" for s in cl.get("/api/signals", params={"polarity": "risk", "days": 365}).json()["signals"]))
        self.assertEqual(cl.post("/api/refresh").status_code, 403)

    def test_missing_registers_are_skipped(self):
        """A register that isn't loaded yet must not break the export."""
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute('ALTER TABLE "12_contracts_finder".supplier RENAME TO supplier_off')
        try:
            from rk_service import Dataset
            ds = Dataset.load(dsn=self.dsn)
            self.assertEqual(len(ds.companies), 10)
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute('ALTER TABLE "12_contracts_finder".supplier_off RENAME TO supplier')


class MappingTests(unittest.TestCase):
    def test_mapping(self):
        import rk_mapping as m
        self.assertEqual(m.region_for("BB10 1DT"), "North West")
        self.assertEqual(m.sector_for("88100"), "Health & Social Care")
        self.assertEqual(m.title_case("ACME CARE (UK) LTD"), "Acme Care (UK) Ltd")
        self.assertEqual(m.person_name("SMITH, John"), "John Smith")
        self.assertEqual(str(m.to_date("02/11/2026")), "2026-11-02")
        self.assertEqual(m.to_number("£12,500"), 12500.0)
        self.assertEqual(m.control_text(["ownership-of-shares-25-to-50-percent"]), "25–50% shares")


if __name__ == "__main__":
    unittest.main()
