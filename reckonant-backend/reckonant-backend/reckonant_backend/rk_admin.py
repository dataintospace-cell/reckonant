"""rk_admin.py - Admin screens for Reckonant: Daily tracker and Daily tracker (Details).

The sub categories, counts and records come from the Alert Engine's own tracker code
(alert_tracker_ui.py in the loaders folder, its REGISTERS list), so both screens show the same numbers.

    http://localhost:8001/admin/          Daily tracker (sections start collapsed; Expand all / Collapse all)
    http://localhost:8001/admin/details   Daily tracker (Details): pick a register, a sub category and a day

To add a register later, add its block to REGISTERS in loaders\\alert_tracker_ui.py. These pages update by themselves.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

router = APIRouter(prefix="/admin")

LOADERS_DIR = Path(os.environ.get("RK_LOADERS_DIR", r"D:\Reckonant\loaders"))
CACHE_SECONDS = 60          # the tracker is rebuilt at most this often
DAYS_SHOWN = 7              # days on the tracker, ending with today

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}
_ui = None


def alert_ui():
    """The Alert Engine's tracker module, loaded once from the loaders folder."""
    global _ui
    if _ui is None:
        if str(LOADERS_DIR) not in sys.path:
            sys.path.insert(0, str(LOADERS_DIR))
        try:
            import alert_tracker_ui as mod
        except Exception as e:
            raise HTTPException(503, f"Could not load alert_tracker_ui.py from {LOADERS_DIR}: {e}")
        _ui = mod
    return _ui


def build_tracker():
    now = time.time()
    with _lock:
        if _cache["data"] and now - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
        raw = alert_ui().build_tracker()
        if raw.get("error") and not raw.get("registers"):
            raise HTTPException(503, raw["error"])
        window = raw["days"]                     # today first, then 7 earlier days
        days = list(reversed(window[:DAYS_SHOWN]))   # oldest first, ending with today
        today_key, yesterday_key = window[0], window[1]
        registers = []
        for r in raw["registers"]:
            subs = []
            for s in r["subs"]:
                counts = {d: int(s["counts"].get(d, 0)) for d in days}
                change = int(s["counts"].get(today_key, 0)) - int(s["counts"].get(yesterday_key, 0))
                subs.append({"name": s["name"], "counts": counts, "change": change, "total_7d": sum(counts.values())})
            registers.append({"code": r["code"], "name": r["name"], "category": r.get("category", ""),
                              "frequency": r.get("frequency", ""), "error": r.get("error"),
                              "last": r.get("last"), "subs": subs})
        data = {"generated": raw.get("generated", ""), "days": days, "registers": registers}
        _cache.update(at=now, data=data)
        return data


def as_json(obj):
    return Response(content=json.dumps(obj, default=str), media_type="application/json")


@router.get("/api/tracker")
def api_tracker():
    return as_json(build_tracker())


@router.get("/api/drill")
def api_drill(code: str = Query(...), day: str = Query(...), sub: str = Query("")):
    mod = alert_ui()
    reg = next((r for r in mod.REGISTERS if r["code"] == code), None)
    if not reg:
        raise HTTPException(404, "Unknown register")
    try:
        the_day = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")
    try:
        with mod.connect() as con:
            cols, rows = reg["drill"](con, the_day, sub)
            rows = [list(r) for r in rows]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, str(e).splitlines()[0])
    return as_json({"code": code, "name": reg["name"], "day": day, "sub": sub,
                    "columns": cols, "rows": rows, "limit": getattr(mod, "DRILL_LIMIT", 500)})


TRACKER_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reckonant admin - Daily tracker</title>
<style>
:root{--ink:#1d2330;--muted:#6b7280;--faint:#c3c8d1;--line:#e5e7eb;--bg:#ffffff;--band:#f3f5f8;--accent:#1e6fb8;--up:#067647;--down:#b42318}
@media (prefers-color-scheme: dark){:root{--ink:#e7eaf0;--muted:#9aa3b2;--faint:#4b5566;--line:#2a3140;--bg:#12161d;--band:#1b2130;--accent:#63a8ea;--up:#4ade80;--down:#f97066}}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--ink);font-size:14px}
header{padding:14px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}
.meta{color:var(--muted);font-size:12px;margin-top:2px}
nav{display:flex;gap:8px;flex-wrap:wrap}
a.btn,button{color:var(--accent);background:none;border:1px solid var(--accent);border-radius:6px;padding:5px 11px;text-decoration:none;font:inherit;cursor:pointer}
main{padding:12px 16px 24px}
details.reg{border:1px solid var(--line);border-radius:8px;margin-bottom:10px;overflow:hidden}
details.reg>summary{list-style:none;cursor:pointer;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline;padding:10px 12px;background:var(--band);font-weight:600}
details.reg>summary::-webkit-details-marker{display:none}
details.reg>summary::before{content:"\25B8";color:var(--muted);margin-right:2px}
details.reg[open]>summary::before{content:"\25BE"}
.code{font-family:Consolas,monospace;color:var(--accent);font-weight:700}
.sub{color:var(--muted);font-weight:400;font-size:12px}
.regtot{margin-left:auto;font-weight:400;font-size:12px;color:var(--muted)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:700px;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;border-top:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-size:12px;color:var(--muted);font-weight:600;border-top:none;background:var(--bg)}
th:first-child,td:first-child{text-align:left;position:sticky;left:0;background:var(--bg);min-width:260px}
td.n a{color:var(--accent);text-decoration:none;padding:2px 4px;border-radius:4px}
td.n a:hover{background:var(--band)}
td.zero{color:var(--faint)}
td.up{color:var(--up);font-weight:600} td.down{color:var(--down);font-weight:600} td.flat{color:var(--muted)}
td.tot{font-weight:600}
.empty{padding:12px 14px;color:var(--muted)}
.bad{color:var(--down)}
.legend{color:var(--muted);font-size:12px;margin:8px 0 0}
</style></head>
<body>
<header>
  <div><h1>Daily tracker</h1><div class="meta" id="gen">Loading...</div></div>
  <nav><a class="btn" href="/admin/details">Details</a><button id="expand" type="button">Expand all</button><button id="collapse" type="button">Collapse all</button><button id="reload" type="button">Refresh</button></nav>
</header>
<main id="main"><div class="empty">Loading...</div></main>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = n => Number(n ?? 0).toLocaleString("en-GB");
function dayLabel(iso){ const [y,m,d] = iso.split("-").map(Number); return new Date(y, m-1, d).toLocaleDateString("en-GB",{weekday:"short",day:"numeric",month:"short"}); }
function numCell(n, href){ return n === 0 ? `<td class="n zero">·</td>` : `<td class="n"><a href="${href}">${fmt(n)}</a></td>`; }
function changeCell(v){
  if (v > 0) return `<td class="up">▲ ${fmt(v)}</td>`;
  if (v < 0) return `<td class="down">▼ ${fmt(-v)}</td>`;
  return `<td class="flat">0</td>`;
}
function renderRegister(reg, days){
  const total = reg.subs.reduce((s, x) => s + x.total_7d, 0);
  const last = reg.last ? `last entry ${dayLabel(reg.last.date)} (${fmt(reg.last.n)})` : "no entries in the last year";
  let h = `<details class="reg">
    <summary><span class="code">${esc(reg.code)}</span> ${esc(reg.name)}
      <span class="sub">${esc(reg.category)} · ${esc(reg.frequency)}</span>
      <span class="regtot">7 days: <b>${fmt(total)}</b> · ${esc(last)}</span></summary>`;
  if (reg.error) {
    h += `<div class="empty bad">${esc(reg.error)}</div>`;
  } else if (!reg.subs.length) {
    h += `<div class="empty">No sub categories.</div>`;
  } else {
    h += `<div class="scroll"><table><thead><tr><th>Sub category</th>` +
         days.map(d => `<th>${esc(dayLabel(d))}</th>`).join("") +
         `<th>Change</th><th>7 days</th></tr></thead><tbody>`;
    for (const s of reg.subs) {
      const href = d => `/admin/details?code=${encodeURIComponent(reg.code)}&sub=${encodeURIComponent(s.name)}&day=${d}`;
      h += `<tr><td>${esc(s.name)}</td>` + days.map(d => numCell(s.counts[d] || 0, href(d))).join("") +
           changeCell(s.change) + `<td class="tot">${fmt(s.total_7d)}</td></tr>`;
    }
    h += `</tbody></table></div>`;
  }
  return h + `</details>`;
}
function setAll(open){ document.querySelectorAll("details.reg").forEach(d => d.open = open); }

async function load(){
  const main = document.getElementById("main"), gen = document.getElementById("gen");
  main.innerHTML = `<div class="empty">Loading...</div>`;
  const r = await fetch("/admin/api/tracker", {cache: "no-store"});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { gen.textContent = ""; main.innerHTML = `<div class="empty bad">${esc(d.detail || "Could not load the tracker")}</div>`; return; }
  const subs = d.registers.reduce((s, g) => s + g.subs.length, 0);
  gen.textContent = `${d.registers.length} registers · ${subs} sub categories · updated ${d.generated} · last column is today`;
  if (!d.registers.length) { main.innerHTML = `<div class="empty">No registers yet.</div>`; return; }
  main.innerHTML = d.registers.map(reg => renderRegister(reg, d.days)).join("") +
    `<p class="legend">Each number is records added that day. Click it to see the records. Change = today minus yesterday. · means zero.</p>`;
  setAll(false);
}
document.getElementById("expand").onclick = () => setAll(true);
document.getElementById("collapse").onclick = () => setAll(false);
document.getElementById("reload").onclick = load;
load();
</script>
</body></html>
"""

DETAILS_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reckonant admin - Daily tracker (details)</title>
<style>
:root{--ink:#1d2330;--muted:#6b7280;--line:#e5e7eb;--bg:#ffffff;--band:#f3f5f8;--accent:#1e6fb8;--bad:#b42318}
@media (prefers-color-scheme: dark){:root{--ink:#e7eaf0;--muted:#9aa3b2;--line:#2a3140;--bg:#12161d;--band:#1b2130;--accent:#63a8ea;--bad:#f97066}}
body{margin:0;font-family:system-ui,"Segoe UI",Arial,sans-serif;background:var(--bg);color:var(--ink);font-size:14px}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
h1{font-size:18px;margin:0}
.meta{color:var(--muted);font-size:12px;margin-top:2px}
a.btn,button,select{font:inherit;border-radius:6px;padding:5px 10px;border:1px solid var(--accent);background:none;color:var(--accent);text-decoration:none;cursor:pointer}
select{color:var(--ink);border-color:var(--line);background:var(--bg);max-width:100%}
button.on{background:var(--accent);color:var(--bg)}
.bar{padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
main{padding:0 16px 16px;overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:600px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;vertical-align:top;max-width:340px;overflow:hidden;text-overflow:ellipsis}
th{background:var(--band);position:sticky;top:0}
.note{color:var(--muted);font-size:13px;margin:8px 0}
.bad{color:var(--bad)}
</style></head>
<body>
<header><div><h1>Daily tracker (details)</h1><div class="meta" id="sub"></div></div>
<a class="btn" href="/admin/">Back to the tracker</a></header>
<div class="bar">
  <select id="reg" aria-label="Register"></select>
  <select id="sub_sel" aria-label="Sub category"></select>
  <span id="days"></span>
</div>
<main><div class="note" id="info"></div><table><thead id="head"></thead><tbody id="body"></tbody></table></main>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const q = new URLSearchParams(location.search);
let tracker = null;
const state = { code: q.get("code") || "", sub: q.get("sub") || "", day: q.get("day") || "" };

function dayLabel(iso){ const [y,m,d] = iso.split("-").map(Number); return new Date(y, m-1, d).toLocaleDateString("en-GB",{weekday:"short",day:"numeric",month:"short"}); }
function currentReg(){ return tracker.registers.find(g => g.code === state.code); }

async function init(){
  const r = await fetch("/admin/api/tracker", {cache: "no-store"});
  tracker = await r.json().catch(() => ({}));
  if (!r.ok) { document.getElementById("info").innerHTML = `<span class="bad">${esc(tracker.detail || "Could not load")}</span>`; return; }
  if (!state.day) state.day = tracker.days[tracker.days.length - 1];
  if (!tracker.registers.some(g => g.code === state.code)) state.code = tracker.registers[0] ? tracker.registers[0].code : "";
  const regSel = document.getElementById("reg");
  regSel.innerHTML = tracker.registers.map(g => `<option value="${esc(g.code)}">${esc(g.code)} - ${esc(g.name)}</option>`).join("");
  regSel.value = state.code;
  regSel.onchange = () => { state.code = regSel.value; state.sub = ""; fillSubs(); loadRecords(); };
  document.getElementById("sub_sel").onchange = e => { state.sub = e.target.value; loadRecords(); };
  fillSubs();
  renderDays();
  loadRecords();
}

function fillSubs(){
  const reg = currentReg(), sel = document.getElementById("sub_sel");
  const names = reg ? reg.subs.map(s => s.name) : [];
  sel.innerHTML = `<option value="">All sub categories</option>` + names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = names.includes(state.sub) ? state.sub : "";
  state.sub = sel.value;
}

function renderDays(){
  const box = document.getElementById("days");
  box.innerHTML = tracker.days.map(d => `<button type="button" class="${d === state.day ? "on" : ""}" data-day="${d}">${esc(dayLabel(d))}</button>`).join(" ");
  box.querySelectorAll("button").forEach(b => b.onclick = () => { state.day = b.dataset.day; renderDays(); loadRecords(); });
}

async function loadRecords(){
  const head = document.getElementById("head"), body = document.getElementById("body"), info = document.getElementById("info");
  history.replaceState(null, "", `?code=${encodeURIComponent(state.code)}&sub=${encodeURIComponent(state.sub)}&day=${state.day}`);
  document.getElementById("sub").textContent = (currentReg() ? currentReg().name : "") + " · " + (state.sub || "all sub categories") + " · " + dayLabel(state.day);
  if (!state.code) { head.innerHTML = ""; body.innerHTML = ""; info.textContent = "No register to show."; return; }
  info.textContent = "Loading...";
  const url = `/admin/api/drill?code=${encodeURIComponent(state.code)}&sub=${encodeURIComponent(state.sub)}&day=${state.day}`;
  const r = await fetch(url, {cache: "no-store"});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { info.innerHTML = `<span class="bad">${esc(d.detail || "Could not load the records")}</span>`; head.innerHTML = ""; body.innerHTML = ""; return; }
  info.textContent = d.rows.length + " record(s)" + (d.rows.length >= d.limit ? " (first " + d.limit + " shown)" : "");
  head.innerHTML = "<tr>" + d.columns.map(c => `<th>${esc(c)}</th>`).join("") + "</tr>";
  body.innerHTML = d.rows.map(row => "<tr>" + row.map(v => `<td title="${esc(v)}">${esc(v)}</td>`).join("") + "</tr>").join("");
}

init();
</script>
</body></html>
"""


@router.get("/", response_class=HTMLResponse)
def page_tracker():
    return HTMLResponse(TRACKER_HTML)


@router.get("/details", response_class=HTMLResponse)
def page_details():
    return HTMLResponse(DETAILS_HTML)
