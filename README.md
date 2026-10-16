# peak-fo-autoreg

Mass-registration toolkit for **peak.fo** (Supabase GoTrue backend + hCaptcha),
with a local web dashboard and an IMAP confirmation sweeper.

Headless-first: runs on a Linux VPS or Windows with **CloakBrowser** (Camoufox)
and any OpenAI-compatible **vision** endpoint for captcha solving.

> Automation/research tooling. You need your own catch-all email domain and IMAP
> mailbox. Respect the target site's terms and the laws of your jurisdiction.

---

## What you need (checklist)

| # | Requirement | Why | Where to get |
|---|-------------|-----|--------------|
| 1 | Python 3.11+ | runtime | python.org |
| 2 | A **catch-all email domain** + IMAP mailbox | each account gets a unique alias `pk<ts><rand>@yourdomain`; all mail lands in one inbox | any domain + ForwardEmail/self-hosted forwarder, or a mail provider with catch-all |
| 3 | An **OpenAI-compatible vision endpoint** | solves hCaptcha image tasks (default model id: `qwen3-vl-plus`; any decent VLM works) | DashScope, OpenRouter, local vLLM/Ollama with a vision model |
| 4 | ~2 GB RAM per browser lane | CloakBrowser/Camoufox headless | VPS or PC |
| 5 | (optional) clean IP / proxies | signup IP reputation | residential proxies work best; `PEAK_PROXY` per lane |

No API keys, emails, or domains are hardcoded - everything comes from env
(see `.env.example`). Supabase URL / anon key / sitekey are extracted at runtime
from the site's own JS bundle.

---

## Quick start

```bash
git clone https://github.com/ArthurMonteiro08586/peak-fo-autoreg
cd peak-fo-autoreg
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m cloakbrowser fetch                        # download the Camoufox browser binary

cp .env.example .env                                # fill IMAP_*, PEAK_ALIAS_DOMAIN, VISION_*
set -a; . ./.env; set +a                            # export env (Windows: set vars in your shell)

python fetch_peak_bundle.py                         # download site JS -> chunks/

python peak_reg2.py                                 # ONE registration (smoke test)
python runner_vps.py 10                             # wave of 10 sequential registrations
python confirm_sweep.py 6                           # confirm links < 6h old found in mailbox
python dashboard.py                                 # http://127.0.0.1:8575
```

Results append to `peak_bank.jsonl` - one JSON row per attempt:
`{email, password, status, http, confirm_http, ts}`, status in
`CONFIRMED` / `LIVE` / `UNCONFIRMED` / `HCAPTCHA_FAIL` / `SIGNUP_FAIL`.

### Multiple lanes

```bash
PEAK_PROXY=http://user:***@host:port python runner_vps.py 20 >> waveA.log 2>&1 &
python runner_vps.py 20 >> waveB.log 2>&1 &
```

Keep 1 browser lane per ~2 GB RAM. Pacing (20-45 s between accounts) is built in.

---

## How it works

```
fetch_peak_bundle.py   downloads peak.fo JS -> chunks/
                       SUPA_URL + ANON_KEY extracted at runtime (nothing hardcoded)
        |
peak_reg2.py           per-account flow:
        |                1. gen_alias()  -> pk<ts><rand>@$PEAK_ALIAS_DOMAIN (catch-all)
        |                2. solve hCaptcha via peak_solver.py (headless browser + vision voting)
        |                3. POST {SUPA}/auth/v1/signup  (gotrue_meta_security.captcha_token)
        |                4. IMAP: find "Confirm your email address" from noreply@peak.fo
        |                5. GET the GoTrue /auth/v1/verify link (303 + access_token = CONFIRMED)
        |                6. append row to peak_bank.jsonl
        |
runner_vps.py N        sequential wave of N registrations with pacing
confirm_sweep.py       batch-confirms accounts whose email lagged past the in-run window
dashboard.py           live stats: status counts, latest bank rows, wave log tails
```

### hCaptcha solver (`peak_solver.py` + `cs_sidecar/`)

Drives the real challenge iframe in a headless Camoufox browser:

- **grid tasks** - largest-visible-canvas capture + yellow numbered overlay,
  vision voting, same-target repeat detection, escalation to describe+reason
  after repeated misses. No reload on failure (reloads escalate hCaptcha
  suspicion -> striped/obfuscated canvases).
- **"covered by object" tasks** - the EXAMPLE object lives *outside* the canvas,
  so the solver captures the **full challenge frame**, detects grid dimension
  (3x3 vs 4x4 from canvas aspect), and builds a **red-rect montage**: the example
  footprint at true pixel size drawn centered on every cell crop, then asks the
  VLM a purely perceptual question - *does the object protrude past the red
  rectangle?* VLMs cannot measure sizes reliably; the montage makes it visual.
- **drag/puzzle tasks** - gap + candidate piece in % coords, alternates candidate
  on retry, human-like eased trajectories with jitter and micro-pauses.
- **balance/seesaw tasks** - grounded protocol: one vision call returns
  `PX PY TX TY` percent coords ("+ Move" pill, lighter pan), click-path with a
  mid-click screenshot for forensics.
- Clicks use the **actual grid dimension** (tile geometry from the canvas box),
  never a hardcoded 4x4.

Debug artifacts land in `dbg_imgs/` (`r<round>_full_*.jpg`, `r<round>_mont_*.jpg`,
`drag_before/after_*.jpg`, `click_mid_*.jpg`) - inspect them when a wave
underperforms.

---

## Env reference

| Var | Default | Meaning |
|-----|---------|---------|
| `IMAP_HOST` / `IMAP_PORT` | `imap.gmail.com` / `993` | mailbox receiving confirmations |
| `IMAP_USER` / `IMAP_PASS` | - | IMAP login (Gmail: app password) |
| `PEAK_ALIAS_DOMAIN` | `example.com` | your catch-all domain |
| `VISION_BASE` | `http://127.0.0.1:16432` | OpenAI-compatible endpoint root |
| `VISION_KEY` | `local-vote` | bearer key for the endpoint |
| `PEAK_HOME` / `PEAK_BANK` / `PEAK_SIDECAR` | repo dir | paths |
| `PEAK_SITEKEY` / `PEAK_SUPA_URL` / `PEAK_ANON_KEY` | auto | manual overrides |
| `PEAK_PROXY` | - | `http://user:***@host:port` for this lane |
| `PEAK_DASH_PORT` | `8575` | dashboard port |

Remote VPS without a local vision endpoint? Use an SSH reverse tunnel:
`ssh -N -R 16432:127.0.0.1:16432 user@vps`.

---

## Dashboard

`python dashboard.py` -> http://127.0.0.1:8575 (auto-refresh 15 s, stdlib only):
usable/total counters, per-status cards, latest 60 bank rows, tails of the newest
wave logs. Binds to loopback only - expose remotely via SSH tunnel:
`ssh -L 8575:127.0.0.1:8575 your-vps`.

---

## Troubleshooting

- **`no chunks/*.js`** - run `python fetch_peak_bundle.py`; the site is behind
  Cloudflare, so fetch from a clean IP or via browser.
- **`HCAPTCHA_FAIL` everywhere** - check `dbg_imgs/` + `logs/`: empty vision
  answers mean your `VISION_BASE` endpoint/model is down or not vision-capable.
- **confirm email never arrives** - catch-all misconfigured or the forwarder
  rejects the domain; test by mailing `anything@yourdomain`.
- **`SIGNUP_FAIL 403 disposable`** - your alias domain is denylisted; use another.
- **rate limits / captcha difficulty spikes** - the IP is flagged; rotate to
  residential proxies or another VPS and slow the pacing.
- **browser launch errors on Linux** - `python -m cloakbrowser fetch` and make
  sure `HOME` is set (headless Camoufox needs it).

## Pitfalls learned the hard way

- Confirmation mail arrives in ~2 s, but a full-mailbox `UNSEEN` IMAP scan is too
  slow for the in-run window -> server-side `SUBJECT` search + `confirm_sweep.py`.
- GoTrue verify links expire after a few hours (`otp_expired`) - sweep promptly.
- Two `peak_reg2` processes must not hammer one IMAP mailbox concurrently
  (Gmail rate limits) - sequential runner + sweep scales better.
- Datacenter IPs: signup works, but watch for captcha difficulty spikes.
