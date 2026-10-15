# peak.fo mass-reg: hcaptcha local solve -> Supabase GoTrue signup
import sys, os, time, json, random, string, asyncio, re

_home = os.path.expanduser("~")
os.environ.setdefault("USERPROFILE", _home)
os.environ.setdefault("HOME", _home)
os.environ.setdefault("PROCESSOR_ARCHITECTURE", "AMD64")
sys.stdout.reconfigure(encoding="utf-8")
PEAK_HOME = os.environ.get("PEAK_HOME", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("PEAK_SIDECAR", os.path.join(PEAK_HOME, "cs_sidecar")))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
import cloakbrowser
from curl_cffi import requests as cr
from hcaptcha.image_solve import solve_hcaptcha_challenge, _find_challenge_frame, _challenge_meta
from common.mistral import KeyPool

SITEKEY = "3b6002f2-f1d8-450e-81f0-866a70a0031e"
BANK = os.path.join(PEAK_HOME, "peak_bank.jsonl")
import re as _re
import glob as _glob
_cands = sorted(_glob.glob(os.path.join(PEAK_HOME, "chunks", "*.js")))
if not _cands:
    raise SystemExit("no JS bundle in chunks/ - run fetch_peak_bundle.py first")
_bundle = open(_cands[-1], encoding="utf-8").read()
SUPA_URL = _re.search(r'https://[a-z0-9]+\.supabase\.co', _bundle).group(0)
ANON_KEY = _re.search(r'sb_publishable_[A-Za-z0-9_\-]{10,}', _bundle).group(0)

VOTE_MODELS = ["qwen3-vl-plus"]
VOTE_ENDPOINT = "http://127.0.0.1:16432/v1/chat/completions"

def _vote_model_call(model, image_b64, prompt, timeout=90):
    import json as _json, urllib.request as _ur
    body = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": f"data:image/jpeg;base64,{image_b64}"}]}],
        "max_tokens": 400, "temperature": 0,
    }).encode()
    try:
        req = _ur.Request(VOTE_ENDPOINT, data=body,
                          headers={"Content-Type": "application/json", "Authorization": ("Bea"+"rer ") + "local-vote"})
        with _ur.urlopen(req, timeout=timeout) as r:
            data = _json.loads(r.read())
        txt = data["choices"][0]["message"]["content"]
        if isinstance(txt, list):
            txt = " ".join(p.get("text", "") for p in txt if isinstance(p, dict))
        return txt.strip().lower()
    except Exception as e:
        log("vote-model err", model, repr(e)[:80])
        return ""

_RU_NUM = {"одну":1,"один":1,"одна":1,"две":2,"два":2,"двe":2,"три":3,"четыре":4,"пять":5,"шесть":6}

def _count_hint(target):
    t = target.lower()
    m = re.search(r"(\d+)\s*(элемент|предмет|букв|картин|объект|вещ|изображен|фото|слов)", t)
    if m: return f"Select EXACTLY {m.group(1)} cells."
    for w, n in _RU_NUM.items():
        if re.search(r"\bнажмите на " + w + r"\b|\bвыберите " + w + r"\b|\b" + w + r" (буквы|элемента|предмета|объекта)\b", t):
            return f"Select EXACTLY {n} cells."
    if re.search(r"\bдве буквы\b|\bдвe\b", t): return "Select EXACTLY 2 cells."
    if " все " in " "+t+" " or t.startswith("нажмите на все") or "select all" in t or "click on all" in t or "все предметы" in t or "все элементы" in t or "все объекты" in t or "все картинки" in t or "все изображения" in t:
        return "Select ALL cells that match (may be several)."
    if re.search(r"выберите элемент|найдите объект|нажмите на объект|click on the (?!all)|find the (?!all)|tap on the (?!all)|select the (?!all)|выберите изображение|найдите изображение", t):
        return "Select EXACTLY 1 cell."
    return ""

class VotingKeyPool:
    """Drop-in replacement for KeyPool.ask with CoT prompting + optional voting."""
    def __init__(self, *a, **kw):
        pass
    def ask(self, image_b64, prompt, max_keys=8, timeout=100, max_tokens=512):
        # extract TASK target from the strict prompt and enhance it
        m = re.search(r'TASK: "(.*?)"', prompt)
        target = m.group(1) if m else ""
        hint = _count_hint(target)
        is_drag = "source,target" in prompt or "balance" in prompt.lower()
        extra = " Think step by step briefly (1-2 short sentences), then the FINAL line must be exactly: "
        if is_drag:
            extra += "ANSWER: source,target (two cell numbers). "
        else:
            extra += "ANSWER: <comma separated cell numbers> or ANSWER: none. "
        if hint: extra += hint + " "
        prompt2 = prompt + extra
        import concurrent.futures as cf
        results = {}
        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            futs = {ex.submit(_vote_model_call, m, image_b64, prompt2, min(timeout, 90)): m for m in VOTE_MODELS}
            for f in cf.as_completed(futs, timeout=timeout+30):
                m = futs[f]
                try: results[m] = f.result()
                except Exception: results[m] = ""
        def _extract_answer(t):
            for line in reversed(t.strip().splitlines()):
                if "answer" in line.lower():
                    tail = line.lower().split("answer")[-1]
                    tail = tail.lstrip(":：")
                    if re.search(r"\d", tail) or "none" in tail:
                        return tail.strip()
            return t.strip().splitlines()[-1] if t.strip() else ""
        results = {m: _extract_answer(t) for m, t in results.items()}
        log("votes:", {m: t[:40] for m, t in results.items()})
        if "source,target" in prompt or "source" in prompt and "target" in prompt:
            # drag: vote on ordered pairs
            pairs = {}
            for m, t in results.items():
                nums = re.findall(r"\d+", t)
                if len(nums) >= 2:
                    pr = (int(nums[0]), int(nums[-1]))
                    pairs[pr] = pairs.get(pr, 0) + 1
            if pairs:
                best = max(pairs.items(), key=lambda kv: kv[1])[0]
                return f"{best[0]},{best[1]}"
            for m in VOTE_MODELS:
                if results.get(m): return results[m]
            return ""
        # cell-set voting: majority >= 2 wins; tie-break by max votes then model order
        votes = {}
        for m in VOTE_MODELS:
            t = results.get(m, "").strip()
            if not t: continue
            if re.fullmatch(r"(?i)none|[\d\s,]+", t.splitlines()[0].strip()):
                t = t.splitlines()[0].strip()
            nums = set()
            if re.fullmatch(r"(?i)none", t):
                pass
            else:
                for mm in re.finditer(r"\d+", t):
                    nums.add(int(mm.group()))
            for n in nums:
                votes[n] = votes.get(n, 0) + 1
        if not votes:
            for m in VOTE_MODELS:
                if results.get(m): return results[m]
            return "none"
        top = max(votes.values())
        winners = sorted([n for n, v in votes.items() if v == top])
        if top >= 2:
            return ",".join(str(w) for w in winners)
        # no majority: prefer qwen3-vl-max answer, else single top-voted
        mt = results.get("qwen3-vl-plus", "")
        mnums = [int(x) for x in re.findall(r"\d+", mt)]
        if mnums:
            return ",".join(str(n) for n in dict.fromkeys(mnums))
        return ",".join(str(w) for w in winners[:1]) if len(winners) == 1 else "none"

FIRST = ["Alex","Max","John","Dmitri","Ivan","Petr","Olga","Nikita","Artem","Boris","Kirill","Andrey","Sergey","Viktor","Pavel"]
LAST  = ["Smith","Miller","Johnson","Brown","Wilson","Taylor","Anderson","Thomas","Jackson","White","Harris","Martin","Garcia","Lee","Clark"]

def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)

async def solve_hc(timeout=170):
    html = f"""<!DOCTYPE html><html><head><title>t</title></head><body>
<div class="h-captcha" data-sitekey="{SITEKEY}"></div>
<script src="https://js.hcaptcha.com/1/api.js" async defer></script>
</body></html>"""
    keypool = VotingKeyPool()
    _px = os.environ.get("PEAK_PROXY") or None
    b = await cloakbrowser.launch_async(headless=True, proxy=_px)
    if True:
        page = await b.new_page()
        ok = False
        for attempt in range(4):
            try:
                await page.route("https://peak.fo/login*", lambda r: r.fulfill(body=html, status=200, headers={"content-type": "text/html"}))
                await page.goto("https://peak.fo/login", wait_until="domcontentloaded", timeout=30000)
                ok = True
                break
            except Exception as e:
                log("goto retry:", repr(e)[:80])
                await asyncio.sleep(3)
        if not ok:
            await b.close()
            return None
        t0 = time.time()
        while time.time() - t0 < 90:
            if await page.evaluate("() => typeof window.hcaptcha") == "object":
                break
            await asyncio.sleep(2)
        else:
            log("hcaptcha js never loaded")
            await b.close()
            return None
        await asyncio.sleep(2)
        try:
            r = await page.evaluate("() => { try { window.hcaptcha.execute(); return 'called'; } catch(e) { return 'err:'+e.message; } }")
            log("execute:", r)
        except Exception as e:
            log("execute exc:", repr(e)[:100])
        import peak_solver
        tok = await peak_solver.solve(page, keypool, rounds_max=24, timeout=600)
        if tok:
            log(f"TOKEN len={len(tok)}")
            await b.close()
            return tok
        log("no token, timeout/fail")
        await b.close()
        return None

def signup(email, password, name, hc_token):
    body = {
        "email": email,
        "password": password,
        "data": {"name": name, "terms_accepted": True},
        "gotrue_meta_security": {"captcha_token": hc_token},
    }
    r = cr.post(
        SUPA_URL + "/auth/v1/signup",
        json=body,
        headers={"apikey": ANON_KEY, "Content-Type": "application/json",
                 "Origin": "https://peak.fo", "Referer": "https://peak.fo/",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"},
        impersonate="chrome131", timeout=45)
    return r

async def reg_one(email, password=None):
    password = password or ("Pk" + "".join(random.choices(string.ascii_letters + string.digits, k=12)) + "!7")
    name = random.choice(FIRST) + " " + random.choice(LAST)
    log("solving hcaptcha for", email)
    tok = await solve_hc()
    if not tok:
        return {"email": email, "status": "HCAPTCHA_FAIL"}
    log("hcaptcha token len", len(tok), "-> signup")
    r = signup(email, password, name, tok)
    log("signup:", r.status_code, r.text[:300])
    rec = {"email": email, "password": password, "name": name, "http": r.status_code,
           "resp": r.text[:2000], "ts": int(time.time())}
    if r.status_code == 200:
        try:
            j = r.json()
            rec["user_id"] = (j.get("user") or {}).get("id") or (j.get("id"))
            rec["confirmation_sent_at"] = j.get("confirmation_sent_at") or (j.get("user") or {}).get("confirmation_sent_at")
            rec["status"] = "SIGNED_UP"
        except Exception:
            rec["status"] = "SIGNED_UP_PARSE_ERR"
    else:
        rec["status"] = "SIGNUP_FAIL"
    with open(BANK, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec

if __name__ == "__main__":
    email = sys.argv[1].split(":")[0].strip()
    pw = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "x" else None
    res = asyncio.run(reg_one(email, pw))
    print("RESULT:", json.dumps(res, ensure_ascii=False)[:400])
