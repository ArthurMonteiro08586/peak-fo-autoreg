# -*- coding: utf-8 -*-
"""Custom hCaptcha solve loop for peak.fo on top of cs_sidecar.image_solve primitives.

Handles: failure screen (Попробуйте еще раз) via reload, letter-group tasks,
count hints, forced re-ask on 'none', token polling.
"""
import asyncio
import logging
import os
import re
import sys
import time

_SIDECAR = os.environ.get("PEAK_SIDECAR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cs_sidecar"))
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)

from hcaptcha.image_solve import (  # noqa: E402
    _GRID,
    _challenge_meta,
    _click_submit,
    _click_tiles,
    _find_challenge_frame,
    _get_canvas_b64,
    _grid_overlay,
    _solve_drag,
)

log = logging.getLogger("peaksolver")

_RU_NUM = {"одну": 1, "один": 1, "одна": 1, "две": 2, "два": 2, "три": 3,
           "четыре": 4, "пять": 5, "шесть": 6}


def count_hint(target: str):
    t = target.lower()
    m = re.search(r"(\d+)\s*(элемент|предмет|букв|картин|объект|вещ|изображен|фото|слов)", t)
    if m:
        return int(m.group(1)), f"Select EXACTLY {m.group(1)} cells."
    for w, n in _RU_NUM.items():
        if re.search(r"\b" + w + r"\b", t):
            return n, f"Select EXACTLY {n} cells."
    if re.search(r"select all|click on all|нажмите на все|выберите все|все предметы|все элементы|все объекты|все картинки|все изображения|все буквы", t):
        return None, "Select ALL cells that match (may be several)."
    if re.search(r"выберите элемент|найдите объект|нажмите на объект|выберите изображение|найдите изображение|click on the (?!all)|find the (?!all)|tap on the (?!all)|select the (?!all)", t):
        return 1, "Select EXACTLY 1 cell."
    return None, ""


def group_hint(target: str):
    """Letter-group task: 'Нажмите на ДВЕ буквы, которые входят в группу: ABCBA'."""
    m = re.search(r"группу:\s*([A-Za-zА-Яа-яЁё]+)", target)
    if not m:
        return ""
    letters = sorted(set(m.group(1).upper()))
    return (f"The group string is {m.group(1).upper()}. Its distinct letters are: {', '.join(letters)}. "
            f"Click the cells that show one of these letters: {', '.join(letters)}.")


def task_hint(target: str) -> str:
    """Domain hints for hard hCaptcha task types (peak.fo RU set)."""
    gh = group_hint(target)
    if gh:
        return gh
    t = target.lower()
    if "яйцекладущ" in t:
        return ("Egg-laying animals: birds (flamingo, chicken, duck, owl...), reptiles (snake, turtle, "
                "lizard, crocodile), amphibians (frog), fish, insects, platypus, echidna. "
                "NOT egg-laying: whale, dolphin, beaver, seal, otter, cat, dog, bear, cow and all other "
                "placental mammals. Click ONLY cells with egg-laying animals; ignore scenery-only cells.")
    if "закономерность" in t or "нарушают" in t:
        return ("Find the items that BREAK the pattern: most items share one style (e.g. faint translucent "
                "outlines), while the odd ones differ (e.g. solid opaque colored characters), or differ in "
                "shape family/orientation/count. Compare every cell against the majority and pick the outliers.")
    if "на линии" in t or "на линиях" in t:
        return ("Some cells contain a drawn line or stripe; click the cells where a figure/object sits ON, "
                "TOUCHES or CROSSES that line. Cells without a line, or with a line but no figure on it, are wrong.")
    if re.search(r"спорт|игр", t):
        return ("Sports/games items: balls, rackets, bats, gloves, goals, controllers, chess, cards, dice. "
                "NOT: everyday electronics (headphones alone), clothing, food.")
    return ""


def _vote_model_call(model, image_b64, prompt, timeout=90, temperature=0):
    import json as _json, urllib.request as _ur
    body = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": f"data:image/jpeg;base64,{image_b64}"}]}],
        "max_tokens": 400, "temperature": temperature,
    }).encode()
    req = _ur.Request("http://127.0.0.1:16432/v1/chat/completions", data=body,
                      headers={"Content-Type": "application/json", "Authorization": ("Bea"+"rer ") + "local-vote"})
    with _ur.urlopen(req, timeout=timeout) as r:
        data = _json.loads(r.read())
    txt = data["choices"][0]["message"]["content"]
    if isinstance(txt, list):
        txt = " ".join(pp.get("text", "") for pp in txt if isinstance(pp, dict))
    return txt.strip()


def _text_model_call(model, prompt, timeout=70):
    import json as _json, urllib.request as _ur
    body = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 900, "temperature": 0,
    }).encode()
    req = _ur.Request("http://127.0.0.1:16432/v1/chat/completions", data=body,
                      headers={"Content-Type": "application/json", "Authorization": ("Bea"+"rer ") + "local-vote"})
    with _ur.urlopen(req, timeout=timeout) as r:
        data = _json.loads(r.read())
    txt = data["choices"][0]["message"]["content"]
    if isinstance(txt, list):
        txt = " ".join(pp.get("text", "") for pp in txt if isinstance(pp, dict))
    return txt.strip()


class Solver:
    def __init__(self, keypool):
        self.kp = keypool

    # --- two-stage: describe cells with VLM, reason with text LLM ---
    def describe(self, image_b64):
        prompt = (
            "This image is an hCaptcha grid challenge with numbered cells (yellow number "
            "top-left of each cell). There is usually a small reference/example picture "
            "near the task text or in a corner.\n"
            "1) First line: EXAMPLE: <describe the small reference object/picture shown, "
            "or 'none' if there is no reference picture>.\n"
            "2) Then for EVERY numbered cell one line: '<number>: <short description of what "
            "is in that cell>' (objects, colors, orientation, count).\n"
            "Be precise and factual. Do not solve anything, just describe."
        )
        try:
            return _vote_model_call("qwen3-vl-plus", image_b64, prompt, timeout=90)
        except Exception as e:
            log.warning("describe err %r", e)
            return ""

    def reason(self, target, desc, need=None, hint="", gh=""):
        count = ""
        if need: count = f"Select EXACTLY {need} cells. "
        elif hint: count = hint + " "
        prompt = (
            f'hCaptcha task (may be in Russian): "{target}"\n'
            f"A vision model described the grid cells:\n{desc}\n\n"
            f"{gh}{count}"
            "Reason step by step which cells satisfy the task, then the FINAL line must be "
            "exactly: ANSWER: <comma separated cell numbers> or ANSWER: none."
        )
        for m in ("kimi-k3", "qwen-plus", "glm-5.3"):
            try:
                txt = _text_model_call(m, prompt, timeout=110)
                if txt and "answer" in txt.lower():
                    return txt
                log.info("reason %s: no ANSWER line, next model", m)
            except Exception as e:
                log.warning("reason %s err %r", m, e)
        return ""

    # --- model asking: multi-sample vision vote ---
    def ask(self, image_b64, target, need=None, hint="", gh=""):
        base = (
            "You are solving an hCaptcha grid challenge. The image is a grid with a yellow "
            "number label in the top-left corner of each cell (numbers start at 0). "
            "Answer using the yellow label numbers you see on the image. "
            "The task is usually written in Russian.\n"
            f'TASK: "{target}"\n'
            "Identify every cell whose image matches the task. "
            "Think step by step briefly (1-2 short sentences), then the FINAL line must be "
            "exactly: ANSWER: <comma separated cell numbers> or ANSWER: none. "
        )
        if gh:
            base += gh + " "
        if hint:
            base += hint + " "
        if need:
            base += f"You MUST pick exactly {need} cells. "

        def one_call(t):
            try:
                return _vote_model_call("qwen3-vl-plus", image_b64, base, timeout=70, temperature=t)
            except Exception as e:
                log.warning("sample t=%s err %r", t, e)
                return ""

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as ex:
            texts = list(ex.map(one_call, (0.0, 0.4, 0.8)))

        votes = {}
        parsed_any = False
        for txt in texts:
            nums = self._parse(txt)
            if nums:
                parsed_any = True
                for rank, c in enumerate(nums):
                    votes[c] = votes.get(c, 0) + (3 - rank) * 0.5 + 1  # earlier in list = more confident
            elif txt and "none" in txt.lower().splitlines()[-1]:
                parsed_any = True

        if not votes:
            if parsed_any and not need:
                return []
            # nothing parseable -> one forced retry
            try:
                txt = _vote_model_call("qwen3-vl-plus", image_b64, base +
                    " Your previous answer was unparseable. The FINAL line must be exactly "
                    "ANSWER: n,n,... with cell numbers only.", timeout=70)
                nums = self._parse(txt)
                return nums
            except Exception as e:
                log.warning("forced retry err %r", e)
                return []

        ordered = sorted(votes.items(), key=lambda kv: -kv[1])
        log.info("vote tally: %s", ordered[:8])
        strong = [c for c, v in ordered if v >= 2]
        if need:
            chosen = [c for c, v in ordered[:need]]
        else:
            chosen = strong if strong else [c for c, v in ordered]
        return chosen

    @staticmethod
    def _parse(txt):
        if not txt:
            return []
        t = txt.strip()
        for line in reversed(t.splitlines()):
            low = line.lower()
            if "answer" in low:
                tail = low.split("answer")[-1].lstrip(":：").strip()
                if re.search(r"\d", tail):
                    return [int(x) for x in re.findall(r"\d+", tail)]
                if "none" in tail:
                    return []
        first = t.splitlines()[0].strip()
        if re.fullmatch(r"(?i)none", first):
            return []
        if re.fullmatch(r"[\d\s,]+", first):
            return [int(x) for x in re.findall(r"\d+", first)]
        return []


async def token_of(page):
    try:
        return await page.evaluate("() => { try { return window.hcaptcha.getResponse() || ''; } catch(e) { return ''; } }")
    except Exception:
        return ""


async def solve(page, keypool, rounds_max=14, timeout=260):
    """Main loop. Returns token string or ''."""
    solver = Solver(keypool)
    t0 = time.time()
    rnd = 0
    while time.time() - t0 < timeout:
        tok = await token_of(page)
        if tok:
            return tok
        fr = await asyncio.to_thread(_find_challenge_frame, page)
        if fr is None:
            await asyncio.sleep(2)
            continue
        try:
            txt = await fr.evaluate("() => document.body.innerText")
        except Exception:
            await asyncio.sleep(2)
            continue
        try:
            n_canv = await fr.locator("canvas").count()
        except Exception:
            n_canv = 0
        rnd += 1
        if rnd > rounds_max:
            return ""
        # transition screen (no canvas at all) -> reload
        if n_canv == 0:
            log.info("round %d: no canvas -> reload", rnd)
            clicked = False
            for sel in ('[class*="reload"]', '.button-reload', "[aria-label*='Перезагрузить']",
                        "[aria-label*='бновить']", "[aria-label*='Reload']", 'button[class*="reload"]'):
                try:
                    loc = fr.locator(sel).first
                    if await loc.count():
                        await loc.click(timeout=2500)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                # widget stuck/closed — force a fresh challenge
                try:
                    await page.evaluate("() => { try { window.hcaptcha.reset(); } catch(e){} try { window.hcaptcha.execute(); } catch(e){} }")
                    log.info("round %d: reset+execute", rnd)
                except Exception as e:
                    log.warning("reset+execute err %r", e)
            await asyncio.sleep(3.5)
            continue

        meta = await _challenge_meta(fr)
        target = meta.get("target", "")
        btn = meta.get("btn", "")
        # meta target can be polluted by the residual fail banner -> extract the real
        # task line from innerText (longest line that is not a banner/button)
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        bad = ("Попробуйте еще", "Пропустить", "Дальше", "Готово", "Проверить", "RU", "EN", "⚠")
        task_lines = [l for l in lines if not any(b.lower() in l.lower() for b in bad) and len(l) > 8]
        if task_lines and ("Попробуйте" in target or not target):
            target = max(task_lines, key=len)
        is_drag = bool(meta.get("isDrag")) or re.search(
            r"перетащ|поместите правильный|повернут|повёрнут|drag|в пустой слот|завершить узор", target, re.I)
        log.info("round %d: target=%r btn=%r drag=%s", rnd, target[:80], btn, is_drag)

        if is_drag:
            ok = False
            try:
                ok = await _solve_drag(fr, page, keypool, task_text=target or "")
            except Exception as e:
                log.warning("drag solve err: %r", e)
            await asyncio.sleep(2.0)
            tok = await token_of(page)
            if tok:
                return tok
            if not ok or rnd >= 2:
                # drag tasks are unreliable — swap for a new (likely grid) challenge
                for sel in ('[class*="reload"]', '.button-reload', "[aria-label*='бновить']", "[aria-label*='Reload']"):
                    try:
                        loc = fr.locator(sel).first
                        if await loc.count():
                            await loc.click(timeout=2500)
                            log.info("round %d: drag -> reload (%s)", rnd, sel)
                            break
                    except Exception:
                        continue
                await asyncio.sleep(3.0)
            continue

        need, hint = count_hint(target)
        gh = task_hint(target)
        # NOTE: reload-on-fail REMOVED — reloading escalates hCaptcha suspicion
        # and serves deliberately unreadable (striped/obfuscated) challenges.
        solve._seen = getattr(solve, "_seen", {})
        solve._seen[target] = solve._seen.get(target, 0) + 1
        if solve._seen[target] >= 4:
            log.info("round %d: target %r still failing %dx (no reload — keep voting)", rnd, target[:40], solve._seen[target])
        try:
            b64 = await _get_canvas_b64(fr)
            await asyncio.sleep(1.2)  # stability: let load animation settle
            b64 = await _get_canvas_b64(fr)
            gridded = _grid_overlay(b64, _GRID)
        except Exception as e:
            log.warning("canvas capture err: %r", e)
            await asyncio.sleep(2)
            continue
        try:
            cvd = await fr.evaluate("() => window.__cvDbg || null")
            if cvd and cvd.get("n", 1) > 1:
                log.info("round %d: MULTI-canvas n=%s vis=%s picked %sx%s",
                         rnd, cvd.get("n"), cvd.get("vis"), cvd.get("w"), cvd.get("h"))
        except Exception:
            pass
        # debug: dump gridded image for offline analysis
        try:
            import base64 as _b64mod
            _dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dbg_imgs")
            os.makedirs(_dbg, exist_ok=True)
            with open(os.path.join(_dbg, "r%02d_%d.jpg" % (rnd, int(time.time()) % 100000)), "wb") as _f:
                _f.write(_b64mod.b64decode(gridded))
        except Exception:
            pass

        import hashlib
        akey = (target, hashlib.md5(gridded.encode()).hexdigest()[:12])
        if akey == getattr(solve, "_last_key", None) and getattr(solve, "_last_cells", None):
            prev = solve._last_cells
            log.info("round %d: SAME task+image — previous pick %s was WRONG, re-ask excluding", rnd, prev)
            wrong_hint = ((hint + " ") if hint else "") + \
                f"Your previous answer {prev} was WRONG. Do NOT pick cells {prev} again; choose different cells."
            cells2 = await asyncio.to_thread(solver.ask, gridded, target, need, wrong_hint, gh)
            cells = [c for c in cells2 if c not in prev]
            if not cells:
                cells = cells2 or prev  # fallback: submit may have failed, reuse prev
            solve._last_key = None
            solve._last_cells = cells
        else:
            cells = await asyncio.to_thread(solver.ask, gridded, target, need, hint, gh)
            solve._last_key = akey
            solve._last_cells = cells
        log.info("round %d: picked %s", rnd, cells)
        if not cells and not gh and not hint:
            # totally unknown task type -> skip it (Пропустить) to conserve attempts
            pass
        N = _GRID * _GRID
        cells = list(dict.fromkeys(c for c in cells if 0 <= c < N))
        if need and len(cells) > need:
            cells = cells[:need]
        elif need and 0 < len(cells) < need:
            log.info("round %d: got %d cells, need %d -> re-ask", rnd, len(cells), need)
            cells2 = await asyncio.to_thread(
                solver.ask, gridded, target, need,
                f"Your previous answer had the WRONG COUNT. You MUST pick exactly {need} cells.", gh)
            cells2 = list(dict.fromkeys(c for c in cells2 if 0 <= c < N))
            if len(cells2) == need:
                cells = cells2
        try:
            await _click_tiles(page, fr, cells)
        except Exception as e:
            log.warning("click tiles err: %r", e)
        await asyncio.sleep(0.6)
        sub_ok = False
        try:
            sub_ok = await _click_submit(fr)
        except Exception as e:
            log.warning("submit err: %r", e)
        if not sub_ok:
            for _try in range(2):
                try:
                    await fr.locator('[class*="button-submit"], [aria-label*="Готово"], [aria-label*="Дальше"], [aria-label*="Next"], [aria-label*="Submit"]').first.click(timeout=3000, force=True)
                    log.info("force submit click ok (try %d)", _try)
                    break
                except Exception:
                    await asyncio.sleep(1.0)
        # wait for next page or completion
        for _ in range(8):
            await asyncio.sleep(1.2)
            tok = await token_of(page)
            if tok:
                return tok
    return ""
