"""Stored menu (screen firmware v17+): every menu screen is rendered once and written into the screen chip's own
flash. From then on the screen chip moves the frame, flips pages and opens groups / option lists by itself on
the knob events the keyboard chip sends it, so navigating needs no upload (no keyboard light blinks).
The PC follows along from the same knob keys and runs the action of a pressed entry.

Flash area 0x7A0000..0x7FFFFF (the stock picture slots, unused by the menu):
  sector 0: directory: b"MENU", version, page count, 10 spare bytes; then one 128-byte entry per page:
    +0 first sector, +1 sector count, +2 cells, +3 frame thickness, +4 flags (bit0 wrap), +5 next page,
    +6 previous page (0xFF = none), +8/+10/+12 accent/background/inactive RGB565 (big endian),
    +16 cells 16 x (x, y, w, h), +80 target page per cell (0xFF = PC action), +96 cell to land on.
  later sectors: the pages, as rectangle-mode blocks (b"RECT" + records), one 4 KB block per sector.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from .. import screen
from . import config, render

BASE, END = 0x7A0000, 0x800000
SECTOR, PAGE = 4096, 256
EMPTY_PAGE = bytes([0xFF]) * PAGE
SECTORS = (END - BASE) // SECTOR
MAX_PAGES = (SECTOR - 16) // 128           # 31 directory entries
MAX_CELLS = 16
NONE = 0xFF
VERSION = 1
GRID_PER, ROWS_PER = render.PER_PAGE, 4
REC_PAGES = 15                             # flash pages per flash record (one record per 4 KB upload block)
CACHE = config.ROOT / "stored_flash.bin"   # what we last wrote (only changed sectors are sent again)


class StoredError(Exception):
    pass


@dataclass
class Page:
    kind: str                              # "grid" | "options"
    title: str
    image: Image.Image | None = None
    cells: list = field(default_factory=list)
    wrap: bool = False
    next: int = NONE
    prev: int = NONE
    targets: list = field(default_factory=list)     # per cell: (page, cell) or None
    actions: list = field(default_factory=list)     # per cell: (item, option) or None
    entries: list = field(default_factory=list)     # per cell: the item / option / BACK shown there
    owner: dict | None = None                       # option lists: the item they belong to


BACK = {"kind": "back", "name": "Back"}


def usable_options(item: dict) -> list[dict]:
    """Options a press can run (per-app volume needs the knob in volume mode, which the stored menu has not)."""
    return [o for o in item["options"] if o["action"] != "volume"]


# --- page graph ---------------------------------------------------------------------------------------------
def build(menu: dict) -> list[Page]:
    """All screens of the menu as pages. Groups and option lists start with a Back cell."""
    menu = config.normalise(menu)
    s = menu["settings"]
    pages: list[Page] = []

    def add_list(kind: str, title: str, entries: list, back, owner=None) -> int:
        per = GRID_PER if kind == "grid" else ROWS_PER
        chunks = [entries[i:i + per] for i in range(0, len(entries), per)] or [[]]
        first = len(pages)
        if first + len(chunks) > MAX_PAGES:
            raise StoredError(f"the menu needs more than {MAX_PAGES} screens; make groups smaller")
        for _ in chunks:
            pages.append(Page(kind, title))
        for n, chunk in enumerate(chunks):
            p = pages[first + n]
            p.next = first + n + 1 if n + 1 < len(chunks) else NONE
            p.prev = first + n - 1 if n > 0 else NONE
            p.wrap = len(chunks) == 1 and len(chunk) > 0
            p.cells = render.grid_frames(len(chunk)) if kind == "grid" else render.option_frames(len(chunk))
            p.entries, p.owner = list(chunk), owner
            for i, e in enumerate(chunk):
                here = (first + n, i)
                target, action = None, None
                if e is BACK:
                    target = back
                elif kind == "options":
                    action = (owner, e)
                elif e["kind"] == "group":
                    kids = [BACK] + e["children"]
                    target = (add_list("grid", e["name"], kids, here), 1 if e["children"] else 0)
                else:
                    opts = usable_options(e)
                    if len(opts) == 1:
                        action = (e, opts[0])
                    elif opts:
                        target = (add_list("options", e["name"], [BACK] + opts, here, owner=e), 1)
                p.targets.append(target)
                p.actions.append(action)
        for n, chunk in enumerate(chunks):
            p = pages[first + n]
            pg = f"  {n + 1}/{len(chunks)}" if len(chunks) > 1 else ""
            p.image = (_grid_image(chunk, title + pg, s) if kind == "grid"
                       else _options_image(owner, chunk, pg.strip(), s))
        return first

    add_list("grid", "Menu", list(menu["items"]), None)
    return pages


def _back_tile(size: int, accent) -> Image.Image:
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=(32, 37, 46, 255))
    c, w = size / 2, size * 0.22
    d.line([(c + w, c), (c - w, c)], fill=accent + (255,), width=max(2, size // 12))
    d.line([(c - w + 1, c), (c, c - w)], fill=accent + (255,), width=max(2, size // 12))
    d.line([(c - w + 1, c), (c, c + w)], fill=accent + (255,), width=max(2, size // 12))
    return out


def _grid_image(entries, title, s) -> Image.Image:
    img, d = render._base(s)
    accent = render._rgb(s["accent"])
    render._header(img, d, title, s)
    if not entries:
        d.text((render.W / 2, 70), "No entries yet", font=render.font(16, True), fill=render.TEXT, anchor="mm")
        d.text((render.W / 2, 92), "Add some in the editor", font=render.font(12), fill=render.DIM, anchor="mm")
        return img
    rgba = img.convert("RGBA")
    for i, it in enumerate(entries):
        x0, y0, _, _ = render._cell(i)
        t = _back_tile(36, accent) if it is BACK else render.tile(it, 36, accent)
        rgba.alpha_composite(t, (x0 + 22, y0 + 5))
    img = rgba.convert("RGB")
    d = ImageDraw.Draw(img)
    for i, it in enumerate(entries):
        x0, y0, x1, _ = render._cell(i)
        name = it["name"] + (" >" if it.get("kind") == "group" else "")
        f = render.font(11, True)
        d.text(((x0 + x1) / 2, y0 + 48), render._fit(d, name, f, 68), font=f, fill=render.TEXT, anchor="mm")
    return img


def _options_image(item, entries, counter, s) -> Image.Image:
    img, d = render._base(s)
    accent = render._rgb(s["accent"])
    rgba = img.convert("RGBA")
    rgba.alpha_composite(render.tile(item, 16, accent), (5, 2))
    img = rgba.convert("RGB")
    d = ImageDraw.Draw(img)
    f13, f15 = render.font(13, True), render.font(15, True)
    d.text((26, 9), render._fit(d, item["name"], f13, 168), font=f13, fill=render.TEXT, anchor="lm")
    d.line([0, 19, render.W, 19], fill=accent, width=1)
    if counter:
        d.text((render.W - 6, 9), counter, font=render.font(12, True), fill=render.DIM, anchor="rm")
    for row, o in enumerate(entries):
        y = 24 + row * 27
        if o is BACK:
            rgba = img.convert("RGBA")
            rgba.alpha_composite(_back_tile(18, accent), (12, y + 3))
            img = rgba.convert("RGB")
            d = ImageDraw.Draw(img)
            d.text((36, y + 12), "Back", font=f15, fill=render.DIM, anchor="lm")
        else:
            d.text((14, y + 12), render._fit(d, o["label"], f15, 200), font=f15, fill=render.TEXT, anchor="lm")
    return img


# --- flash image --------------------------------------------------------------------------------------------
def page_blocks(img: Image.Image) -> list[bytes]:
    """A full-screen picture as rectangle-mode 4 KB blocks (zero padded: a zero record ends a block)."""
    data = screen.rect_payload(img, [(0, 0, screen.W, screen.H)])
    return [data[i:i + SECTOR].ljust(SECTOR, b"\0") for i in range(0, len(data), SECTOR)]


def flash_image(pages: list[Page], settings: dict) -> bytes:
    """The whole menu area (directory sector + page sectors), 0xFF where nothing is stored."""
    if not 0 < len(pages) <= MAX_PAGES:
        raise StoredError(f"{len(pages)} screens (1..{MAX_PAGES} fit)")
    acc = screen.rgb565(render._rgb(settings["accent"]))
    bg = screen.rgb565(render._rgb(settings["background"], (14, 17, 22)))
    ina = screen.rgb565(render.INACTIVE)
    dir_ = bytearray(b"\xff" * SECTOR)
    dir_[0:16] = b"MENU" + bytes([VERSION, len(pages)]) + bytes(10)
    body, sector = bytearray(), 1
    for n, p in enumerate(pages):
        blocks = page_blocks(p.image)
        if sector + len(blocks) > SECTORS:
            raise StoredError("the menu pictures do not fit into the screen's flash; use fewer entries or icons")
        e = bytearray(128)
        e[0], e[1], e[2], e[3] = sector, len(blocks), len(p.cells), render.FRAME_T
        e[4], e[5], e[6] = 1 if p.wrap else 0, p.next, p.prev
        e[8:14] = bytes([acc >> 8, acc & 0xFF, bg >> 8, bg & 0xFF, ina >> 8, ina & 0xFF])
        assert len(p.cells) <= MAX_CELLS
        for i, (x, y, w, h) in enumerate(p.cells):
            e[16 + 4 * i:20 + 4 * i] = bytes((x, y, w, h))
        for i in range(MAX_CELLS):
            t = p.targets[i] if i < len(p.targets) else None
            e[80 + i], e[96 + i] = (t[0], t[1]) if t else (NONE, 0)
        dir_[16 + 128 * n:16 + 128 * (n + 1)] = e
        for b in blocks:
            body += b
        sector += len(blocks)
    img = bytes(dir_) + bytes(body)
    return img + b"\xff" * (SECTOR * SECTORS - len(img))


def changed_sectors(new: bytes, old: bytes | None) -> list[int]:
    """Sectors to write: every used sector that differs from what we wrote last time (all if unknown).
    The directory (sector 0) always goes last so a cut-off write never points at half-written pages."""
    used = [i for i in range(SECTORS) if new[i * SECTOR:(i + 1) * SECTOR] != b"\xff" * SECTOR]
    if old is None or len(old) != len(new):
        out = used
    else:
        out = [i for i in used if new[i * SECTOR:(i + 1) * SECTOR] != old[i * SECTOR:(i + 1) * SECTOR]]
    out = [i for i in out if i != 0]
    return out + [0] if out or old is None or new[:SECTOR] != old[:SECTOR] else []


def flash_records(image: bytes, sectors: list[int]) -> list[bytes]:
    """Records 0xFC [0xFC, 1 (erase each sector at its first page), addr hi, mid, lo, n, n x 256 bytes]: runs of
    consecutive sectors are written as one stream of up to REC_PAGES pages per record (one per upload block)."""
    runs = []
    for sec in sectors:
        if runs and runs[-1][-1] + 1 == sec:
            runs[-1].append(sec)
        else:
            runs.append([sec])
    recs = []
    for run in runs:
        data = image[run[0] * SECTOR:(run[-1] + 1) * SECTOR]
        npages = len(data) // PAGE
        while npages > (len(run) - 1) * SECTOR // PAGE + 1 and data[(npages - 1) * PAGE:npages * PAGE] == EMPTY_PAGE:
            npages -= 1                    # trailing empty pages of the last sector stay erased
        for k in range(0, npages, REC_PAGES):
            n = min(REC_PAGES, npages - k)
            addr = BASE + run[0] * SECTOR + k * PAGE
            recs.append(bytes([0xFC, 1, addr >> 16 & 0xFF, addr >> 8 & 0xFF, addr & 0xFF, n])
                        + data[k * PAGE:(k + n) * PAGE])
    return recs


def show_record(page: int = 0, cell: int = 0) -> bytes:
    """Record 0xFB: the screen chip draws stored page `page` with the frame on `cell` (0xFF = last cell)."""
    return bytes([0xFB, page, cell])


def uploads(records: list[bytes]) -> list[bytes]:
    """Pack records into rectangle-mode uploads: one record per 4 KB block (flash records nearly fill one),
    at most 0xFFFF bytes per upload (the keyboard's length field)."""
    blocks = []
    for r in records:
        b = screen.RECT_MAGIC + r
        if blocks and len(blocks[-1]) + len(r) + 4 <= screen.BLOCK and r[0] != 0xFC:
            blocks[-1] += r                # small records (show) ride along
        else:
            if len(b) + 4 > screen.BLOCK:
                raise StoredError("record too big for one block")
            blocks.append(bytearray(b))
    out, cur = [], []
    for b in blocks:
        if cur and len(cur) * screen.BLOCK + len(b) + 4 > 0xFFFF:
            out.append(cur)
            cur = []
        cur.append(bytes(b) + bytes(4))   # explicit end record
    if cur:
        out.append(cur)
    return [b"".join(x.ljust(screen.BLOCK, b"\0") for x in up[:-1]) + up[-1] for up in out]


def send(kb, data: bytes) -> None:
    screen._send(kb, data, (0, 0, screen.W, screen.H), screen.LIVE_SLOT, screen.LIVE_DELAY)


def write_menu(kb, pages: list[Page], settings: dict, force: bool = False, log=print) -> dict:
    """Write the menu into the screen's flash (only sectors that changed since last time) and show page 0."""
    image = flash_image(pages, settings)
    old = None if force or not CACHE.exists() else CACHE.read_bytes()
    secs = changed_sectors(image, old)
    recs = flash_records(image, secs) + [show_record(0, 0)]
    ups = uploads(recs)
    t0 = time.perf_counter()
    for i, data in enumerate(ups):
        if i:
            time.sleep(0.8)                # let the keyboard chip close the previous upload (it merges them)
        send(kb, data)
    config.ROOT.mkdir(parents=True, exist_ok=True)
    CACHE.write_bytes(image)
    took = time.perf_counter() - t0
    log(f"stored menu: {len(pages)} screens, {len(secs)} sector(s) written in {len(ups)} upload(s), {took:.1f} s")
    return {"pages": len(pages), "sectors": len(secs), "uploads": len(ups), "seconds": round(took, 1),
            "digest": hashlib.sha1(image).hexdigest()[:12]}


def show(kb, page: int = 0, cell: int = 0) -> None:
    """One tiny upload: the screen chip draws a stored page (the menu app's start, hold = home)."""
    data = uploads([show_record(page, cell)])[0]
    send(kb, data)


def picture(page: Page, cell: int | None, settings: dict) -> Image.Image:
    """What the screen shows on `page` with the frame on `cell` (as the screen chip draws it)."""
    img = page.image.copy()
    if cell is not None and 0 <= cell < len(page.cells):
        render._frame(ImageDraw.Draw(img), page.cells[cell], render._rgb(settings["accent"]))
    return img


def locate(pages: list[Page], item_id: str):
    """(page, cell) to preview an item: its group / option list if it opens one, else its own cell."""
    for n, p in enumerate(pages):
        for i, e in enumerate(p.entries):
            if isinstance(e, dict) and e.get("id") == item_id and p.owner is None:
                t = p.targets[i]
                return t if t else (n, i)
    return 0, 0


# --- the PC's copy of the position ----------------------------------------------------------------------------
class Cursor:
    """Mirrors the screen chip's navigation (same rules as the firmware's nav_move / nav_tick)."""

    def __init__(self, pages: list[Page]):
        self.pages = pages
        self.page, self.cell = 0, 0

    def go(self, page: int, cell: int):
        p = self.pages[page] if 0 <= page < len(self.pages) else None
        if p is None:
            return
        n = len(p.cells)
        self.page = page
        self.cell = 0 if n == 0 else (n - 1 if cell >= n else cell)

    def turn(self, step: int):
        p = self.pages[self.page]
        n, new = len(p.cells), self.cell + step
        if 0 <= new < n:
            self.cell = new
            return
        link = p.next if new >= n else p.prev
        if link != NONE:
            self.go(link, 0 if new >= n else NONE)
        elif p.wrap and n:
            self.cell = 0 if new >= n else n - 1

    def press(self):
        """Returns (item, option) to run, or None (the screen chip opened a page by itself)."""
        p = self.pages[self.page]
        if not 0 <= self.cell < len(p.cells):
            return None
        t = p.targets[self.cell]
        if t is not None:
            self.go(*t)
            return None
        return p.actions[self.cell]
