"""
Reckonant: lookups that turn raw register values into what the app shows.

These mirror the rules in the Node server (companies-house.js) so a company looks the same
whichever way it reached the app: postcode area -> region, SIC code -> sector, Companies House
codes -> readable labels, and names in the register's UPPER CASE -> Title Case.
"""
from __future__ import annotations

import re
from datetime import date, datetime

# ── postcode area -> region ───────────────────────────────────────────────────
_AREAS = {
    "London": "E EC N NW SE SW W WC BR CR EN HA IG RM SM TW UB",
    "South East": "BN CT GU ME MK OX PO RG RH SL SO TN HP KT DA",
    "South West": "BA BH BS DT EX GL PL SN SP TA TQ TR",
    "East of England": "AL CB CM CO IP LU NR PE SG SS WD",
    "East Midlands": "DE LE LN NG NN",
    "West Midlands": "B CV DY HR ST TF WR WS WV SY",
    "North West": "BB BL CA CH CW FY L LA M OL PR SK WA WN",
    "North East": "DH DL NE SR TS",
    "Yorkshire": "BD DN HD HG HU HX LS S WF YO",
    "Scotland": "AB DD DG EH FK G HS IV KA KW KY ML PA PH TD ZE",
    "Wales": "CF LD LL NP SA",
    "Northern Ireland": "BT",
}
AREA_REGION = {a: region for region, areas in _AREAS.items() for a in areas.split()}


def region_for(postcode: str | None, country: str | None = None) -> str:
    m = re.match(r"^([A-Z]{1,2})\d", str(postcode or "").upper().strip())
    if m and m.group(1) in AREA_REGION:
        return AREA_REGION[m.group(1)]
    c = str(country or "").lower()
    for name in ("scotland", "wales", "northern ireland"):
        if name in c:
            return name.title()
    return ""


# ── SIC code -> sector ────────────────────────────────────────────────────────
_SIC_BANDS = [(3, "Agriculture"), (9, "Mining & Quarrying"), (33, "Manufacturing"), (35, "Energy & Utilities"),
              (39, "Water & Waste"), (43, "Construction"), (47, "Wholesale & Retail"), (53, "Transport & Logistics"),
              (56, "Hospitality"), (63, "Technology & Media"), (66, "Financial Services"), (68, "Real Estate"),
              (75, "Professional Services"), (82, "Admin & Support Services"), (84, "Public Administration"),
              (85, "Education"), (88, "Health & Social Care"), (93, "Arts & Recreation"), (96, "Other Services"),
              (99, "Other Services")]


def sector_for(sic: str | None) -> str:
    s = str(sic or "").strip()
    if not s[:2].isdigit() or int(s[:2]) == 0:
        return "Unclassified"
    if s in ("99999", "74990"):
        return "Dormant / Non-trading"
    d = int(s[:2])
    return next((name for top, name in _SIC_BANDS if d <= top), "Other Services")


# ── Companies House codes -> labels ───────────────────────────────────────────
COMPANY_TYPES = {
    "ltd": "Private Limited Company", "plc": "Public Limited Company", "llp": "Limited Liability Partnership",
    "private-limited-guarant-nsc": "Private Ltd By Guarantee",
    "private-limited-guarant-nsc-limited-exemption": "Private Ltd By Guarantee (exempt)",
    "private-unlimited": "Private Unlimited Company", "limited-partnership": "Limited Partnership",
    "community-interest-company": "Community Interest Company",
    "charitable-incorporated-organisation": "Charitable Incorporated Organisation",
    "registered-society-non-jurisdictional": "Registered Society", "uk-establishment": "UK Establishment",
}
OFFICER_ROLES = {
    "director": "Director", "secretary": "Secretary", "corporate-director": "Corporate Director",
    "corporate-secretary": "Corporate Secretary", "llp-member": "LLP Member",
    "llp-designated-member": "LLP Designated Member", "corporate-llp-member": "Corporate LLP Member",
    "corporate-llp-designated-member": "Corporate LLP Designated Member", "nominee-director": "Nominee Director",
}
# Statuses that mean the company is closed or in an insolvency process.
INSOLVENT_STATUSES = {"liquidation", "administration", "receivership", "insolvency-proceedings",
                      "voluntary-arrangement", "converted-closed", "dissolved", "closed"}


def humanize(s: str | None) -> str:
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), str(s or "").replace("-", " ").replace("_", " ")).strip()


def status_key(s: str | None) -> str:
    """'In Administration', 'administration', 'ADMINISTRATION' -> 'administration'."""
    k = str(s or "").strip().lower().replace(" ", "-")
    return {"in-administration": "administration"}.get(k, k)


def company_type_label(t: str | None) -> str:
    return COMPANY_TYPES.get(str(t or "").strip().lower(), humanize(t))


def officer_role_label(r: str | None) -> str:
    return OFFICER_ROLES.get(str(r or "").strip().lower(), humanize(r))


def psc_kind_label(k: str | None) -> str:
    k = str(k or "").lower()
    if "individual" in k:
        return "Individual"
    if "corporate" in k:
        return "Corporate"
    if "legal-person" in k or "legal person" in k:
        return "Legal person"
    return "Other"


def control_text(natures: list[str] | None) -> str:
    out: list[str] = []
    for n in natures or []:
        n = str(n)
        m = re.search(r"ownership-of-shares-(\d+)-to-(\d+)-percent", n)
        if m:
            out.append(f"{m.group(1)}–{m.group(2)}% shares"); continue
        m = re.search(r"voting-rights-(\d+)-to-(\d+)-percent", n)
        if m:
            out.append(f"{m.group(1)}–{m.group(2)}% votes"); continue
        if "right-to-appoint-and-remove" in n:
            out.append("Appoints directors"); continue
        if "significant-influence-or-control" in n:
            out.append("Significant influence")
    return ", ".join(dict.fromkeys(out)) or "Significant control"


# ── names ─────────────────────────────────────────────────────────────────────
_KEEP_UP = {"UK", "PLC", "LLP", "CIC", "NHS", "IT", "HR", "(UK)", "GB", "EU", "II", "III", "IV"}
_SMALL = {"and", "of", "the", "for", "in", "on", "at", "to"}


def title_case(s: str | None) -> str:
    words = re.findall(r"[A-Za-z0-9'&.()\-]+|[^A-Za-z0-9'&.()\-]+", str(s or "").lower())
    out, first = [], True
    for w in words:
        if not re.match(r"[A-Za-z0-9'&.()\-]", w):
            out.append(w); continue
        up = w.upper()
        if up == "LTD":
            out.append("Ltd")
        elif up in _KEEP_UP:
            out.append(up)
        elif not first and w in _SMALL:
            out.append(w)
        else:
            w2 = re.sub(r"^(\(?)([a-z])", lambda m: m.group(1) + m.group(2).upper(), w)
            w2 = re.sub(r"^(\(?)Mc([a-z])", lambda m: m.group(1) + "Mc" + m.group(2).upper(), w2)
            out.append(w2)
        first = False
    return "".join(out)


def person_name(n: str | None) -> str:
    """'SMITH, John Paul' -> 'John Paul Smith'; drops titles such as Mr/Mrs/Dr."""
    s = re.sub(r"^(mr|mrs|ms|miss|dr|sir|dame|prof)\.?\s+", "", str(n or "").strip(), flags=re.I)
    if "," in s:
        last, first = [x.strip() for x in s.split(",", 1)]
        s = f"{first} {last}"
    return title_case(s)


# ── dates ─────────────────────────────────────────────────────────────────────
def to_date(v) -> date | None:
    """Accepts date, datetime or text such as '2026-11-02', '02/11/2026', '02-Nov-2026', '02 November 2026'."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%b-%Y", "%d %B %Y", "%d %b %Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def iso(v) -> str:
    d = to_date(v)
    return d.isoformat() if d else ""


def dmy(v) -> str:
    d = to_date(v)
    return d.strftime("%d/%m/%Y") if d else ""


def years_since(v, today: date | None = None) -> int:
    d = to_date(v)
    if not d:
        return 0
    t = today or date.today()
    return max(0, t.year - d.year - ((t.month, t.day) < (d.month, d.day)))


def days_since(v, today: date | None = None) -> int | None:
    d = to_date(v)
    return None if not d else ((today or date.today()) - d).days


def to_number(v) -> float | None:
    """'£12,500.00' -> 12500.0; numbers pass through; blanks -> None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)) or "x")
    except ValueError:
        return None
