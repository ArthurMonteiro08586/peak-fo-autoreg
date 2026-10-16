"""Solve the hCaptcha IMAGE challenge with a vision model using numbered-grid classification.

hCaptcha renders images on a <canvas>. Instead of slicing the canvas into tiles and
classifying each tile individually (which fails on reasoning-style challenges like
"click the flower the bee never lands on"), we overlay a numbered grid on the full
canvas and ask the vision model: "which cell number?" This turns an expensive
grounding problem into a simple classification problem — the VLM chooses from N²
labels instead of guessing pixel coordinates, and it costs ONE API call per page
instead of 16.

Grid-overlay also enables drag challenges: ask for two cells (source, target) and
execute a programmatic drag via page.mouse.
"""
import asyncio
import base64
import io
import logging
import random
import re

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

_GRID = 4
_SKIP_SEL = ".button-submit"


# Extracted so the challenge-detection JS is assertable offline. Keep it a NON-RAW
# string: the drag boundary must stay `\\b` (a bare \b is Python backspace 0x08) and the
# '\\n' delimiter must stay literal, else the regex and newline-split silently break.
_CHALLENGE_META_JS = """() => {
        const txt = document.body.innerText;
        const lines = txt.split('\\n').map(l => l.trim()).filter(l => l);
        const skipRe = /(Skip|Пропустить|Next|Далее|Verify|Готово|^EN$|hcaptcha|privacy|terms|report a problem|try again)/i;
        const taskRe = /(Select all|Click on|Tap on|Find the|Найдите|Выберите|Нажмите|Кликните|Перетащите|перетащ|повернут|повёрнут|в пустой слот|drag)/i;
        let task = lines.find(l => taskRe.test(l) && !skipRe.test(l)) || '';
        if (!task) task = lines.filter(l => l.length > 20 && !skipRe.test(l))[0] || '';
        const c = document.querySelector('canvas');
        const r = c ? c.getBoundingClientRect() : null;
        const hasDrag = /^drag\\b/i.test(task) || /help the (creature|monkey|robot|character)/i.test(task) || /перетащ|перемест|drag|перенес/i.test(task);
        const btn = document.querySelector('.button-submit');
        return {
            target: task,
            canvasRect: r ? {x: r.x, y: r.y, w: c.width, h: c.height, cssW: r.width, cssH: r.height} : null,
            isDrag: hasDrag,
            buttonText: btn ? btn.innerText.trim() : '',
        };
    }"""


async def _challenge_meta(fr) -> dict:
    """Extract challenge task text + detect layout."""
    return await fr.evaluate(_CHALLENGE_META_JS)


async def _get_canvas_b64(fr) -> str:
    """Get the ACTIVE challenge canvas as a base64-encoded PNG data URL.

    hCaptcha iframes may contain several canvases (asset sprite/atlas canvases
    with striped packed imagery + the visible challenge canvas). The first
    canvas in DOM order is NOT necessarily the active one — pick the largest
    VISIBLE canvas instead.
    """
    return await fr.evaluate("""() => {
        const cs = [...document.querySelectorAll('canvas')];
        if (!cs.length) return '';
        const vis = cs.filter(c => c.width > 0 && c.height > 0 &&
                                   c.offsetParent !== null &&
                                   getComputedStyle(c).display !== 'none' &&
                                   getComputedStyle(c).visibility !== 'hidden');
        const pool = vis.length ? vis : cs;
        const pick = pool.sort((a, b) => (b.width * b.height) - (a.width * a.height))[0];
        window.__cvDbg = {n: cs.length, vis: vis.length,
                          w: pick.width, h: pick.height};
        return pick.toDataURL('image/png').split(',')[1];
    }""")


def _grid_overlay(b64: str, grid: int = _GRID) -> str:
    """Overlay a numbered grid on a base64 PNG, return base64 PNG.

    Each cell is labelled with its index (0..grid²-1) in a yellow box.
    Grid lines in red. Turns pixel-grounding into cell-classification.
    """
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    # downscale big hCaptcha canvases (dashscope vision chokes on multi-MB PNGs)
    if max(img.size) > 768:
        img.thumbnail((768, 768))
    w, h = img.size
    cw, ch = w // grid, h // grid
    draw = ImageDraw.Draw(img)
    n = 0
    for r in range(grid):
        for c in range(grid):
            x0, y0 = c * cw, r * ch
            draw.rectangle([x0, y0, x0 + cw, y0 + ch], outline="red", width=3)
            tx, ty = x0 + 6, y0 + 6
            draw.rectangle([tx - 2, ty - 2, tx + 38, ty + 26], fill="yellow")
            draw.text((tx, ty), str(n), fill="black")
            n += 1
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def _pick_cells(fr, keypool, target: str, grid: int = _GRID) -> list[int]:
    """Ask the vision model which numbered-grid cells satisfy *target*.

    ONE model call per page. The numbered grid turns grounding into
    classification — VLMs handle this far more accurately than pixel
    coordinates. Returns deduplicated cell indices or [].
    """
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return []
    gridded = _grid_overlay(b64, grid)
    N = grid * grid
    prompt = (
        f"This image has a {grid}x{grid} numbered grid (0..{N - 1}, yellow label "
        f"top-left of each cell). Task: {target!r}. "
        f"Look carefully at EVERY cell one by one. If the task asks about rotation "
        f"or orientation (e.g. find the object rotated against the group), compare "
        f"the rotation/orientation of the object in each cell against the majority. "
        f"Reply ONLY the cell number(s) that satisfy the task, comma-separated "
        f"(e.g. 3 or 1,4,9), or none."
    )
    text = await asyncio.to_thread(keypool.ask, gridded, prompt)
    log.info("_pick_cells(%r) -> %s", target, text[:120])
    t = text.strip()
    # STRICT contract: only "none" or a bare comma list of numbers is accepted.
    # Prose answers leak every number in the text into clicks (worse than none).
    if not re.fullmatch(r"(?i)none|[\d\s,]+", t):
        first = t.splitlines()[0].strip() if t else ""
        if not re.fullmatch(r"(?i)none|[\d\s,]+", first):
            log.info("_pick_cells: prose answer rejected")
            return []
        t = first
    nums = []
    for m in re.finditer(r"\d+", t):
        v = int(m.group())
        if 0 <= v < N:
            nums.append(v)
    seen = set()
    return [x for x in nums if not (x in seen or seen.add(x))]




async def _classify_tiles(fr, keypool, target: str) -> list[int]:
    """DEPRECATED — use _pick_cells instead."""
    return await _pick_cells(fr, keypool, target)


async def _click_tiles(page, fr, indices: list[int], grid: int = None) -> None:
    """Click matching tile positions on the canvas via page.mouse.

    page.mouse.click dispatches real OS-level events that hCaptcha respects.
    grid: actual grid dimension (3 for 3x3 covered-tasks); defaults to _GRID (4).
    """
    cbox = await _active_canvas_box(fr)
    if not cbox:
        return
    g = grid or _GRID
    tw = cbox["width"] / g
    th = cbox["height"] / g
    for idx in indices:
        col = idx % g
        row = idx // g
        x = cbox["x"] + (col + 0.5) * tw
        y = cbox["y"] + (row + 0.5) * th
        try:
            await page.mouse.click(x, y)
            await asyncio.sleep(0.3)
        except Exception as e:
            log.debug("tile %d click: %s", idx, str(e).splitlines()[0])


async def _active_canvas_box(fr):
    """Bounding box (PAGE-relative) of the SAME canvas _get_canvas_b64 picks:
    the largest VISIBLE one. Falls back to first-canvas locator box.
    The in-frame rect is offset by the frame element's page position."""
    try:
        box = await fr.evaluate("""() => {
            const cs = [...document.querySelectorAll('canvas')];
            const vis = cs.filter(c => c.width > 0 && c.height > 0 &&
                                       c.offsetParent !== null &&
                                       getComputedStyle(c).display !== 'none' &&
                                       getComputedStyle(c).visibility !== 'hidden');
            const pool = vis.length ? vis : cs;
            if (!pool.length) return null;
            const pick = pool.sort((a, b) => (b.width * b.height) - (a.width * a.height))[0];
            const r = pick.getBoundingClientRect();
            return {x: r.x, y: r.y, width: r.width, height: r.height};
        }""")
    except Exception:
        box = None
    if not box or not box.get("width"):
        return await fr.locator("canvas").bounding_box()
    try:
        fe = await fr.frame_element()
        fb = await fe.bounding_box()
        if fb:
            box["x"] += fb["x"]
            box["y"] += fb["y"]
    except Exception:
        pass
    return box


def _cell_center(cbox: dict, idx: int, grid: int = _GRID) -> tuple[float, float]:
    """Return (x, y) center pixel of cell `idx` within the canvas bounding box."""
    tw = cbox["width"] / grid
    th = cbox["height"] / grid
    col = idx % grid
    row = idx // grid
    return (cbox["x"] + (col + 0.5) * tw, cbox["y"] + (row + 0.5) * th)


_drag_state = {"task": None, "calls": 0}


async def _solve_drag(fr, page, keypool, task_text: str = "") -> bool:
    """Task-aware grounding drag solver.

    Modes:
    - puzzle  ("drag the correct puzzle piece to complete the image"):
      vision returns gap center + two candidate piece centers (%); we drag
      piece->gap and ALTERNATE candidate on repeated identical task.
    - balance ("seesaw/scale/heavier/lighter"): animal center + both pans +
      which pan is higher (lighter); alternate pan on retry.
    - generic: ask for grab point + drop point directly.
    All coordinates are percentages of the active canvas; eased human drag.
    """
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return False
    tl = (task_text or "").lower()
    puzzle = any(k in tl for k in ("puzzle piece", "complete the image", "missing piece", "fit the", "jigsaw"))
    balance = any(k in tl for k in ("balance", "scale", "seesaw", "heavier", "lighter"))
    if task_text and task_text == _drag_state["task"]:
        _drag_state["calls"] += 1
    else:
        _drag_state["task"] = task_text or _drag_state["task"]
        _drag_state["calls"] = 0
    alt = _drag_state["calls"] % 2 == 1
    sx = sy = tx = ty = None
    if puzzle:
        prompt = (
            "hCaptcha drag challenge: an incomplete picture with one MISSING "
            "area (a gap/hole), plus puzzle piece(s) near the edges that can "
            "be dragged. Reply with ONLY one line, no prose: "
            "GX,GY PX1,PY1 PX2,PY2 — GX,GY = center of the gap; PX1,PY1 = "
            "center of the MOST likely correct piece; PX2,PY2 = center of the "
            "second-most likely piece (repeat the first if only one exists). "
            "All values are PERCENTAGES of image width,height (0-100). "
            "Example: 62,40 15,85 85,85"
        )
        text = await asyncio.to_thread(keypool.ask, b64, prompt)
        log.info("_solve_drag puzzle grounding -> %r", text[:140])
        nums = re.findall(r"\d+(?:\.\d+)?", text or "")
        if len(nums) >= 6:
            gx, gy, p1x, p1y, p2x, p2y = (float(v) for v in nums[:6])
            px, py = (p2x, p2y) if alt else (p1x, p1y)
            sx, sy, tx, ty = px, py, gx, gy
        elif len(nums) >= 4:
            gx, gy, px, py = (float(v) for v in nums[:4])
            sx, sy, tx, ty = px, py, gx, gy
        else:
            log.info("_solve_drag: puzzle parse failed -> grid fallback")
            return await _solve_drag_grid(fr, page, keypool)
    elif balance:
        # one-shot DOM forensics: capture the real widget structure (tokens,
        # buttons, canvas box) — the side panel may be DOM, not canvas.
        if not _drag_state.get("dom_dumped"):
            _drag_state["dom_dumped"] = True
            try:
                import os, json as _j
                d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dbg_imgs")
                os.makedirs(d, exist_ok=True)
                html = await fr.evaluate("document.body.outerHTML")
                with open(os.path.join(d, "balance_dom.html"), "w", encoding="utf-8") as f:
                    f.write(html)
                boxes = await fr.evaluate(
                    "() => { const out = []; "
                    "document.querySelectorAll('canvas, button, img, svg, [draggable], [class*=drag], [class*=move], [class*=animal], [class*=pan], [class*=token]').forEach(el => { "
                    "const r = el.getBoundingClientRect(); "
                    "if (r.width > 0 && r.height > 0) out.push({tag: el.tagName, cls: (el.className||'').toString().slice(0,90), txt: (el.innerText||el.alt||'').slice(0,50), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}); }); "
                    "return out; }"
                )
                with open(os.path.join(d, "balance_boxes.json"), "w") as f:
                    f.write(_j.dumps(boxes, indent=1))
                log.info("balance DOM dumped: %d boxes, html %d chars", len(boxes), len(html))
            except Exception as e:
                log.warning("balance dom dump err: %s", str(e)[:120])
        # REAL mechanics (forensics 2026-10-15, full-page screenshots):
        # - center: balance scale; the HEAVIER (lower) pan holds one animal
        #   that the LIGHTER pan is missing.
        # - RIGHT-side panel (x~82-94%): TWO draggable animal tokens stacked
        #   vertically (TOP and BOTTOM), each with a "+ Move" pill above it.
        #   One token matches the missing animal; the other is a distractor.
        # - Correct action: drag the MATCHING token onto the LIGHTER pan.
        # Grounded balance flow v2: ONE vision query returns the '+ Move'
        # pill of the matching token AND the lighter pan center as percents
        # of the canvas image. Fixed geometry missed real layouts (2 vs 3
        # tokens, variable pill positions) — forensics 21:32 proved it.
        p1 = (
            "hCaptcha balance-scale challenge. Center: a scale with two pans, "
            "one pan hangs LOWER (heavier), the other HIGHER (lighter) and is "
            "missing one animal. Right side: a panel of animal tokens, each "
            "with a small '+ Move' button above it. Compare the animals on "
            "both pans and find which animal the LIGHTER pan is missing. "
            "Reply with ONLY 4 integers separated by spaces: PX PY TX TY = "
            "percent coordinates (0-100 of image width, height): PX,PY is the "
            "'+ Move' button above the matching token in the right panel; "
            "TX,TY is the center of the LIGHTER pan. Example: 88 30 28 55"
        )
        t1 = await asyncio.to_thread(keypool.ask, b64, p1)
        log.info("_solve_drag balance grounding -> %r", (t1 or "")[:90])
        nums = re.findall(r"\d+(?:\.\d+)?", t1 or "")
        n = _drag_state["calls"] + 1
        if len(nums) >= 4:
            gx, gy, tx2, ty2 = (float(v) for v in nums[:4])
        else:
            # legacy fallback: fixed geometry, alternate token/pan per round
            gx, gy = 88.0, (30.0 if n % 2 == 1 else 44.0)
            tx2, ty2 = (28.0, 55.0) if n % 2 == 1 else (72.0, 55.0)
        # escalation: round 2+ targets the OPPOSITE pan (vision may confuse
        # heavier/lighter); round 3+ shifts to the token below (distractor).
        if n >= 2:
            tx2 = 100.0 - tx2
        if n >= 3:
            gy = min(gy + 14.0, 92.0)
        gx = min(max(gx, 2.0), 98.0); gy = min(max(gy, 2.0), 98.0)
        tx2 = min(max(tx2, 2.0), 98.0); ty2 = min(max(ty2, 2.0), 98.0)
        # ODD rounds: '+ Move' accessibility click-path (click pill, then
        # click pan) with a MID screenshot to see if pill-click arms a mode.
        # EVEN rounds: fall through to raw drag from token (just below pill).
        if n % 2 == 1:
            cbox2 = await _active_canvas_box(fr)
            if cbox2:
                pill_x = cbox2["x"] + cbox2["width"] * gx / 100.0
                pill_y = cbox2["y"] + cbox2["height"] * gy / 100.0
                pan_x = cbox2["x"] + cbox2["width"] * tx2 / 100.0
                pan_y = cbox2["y"] + cbox2["height"] * ty2 / 100.0
                log.info("_solve_drag[balance-click] n=%d pill=(%.0f,%.0f) pan=(%.0f,%.0f)",
                         n, pill_x, pill_y, pan_x, pan_y)
                import os as _os, time as _t
                try:
                    d2 = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "dbg_imgs")
                    _os.makedirs(d2, exist_ok=True)
                    tag2 = _t.strftime("%H%M%S")
                except Exception:
                    d2 = None
                    tag2 = None
                await page.mouse.click(pill_x, pill_y)
                await asyncio.sleep(0.4)
                if d2:
                    try:
                        await page.screenshot(path=_os.path.join(d2, f"click_mid_{tag2}.jpg"), quality=70, type="jpeg")
                    except Exception:
                        pass
                await page.mouse.click(pan_x, pan_y)
                await asyncio.sleep(1.0)
                if d2:
                    try:
                        await page.screenshot(path=_os.path.join(d2, f"click_after_{tag2}.jpg"), quality=70, type="jpeg")
                    except Exception:
                        pass
                return True
        # drag path: grab the TOKEN itself (a bit below its pill), drop on pan
        sx, sy = gx, min(gy + 8.0, 96.0)
        tx, ty = tx2, ty2
    else:
        prompt = (
            "hCaptcha drag challenge. Reply with ONLY one line, no prose: "
            "SX,SY TX,TY — SX,SY = the point to GRAB (the object that must be "
            "dragged), TX,TY = the point to DROP it on, as PERCENTAGES of "
            "image width,height (0-100). Example: 20,80 70,30"
        )
        text = await asyncio.to_thread(keypool.ask, b64, prompt)
        log.info("_solve_drag generic grounding -> %r", text[:140])
        nums = re.findall(r"\d+(?:\.\d+)?", text or "")
        if len(nums) < 4:
            log.info("_solve_drag: generic parse failed -> grid fallback")
            return await _solve_drag_grid(fr, page, keypool)
        sx, sy, tx, ty = (float(v) for v in nums[:4])
    sx = min(max(sx, 2.0), 98.0); sy = min(max(sy, 2.0), 98.0)
    tx = min(max(tx, 2.0), 98.0); ty = min(max(ty, 2.0), 98.0)
    cbox = await _active_canvas_box(fr)
    if not cbox:
        return False
    ax_ = cbox["x"] + cbox["width"] * sx / 100.0
    ay_ = cbox["y"] + cbox["height"] * sy / 100.0
    ex_ = cbox["x"] + cbox["width"] * tx / 100.0
    ey_ = cbox["y"] + cbox["height"] * ty / 100.0
    mode = "puzzle" if puzzle else ("balance" if balance else "generic")
    log.info("_solve_drag[%s]: attempt %d src=(%.0f,%.0f) dst=(%.0f,%.0f)",
             mode, _drag_state["calls"] + 1, ax_, ay_, ex_, ey_)
    # forensics: screenshot before/after drag so we can SEE if the sprite moved
    tag = None
    d = None
    try:
        import os, time as _t
        d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "dbg_imgs")
        os.makedirs(d, exist_ok=True)
        tag = _t.strftime("%H%M%S")
        await page.screenshot(path=os.path.join(d, f"drag_before_{tag}.jpg"), quality=70, type="jpeg")
    except Exception:
        tag = None
    ok = await _human_drag(page, ax_, ay_, ex_, ey_)
    if tag:
        try:
            # draw crosshair markers at the mapped src/dst points AFTER drag
            await page.evaluate(
                """(pts) => {
                    document.querySelectorAll('[data-enimark]').forEach(e => e.remove());
                    for (const [x, y, c] of pts) {
                        const d = document.createElement('div');
                        d.setAttribute('data-enimark', '1');
                        d.style.cssText = 'position:fixed;z-index:2147483647;width:26px;height:26px;'
                          + 'border:3px solid ' + c + ';border-radius:50%;pointer-events:none;'
                          + 'left:' + (x - 13) + 'px;top:' + (y - 13) + 'px;';
                        document.body.appendChild(d);
                    }
                }""",
                [[ax_, ay_, "red"], [ex_, ey_, "lime"]],
            )
            await asyncio.sleep(0.4)
            await page.screenshot(path=os.path.join(d, f"drag_after_{tag}.jpg"), quality=70, type="jpeg")
        except Exception:
            pass
    return ok


async def _human_drag(page, sx, sy, tx, ty) -> bool:
    """Human-like eased drag with jitter, micro-pauses and settle."""
    try:
        await page.mouse.move(sx, sy)
        await asyncio.sleep(random.uniform(0.15, 0.35))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.08, 0.2))
        steps = random.randint(18, 28)
        for i in range(1, steps + 1):
            p = i / steps
            ease = 1 - (1 - p) ** 2
            fx = sx + (tx - sx) * ease + random.uniform(-2.5, 2.5)
            fy = sy + (ty - sy) * ease + random.uniform(-2.5, 2.5)
            await page.mouse.move(fx, fy)
            await asyncio.sleep(random.uniform(0.012, 0.032))
        await page.mouse.move(tx, ty)
        await asyncio.sleep(random.uniform(0.12, 0.3))
        await page.mouse.up()
        await asyncio.sleep(1)
        log.info("_solve_drag: drag done")
        return True
    except Exception as e:
        log.warning("_solve_drag failed: %s", str(e).splitlines()[0])
        return False


async def _solve_drag_grid(fr, page, keypool) -> bool:
    """Legacy numbered-grid drag: asks `source,target` cells, drags between
    their centers. Kept as fallback when grounding parse fails."""
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return False
    gridded = _grid_overlay(b64, _GRID)
    prompt = (
        f"This image has a {_GRID}x{_GRID} numbered grid (0..{_GRID * _GRID - 1}, "
        f"yellow label). The task requires dragging one cell to another. "
        f"Reply ONLY two cell numbers: `source,target` — the block to drag, "
        f"then where to drop it (e.g. `4,10`)."
    )
    text = await asyncio.to_thread(keypool.ask, gridded, prompt)
    log.info("_solve_drag -> %s", text[:100])
    nums = re.findall(r"\d+", text)
    if len(nums) < 2:
        log.warning("_solve_drag: couldn't parse source/target from %r", text[:80])
        return False
    src, tgt = int(nums[0]), int(nums[-1])
    cbox = await _active_canvas_box(fr)
    if not cbox:
        return False
    sx, sy = _cell_center(cbox, src)
    tx, ty = _cell_center(cbox, tgt)
    try:
        await page.mouse.move(sx, sy)
        await asyncio.sleep(0.2)
        await page.mouse.down()
        # animate drag in ~10 smooth steps
        steps = 10
        for i in range(1, steps + 1):
            fx = sx + (tx - sx) * i / steps
            fy = sy + (ty - sy) * i / steps
            await page.mouse.move(fx, fy)
            await asyncio.sleep(0.05)
        await page.mouse.up()
        await asyncio.sleep(1)
        log.info("_solve_drag: %d -> %d done", src, tgt)
        return True
    except Exception as e:
        log.warning("_solve_drag failed: %s", str(e).splitlines()[0])
        return False


async def _click_submit(fr) -> bool:
    """Click the submit button (Skip/Next/Verify). Returns True if clicked."""
    try:
        btn = await fr.query_selector(_SKIP_SEL)
        if btn:
            text = await btn.inner_text()
            log.info("submit button: %r", text)
            await btn.click(timeout=5000)
            await asyncio.sleep(2)
            return True
    except Exception as e:
        log.warning("submit click: %s", str(e).splitlines()[0])
    return False


async def solve_hcaptcha_challenge(fr, page, keypool, max_pages: int = 5) -> bool:
    """Run the hCaptcha image challenge through all pages to completion.

    Returns True if Verify was pressed (caller checks for token).
    """
    # Canvas loads lazily — retry up to ~35s before giving up (slow proxies).
    # Re-acquire the frame each iteration: Discord re-renders and the original
    # frame handle goes stale (evaluate throws forever => false "no canvas").
    meta = {}
    for _wait in range(35):
        cur = _find_challenge_frame(page) or fr
        try:
            meta = await _challenge_meta(cur)
        except Exception:
            meta = {}
        if meta.get("canvasRect"):
            fr = cur
            break
        if _wait == 12:
            try:
                fr2 = _find_challenge_frame(page)
                if fr2:
                    t = await fr2.evaluate("() => document.body ? document.body.innerText.slice(0,200) : ''")
                    cc = await fr2.evaluate("() => document.querySelectorAll('canvas').length")
                    log.info("retry-dbg: canvases=%s text=%r", cc, t)
            except Exception as e:
                log.info("retry-dbg err: %s", str(e).splitlines()[0])
        await asyncio.sleep(1)
    if not meta.get("canvasRect"):
        log.info("no challenge canvas found after retries")
        return False
    if meta.get("isDrag"):
        log.warning("drag challenge — best-effort via grid")
        solved = await _solve_drag(fr, page, keypool)
        if not solved:
            await _click_submit(fr)  # Skip if drag fails
        return solved

    target = meta["target"]
    if not target:
        log.info("no challenge target found")
        return False

    log.info("challenge: target=%r btn=%r", target, meta.get("buttonText"))

    for page_num in range(1, max_pages + 1):
        meta = await _challenge_meta(fr)
        target = meta.get("target", "")
        btn_text = meta.get("buttonText", "")

        log.info("page %d/%d: target=%r btn=%r", page_num, max_pages, target, btn_text)

        if btn_text.lower() in ("verify", "verifizieren", "verificar", "vahvista"):
            break  # already on verify page, just click

        # Skip check for this fr before classify (can get stale between pages)
        fr = _find_challenge_frame(page)
        if not fr:
            log.warning("challenge frame lost")
            return False

        # Classify tiles and click matches
        if target:
            if meta.get("isDrag"):
                log.info("drag task on page %d — routing to _solve_drag", page_num)
                if await _solve_drag(fr, page, keypool):
                    await asyncio.sleep(1)
                    await _click_submit(fr)
                    await asyncio.sleep(2)
                    continue
                # drag failed -> skip this page
                await _click_submit(fr)
                return False
            positives = await _pick_cells(fr, keypool, target)
            log.info("grid pick -> %s", positives)
            await _click_tiles(page, fr, positives)
        else:
            log.info("no target text on this page — clicking first tiles as guess")
            await _click_tiles(page, fr, list(range(_GRID)))

        await asyncio.sleep(2)

        # Click Next/Verify to advance
        clicked = await _click_submit(fr)
        if not clicked:
            return False
        if btn_text.lower() in ("skip", "跳过", "huppel", "ohita"):
            return False  # genuinely unanswerable page (button stayed Skip)
        await asyncio.sleep(2)

    # After last page, check for "Verify" button
    fr = _find_challenge_frame(page)
    if fr:
        meta = await _challenge_meta(fr)
        log.info("final state: btn=%r", meta.get("buttonText"))
        if meta.get("buttonText", "").lower() in ("skip", "пропустить"):
            return False
        if meta.get("buttonText", "").lower() != "skip":
            await _click_submit(fr)

    return True


def _find_challenge_frame(page):
    """Return the hCaptcha challenge frame, or None."""
    for fr in page.frames:
        u = fr.url or ""
        if "#frame=challenge" in u and "hcaptcha" in u:
            return fr
    return None
