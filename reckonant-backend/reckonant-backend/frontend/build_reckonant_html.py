#!/usr/bin/env python3
"""
Reckonant: builds the standalone Reckonant.html (and the app/ folder for the server)
from the app's source file, Reckonant-ICP.jsx.

Usage:
    python3 build_reckonant_html.py                       # uses Reckonant-ICP.jsx next to this script
    python3 build_reckonant_html.py path/to/App.jsx       # any version of the JSX
    python3 build_reckonant_html.py App.jsx -o Out.html   # choose the output file name

What it makes (next to the output file):
    Reckonant.html  one self-contained file; double-click to open it in a browser
    app/            the server version (signs users in, loads /api/companies, saves their work),
                    for /var/www/reckonant/app on the server

Needs Node.js 18+ (https://nodejs.org) for the build step; npm downloads React, lucide-react
and Vite into a temporary folder the first time (about 30 seconds), then reuses it.
"""
import argparse, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = Path(tempfile.gettempdir()) / "reckonant-build"   # reused between runs so npm install is quick

PACKAGE_JSON = """{ "name": "reckonant-app", "private": true, "type": "module",
  "dependencies": { "react": "18.3.1", "react-dom": "18.3.1", "lucide-react": "0.383.0" },
  "devDependencies": { "vite": "5.4.10", "@vitejs/plugin-react": "4.3.3" } }
"""

VITE_CONFIG = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({ plugins: [react()], base: "./", build: { outDir: "dist", assetsInlineLimit: 100000000, chunkSizeWarningLimit: 10000, modulePreload: false, rollupOptions: { output: { inlineDynamicImports: true } } } });
"""

STYLE = ('<style>html,body,#root{margin:0;height:100%}body{background:#F8FAFC;font-family:Inter,system-ui,'
         '-apple-system,"Segoe UI",Roboto,Arial,sans-serif}</style>')

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E"
           "%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%236366F1'/%3E"
           "%3Cstop offset='.5' stop-color='%238B5CF6'/%3E%3Cstop offset='1' stop-color='%23F2613F'/%3E"
           "%3C/linearGradient%3E%3C/defs%3E%3Crect width='32' height='32' rx='9' fill='url(%23g)'/%3E"
           "%3Cpath d='M10 24V9h7a5 5 0 0 1 0 10h-7M17 19l6 5' fill='none' stroke='white' stroke-width='3' "
           "stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")

# The server version must NOT inline the app into the start-up file: the app reads window.__RK_DATA when it
# loads, so it has to load after sign-in has fetched the data. This config keeps it as a separate file.
VITE_CONFIG_SERVER = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({ plugins: [react()], base: "./", build: { outDir: "dist", chunkSizeWarningLimit: 10000 } });
"""

INDEX_HTML = f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Reckonant</title>
{STYLE}</head>
<body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body></html>
"""

# Server build: signs the user in, loads /api/companies and their saved workspace, then starts the app.
MAIN_SERVER_JSX = r"""import React from "react";
import { createRoot } from "react-dom/client";

// Server start-up (https://reckonant.com/app/): signs the user in with the session cookie,
// loads the company data and their saved workspace, then starts the app. Changes are saved
// back to the server automatically. Opened without a server (local preview) it just starts.
const DATA_KEYS = ["compareIds", "watch", "recentlyViewed", "notes", "followUps", "activity", "savedSearches", "activeIcp", "today5", "feedDismissed", "agentDesk", "agentFocus"];

async function boot() {
  let me = null;
  try {
    const r = await fetch("/api/auth/me", { credentials: "include" });
    if (r.status === 401) { location.replace("/login.html?next=" + encodeURIComponent(location.pathname)); return; }
    if (r.ok) me = await r.json();
  } catch (e) { /* no server (local preview): run without an account */ }

  if (me) {
    window.__RK_USER = me;
    const key = "reckonant:" + me.email;
    window.__RK_KEY = key;

    // 1. Company data from the server (Python data API or the Companies House import)
    let dataset = "dg-export";
    try {
      const c = await fetch("/api/companies", { credentials: "include" });
      if (c.ok) { const d = await c.json(); if (d.companies && d.companies.length) { window.__RK_DATA = d; dataset = d.datasetId || "live"; } }
    } catch (e) {}

    // 2. The user's saved workspace
    let state = null;
    try {
      const s = await fetch("/api/state", { credentials: "include" });
      if (s.ok) { const t = await s.text(); if (t && t !== "{}") state = JSON.parse(t); }
    } catch (e) {}
    if (!state) { try { state = JSON.parse(localStorage.getItem(key) || "null"); } catch (e) {} }
    state = state || {};
    // Saved items point at companies by position. If the company list was swapped, clear those references once.
    if ((state.__dataset || "dg-export") !== dataset) {
      DATA_KEYS.forEach((k) => delete state[k]);
      if (Array.isArray(state.workspaces)) state.workspaces = state.workspaces.map((w) => ({ ...w, items: [] }));
      if (Array.isArray(state.leadLists)) state.leadLists = state.leadLists.map((l) => ({ ...l, companies: [], notes: {} }));
    }
    state.__dataset = dataset;
    try { localStorage.setItem(key, JSON.stringify(state)); } catch (e) {}

    // 3. ICP focus: reopen the saved focus, and show the ICP step once after each sign-in
    if (state.activeIcp && Array.isArray(state.activeIcp.ids)) window.__RK_SCOPE = state.activeIcp;
    try {
      const flag = "rk_gate_session:" + me.email;
      if (!sessionStorage.getItem(flag)) { sessionStorage.setItem(flag, "1"); window.__RK_SHOW_GATE = true; }
    } catch (e) {}

    // 4. Save changes back to the server
    let timer = null, pending = null;
    const flush = () => {
      if (!pending) return; const body = JSON.stringify(pending); pending = null; clearTimeout(timer);
      return fetch("/api/state", { method: "PUT", credentials: "include", keepalive: body.length < 60000, headers: { "Content-Type": "application/json" }, body })
        .then((r) => { if (r.status === 401) location.replace("/login.html"); }).catch(() => {});
    };
    window.__RK_sync = (obj) => { pending = obj; clearTimeout(timer); timer = setTimeout(flush, 1200); };
    window.__RK_flush = flush;
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(); });
    window.__RK_logout = async () => { await flush(); await fetch("/api/auth/logout", { method: "POST", credentials: "include" }).catch(() => {}); try { sessionStorage.clear(); } catch (e) {} location.href = "/login.html"; };
  }

  const { default: App } = await import("./App.jsx");
  createRoot(document.getElementById("root")).render(<App />);
}
boot();
"""

MAIN_JSX = """import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
createRoot(document.getElementById("root")).render(<App />);
"""


def run(cmd, cwd):
    npm = shutil.which(cmd[0]) or shutil.which(cmd[0] + ".cmd")
    if not npm:
        sys.exit(f"Can't find '{cmd[0]}'. Install Node.js 18 or newer from https://nodejs.org and try again.")
    r = subprocess.run([npm] + cmd[1:], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"'{' '.join(cmd)}' failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return r.stdout


def build(jsx: Path, out_html: Path):
    if not jsx.is_file():
        sys.exit(f"Can't find {jsx}")
    print(f"• Building from {jsx.name} ({jsx.stat().st_size // 1024} KB)")

    # 1. A small Vite project around the JSX
    (CACHE / "src").mkdir(parents=True, exist_ok=True)
    (CACHE / "package.json").write_text(PACKAGE_JSON)
    (CACHE / "index.html").write_text(INDEX_HTML)
    shutil.copyfile(jsx, CACHE / "src" / "App.jsx")

    # 2. Install packages once
    if not (CACHE / "node_modules" / "vite").is_dir():
        print("• Installing React, lucide-react and Vite (first run only)…")
        run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], CACHE)

    # 3. Server build first (with sign-in and saving), kept aside for app/
    print("• Compiling the server version…")
    (CACHE / "src" / "main.jsx").write_text(MAIN_SERVER_JSX)
    (CACHE / "vite.config.js").write_text(VITE_CONFIG_SERVER)
    shutil.rmtree(CACHE / "dist", ignore_errors=True)
    run(["npx", "vite", "build"], CACHE)
    server_dist = CACHE / "dist-server"
    shutil.rmtree(server_dist, ignore_errors=True)
    shutil.move(str(CACHE / "dist"), str(server_dist))

    # 4. Stand-alone build (no server needed) for Reckonant.html
    print("• Compiling the stand-alone version…")
    (CACHE / "src" / "main.jsx").write_text(MAIN_JSX)
    (CACHE / "vite.config.js").write_text(VITE_CONFIG)
    run(["npx", "vite", "build"], CACHE)

    dist = CACHE / "dist"
    js_files = sorted((dist / "assets").glob("*.js"))
    if len(js_files) != 1:
        sys.exit(f"Expected one JavaScript file from the build, found {len(js_files)}.")
    js = js_files[0].read_text(encoding="utf-8")

    # 5. One self-contained HTML file (script inlined; "</script" escaped so it can't end the tag early)
    js_safe = re.sub(r"</(script)", r"<\\/\1", js, flags=re.I)
    html = ("<!doctype html>\n<html lang=\"en-GB\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>Reckonant</title>\n"
            f"<link rel=\"icon\" href=\"{FAVICON}\">\n{STYLE}\n"
            f"<script type=\"module\">\n{js_safe}\n</script>\n</head>\n<body><div id=\"root\"></div></body></html>\n")
    out_html.write_text(html, encoding="utf-8")
    print(f"✓ {out_html}  ({len(html) // 1024} KB) — double-click to open")

    # 6. The server version as files, for /var/www/reckonant/app
    app_dir = out_html.parent / "app"
    shutil.rmtree(app_dir, ignore_errors=True)
    shutil.copytree(server_dist, app_dir)
    print(f"✓ {app_dir}/  — upload this folder to the server to update https://reckonant.com/app/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build Reckonant.html from the Reckonant JSX source.")
    ap.add_argument("jsx", nargs="?", default=str(HERE / "Reckonant-ICP.jsx"), help="the app's .jsx file")
    ap.add_argument("-o", "--out", default=None, help="output .html file (default: Reckonant.html next to the .jsx)")
    a = ap.parse_args()
    jsx = Path(a.jsx).expanduser().resolve()
    out = Path(a.out).expanduser().resolve() if a.out else jsx.parent / "Reckonant.html"
    build(jsx, out)
