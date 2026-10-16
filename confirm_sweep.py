#!/usr/bin/env python3
"""confirm_sweep.py — click all unexpired peak.fo confirm links found in Gmail.
Runs on VPS. Updates peak_bank.jsonl rows (email match by alias) -> CONFIRMED.
Usage: python confirm_sweep.py [max_age_hours=6]
"""
import imaplib, json, os, re, sys, time, quopri
BASE = os.path.dirname(os.path.abspath(__file__))
GMAIL_USER = os.environ["IMAP_USER"]
GMAIL_PW = os.environ["IMAP_PASS"]
BANK = os.path.join(BASE, "peak_bank.jsonl")
LINK_RE = re.compile(r'https://[a-zA-Z0-9.\-]+\.supabase\.co/auth/v1/verify\?[^\s"\'<>]+')
ALIAS_RE = re.compile(r'(pk\d+[a-z]{3}@' + re.escape(os.environ.get('PEAK_ALIAS_DOMAIN', 'example.com')) + r')')

def main():
    max_age_h = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    from curl_cffi import requests as creq
    M = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"), int(os.environ.get("IMAP_PORT", "993")))
    M.login(GMAIL_USER, GMAIL_PW)
    M.select("INBOX")
    typ, data = M.search(None, '(SUBJECT "Confirm your email address")')
    ids = data[0].split()[-60:]
    confirmed, expired, nomail = [], [], []
    for i in ids:
        typ, hd = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM DATE)])")
        h = hd[0][1].decode("utf-8", "replace")
        if "peak.fo" not in h:
            continue
        dm = re.search(r"Date: .*?(\d{1,2}) (\w{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2})", h)
        age_h = 99
        if dm:
            try:
                ts = time.mktime(time.strptime(" ".join(dm.groups()[:6]), "%d %b %Y %H %M %S"))
                age_h = (time.time() - ts) / 3600 - 3  # gmail date is UTC
            except Exception:
                pass
        if age_h > max_age_h:
            continue
        typ, md = M.fetch(i, "(BODY.PEEK[])")
        raw = quopri.decodestring(md[0][1]).decode("utf-8", "replace")
        am = ALIAS_RE.search(raw)
        alias = am.group(1) if am else None
        links = LINK_RE.findall(raw)
        if not links:
            continue
        url = links[0].replace("&amp;", "&")
        try:
            r = creq.get(url, impersonate="chrome131", allow_redirects=False, timeout=30)
            loc = r.headers.get("location", "")
            ok = r.status_code in (200, 301, 302, 303, 307, 308) and "access_token=" in loc
            if ok:
                confirmed.append((alias, r.status_code))
                M.store(i, "+FLAGS", "\\Seen")
            else:
                expired.append((alias, r.status_code, loc[:60]))
        except Exception as e:
            nomail.append((alias, repr(e)[:60]))
        time.sleep(1)
    M.logout()
    # update bank
    if confirmed and os.path.exists(BANK):
        rows = [json.loads(l) for l in open(BANK, encoding="utf-8") if l.strip()]
        cmap = {a: s for a, s in confirmed if a}
        n = 0
        for row in rows:
            if row.get("email") in cmap and row.get("status") != "CONFIRMED":
                row["status"] = "CONFIRMED"
                row["confirm_http"] = cmap[row["email"]]
                row["confirmed_sweep"] = True
                n += 1
        tmp = BANK + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, BANK)
        print(f"bank updated: {n} rows -> CONFIRMED")
    print(f"sweep: confirmed={len(confirmed)} expired={len(expired)} err={len(nomail)}")
    for a, s in confirmed:
        print("  OK", a or "?", s)

if __name__ == "__main__":
    main()
