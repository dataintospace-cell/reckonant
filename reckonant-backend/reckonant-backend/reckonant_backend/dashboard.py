"""
Reckonant main dashboard, in Python (Streamlit). Shows the same headline numbers and lists as the
app's dashboard after sign-in, straight from the Signals Feeder Engine data.

Run (pick one data source):
    RK_API_URL=http://127.0.0.1:8000 streamlit run dashboard.py           # through the data API (api.py)
    RK_DATABASE_URL=postgresql://...  streamlit run dashboard.py           # or straight from Postgres

Then open http://localhost:8501
"""
from __future__ import annotations

import os
from datetime import date

import pandas as pd
import requests
import streamlit as st

API = os.environ.get("RK_API_URL", "").rstrip("/")
INDIGO, CORAL, GREEN, AMBER, RED, GREY = "#4338CA", "#F2613F", "#15803D", "#B45309", "#B91C1C", "#64748B"
POLARITY = {"opportunity": ("Opportunities", GREEN), "risk": ("Risk", RED), "info": ("Info", GREY)}
BAND = {"LOW": ("Low", GREEN), "MEDIUM": ("Moderate", AMBER), "HIGH": ("High", RED), "CRITICAL": ("Very high", RED)}

st.set_page_config(page_title="Reckonant · Dashboard", page_icon="◆", layout="wide")
st.markdown(f"""<style>
  .block-container {{padding-top: 1.6rem; max-width: 1400px}}
  h1, h2, h3 {{letter-spacing: -.01em}}
  div[data-testid="stMetric"] {{background: #fff; border: 1px solid #E2E8F0; border-radius: 14px; padding: 14px 16px}}
  div[data-testid="stMetricValue"] {{color: {INDIGO}}}
  .rk-pill {{display:inline-block; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:600}}
  .rk-sub {{color:{GREY}; margin-top:-10px}}
</style>""", unsafe_allow_html=True)


# ── data ──────────────────────────────────────────────────────────────────────
@st.cache_resource(ttl=600, show_spinner="Reading the registers…")
def _dataset():
    from rk_service import Dataset  # only needed without the API
    return Dataset.load()


@st.cache_data(ttl=300, show_spinner=False)
def get_dashboard(days: int, sectors: tuple, regions: tuple) -> dict:
    if API:
        r = requests.get(f"{API}/api/dashboard", params={"days": days, "sector": list(sectors), "region": list(regions)}, timeout=60)
        r.raise_for_status()
        return r.json()
    return _dataset().dashboard(days, list(sectors) or None, list(regions) or None)


@st.cache_data(ttl=300, show_spinner=False)
def get_companies() -> list[dict]:
    if API:
        r = requests.get(f"{API}/api/companies", timeout=120)
        r.raise_for_status()
        return r.json()["companies"]
    return _dataset().companies


def pill(text: str, color: str) -> str:
    return f'<span class="rk-pill" style="background:{color}1A;color:{color}">{text}</span>'


def source_link(s: dict) -> str:
    return f'<a href="{s["url"]}" target="_blank">{s["source"]}</a>' if s.get("url") else s["source"]


if not API and not os.environ.get("RK_DATABASE_URL"):
    st.error("Set RK_API_URL (the data API) or RK_DATABASE_URL (Postgres) before starting the dashboard.")
    st.stop()

try:
    everything = get_dashboard(30, (), ())
except Exception as e:
    st.error(f"Couldn't load the data: {e}")
    st.stop()

# ── sidebar: focus ────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(f"## <span style='color:{INDIGO}'>◆ Reckonant</span>", unsafe_allow_html=True)
    st.caption("UK business intelligence")
    days = st.select_slider("Signals from the last", options=[7, 30, 90, 180, 365], value=30, format_func=lambda d: f"{d} days")
    sectors = st.multiselect("Sectors", [s["name"] for s in everything["bySector"]])
    regions = st.multiselect("Regions", [r["name"] for r in everything["byRegion"]])
    st.divider()
    st.caption(f"Source: Signals Feeder Engine · {len(everything['registers'])} registers")
    st.caption(f"Updated {everything['updated'][:16].replace('T', ' ')} UTC")
    if st.button("Refresh data"):
        st.cache_data.clear(); st.cache_resource.clear(); st.rerun()

d = get_dashboard(days, tuple(sectors), tuple(regions))
t = d["totals"]

# ── header + KPIs ─────────────────────────────────────────────────────────────
st.title("Dashboard")
focus = " · ".join(sectors + regions) or "all companies"
st.markdown(f"<p class='rk-sub'>What changed across {focus} in the last {days} days, from official UK registers.</p>",
            unsafe_allow_html=True)
k = st.columns(6)
kpis = [("Companies", t["companies"], f"{t['active']:,} active"),
        ("New signals", t[f"signals{days}d"], f"{t['signals7d']} this week"),
        ("With opportunities", t["opportunities"], "companies with a reason to engage"),
        ("Buying windows", t["buyingWindows"], f"{t['contractsEnding']} contracts ending"),
        ("Rising risk", t["risks"], "high or very high risk"),
        ("Open tenders", t["openTenders"], "public bids still open")]
for col, (label, value, note) in zip(st.columns(6), kpis):
    col.metric(label, f"{value:,}")
    col.caption(note)

# ── top opportunities + signal mix ────────────────────────────────────────────
left, right = st.columns([3, 2])
with left:
    st.subheader("Top opportunities")
    if d["topOpportunities"]:
        df = pd.DataFrame(d["topOpportunities"])[["name", "opportunityScore", "reason", "sector", "region"]]
        st.dataframe(df.rename(columns={"name": "Company", "sector": "Sector", "region": "Region", "opportunityScore": "Score",
                                        "reason": "Why now"}),
                     hide_index=True, use_container_width=True,
                     column_config={"Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d", width="small"),
                                    "Why now": st.column_config.TextColumn(width="large")})
    else:
        st.info("No opportunity signals in this focus yet.")
with right:
    st.subheader("Signal mix")
    if d["signalMix"]:
        mix = pd.DataFrame(d["signalMix"]).set_index("label")["count"].sort_values()
        st.bar_chart(mix, horizontal=True, color=INDIGO, height=320)
    else:
        st.info("No signals in this period.")

# ── latest signals ────────────────────────────────────────────────────────────
st.subheader("Latest signals")
if d["latestSignals"]:
    rows = "".join(
        f"<tr><td style='white-space:nowrap;color:{GREY}'>{s['date']}</td><td>{pill(*POLARITY[s['polarity']])}</td>"
        f"<td><b>{s['name']}</b><br><span style='color:{GREY};font-size:12px'>{s['sector']} · {s['region'] or '—'}</span></td>"
        f"<td><b>{s['label']}</b><br>{s['detail']}</td>"
        f"<td style='color:{GREY}'>{source_link(s)}</td></tr>"
        for s in d["latestSignals"][:20])
    st.markdown(f"<table style='width:100%;font-size:14px'><thead><tr><th>Date</th><th></th><th>Company</th><th>Signal</th>"
                f"<th>Source</th></tr></thead><tbody>{rows}</tbody></table>", unsafe_allow_html=True)
else:
    st.info("Nothing new in this period.")

# ── risk + contracts ending ───────────────────────────────────────────────────
a, b = st.columns(2)
with a:
    st.subheader("Risk alerts")
    for r in d["riskAlerts"][:8]:
        st.markdown(f"{pill(*BAND[r['riskBand']])} **{r['name']}** · {r['sector']}  \n"
                    f"<span style='color:{GREY};font-size:13px'>{'; '.join(r['reasons'])}</span>", unsafe_allow_html=True)
    if not d["riskAlerts"]:
        st.success("No high-risk companies in this focus.")
with b:
    st.subheader("Contracts ending soon")
    for r in d["contractsEnding"][:8]:
        st.markdown(f"{pill(str(r['daysUntil']) + ' days', CORAL)} **{r['name']}**  \n"
                    f"<span style='color:{GREY};font-size:13px'>{r['detail']}</span>", unsafe_allow_html=True)
    if not d["contractsEnding"]:
        st.info("No public contracts ending in the next 9 months.")

# ── open tenders ──────────────────────────────────────────────────────────────
st.subheader("Open public tenders")
if d["openTenders"]:
    td = pd.DataFrame(d["openTenders"])[["title", "buyer", "value", "closes", "region", "url"]]
    st.dataframe(td.rename(columns={"title": "Tender", "buyer": "Buyer", "value": "Value (£)", "closes": "Closes", "region": "Region",
                                    "url": "Link"}), hide_index=True, use_container_width=True,
                 column_config={"Value (£)": st.column_config.NumberColumn(format="£%,.0f"),
                                "Link": st.column_config.LinkColumn(display_text="Open")})
else:
    st.info("No open tenders found.")

# ── company lookup ────────────────────────────────────────────────────────────
st.divider()
st.subheader("Look up a company")
cos = get_companies()
names = {f"{c['name']} ({c['num']})": c for c in cos}
pick = st.selectbox("Company", [""] + sorted(names), index=0, placeholder="Type a company name…")
if pick:
    c = names[pick]
    st.markdown(f"### {c['name']}  \n<span style='color:{GREY}'>{c['num']} · {c['status']} · {c['type']} · {c['sector']} · "
                f"{c['city'] or '—'}, {c['region'] or '—'}</span>", unsafe_allow_html=True)
    m = st.columns(5)
    m[0].metric("Opportunity score", c["opportunityScore"])
    m[1].metric("Risk", BAND[c["riskBand"]][0])
    m[2].metric("Age", f"{c['age']} yrs")
    m[3].metric("Directors", c["directorCount"])
    m[4].metric("Care locations", (c.get("cqc") or {}).get("locations", 0))
    tabs = st.tabs(["Signals", "Registers", "People"])
    with tabs[0]:
        if c["signals"]:
            st.dataframe(pd.DataFrame(c["signals"])[["date", "label", "detail", "source"]]
                         .rename(columns={"date": "Date", "label": "Signal", "detail": "Detail", "source": "Source"}),
                         hide_index=True, use_container_width=True)
        else:
            st.info("No signals in the last year.")
    with tabs[1]:
        if c.get("cqc"):
            st.markdown(f"**CQC:** {c['cqc']['locations']} locations, {c['cqc']['beds']} beds · "
                        + ", ".join(f"{k}: {v}" for k, v in c["cqc"]["ratings"].items()))
            st.dataframe(pd.DataFrame(c["cqc"]["list"]), hide_index=True, use_container_width=True)
        if c.get("contracts"):
            st.markdown("**Public contracts**")
            st.dataframe(pd.DataFrame(c["contracts"]), hide_index=True, use_container_width=True)
        if c.get("sponsor"):
            sp = c["sponsor"]
            st.markdown(f"**Visa sponsor:** {'on the register' if sp['onRegister'] else 'no longer listed'} · {sp['rating']} · {sp['route']}")
        if c.get("hmrc"):
            st.markdown("**HMRC tax defaulter listing**"); st.dataframe(pd.DataFrame(c["hmrc"]), hide_index=True)
        if c.get("gazette"):
            st.markdown("**Gazette notices**"); st.dataframe(pd.DataFrame(c["gazette"]), hide_index=True)
        st.caption("On registers: " + (", ".join(c.get("registers") or []) or "Companies House"))
    with tabs[2]:
        st.markdown("**Directors**")
        st.dataframe(pd.DataFrame(c["officers"])[["name", "role", "appointedOn", "active"]], hide_index=True, use_container_width=True)
        if c["pscs"]:
            st.markdown("**Owners**")
            st.dataframe(pd.DataFrame(c["pscs"]), hide_index=True, use_container_width=True)
