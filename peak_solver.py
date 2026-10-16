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
_VISION_URL = os.environ.get("VISION_BASE", "http://127.0.0.1:16432").rstrip("/") + "/v1/chat/completions"
_VISION_KEY = os.environ.get("VISION_KEY", "local-vote")

_SIDECAR = os.environ.get("PEAK_SIDECAR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cs_sidecar")
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)

from hcaptcha.image_solve import (  # noqa: E402
    _GRID,
    _active_canvas_box,
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
    if "закономерность" in t or "нарушают" in t or "break the pattern" in t or "pattern" in t:
        return ("Find the items that BREAK the pattern: most items share one style (e.g. faint translucent "
                "outlines), while the odd ones differ (e.g. solid opaque colored characters), or differ in "
                "shape family/orientation/count. Compare every cell against the majority and pick the outliers.")
    if "на линии" in t or "на линиях" in t or "on the line" in t or "on a line" in t:
        return ("Some cells contain a drawn line or stripe; click the cells where a figure/object sits ON, "
                "TOUCHES or CROSSES that line. Cells without a line, or with a line but no figure on it, are wrong.")
    if "яйцекладущ" in t or "egg-laying" in t or "egg laying" in t or "lay eggs" in t:
        return ("Egg-laying animals: birds (flamingo, chicken, duck, owl...), reptiles (snake, turtle, "
                "lizard, crocodile), amphibians (frog), fish, insects, platypus, echidna. "
                "NOT egg-laying: whale, dolphin, beaver, seal, otter, cat, dog, bear, cow and all other "
                "placental mammals. Click ONLY cells with egg-laying animals; ignore scenery-only cells.")
    if "covered" in t or "покрыт" in t or "накрыт" in t:
        return ("A small reference object is shown near the task text (EXAMPLE picture). An item qualifies "
                "only if that reference object, placed over the item, would hide it COMPLETELY — i.e. the item "
                "is smaller than or equal to the reference object. Pick cells whose item is small enough to be "
                "fully covered by the reference; skip items bigger than the reference and empty cells.")
    if re.search(r"спорт|игр|sport|game|playing", t):
        return ("Sports/games items: balls, rackets, bats, gloves, goals, controllers, chess, cards, dice. "
                "NOT: everyday electronics (headphones alone), clothing, food.")
    return ""


def _vote_model_call(model, image_b64, prompt, timeout=90, temperature=0, max_tokens=400):
    import json as _json, urllib.request as _ur
    body = _json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": f"data:image/jpeg;base64,{image_b64}"}]}],
        "max_tokens": max_tokens, "temperature": temperature,
    }).encode()
    req = _ur.Request(_VISION_URL, data=body,
                      headers={"Content-Type": "application/json", "Authorization": ("Bea"+"rer ") + _VISION_KEY})
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
    req = _ur.Request(_VISION_URL, data=body,
                      headers={"Content-Type": "application/json", "Authorization": ("Bea"+"rer ") + _VISION_KEY})
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
    def describe(self, image_b64, note=""):
        if note:
            # full-frame challenge screenshot: no yellow labels, unknown dimensions
            prompt = (
                note +
                "Describe the image:\n"
                "1) First line: EXAMPLE: <describe the small reference object/picture shown, "
                "or 'none' if there is no reference picture>.\n"
                "2) State GRID: <rows>x<cols>.\n"
                "3) Then for EVERY cell one line: '<number>: <short description of what "
                "is in that cell>' (objects, colors, orientation, count, SIZE relative to "
                "the EXAMPLE object: smaller / about equal / larger).\n"
                "Be precise and factual. Do not solve anything, just describe."
            )
        else:
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
        # for "covered by object" tasks the reference EXAMPLE object lives OUTSIDE the
        # canvas (in the challenge panel) — canvas-only capture is unreadable garbage.
        # Capture the FULL iframe screenshot instead and let the model number cells itself.
        full_note = ""
        model_img = gridded
        grid_n = [4]  # actual grid dimension (covered tasks are often 3x3)
        montage_b64 = ""
        if re.search(r"covered|накрыть|накрыт", target, re.I):
            try:
                import base64 as _b64m
                _fel = await fr.frame_element()
                png = await _fel.screenshot()
                shot = _b64m.b64encode(png).decode()
                _dbg2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dbg_imgs")
                os.makedirs(_dbg2, exist_ok=True)
                with open(os.path.join(_dbg2, "r%02d_full_%d.jpg" % (rnd, int(time.time()) % 100000)), "wb") as _f2:
                    _f2.write(png)
                model_img = shot
                full_note = ("NOTE: this image is a FULL screenshot of the challenge window: task text, a small "
                             "EXAMPLE/reference object, and a photo grid. First determine the grid dimensions from "
                             "the image (it may be 3x3 OR 4x4). Cells have NO yellow numbers; number them row-major "
                             "from the top-left starting at 0 (for 3x3: top row = 0,1,2; for 4x4: top row = 0,1,2,3). "
                             "Use the EXAMPLE object for size comparison. ")
                log.info("round %d: covered-task -> full frame shot %d bytes", rnd, len(png))
                # ---- OVERLAY MONTAGE: draw the example FOOTPRINT as a red rectangle
                # (true pixel size) centered on each cell crop -> model only judges
                # "does the object protrude past the red rect?" (purely perceptual)
                try:
                    from PIL import Image as _PILImage, ImageDraw as _PILDraw
                    import io as _io
                    _im = _PILImage.open(_io.BytesIO(png)).convert("RGB")
                    _W, _H = _im.size
                    _fbox = await _fel.bounding_box()
                    _cbox = await _active_canvas_box(fr)
                    if _fbox and _cbox:
                        cx0 = int(_cbox["x"] - _fbox["x"]); cy0 = int(_cbox["y"] - _fbox["y"])
                        cw = int(_cbox["width"]); ch = int(_cbox["height"])
                    else:
                        cx0, cy0, cw, ch = 0, int(_H * 0.35), _W, _H - int(_H * 0.35)
                    _gdim = 3 if cw / max(ch, 1) > 1.2 else 4
                    grid_n[0] = _gdim
                    ex = _im.crop((max(_W - 100, 0), 8, _W, min(112, _H)))
                    ex_w = max(ex.size[0] - 16, 20); ex_h = max(ex.size[1] - 16, 20)
                    tw = cw // _gdim; th = ch // _gdim
                    panels = []
                    for i in range(_gdim * _gdim):
                        r_, c_ = divmod(i, _gdim)
                        comp = _im.crop((cx0 + c_ * tw, cy0 + r_ * th,
                                         cx0 + (c_ + 1) * tw, cy0 + (r_ + 1) * th)).convert("RGB")
                        d_ = _PILDraw.Draw(comp)
                        px0 = (comp.size[0] - ex_w) // 2; py0 = (comp.size[1] - ex_h) // 2
                        d_.rectangle([px0, py0, px0 + ex_w, py0 + ex_h], outline="red", width=3)
                        d_.rectangle([0, 0, 26, 22], fill="black")
                        d_.text((4, 4), str(i), fill="yellow")
                        panels.append(comp)
                    mw = tw + 6; mh = th + 6
                    mont = _PILImage.new("RGB", (mw * _gdim, mh * _gdim), "black")
                    for i, p in enumerate(panels):
                        r_, c_ = divmod(i, _gdim)
                        mont.paste(p, (c_ * mw + 3, r_ * mh + 3))
                    _buf = _io.BytesIO(); mont.save(_buf, "JPEG", quality=88)
                    montage_b64 = _b64m.b64encode(_buf.getvalue()).decode()
                    with open(os.path.join(_dbg2, "r%02d_mont_%d.jpg" % (rnd, int(time.time()) % 100000)), "wb") as _fm:
                        _fm.write(_buf.getvalue())
                    log.info("round %d: montage built %dx%d grid=%d ex=%s",
                             rnd, mont.size[0], mont.size[1], _gdim, ex.size)
                except Exception as e:
                    log.warning("montage err %r", e)
            except Exception as e:
                log.warning("frame shot err %r", e)
        akey = (target, hashlib.md5(model_img.encode()).hexdigest()[:12])
        _seen_n = solve._seen.get(target, 0)

        def _ask2(h_):
            hh = full_note + (h_ or "")
            # covered-by-object tasks: canvas voting is systematically blind (reference
            # object is outside the canvas) -> escalate to describe+reason IMMEDIATELY
            # when we have a full-frame shot; other tasks escalate after 5 failed rounds
            esc_now = _seen_n >= 5 or (bool(full_note))
            if esc_now:
                log.info("round %d: escalate %r -> describe+reason", rnd, target[:40])
                if montage_b64:
                    # perceptual red-rect judgement (no size estimation by the model)
                    try:
                        mp = ("Panels 0-%d show hCaptcha grid cells (yellow number in top-left = panel id). "
                              "Each panel has the SAME red rectangle drawn in its CENTER. The red rectangle is "
                              "the footprint of the EXAMPLE object at TRUE size (the example is the small picture "
                              "near the task text).\n"
                              "A panel QUALIFIES if the main object in that photo fits COMPLETELY INSIDE the red "
                              "rectangle (no part of the object sticks out beyond any red edge).\n"
                              "Judge each panel strictly by whether the object protrudes past the red rectangle.\n"
                              "Last line EXACTLY:\n"
                              "ANSWER: <comma separated qualifying panel numbers> or ANSWER: none"
                              % (grid_n[0] * grid_n[0] - 1))
                        mtxt = _vote_model_call("qwen3-vl-plus", montage_b64, mp, timeout=120, max_tokens=700)
                        mnums = solver._parse(mtxt)
                        log.info("round %d: montage -> %s", rnd, mnums)
                        if mtxt and re.search(r"ANSWER\s*:", mtxt):
                            return mnums
                    except Exception as e:
                        log.warning("montage ask err %r", e)
                if full_note:
                    # covered-task: direct vision pass on the full-frame shot first
                    # (describe step mis-estimates relative sizes -> reason inherits the error)
                    try:
                        cp = (full_note +
                              'TASK: "' + target + '" (Russian; = click all items that can be COMPLETELY '
                              'COVERED by the small EXAMPLE object shown near the task text).\n'
                              "An item qualifies ONLY if its bounding box is SMALLER THAN OR EQUAL to the "
                              "EXAMPLE's bounding box in BOTH width and height. Items filling the full cell "
                              "width are usually WIDER than the small example -> disqualified.\n"
                              "Reply format — FIRST line exactly 'GRID: <rows>x<cols>' (count the photo grid), "
                              "then ONE short line per cell 'N: OK/NO (why)', then the FINAL line exactly:\n"
                              "ANSWER: <comma separated cell numbers> or ANSWER: none")
                        dtxt = _vote_model_call("qwen3-vl-plus", model_img, cp, timeout=140, max_tokens=1400)
                        dnums = solver._parse(dtxt)
                        gm = re.search(r"GRID:\s*(\d)\s*[xX\u00d7]\s*(\d)", dtxt or "")
                        if not montage_b64 and gm and gm.group(1) == gm.group(2) and gm.group(1) in "23":
                            grid_n[0] = int(gm.group(1))
                            log.info("round %d: covered GRID %dx%d detected", rnd, grid_n[0], grid_n[0])
                        log.info("round %d: covered direct -> %s", rnd, dnums)
                        if dnums:
                            return dnums
                    except Exception as e:
                        log.warning("covered direct err %r", e)
                try:
                    desc = solver.describe(model_img, note=full_note)
                    gm2 = re.search(r"GRID:\s*(\d)\s*[xX\u00d7]\s*(\d)", desc or "")
                    if gm2 and gm2.group(1) == gm2.group(2) and gm2.group(1) in "23":
                        grid_n[0] = int(gm2.group(1))
                    rtxt = solver.reason(target, desc, need, hh, gh)
                    nums = solver._parse(rtxt)
                    if nums or (rtxt and "none" in rtxt.lower().splitlines()[-1]):
                        log.info("round %d: reason path -> %s", rnd, nums)
                        return nums
                except Exception as e:
                    log.warning("reason path err %r", e)
            return solver.ask(model_img, target, need, hh, gh)

        if akey == getattr(solve, "_last_key", None) and getattr(solve, "_last_cells", None):
            prev = solve._last_cells
            log.info("round %d: SAME task+image — previous pick %s was WRONG, re-ask excluding", rnd, prev)
            wrong_hint = ((hint + " ") if hint else "") + \
                f"Your previous answer {prev} was WRONG. Do NOT pick cells {prev} again; choose different cells."
            cells2 = await asyncio.to_thread(_ask2, wrong_hint)
            cells = [c for c in cells2 if c not in prev]
            if not cells:
                cells = cells2 or prev  # fallback: submit may have failed, reuse prev
            solve._last_key = None
            solve._last_cells = cells
        else:
            cells = await asyncio.to_thread(_ask2, hint)
            solve._last_key = akey
            solve._last_cells = cells
        log.info("round %d: picked %s", rnd, cells)
        if not cells and not gh and not hint:
            # totally unknown task type -> skip it (Пропустить) to conserve attempts
            pass
        N = grid_n[0] * grid_n[0]
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
            await _click_tiles(page, fr, cells, grid=grid_n[0])
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
