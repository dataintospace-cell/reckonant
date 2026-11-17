"""
daily_report.py - builds the daily e-mail: a table of every register and sub-category with what was downloaded, the new entries (hits),
updates, removals and the change in count.  daily_update.py calls build_report(); you can also run it by hand to preview:

    python daily_report.py                  writes daily_report_preview.html next to this script (open it in a browser)

Numbers come from the same register definitions as the tracker (alert_tracker_ui.REGISTERS), so a register you add there appears here with no change.
  Downloaded       records read from the source in today's run (files / pages / API releases)
  New (hits)       new entries found today          Updated  entries that changed today          Removed  entries that disappeared today
  Change in count  New - Removed                    Last 7 days  all entries of the register in the last 7 days
For the Gazette and CQC (which have no fixed sub-categories) the sub-categories are the notice types / tables found.
"""
import html as H
import re
from datetime import date, datetime, timedelta

UPD = re.compile(r"updat|chang|amend|modif|correct|renam", re.I)
REM = re.compile(r"remov|deactiv|ceas|withdraw|delist|closed", re.I)

# Downloaded today, per register code: SQL returning one number (None / error -> "-").  A text means "take it from the step's output".
DOWNLOADED = {
    "CQC-01": "SELECT sum(rows_imported) FROM public.cqc_import_log WHERE source_file LIKE '1\\_cqc%%' AND imported_at::date = %(t)s AND table_name NOT LIKE '\\_\\_%%'",
    "CQC-02": "SELECT sum(rows_imported) FROM public.cqc_import_log WHERE source_file LIKE '2\\_care%%' AND imported_at::date = %(t)s AND table_name NOT LIKE '\\_\\_%%'",
    "CQC-03": "SELECT sum(rows_imported) FROM public.cqc_import_log WHERE source_file LIKE '3\\_care%%' AND imported_at::date = %(t)s AND table_name NOT LIKE '\\_\\_%%'",
    "CQC-04": "SELECT sum(rows_imported) FROM public.cqc_import_log WHERE source_file LIKE '4\\_deact%%' AND imported_at::date = %(t)s AND table_name NOT LIKE '\\_\\_%%'",
    "HMD-01": 'SELECT sum(rows_read) FROM "R10_hmrc_tax_defaulters".list_file WHERE downloaded_at::date = %(t)s',
    "HOS-01": 'SELECT sum(rows_read) FROM "R11_sponsor_register".file_snapshot WHERE loaded_at::date = %(t)s',
    "CON-01": 'SELECT sum(releases) FROM "R12_contracts_finder".fetch_log WHERE fetched_at::date = %(t)s',
    "FTS-01": 'SELECT sum(releases) FROM "R13_find_a_tender".fetch_log WHERE fetched_at::date = %(t)s',
    "FIN-01": 'SELECT count(*) FROM "R15_company_financials".filing WHERE loaded_at::date = %(t)s',
    "PTR-01": 'SELECT sum(payments) FROM "R14_payments_transparency".fetch_log WHERE fetched_at::date = %(t)s',
}
FROM_STEP = {"INS-01": ("gazette", r"([\d,]+) notices"), "INS-02": ("gazette", r"([\d,]+) notices"), "GAZ-01": ("gazette", r"([\d,]+) notices"),
             "COM-01": ("company", r"([\d,]+) saved")}


def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def cqc_today(con, schema, today):
    """CQC changes are dated by the file; for the e-mail use the tables worked out in today's run."""
    out = []
    for cur, a, c, r in con.execute("SELECT current_table, rows_added, rows_changed, rows_removed FROM public.cqc_diff_summary "
                                    "WHERE schema_name = %s AND created_at::date = %s", (schema, today)).fetchall():
        m = re.match(r"^\d{1,2}_[a-z]+_\d{4}_(.+)$", cur or "")
        t = m.group(1) if m else cur
        out += [(today, f"{t}: Added", num(a)), (today, f"{t}: Changed", num(c)), (today, f"{t}: Removed", num(r))]
    return out


def collect(con, reg_list, today, results):
    step_info = {k: i for k, i in results}
    groups = {}
    for r in reg_list:
        item = {"code": r["code"], "name": r["name"], "subs": [], "err": None, "down": None, "week": 0}
        try:
            if r["code"].startswith("CQC-") and "schema_name" not in r:
                schema = {"CQC-01": "R1_cqc_care_directory", "CQC-02": "R2_care_directory_with_filters",
                          "CQC-03": "R3_care_directory_with_ratings", "CQC-04": "R4_deactivated_locations"}.get(r["code"])
                t_rows = cqc_today(con, schema, today) if schema else r["fetch"](con, today, today)
            else:
                t_rows = r["fetch"](con, today, today)
            w_rows = r["fetch"](con, today - timedelta(days=6), today)
        except Exception as e:
            item["err"] = str(e).splitlines()[0][:80]
            t_rows, w_rows = [], []
        t, w = {}, {}
        for d, sub, n in t_rows:
            t[sub] = t.get(sub, 0) + num(n)
        for d, sub, n in w_rows:
            w[sub] = w.get(sub, 0) + num(n)
        order = list(r.get("subs") or []) + sorted((set(t) | set(w)) - set(r.get("subs") or []))
        for s in order:
            item["subs"].append({"sub": s, "today": t.get(s, 0), "week": w.get(s, 0),
                                 "kind": "rem" if REM.search(s) else "upd" if UPD.search(s) else "new"})
        item["week"] = sum(x["week"] for x in item["subs"])
        for kind, key in (("new", "new"), ("upd", "upd"), ("rem", "rem")):
            item[key] = sum(x["today"] for x in item["subs"] if x["kind"] == kind)
        item["net"] = item["new"] - item["rem"]
        # downloaded
        q = DOWNLOADED.get(r["code"])
        if q:
            try:
                item["down"] = con.execute(q, {"t": today}).fetchone()[0]
            except Exception:
                item["down"] = None
        elif r["code"] in FROM_STEP:
            k, rx = FROM_STEP[r["code"]]
            m = re.search(rx, step_info.get(k, "") or "")
            item["down"] = int(m.group(1).replace(",", "")) if m else None
        groups.setdefault(r.get("category") or "Other", []).append(item)
    return groups


def fmt(n, zero="–"):
    if n is None:
        return "–"
    return f"{int(n):,}" if n else zero


def build_report(start, end, results, con, reg_list, logfile):
    """results = [(short name, display name, status, info, seconds)] -> (subject, html, text)"""
    today = start.date()
    groups = collect(con, reg_list, today, [(k, i) for k, n, s, i, sec in results])
    bad = [x for x in results if x[2] != "ok"]
    mins = int((end - start).total_seconds() // 60)
    took = f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"
    tot_new = sum(i["new"] for g in groups.values() for i in g)
    tot_upd = sum(i["upd"] for g in groups.values() for i in g)
    tot_rem = sum(i["rem"] for g in groups.values() for i in g)
    subject = ("PROBLEM - " if bad else "") + f"Alert Engine daily update {start:%d %b %Y}: " + (f"{len(bad)} step(s) failed" if bad else f"{tot_new:,} new, {tot_upd:,} updated, {tot_rem:,} removed")

    th = "background:#1F5F8B;color:#fff;padding:7px 8px;font-weight:600;text-align:%s;white-space:nowrap"
    td = "padding:5px 8px;border-bottom:1px solid #e3e7ea;%s"
    dtd = "padding:5px 8px;border-bottom:1px solid #e3e7ea;white-space:nowrap"
    rows, text = [], []
    head = [("Date", "left"), ("Register", "left"), ("Sub-category", "left"), ("Downloaded", "right"), ("New (hits)", "right"), ("Updated", "right"),
            ("Removed", "right"), ("Change in count", "right"), ("Last 7 days", "right")]
    text.append(f"{'Register / sub-category':<46}{'Downl.':>9}{'New':>7}{'Upd.':>7}{'Rem.':>7}{'Change':>8}{'7 days':>8}")
    for cat, items in groups.items():
        rows.append(f'<tr><td colspan="9" style="background:#e8eef3;padding:6px 8px;font-weight:700;color:#1F3A4F;border-bottom:1px solid #cfd8de">{H.escape(cat)}</td></tr>')
        text.append(f"\n{cat.upper()}")
        for it in items:
            net = it["net"]
            ncol = "#1e7a3c" if net > 0 else "#b3261e" if net < 0 else "#7a8691"
            nstr = f"+{net:,}" if net > 0 else (f"{net:,}" if net < 0 else "–")
            if it["err"]:
                rows.append(f'<tr><td style="{dtd}">{today:%d %b}</td><td colspan="2" style="{td % "font-weight:600"}">{H.escape(it["code"])} {H.escape(it["name"])}</td>'
                            f'<td colspan="6" style="{td % "color:#b3261e"}">not available ({H.escape(it["err"])})</td></tr>')
                text.append(f"{it['code'] + ' ' + it['name']:<46} not available")
                continue
            rows.append(f'<tr style="background:#f6f9fb"><td style="{dtd}">{today:%d %b}</td><td colspan="2" style="{td % "font-weight:600"}">{H.escape(it["code"])} &middot; {H.escape(it["name"])}</td>'
                        f'<td style="{td % "text-align:right"}">{fmt(it["down"])}</td><td style="{td % "text-align:right;font-weight:600"}">{fmt(it["new"])}</td>'
                        f'<td style="{td % "text-align:right;font-weight:600"}">{fmt(it["upd"])}</td><td style="{td % "text-align:right;font-weight:600"}">{fmt(it["rem"])}</td>'
                        f'<td style="{td % ("text-align:right;font-weight:700;color:" + ncol)}">{nstr}</td><td style="{td % "text-align:right"}">{fmt(it["week"])}</td></tr>')
            text.append(f"{(it['code'] + ' ' + it['name'])[:45]:<46}{fmt(it['down']):>9}{fmt(it['new']):>7}{fmt(it['upd']):>7}{fmt(it['rem']):>7}{nstr:>8}{fmt(it['week']):>8}")
            for s in it["subs"]:
                cells = {"new": "", "upd": "", "rem": ""}
                cells[s["kind"]] = fmt(s["today"])
                rows.append(f'<tr><td style="{td % ""}"></td><td style="{td % ""}"></td><td style="{td % "padding-left:18px;color:#33424f"}">{H.escape(s["sub"])}</td><td style="{td % ""}"></td>'
                            + "".join(f'<td style="{td % "text-align:right;color:" + ("#33424f" if cells[k] not in ("", "–") else "#9aa5ae")}">{cells[k] or ""}</td>' for k in ("new", "upd", "rem"))
                            + f'<td style="{td % ""}"></td><td style="{td % "text-align:right;color:#7a8691"}">{fmt(s["week"])}</td></tr>')
                text.append(f"    {s['sub'][:41]:<42}{'':>9}{cells['new']:>7}{cells['upd']:>7}{cells['rem']:>7}{'':>8}{fmt(s['week']):>8}")
    table = ('<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:12.5px"><tr>'
             + "".join(f'<th style="{th % a}">{h}</th>' for h, a in head) + "</tr>" + "".join(rows) + "</table>")

    st_rows = []
    for k, name, status, info, secs in results:
        ok = status == "ok"
        st_rows.append(f'<tr><td style="{td % ""}">{H.escape(name)}</td><td style="{td % ("font-weight:600;color:" + ("#1e7a3c" if ok else "#b3261e"))}">{"OK" if ok else "FAILED"}</td>'
                       f'<td style="{td % "text-align:right"}">{int(secs // 60)} min {int(secs % 60):02d} s</td>'
                       f'<td style="{td % "color:#b3261e"}">{"" if ok else H.escape(str(info)[:240])}</td></tr>')
    steps = ('<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:12.5px"><tr>'
             + "".join(f'<th style="{th % a}">{h}</th>' for h, a in (("Step", "left"), ("Result", "left"), ("Time", "right"), ("Problem", "left"))) + "</tr>" + "".join(st_rows) + "</table>")
    note = ("Downloaded = records read from the source in today's run (0 or – = nothing new published). Gazette: one download figure covers all Gazette categories. "
            "Change in count = New &minus; Removed. CQC changes are shown on the day the new file was processed.")
    html = (f'<div style="font-family:Arial,Helvetica,sans-serif;color:#1c2630;max-width:900px">'
            f'<div style="font-size:18px;font-weight:700;color:#1F3A4F">Alert Engine &mdash; daily update</div>'
            f'<div style="color:#52606b;margin:2px 0 12px">{start:%A %d %B %Y} &middot; started {start:%H:%M} &middot; took {took} &middot; '
            f'{len(results) - len(bad)} of {len(results)} steps OK</div>'
            f'<div style="margin-bottom:12px"><span style="display:inline-block;background:#eaf5ee;border:1px solid #bfe0c9;border-radius:6px;padding:6px 12px;margin-right:6px"><b>{tot_new:,}</b> new</span>'
            f'<span style="display:inline-block;background:#fff6e5;border:1px solid #f0d9a8;border-radius:6px;padding:6px 12px;margin-right:6px"><b>{tot_upd:,}</b> updated</span>'
            f'<span style="display:inline-block;background:#fdecea;border:1px solid #f1c0bb;border-radius:6px;padding:6px 12px"><b>{tot_rem:,}</b> removed</span></div>'
            f'{table}<div style="color:#7a8691;font-size:11.5px;margin:8px 0 18px">{note}</div>'
            f'<div style="font-size:14px;font-weight:700;color:#1F3A4F;margin-bottom:6px">Run status</div>{steps}'
            f'<div style="color:#7a8691;font-size:11.5px;margin-top:10px">Full log: {H.escape(str(logfile))}</div></div>')
    plain = [f"Alert Engine - daily update, {start:%A %d %B %Y}, started {start:%H:%M}, took {took}, {len(results) - len(bad)} of {len(results)} steps OK",
             f"New {tot_new:,} | Updated {tot_upd:,} | Removed {tot_rem:,}", ""] + text + ["", "RUN STATUS"]
    plain += [f"{'OK    ' if s == 'ok' else 'FAILED'}  {n} ({int(sec)} s)" + ("" if s == "ok" else f": {i}") for k, n, s, i, sec in results]
    plain.append(f"\nFull log: {logfile}")
    return subject, html, "\n".join(plain)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    import alert_tracker_ui as u
    now = datetime.now()
    with u.connect() as con:
        sub, html, text = build_report(now.replace(hour=10, minute=0), now, [("cqc", "CQC (download, load, changes)", "ok", "", 14), ("company", "Company master", "ok", "Done. 1499 saved", 4907)],
                                       con, u.REGISTERS, "daily.log")
    out = Path(__file__).resolve().parent / "daily_report_preview.html"
    out.write_text(html, encoding="utf-8")
    print(sub)
    print(f"Wrote {out}")
