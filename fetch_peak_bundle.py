#!/usr/bin/env python3
"""Download peak.fo JS bundle into chunks/ and print extracted public config.
SUPA_URL / ANON_KEY / SITEKEY are public values embedded in the site JS.
"""
import os, re, sys
from curl_cffi import requests as cr

BASE = os.environ.get("PEAK_HOME", os.path.dirname(os.path.abspath(__file__)))
CH = os.path.join(BASE, "chunks")
os.makedirs(CH, exist_ok=True)

r = cr.get("https://peak.fo/login", impersonate="chrome131", timeout=30)
r.raise_for_status()
srcs = set(re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', r.text))
print("chunks on page:", len(srcs))
found = {}
for s in srcs:
    js = cr.get("https://peak.fo" + s, impersonate="chrome131", timeout=30).text
    name = os.path.basename(s)
    open(os.path.join(CH, name), "w", encoding="utf-8").write(js)
    for pat, key in ((r"https://[a-z0-9]+\.supabase\.co", "SUPA_URL"),
                     (r"sb_publishable_[A-Za-z0-9_\-]{10,}", "ANON_KEY"),
                     (r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "SITEKEY?")):
        m = re.search(pat, js)
        if m and key not in found:
            found[key] = m.group(0)
for k, v in found.items():
    print(k, "=", v[:12] + "..." if k != "SUPA_URL" else v)
print("saved to", CH)
