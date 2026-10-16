# peak.fo reg v2: gmail dot-alias + hcaptcha + signup + IMAP confirm + login verify
# usage: python peak_reg2.py [email]  (default: generate dot-alias)
import sys, os, json, time, random, string, asyncio, re, imaplib

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.environ.get("PEAK_HOME", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from peak_reg import solve_hc, SUPA_URL, ANON_KEY, BANK, log, cr  # noqa: E402

PEAK_PROXY = os.environ.get("PEAK_PROXY") or None
PXD = {"http": PEAK_PROXY, "https": PEAK_PROXY} if PEAK_PROXY else None
if PEAK_PROXY:
    log("using proxy", PEAK_PROXY.split("@")[-1])

GMAIL_USER = os.environ["IMAP_USER"]
GMAIL_PW = os.environ["IMAP_PASS"]
USED = os.path.join(BASE, "used_aliases.txt")
PK = "Pk" + "".join(random.choices(string.ascii_letters, k=2))

def gen_alias():
    # catch-all domain -> forwarder -> your IMAP mailbox (unique local part, no normalization)
    email = "pk%d%s@%s" % (int(time.time()), "".join(random.choices(string.ascii_lowercase, k=3)),
                        os.environ.get("PEAK_ALIAS_DOMAIN", "example.com"))
    return email

def mark_used(email):
    with open(USED, "a", encoding="utf-8") as f:
        f.write(email + "\n")

def signup(email, password, name, tok):
    body = {"email": email, "password": password,
            "data": {"name": name, "terms_accepted": True},
            "gotrue_meta_security": {"captcha_token": tok}}
    return cr.post(SUPA_URL + "/auth/v1/signup", json=body,
        headers={"apikey": ANON_KEY, "Content-Type": "application/json",
                 "Origin": "https://peak.fo", "Referer": "https://peak.fo/",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
        impersonate="chrome131", timeout=45, proxies=PXD)

def login_check(email, password):
    r = cr.post(SUPA_URL + "/auth/v1/token?grant_type=password",
        json={"email": email, "password": password},
        headers={"apikey": ANON_KEY, "Content-Type": "application/json",
                 "Origin": "https://peak.fo", "User-Agent": "Mozilla/5.0"},
        impersonate="chrome131", timeout=30, proxies=PXD)
    return r

CONFIRM_RE = re.compile(r'https://[^\s"\'<>]+(?:verify|confirm)[^\s"\'<>]*token[^\s"\'<>]*', re.I)

def fetch_confirm_link(since_ts, timeout=150):
    import quopri
    t0 = time.time()
    m = None
    while time.time() - t0 < timeout:
        try:
            if m is None:
                m = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"), int(os.environ.get("IMAP_PORT", "993")))
                m.login(GMAIL_USER, GMAIL_PW)
                m.select("INBOX")
            # fast server-side search; fallback to last-30 UNSEEN scan
            typ, data = m.search(None, '(UNSEEN SUBJECT "Confirm your email address")')
            ids = data[0].split()[-10:]
            if not ids:
                typ, data = m.search(None, "(UNSEEN)")
                ids = data[0].split()[-30:]
            for i in ids:
                typ, hd = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
                if typ != "OK":
                    continue
                hdr = hd[0][1].decode("utf-8", errors="replace").lower()
                if "peak" not in hdr and "confirm" not in hdr:
                    continue
                typ, md = m.fetch(i, "(BODY.PEEK[])")
                if typ != "OK":
                    continue
                raw = quopri.decodestring(md[0][1]).decode("utf-8", errors="replace")
                links = CONFIRM_RE.findall(raw)
                if links:
                    m.store(i, "+FLAGS", "\\Seen")
                    m.logout()
                    return links[0]
        except Exception as e:
            log("imap err:", repr(e)[:80])
            try:
                if m: m.logout()
            except Exception: pass
            m = None
        time.sleep(8)
    try:
        if m: m.logout()
    except Exception: pass
    return None

async def reg_one(email=None):
    email = email or gen_alias()
    password = PK + "".join(random.choices(string.ascii_letters + string.digits, k=10)) + "!7x"
    name = random.choice(["Alex", "Max", "John", "Dmitri", "Ivan", "Olga", "Nikita", "Artem"]) + " " + \
           random.choice(["Smith", "Miller", "Brown", "Wilson", "Taylor", "Anderson", "Thomas", "White"])
    log("alias:", email)
    t0 = time.time()
    if PEAK_PROXY:
        # preflight: proxy must reach peak.fo before burning a captcha attempt
        try:
            pr = cr.get("https://peak.fo/", impersonate="chrome131", timeout=30, proxies=PXD)
            log("proxy preflight:", pr.status_code)
            if pr.status_code >= 500:
                return {"email": email, "status": "PROXY_FAIL"}
        except Exception as e:
            log("proxy preflight err:", repr(e)[:80])
            return {"email": email, "status": "PROXY_FAIL"}
    tok = await solve_hc()
    if not tok:
        return {"email": email, "status": "HCAPTCHA_FAIL"}
    log(f"token in {time.time()-t0:.0f}s len={len(tok)} -> signup")
    r = signup(email, password, name, tok)
    log("signup:", r.status_code, r.text[:200])
    rec = {"email": email, "password": password, "name": name, "http": r.status_code,
           "resp": r.text[:1500], "ts": int(time.time())}
    if r.status_code != 200:
        rec["status"] = "SIGNUP_FAIL"
        mark_used(email)
        with open(BANK, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    mark_used(email)
    try:
        j = r.json()
        rec["user_id"] = (j.get("user") or {}).get("id") or j.get("id")
    except Exception:
        pass
    # confirm via IMAP link
    link = fetch_confirm_link(time.time())
    if link:
        log("confirm link found, clicking")
        try:
            cr2 = cr.get(link, impersonate="chrome131", timeout=30, allow_redirects=False, proxies=PXD)
            log("confirm GET:", cr2.status_code)
            rec["confirm_http"] = cr2.status_code
        except Exception as e:
            log("confirm err:", repr(e)[:80])
            rec["confirm_http"] = -1
    else:
        log("no confirm email in 180s")
        rec["confirm_http"] = None
    # login verify (token endpoint may be captcha-gated -> 400 is OK if confirm clicked)
    lr = login_check(email, password)
    rec["login_http"] = lr.status_code
    if lr.status_code == 200:
        rec["status"] = "LIVE"
        try:
            rec["access_token_head"] = lr.json().get("access_token", "")[:12]
        except Exception:
            pass
    elif rec.get("confirm_http") in (200, 301, 302, 303, 307, 308):
        rec["status"] = "CONFIRMED"
    else:
        rec["status"] = "UNCONFIRMED"
        rec["login_resp"] = lr.text[:200]
    log("FINAL:", rec["status"])
    with open(BANK, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec

if __name__ == "__main__":
    em = sys.argv[1] if len(sys.argv) > 1 else None
    res = asyncio.run(reg_one(em))
    print("RESULT:", json.dumps(res, ensure_ascii=False))
