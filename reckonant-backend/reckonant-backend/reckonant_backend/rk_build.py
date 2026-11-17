"""
Reckonant: turns raw register rows into
  1. company records in the exact shape the Reckonant app reads from GET /api/companies,
  2. a dated list of signals (events) per company, and
  3. the numbers and lists behind the main dashboard (GET /api/dashboard).

No database code here: rk_registers.py fetches the rows, this file only shapes them, so it can be
unit-tested with plain dicts.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone

from rk_mapping import (INSOLVENT_STATUSES, company_type_label, control_text, days_since, dmy, humanize, iso,
                        officer_role_label, person_name, psc_kind_label, region_for, sector_for, status_key,
                        title_case, to_date, to_number, years_since)

# ── signal types ──────────────────────────────────────────────────────────────
# polarity: "opportunity" (a reason to engage), "risk" (a warning sign), "info" (worth knowing)
SIGNAL_TYPES = {
    "contract_win":         ("Contract won", "opportunity", 30),
    "contract_ending":      ("Contract ending soon", "opportunity", 25),
    "new_location":         ("New care location", "opportunity", 25),
    "sponsor_licence":      ("New visa sponsor licence", "opportunity", 20),
    "rating_up":            ("CQC rating improved", "opportunity", 15),
    "new_director":         ("New director", "opportunity", 15),
    "ownership_change":     ("Ownership change", "opportunity", 12),
    "public_payment":       ("Public-sector payment", "opportunity", 10),
    "new_company":          ("Newly incorporated", "opportunity", 8),
    "address_change":       ("Moved address", "info", 6),
    "company_change":       ("Company record changed", "info", 4),
    "director_left":        ("Director left", "info", 8),
    "insolvency_notice":    ("Insolvency notice", "risk", 40),
    "hmrc_default":         ("HMRC tax defaulter", "risk", 30),
    "director_insolvency":  ("Director personal insolvency", "risk", 25),
    "rating_down":          ("CQC rating fell", "risk", 25),
    "rated_inadequate":     ("Rated Inadequate by CQC", "risk", 25),
    "sponsor_lost":         ("Sponsor licence lost or downgraded", "risk", 20),
    "location_closed":      ("Care location closed", "risk", 15),
    "status_change":        ("Company status changed", "risk", 30),
}
RATING_ORDER = {"outstanding": 4, "good": 3, "requires improvement": 2, "inadequate": 1}
REGISTER_NAMES = {
    "COM-01": "Companies House", "CQC-01": "CQC Care Directory", "CQC-02": "CQC Care Directory (filters)",
    "CQC-03": "CQC Ratings", "CQC-04": "CQC Deactivated Locations", "INS-00": "Gazette", "INS-01": "Gazette personal notices",
    "INS-02": "Gazette corporate notices", "GAZ-01": "Gazette other notices", "HMD-01": "HMRC Tax defaulters",
    "HOS-01": "Home Office sponsor register", "CON-01": "Contracts Finder", "FTS-01": "Find a Tender",
    "PTR-01": "Payments Transparency",
}
CH_URL = "https://find-and-update.company-information.service.gov.uk/company/{}"


def _money(v) -> str:
    n = to_number(v)
    if n is None:
        return ""
    return f"£{n / 1e6:.1f}m" if abs(n) >= 1e6 else f"£{n / 1e3:.0f}k" if abs(n) >= 1e3 else f"£{n:.0f}"


def _sig(key: str, when, detail: str, source: str, url: str = "", title: str | None = None, **extra) -> dict:
    label, polarity, weight = SIGNAL_TYPES[key]
    d = to_date(when)
    return {"key": key, "label": label, "polarity": polarity, "weight": weight, "title": title or label,
            "detail": detail, "date": d.isoformat() if d else "", "source": source, "url": url or "", **extra}


def _recency(d: str, today: date) -> float:
    """1.0 for this month, fading to 0.3 over a year. Future dates (a contract ending) count by how soon."""
    if not d:
        return 0.3
    gap = abs(days_since(d, today) or 0)
    return 1.0 if gap <= 30 else 0.75 if gap <= 90 else 0.5 if gap <= 180 else 0.3


# ── signals for one company ───────────────────────────────────────────────────
def build_signals(num: str, raw: dict, today: date, lookback_days: int = 365, ending_within_days: int = 270) -> list[dict]:
    out: list[dict] = []
    recent = lambda v: (d := days_since(v, today)) is not None and 0 <= d <= lookback_days
    co = raw["companies"].get(num) or {}

    # Companies House: change log, officers, owners, incorporation
    seen_dirs: set[str] = set()
    for ch in raw["changes"].get(num, []):
        t, detail = str(ch.get("change_type") or "").lower(), str(ch.get("detail") or "")
        if "appoint" in t:
            key = "new_director"; seen_dirs.add(detail.lower())
        elif "resign" in t or "ceas" in t and "officer" in t:
            key = "director_left"
        elif "status" in t:
            key = "status_change" if any(s in detail.lower() for s in INSOLVENT_STATUSES) else "company_change"
        elif "address" in t or "office" in t:
            key = "address_change"
        elif "psc" in t or "owner" in t or "control" in t:
            key = "ownership_change"
        else:
            key = "company_change"
        out.append(_sig(key, ch.get("detected_date"), detail or humanize(t), "Companies House", CH_URL.format(num)))
    for o in raw["officers"].get(num, []):
        name = person_name(o.get("officer_name"))
        if recent(o.get("appointed_on")) and not any(name.lower() in s for s in seen_dirs):
            out.append(_sig("new_director", o["appointed_on"], f"{name} appointed {officer_role_label(o.get('officer_role')).lower()}",
                            "Companies House", CH_URL.format(num)))
        if recent(o.get("resigned_on")):
            out.append(_sig("director_left", o["resigned_on"], f"{name} resigned", "Companies House", CH_URL.format(num)))
    for p in raw["pscs"].get(num, []):
        if recent(p.get("notified_on")):
            out.append(_sig("ownership_change", p["notified_on"], f"{title_case(p.get('psc_name'))} became an owner "
                            f"({control_text(p.get('natures_of_control'))})", "Companies House", CH_URL.format(num)))
        if recent(p.get("ceased_on")):
            out.append(_sig("ownership_change", p["ceased_on"], f"{title_case(p.get('psc_name'))} is no longer an owner",
                            "Companies House", CH_URL.format(num)))
    if recent(co.get("date_of_creation")):
        out.append(_sig("new_company", co["date_of_creation"], "Incorporated in the last year", "Companies House", CH_URL.format(num)))

    # CQC: monthly changes, closures, current ratings
    for r in raw["cqc_changes"].get(num, []):
        t = str(r.get("change_type") or "").lower()
        name = r.get("location_name_curr") or r.get("location_name_prev") or "A location"
        city = r.get("location_city_curr") or r.get("location_city_prev") or ""
        where = f"{name}{', ' + city if city else ''}"
        if any(k in t for k in ("add", "new", "insert")):
            beds = to_number(r.get("care_homes_beds_curr"))
            out.append(_sig("new_location", r["diff_date"], f"{where} registered{f' ({beds:.0f} beds)' if beds else ''}", "CQC",
                            location_id=r.get("key_value")))
        elif any(k in t for k in ("remov", "delet", "drop")):
            out.append(_sig("location_closed", r["diff_date"], f"{where} no longer active", "CQC", location_id=r.get("key_value")))
        else:
            prev, curr = (str(r.get(f"location_latest_overall_rating_{x}") or "").strip() for x in ("prev", "curr"))
            if prev and curr and prev.lower() != curr.lower():
                up = RATING_ORDER.get(curr.lower(), 0) > RATING_ORDER.get(prev.lower(), 0)
                out.append(_sig("rating_up" if up else "rating_down", r["diff_date"], f"{where}: {prev} → {curr}", "CQC",
                                location_id=r.get("key_value")))
    closed_ids = {s.get("location_id") for s in out if s["key"] == "location_closed"}
    for r in raw["cqc_closed"].get(num, []):
        if recent(r.get("location_hsca_end_date")) and r.get("location_id") not in closed_ids:
            beds = to_number(r.get("care_homes_beds_at_point_location_de_activated"))
            out.append(_sig("location_closed", r["location_hsca_end_date"],
                            f"{r.get('location_name')}{', ' + r['location_city'] if r.get('location_city') else ''} closed"
                            f"{f' ({beds:.0f} beds)' if beds else ''}", "CQC", location_id=r.get("location_id")))
    for r in raw["cqc"].get(num, []):
        if str(r.get("location_latest_overall_rating") or "").lower() == "inadequate" and recent(r.get("publication_date")):
            out.append(_sig("rated_inadequate", r["publication_date"], f"{r.get('location_name')} rated Inadequate", "CQC",
                            location_id=r.get("location_id")))

    # Gazette and directors' personal insolvency
    for g in raw["gazette"].get(num, []):
        out.append(_sig("insolvency_notice", g.get("published_date"), g.get("type_label") or g.get("title") or "Gazette notice",
                        "The Gazette", g.get("url") or "", title=humanize(g.get("insolvency_kind")) or None))
    for p in raw["director_insolvency"].get(num, []):
        out.append(_sig("director_insolvency", p.get("notice_date"),
                        f"{person_name(p.get('officer_name') or p.get('person_name'))} ({str(p.get('officer_role') or 'officer').lower()}) "
                        f"named in a personal insolvency notice (match: {p.get('match_level')})", "The Gazette"))

    # HMRC defaulters and Home Office sponsors
    for h in raw["hmrc"].get(num, []):
        tax = _money(h.get("Total amount of tax/duty on which penalties are based"))
        out.append(_sig("hmrc_default", h.get("First listed"),
                        f"Listed as a deliberate tax defaulter{f' ({tax} tax)' if tax else ''}"
                        f"{'' if h.get('Still listed') else ', no longer listed'}", "HMRC", h.get("url") or ""))
    for s in raw["sponsor"].get(num, []):
        if s.get("On register") and recent(s.get("First seen")):
            out.append(_sig("sponsor_licence", s["First seen"], f"Licensed to sponsor visas: {s.get('Route') or ''} "
                            f"({s.get('Type & Rating') or ''})".replace(":  (", ": (").strip(), "Home Office", s.get("url") or ""))
    for c in raw["sponsor_changes"].get(num, []):
        t = str(c.get("change_type") or "").lower()
        if any(k in t for k in ("remov", "delet", "downgrad")) or "b rating" in str(c.get("detail") or "").lower():
            out.append(_sig("sponsor_lost", c.get("detected_date"), c.get("detail") or humanize(t), "Home Office"))

    # Public contracts and payments
    for k in raw["contracts"].get(num, []):
        src = "Find a Tender" if k.get("register") == "FTS-01" else "Contracts Finder"
        title, buyer, value = k.get("Title") or "a public contract", k.get("Buyer") or "a public body", _money(k.get("Award value"))
        if recent(k.get("Award date")):
            out.append(_sig("contract_win", k["Award date"], f"Won {title} from {buyer}{f' ({value})' if value else ''}", src,
                            k.get("url") or "", buyer=buyer, value=to_number(k.get("Award value"))))
        until = -(days_since(k.get("Contract end"), today) or 1)
        if 0 <= until <= ending_within_days:
            out.append(_sig("contract_ending", k["Contract end"], f"{title} with {buyer} ends {dmy(k['Contract end'])}"
                            f"{f' ({value})' if value else ''}", src, k.get("url") or "", buyer=buyer, daysUntil=until))
    paid: Counter = Counter()
    last: dict[str, str] = {}
    for p in raw["payments"].get(num, []):
        if (d := days_since(p.get("Payment date"), today)) is not None and 0 <= d <= 90:
            auth = p.get("Contracting authority") or "a public body"
            paid[auth] += to_number(p.get("Payment value")) or 0
            last[auth] = max(last.get(auth, ""), iso(p.get("Payment date")))
    for auth, total in paid.most_common(3):
        out.append(_sig("public_payment", last[auth], f"Paid {_money(total)} by {auth} in the last 90 days", "Payments Transparency"))

    out.sort(key=lambda s: s["date"] or "", reverse=True)
    return out


# ── risk band and scores ──────────────────────────────────────────────────────
def risk_band(co: dict, signals: list[dict], raw_cqc: list[dict], today: date) -> tuple[str, str, list[str]]:
    """Reckonant's own register-based band (no credit-reference data needed)."""
    reasons, st = [], status_key(co.get("company_status"))
    within = lambda s, days: s["date"] and abs(days_since(s["date"], today) or 0) <= days
    level = "LOW"
    if st in INSOLVENT_STATUSES:
        level = "CRITICAL"; reasons.append(f"Company status: {humanize(st)}")
    if any(s["key"] == "insolvency_notice" and within(s, 730) for s in signals):
        level = "CRITICAL"; reasons.append("Insolvency notice in the Gazette")
    high = [s for s in signals if s["key"] in ("hmrc_default", "director_insolvency", "rated_inadequate", "sponsor_lost") and within(s, 365)]
    if high and level != "CRITICAL":
        level = "HIGH"
    reasons += [s["label"] for s in high]
    ratings = [str(r.get("location_latest_overall_rating") or "").lower() for r in raw_cqc]
    medium = ("requires improvement" in ratings or any(s["key"] in ("rating_down", "location_closed") and within(s, 365) for s in signals)
              or years_since(co.get("date_of_creation"), today) < 2)
    if medium and level == "LOW":
        level = "MEDIUM"
    if medium:
        reasons.append("Watch: rating, closures or a young company")
    return level, {"LOW": "Low Risk", "MEDIUM": "Moderate Risk", "HIGH": "High Risk", "CRITICAL": "Very High Risk"}[level], reasons


def opportunity_score(signals: list[dict], band: str, today: date) -> int:
    opp = sum(s["weight"] * _recency(s["date"], today) for s in signals if s["polarity"] == "opportunity")
    risk = sum(s["weight"] * _recency(s["date"], today) for s in signals if s["polarity"] == "risk")
    score = 35 + opp - 0.6 * risk - {"LOW": 0, "MEDIUM": 5, "HIGH": 20, "CRITICAL": 40}[band]
    return int(max(1, min(99, round(score))))


# ── the company record the app reads ──────────────────────────────────────────
def _zero_year(year: int) -> dict:
    return {"year": year, "revenue": 0, "grossProfit": 0, "ebitda": 0, "netProfit": 0, "totalAssets": 0,
            "totalLiabilities": 0, "cash": 0, "netAssets": 0, "debt": 0, "employees": 0}


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _count(v) -> int | None:
    """Employees: a whole number of at least 1. Anything else (e.g. 0.94) is treated as unknown."""
    x = _num(v)
    return int(x) if x is not None and x >= 1 and x == int(x) else None


def financial_fields(row: dict | None, nowy: int) -> dict:
    """Turns one 15_company_financials.v_latest row into the app's financial fields."""
    zero = {"turnover": 0, "employees": 0, "growth": 0, "empGrowth": 0, "netMargin": 0, "currentRatio": 0, "gearing": 0,
            "turnoverFiled": False, "financialsFiled": False,
            "fin": [_zero_year(nowy - 2), _zero_year(nowy - 1)], "latest": _zero_year(nowy - 1)}
    if not row:
        return zero
    g = lambda k: _num(row.get(k))
    turnover, pturn = g("turnover"), g("prior_turnover")
    net_assets, pnet = g("net_assets"), g("prior_net_assets")
    cur_assets, cred1, cred2 = g("current_assets"), g("creditors_within_1yr"), g("creditors_after_1yr")
    fixed, profit, cash = g("fixed_assets"), g("profit_for_period"), g("cash")
    try:
        year = int(str(row.get("period_end") or "")[:4])
    except ValueError:
        year = nowy - 1
    total_assets = fixed + cur_assets if fixed is not None and cur_assets is not None else None
    debt = (cred1 or 0) + (cred2 or 0) if (cred1 is not None or cred2 is not None) else None

    def m(v):  # the app stores money in £ millions (e.g. revenue 1.2 = £1.2m)
        return round((v or 0) / 1e6, 3)

    def block(y, rev, gp, np_, ta, na, cash_, debt_, emp):
        return {"year": y, "revenue": m(rev), "grossProfit": m(gp), "ebitda": 0, "netProfit": m(np_),
                "totalAssets": m(ta), "totalLiabilities": m((ta - na) if ta is not None and na is not None else 0),
                "cash": m(cash_), "netAssets": m(na), "debt": m(debt_), "employees": emp or 0}

    emp_now, emp_prior = _count(row.get("employees")), _count(row.get("prior_employees"))
    latest = block(year, turnover, g("gross_profit"), profit, total_assets, net_assets, cash, debt, emp_now)
    prior = block(year - 1, pturn, None, g("prior_profit_for_period"), None, pnet, g("prior_cash"), None, emp_prior)
    return {
        "turnover": latest["revenue"], "employees": latest["employees"],
        "growth": round((turnover - pturn) / abs(pturn) * 100, 1) if turnover is not None and pturn else 0,
        "empGrowth": round((emp_now - emp_prior) / emp_prior * 100, 1) if emp_now and emp_prior else 0,
        "netMargin": round(profit / turnover * 100, 1) if profit is not None and turnover else 0,
        "currentRatio": round(cur_assets / cred1, 2) if cur_assets is not None and cred1 else 0,
        "gearing": round(debt / net_assets * 100, 1) if debt is not None and net_assets and net_assets > 0 else 0,
        "turnoverFiled": turnover is not None,
        "financialsFiled": turnover is not None or net_assets is not None,
        "fin": [prior, latest], "latest": latest,
    }


def build_company(num: str, raw: dict, today: date, fetched_at: str, lookback_days: int = 365) -> dict | None:
    co = raw["companies"].get(num)
    if not co:
        return None
    sics = [str(s) for s in (co.get("sic_codes") or []) if s]
    sic = sics[0] if sics else ""
    postcode = (co.get("ro_postal_code") or "").strip()
    addr_parts = [co.get("ro_premises"), co.get("ro_address_line_1"), co.get("ro_address_line_2"), co.get("ro_locality"), co.get("ro_region")]
    address = ", ".join(title_case(x) for x in addr_parts if x) + (f", {postcode}" if postcode else "")
    if not address.strip(", ") and co.get("registered_office"):
        address = str(co["registered_office"])
    st = status_key(co.get("company_status"))

    offs = raw["officers"].get(num, [])
    officers = [{"name": person_name(o.get("officer_name")), "role": officer_role_label(o.get("officer_role")),
                 "nationality": title_case(o.get("nationality")) if o.get("nationality") else "",
                 "appointed": iso(o.get("appointed_on"))[:4], "appointedOn": iso(o.get("appointed_on")),
                 "resignedOn": iso(o.get("resigned_on")), "active": not o.get("resigned_on"), "occupation": ""} for o in offs]
    pscs = [{"name": title_case(p.get("psc_name")), "kind": psc_kind_label(p.get("psc_kind")),
             "control": control_text(p.get("natures_of_control")), "notifiedOn": iso(p.get("notified_on"))}
            for p in raw["pscs"].get(num, []) if not p.get("ceased")]

    signals = build_signals(num, raw, today, lookback_days)
    cqc_rows = raw["cqc"].get(num, [])
    band, band_raw, band_reasons = risk_band(co, signals, cqc_rows, today)
    recent = lambda days: [s for s in signals if s["date"] and 0 <= (days_since(s["date"], today) or 0) <= days]

    # register summaries (extra fields: the current app ignores them; the dashboard API uses them)
    ratings = Counter(str(r.get("location_latest_overall_rating") or "Not rated") for r in cqc_rows)
    beds = sum(to_number(r.get("care_homes_beds")) or 0 for r in cqc_rows)
    cqc = None if not cqc_rows else {
        "locations": len(cqc_rows), "careHomes": sum(1 for r in cqc_rows if str(r.get("care_home") or "").upper().startswith("Y")),
        "beds": int(beds), "ratings": dict(ratings),
        "list": [{"id": r.get("location_id"), "name": r.get("location_name"), "rating": r.get("location_latest_overall_rating") or "",
                  "published": iso(r.get("publication_date")), "beds": int(to_number(r.get("care_homes_beds")) or 0),
                  "city": r.get("location_city") or "", "postcode": r.get("location_postal_code") or "",
                  "type": r.get("location_type_sector") or ""} for r in cqc_rows[:50]],
    }
    web = next((r.get("provider_web_address") for r in cqc_rows if r.get("provider_web_address")), "")
    if web and not str(web).startswith("http"):
        web = "https://" + str(web)
    sponsor = next(iter(raw["sponsor"].get(num, [])), None)
    contracts = raw["contracts"].get(num, [])
    payments = raw["payments"].get(num, [])
    hmrc = raw["hmrc"].get(num, [])

    nowy = today.year
    return {
        # ── fields the current app reads (same as the Companies House import) ──
        "num": num, "name": title_case(co.get("company_name")), "sector": sector_for(sic), "sic": sic, "sics": sics,
        "region": region_for(postcode, co.get("ro_country")), "city": title_case(co.get("ro_locality") or co.get("ro_region") or ""),
        "postcode": postcode, "address": address, "status": humanize(st) or "Unknown",
        "type": company_type_label(co.get("company_type")), "incorporated": dmy(co.get("date_of_creation")),
        "age": years_since(co.get("date_of_creation"), today),
        # financials from 15_company_financials (v_latest); unknown figures are 0 with the *Filed flags False
        **financial_fields(raw.get("fin", {}).get(num), nowy),
        "charges": 0, "ccj": 0, "estimated": False,
        "adverse": band == "CRITICAL", "lateAccounts": False, "accountsDue": "", "lastAccountsFiled": "",
        "officers": officers, "pscs": pscs, "decisionMakers": [],  # contact details are switched off in the app
        "directorCount": sum(1 for o in officers if o["active"]),
        # the current app reads "fresh" as "new accounts filed", which needs filing history (not in the registers yet)
        "fresh": False, "hasRecentSignals": bool(recent(45)), "newDirectorRecent": any(s["key"] == "new_director" for s in recent(90)),
        "riskBand": band, "riskBandRaw": band_raw, "parentId": None, "parent": "", "about": "",
        "importChange": 0, "newSupplier": any(s["key"] == "contract_win" for s in recent(180)),
        "website": web or CH_URL.format(num), "source": "Signals Feeder Engine", "fetchedAt": fetched_at,
        # ── extra fields (register data; ready for the next app version and used by the dashboard) ──
        "registers": sorted({r.get("register_code") for r in raw["registers"].get(num, []) if r.get("register_code")}),
        "riskReasons": band_reasons,
        "opportunityScore": opportunity_score(signals, band, today),
        "signals": signals[:60],
        "cqc": cqc,
        "sponsor": None if not sponsor else {"onRegister": bool(sponsor.get("On register")), "rating": sponsor.get("Type & Rating") or "",
                                              "route": sponsor.get("Route") or "", "firstSeen": iso(sponsor.get("First seen")),
                                              "lastSeen": iso(sponsor.get("Last seen"))},
        "contracts": [{"register": k.get("register"), "title": k.get("Title") or "", "buyer": k.get("Buyer") or "",
                       "value": to_number(k.get("Award value")), "awarded": iso(k.get("Award date")), "start": iso(k.get("Contract start")),
                       "end": iso(k.get("Contract end")), "url": k.get("url") or ""} for k in contracts[:50]],
        "payments": {"count": len(payments), "total": sum(to_number(p.get("Payment value")) or 0 for p in payments),
                     "last": iso(payments[0].get("Payment date")) if payments else ""},
        "gazette": [{"date": iso(g.get("published_date")), "kind": humanize(g.get("insolvency_kind")), "type": g.get("type_label") or "",
                     "url": g.get("url") or ""} for g in raw["gazette"].get(num, [])[:20]],
        "hmrc": [{"firstListed": iso(h.get("First listed")), "stillListed": bool(h.get("Still listed")),
                  "period": h.get("Period of default") or "", "tax": h.get("Total amount of tax/duty on which penalties are based") or "",
                  "penalties": h.get("Total amount of penalties charged") or ""} for h in hmrc],
    }


# ── dashboard ─────────────────────────────────────────────────────────────────
def _brief(c: dict) -> dict:
    return {"num": c["num"], "name": c["name"], "sector": c["sector"], "region": c["region"], "city": c["city"],
            "status": c["status"], "riskBand": c["riskBand"], "opportunityScore": c["opportunityScore"]}


def build_dashboard(companies: list[dict], tenders: list[dict], today: date, days: int = 30,
                    sectors: list[str] | None = None, regions: list[str] | None = None) -> dict:
    """Everything the main dashboard shows, optionally narrowed to an ICP (sectors / regions)."""
    cos = [c for c in companies if (not sectors or c["sector"] in sectors) and (not regions or c["region"] in regions)]
    in_days = lambda s, n: s["date"] and 0 <= (days_since(s["date"], today) or 0) <= n
    sig = [(c, s) for c in cos for s in c["signals"]]
    recent = [(c, s) for c, s in sig if in_days(s, days)]
    ending = sorted([(c, s) for c, s in sig if s["key"] == "contract_ending"], key=lambda x: x[1]["date"])
    window_keys = {"contract_win", "new_location", "sponsor_licence", "new_director", "ownership_change"}
    windows = {c["num"] for c, s in sig if (s["key"] in window_keys and in_days(s, 60))} | {c["num"] for c, _ in ending if _}
    risky = [c for c in cos if c["riskBand"] in ("HIGH", "CRITICAL")]
    top = sorted((c for c in cos if c["riskBand"] in ("LOW", "MEDIUM")), key=lambda c: -c["opportunityScore"])[:10]
    mix = Counter(s["key"] for _, s in recent)
    reg = Counter(code for c in cos for code in c.get("registers", []))
    by = lambda field: [{"name": k, "companies": v, "signals": sum(1 for c, _ in recent if c[field] == k)}
                        for k, v in Counter(c[field] or "Unknown" for c in cos).most_common(12)]
    return {
        "updated": datetime.now(timezone.utc).isoformat(), "periodDays": days,
        "focus": {"sectors": sectors or [], "regions": regions or []},
        "totals": {
            "companies": len(cos), "active": sum(1 for c in cos if c["status"] == "Active"),
            "signals7d": sum(1 for _, s in sig if in_days(s, 7)), f"signals{days}d": len(recent),
            "opportunities": len({c["num"] for c, s in recent if s["polarity"] == "opportunity"}),
            "risks": len(risky), "buyingWindows": len(windows),
            "contractsEnding": len({c["num"] for c, _ in ending}), "openTenders": len(tenders),
        },
        "signalMix": [{"key": k, "label": SIGNAL_TYPES[k][0], "polarity": SIGNAL_TYPES[k][1], "count": n} for k, n in mix.most_common()],
        "bySector": by("sector"), "byRegion": by("region"),
        "topOpportunities": [{**_brief(c), "reason": next((s["detail"] for s in c["signals"] if s["polarity"] == "opportunity"), "")}
                             for c in top],
        "latestSignals": [{**_brief(c), **{k: s[k] for k in ("key", "label", "polarity", "title", "detail", "date", "source", "url")}}
                          for c, s in sorted(recent, key=lambda x: x[1]["date"], reverse=True)[:30]],
        "riskAlerts": [{**_brief(c), "reasons": c["riskReasons"][:3]} for c in
                       sorted(risky, key=lambda c: (c["riskBand"] != "CRITICAL", c["name"]))[:15]],
        "contractsEnding": [{**_brief(c), "detail": s["detail"], "date": s["date"], "daysUntil": s.get("daysUntil")}
                            for c, s in ending[:15]],
        "openTenders": [{"id": t.get("Release ID"), "title": t.get("Title"), "buyer": t.get("Buyer"), "town": t.get("Buyer town"),
                         "value": to_number(t.get("Value")), "closes": iso(t.get("Tender end")), "region": t.get("Region"),
                         "sme": t.get("SME suitable"), "url": t.get("url"), "register": t.get("register")} for t in tenders[:15]],
        "registers": [{"code": k, "name": REGISTER_NAMES.get(k, k), "companies": v} for k, v in sorted(reg.items())],
    }
