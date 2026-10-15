#!/usr/bin/env python3
"""runner_vps.py — sequential peak.fo registration wave on VPS (direct IP, no proxy).
Usage: python runner_vps.py N
Logs: wave_vps.log lines + logs/v_<i>_<ts>.log per account.
"""
import json, os, random, re, subprocess, sys, time
BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
LOGD = os.path.join(BASE, "logs")
os.makedirs(LOGD, exist_ok=True)

def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    ok = 0
    for i in range(n):
        ts = int(time.time())
        lf = os.path.join(LOGD, f"v_{i}_{ts}.log")
        t0 = time.time()
        with open(lf, "w", encoding="utf-8") as f:
            rc = subprocess.call([PY, os.path.join(BASE, "peak_reg2.py")],
                                 stdout=f, stderr=subprocess.STDOUT,
                                 cwd=BASE, timeout=900,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        st, email = "PARSE_FAIL", "?"
        try:
            txt = open(lf, encoding="utf-8", errors="replace").read()
            m = re.findall(r"RESULT: (\{.*\})", txt)
            if m:
                r = json.loads(m[-1])
                st = r.get("status", "?"); email = r.get("email", "?")
        except Exception:
            pass
        if st in ("LIVE", "CONFIRMED"):
            ok += 1
        line = f"[{i+1}/{n}] {email} -> {st} ({int(time.time()-t0)}s) rc={rc} ok={ok}"
        print(line, flush=True)
        with open(os.path.join(BASE, "wave_vps.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        time.sleep(random.uniform(20, 45))
    print(f"=== wave done: {ok}/{n} ok", flush=True)

if __name__ == "__main__":
    main()
