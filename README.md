# peak-fo-autoreg

Mass-registration toolkit for **peak.fo** (Supabase GoTrue backend + hCaptcha).
Built for headless operation on a Linux VPS with a CloakBrowser (Camoufox) and an
OpenAI-compatible **vision** endpoint for captcha solving.

> Research/automation tooling. Bring your own catch-all email domain and IMAP mailbox.

## How it works

```
fetch_peak_bundle.py        download site JS -> chunks/ (SUPA_URL, ANON_KEY, SITEKEY are
                            extracted at runtime; nothing is hardcoded)
        |
peak_reg2.py                per-account flow:
        |                     1. gen_alias()  -> pk<ts><rand>@$PEAK_ALIAS_DOMAIN (catch-all)
        |                     2. solve hCaptcha via peak_solver.py (local browser + vision voting)
        |                     3. POST {SUPA}/auth/v1/signup  (apikey: ANON_KEY, captcha token)
        |                     4. IMAP: find "Confirm your email address" from noreply@peak.fo
        |                     5. GET the GoTrue /auth/v1/verify link (303 + access_token = CONFIRMED)
        |                     6. append row to peak_bank.jsonl
        |
runner_vps.py N             sequential wave of N registrations (20-45s pacing)
confirm_sweep.py            batch-click all unexpired confirm links found in the mailbox
                            (catches accounts whose email lagged past the in-run window)
```

### hCaptcha solver (`peak_solver.py` + `cs_sidecar/`)

- Drives a real browser frame (`_find_challenge_frame`), reads the task text,
  captures the **largest visible canvas** (`_get_canvas_b64`), overlays a numbered grid
  for click tasks, and votes via `qwen3-vl-plus`-class vision models.
- **Task-aware drag solving** (`_solve_drag` in `cs_sidecar/hcaptcha/image_solve.py`):
  - *puzzle piece* tasks -> vision returns gap + 2 candidate pieces (% coords), alternates candidate on retry
  - *balance/seesaw* tasks -> two simple vision calls (animal center; which pan is higher), fixed pan drop zones, alternates side on retry
  - generic -> grab/drop points; grid-cell fallback if parsing fails
- No challenge reloads on failure (reload escalates hCaptcha suspicion -> unreadable obfuscated grids).
- Human-like eased mouse trajectories with jitter and micro-pauses.

## Setup

```bash
python3 -m venv venv && venv/bin/pip install cloakbrowser curl_cffi pillow
venv/bin/python -m camoufox fetch
```

Env vars:

| var | meaning |
|---|---|
| `PEAK_HOME` | working dir (default: repo dir). Holds `chunks/`, `peak_bank.jsonl`, `logs/` |
| `PEAK_SIDECAR` | path to `cs_sidecar` (default: `$PEAK_HOME/cs_sidecar`) |
| `PEAK_ALIAS_DOMAIN` | your catch-all domain (ForwardEmail -> IMAP mailbox) |
| `GMAIL_USER` | IMAP mailbox that receives the confirmation mail |
| `GMAIL_APP_PW` | app password (or put it in `$PEAK_HOME/imap_pw.txt`) |
| `VISION_KEYS_FILE` | text file with API keys for the vision endpoint (one per line) |

The vision endpoint is expected at `http://127.0.0.1:16432/v1/chat/completions`
(any OpenAI-compatible router; on a remote box use an SSH reverse tunnel:
`ssh -N -R 16432:127.0.0.1:16432 user@vps`).

## Run

```bash
python fetch_peak_bundle.py          # once, refreshes chunks/
python runner_vps.py 20              # wave of 20 accounts
python confirm_sweep.py 6            # click confirm links < 6h old, update bank
tail wave_vps.log                    # per-account results
```

`peak_bank.jsonl` row: `{email, password, status, http, confirm_http, ts}` where
status is `LIVE` / `CONFIRMED` / `UNCONFIRMED` / `HCAPTCHA_FAIL`.

## Notes / pitfalls learned

- Confirmation mail arrives in ~2s, but a full-mailbox `UNSEEN` IMAP scan is too slow
  for the in-run window -> use server-side `SUBJECT` search + `confirm_sweep.py`.
- GoTrue verify links expire after a few hours (`otp_expired`), sweep promptly.
- hCaptcha grid reloads after a failed round produce striped/obfuscated canvases -
  re-vote on the same canvas instead.
- Two `peak_reg2` processes must not share one IMAP mailbox concurrently with
  aggressive polling (Gmail rate limits); sequential runner + sweep scales better.
- Datacenter IPs: signup works, but keep an eye on captcha difficulty spikes.
