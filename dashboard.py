#!/usr/bin/env python3
"""Local dashboard for the peak farm: bank stats + wave logs. Stdlib only.

Usage:  python dashboard.py            # http://127.0.0.1:8575
Env:    PEAK_HOME (repo dir), PEAK_BANK (jsonl path), PEAK_DASH_PORT
"""
import os, json, glob, html, time
from http.server import HTTPServer, BaseHTTPRequestHandler

HOME = os.environ.get("PEAK_HOME", os.path.dirname(os.path.abspath(__file__)))
BANK = os.environ.get("PEAK_BANK", os.path.join(HOME, "peak_bank.jsonl"))
PORT = int(os.environ.get("PEAK_DASH_PORT", "8575"))

CSS = """body{background:#0d1117;color:#c9d1d9;font-family:ui-monospace,Consolas,monospace;margin:18px}
h1{color:#58a6ff;font-size:20px}h2{color:#8b949e;font-size:14px;margin:18px 0 6px}
.cards{display:flex;gap:10px;flex-wrap:wrap}.card{background:#161b22;border:1px solid #30363d;
border-radius:8px;padding:10px 18px;text-align:center}.card b{display:block;font-size:26px}
.ok b{color:#3fb950}.fail b{color:#f85149}.wait b{color:#d29922}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{border:1px solid #21262d;padding:3px 8px;text-align:left}
th{color:#8b949e}tr:nth-child(even){background:#11161d}
pre{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;font-size:11px;
overflow-x:auto;white-space:pre-wrap}
.s-CONFIRMED,.s-LIVE{color:#3fb950}.s-HCAPTCHA_FAIL,.s-SIGNUP_FAIL,.s-FAIL{color:#f85149}
.s-PENDING,.s-WAIT{color:#d29922}"""

def load_bank():
    rows = []
    if os.path.exists(BANK):
        for line in open(BANK, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows

def log_tails():
    out = []
    for pat in (os.path.join(HOME, "wave*.log"), os.path.join(HOME, "logs", "*.log")):
        for p in sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)[:3]:
            try:
                lines = open(p, encoding="utf-8", errors="replace").read().splitlines()[-8:]
                out.append((os.path.relpath(p, HOME), lines))
            except Exception:
                pass
        if len(out) >= 6:
            break
    return out

def render():
    rows = load_bank()
    stats = {}
    for r in rows:
        stats[r.get("status", "?")] = stats.get(r.get("status", "?"), 0) + 1
    conf = stats.get("CONFIRMED", 0) + stats.get("LIVE", 0)
    cards = "".join(
        f'<div class="card {"ok" if k in ("CONFIRMED", "LIVE") else "fail" if "FAIL" in k else "wait"}">'
        f"<b>{v}</b>{html.escape(k)}</div>"
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]))
    body = [f"<h1>peak farm dashboard</h1><div class='cards'>"
            f"<div class='card ok'><b>{conf}</b>usable</div>"
            f"<div class='card'><b>{len(rows)}</b>total rows</div>{cards}</div>"]
    body.append("<h2>bank (latest 60)</h2><table><tr><th>ts</th><th>email</th><th>status</th><th>http</th></tr>")
    for r in rows[-60:][::-1]:
        st = html.escape(str(r.get("status", "?")))
        ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0))) if r.get("ts") else ""
        body.append(f"<tr><td>{ts}</td><td>{html.escape(str(r.get('email', '')))}</td>"
                    f"<td class='s-{st}'>{st}</td><td>{html.escape(str(r.get('http', '')))}</td></tr>")
    body.append("</table><h2>logs</h2>")
    for name, lines in log_tails():
        body.append(f"<pre><b>{html.escape(name)}</b>\n{html.escape(chr(10).join(lines))}</pre>")
    return ("<!doctype html><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='15'>"
            f"<style>{CSS}</style>" + "".join(body))

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        page = render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print(f"dashboard on http://127.0.0.1:{PORT} (bank={BANK})")
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
