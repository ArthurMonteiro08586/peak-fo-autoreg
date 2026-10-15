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


async def _click_tiles(page, fr, indices: list[int]) -> None:
    """Click matching tile positions on the canvas via page.mouse.

    page.mouse.click dispatches real OS-level events that hCaptcha respects.
    """
    cbox = await _active_canvas_box(fr)
    if not cbox:
        return
    tw = cbox["width"] / _GRID
    th = cbox["height"] / _GRID
    for idx in indices:
        col = idx % _GRID
        row = idx // _GRID
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
        # Two simple vision calls beat one complex format (model compliance).
        p1 = (
            "This image shows a seesaw / balance scale and one animal. "
            "Reply with ONLY the center of the ANIMAL as two numbers: X,Y — "
            "percentages of image width and height (0-100). No other text. "
            "Example: 50,85"
        )
        t1 = await asyncio.to_thread(keypool.ask, b64, p1)
        log.info("_solve_drag balance animal -> %r", t1[:100])
        n1 = re.findall(r"\d+(?:\.\d+)?", t1 or "")
        if len(n1) < 2:
            log.info("_solve_drag: animal parse failed -> grid fallback")
            return await _solve_drag_grid(fr, page, keypool)
        ax, ay = float(n1[0]), float(n1[1])
        p2 = (
            "This image shows a seesaw / balance scale with a LEFT pan and a "
            "RIGHT pan. One pan sits HIGHER than the other (it is lighter and "
            "needs weight). Reply with ONLY one letter: L if the LEFT pan is "
            "higher, R if the RIGHT pan is higher."
        )
        t2 = await asyncio.to_thread(keypool.ask, b64, p2)
        log.info("_solve_drag balance side -> %r", t2[:40])
        up = (t2 or "").strip().upper()
        pick_left = up.startswith("L") or ("L" in up and "R" not in up)
        if not (pick_left or "R" in up):
            pick_left = True  # default guess; alt flips it
        if alt:
            pick_left = not pick_left  # alternate pan on retry
        # standard hCaptcha seesaw layout: pans ~1/4 and ~3/4 width
        tx, ty = (21.0, 58.0) if pick_left else (79.0, 58.0)
        sx, sy = ax, ay
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
    return await _human_drag(page, ax_, ay_, ex_, ey_)


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
