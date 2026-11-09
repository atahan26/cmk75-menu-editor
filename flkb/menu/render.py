"""Draw the menu screens (240 x 135, what the CMK75 shows) with Pillow."""
from __future__ import annotations

import functools
import time

from PIL import Image, ImageDraw, ImageFont

from . import config

W, H = 240, 135
TEXT = (235, 238, 242)
DIM = (120, 128, 140)


def _rgb(hex_colour: str, default=(46, 168, 255)):
    try:
        h = hex_colour.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, AttributeError):
        return default


@functools.lru_cache(maxsize=16)
def font(size: int, bold: bool = False):
    for name in (("seguisb.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(draw, text: str, f, width: int) -> str:
    if draw.textlength(text, font=f) <= width:
        return text
    while text and draw.textlength(text + "...", font=f) > width:
        text = text[:-1]
    return text + "..."


@functools.lru_cache(maxsize=64)
def _icon_image(icon: str, mtime: float, size: int):
    p = config.icon_path(icon)
    img = Image.open(p).convert("RGBA")
    img.thumbnail((size, size), Image.LANCZOS)
    return img


def tile(item: dict, size: int, accent=(46, 168, 255)) -> Image.Image:
    """Square icon tile: the item's icon on a rounded plate, or a coloured letter tile."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    p = config.icon_path(item.get("icon", ""))
    if p:
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=(32, 37, 46, 255))
        pad = max(2, size // 8)
        img = _icon_image(item["icon"], p.stat().st_mtime, size - 2 * pad)
        out.alpha_composite(img, ((size - img.width) // 2, (size - img.height) // 2))
    else:
        colour = _rgb(item.get("color") or "", accent)
        d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=colour + (255,))
        letter = (item.get("name") or "?")[:1].upper()
        f = font(int(size * 0.55), True)
        d.text((size / 2, size / 2 + 1), letter, font=f, fill=(255, 255, 255), anchor="mm")
    return out


def _header(img, d, title: str, s: dict):
    accent = _rgb(s["accent"])
    d.text((6, 9), _fit(d, title, font(13, True), 228), font=font(13, True), fill=TEXT, anchor="lm")
    d.line([0, 19, W, 19], fill=accent, width=1)


def _base(s: dict):
    img = Image.new("RGB", (W, H), _rgb(s["background"], (14, 17, 22)))
    return img, ImageDraw.Draw(img)


GRID_COLS, GRID_ROWS = 3, 2
PER_PAGE = GRID_COLS * GRID_ROWS
INACTIVE = (70, 76, 88)          # highlight colour while the knob is back to volume


FRAME_T = 2                      # frame thickness; the screen chip draws the same bars (layout record)


def grid_frames(n_on_page: int):
    """Frame rectangles (x, y, w, h) of the grid cells on a page: in the plain margin around tile and name,
    so the screen chip can erase a frame by painting it in the background colour."""
    out = []
    for i in range(n_on_page):
        x0, y0, x1, y1 = _cell(i)
        out.append((x0 + 2, y0 + 1, 76, 56))
    return out


def option_frames(n_rows: int):
    return [(4, 24 + r * 27, 231, 24) for r in range(n_rows)]


def _frame(d, rect, colour):
    x, y, w, h = rect
    d.rectangle([x, y, x + w - 1, y + h - 1], outline=colour, width=FRAME_T)


def _cell(i: int):
    """(x0, y0, x1, y1) of grid cell i on a page (3 x 2 cells of 80 x 57 under the header)."""
    col, row = i % GRID_COLS, i // GRID_COLS
    return col * 80, 21 + row * 57, col * 80 + 80, 21 + row * 57 + 57


def grid(items: list[dict], index: int, title: str, s: dict, active: bool = True) -> Image.Image:
    """Home / group screen: 6 tiles per page with their names; only a thin frame marks the selection, so
    moving it changes very few pixels (fast to send)."""
    img, d = _base(s)
    accent = _rgb(s["accent"])
    page, pages = index // PER_PAGE, max(1, -(-len(items) // PER_PAGE))
    head = title + (f"  {page + 1}/{pages}" if pages > 1 else "")
    _header(img, d, head, s)
    if not items:
        d.text((W / 2, 70), "No entries yet", font=font(16, True), fill=TEXT, anchor="mm")
        d.text((W / 2, 92), "Add some in the editor", font=font(12), fill=DIM, anchor="mm")
        return img
    rgba = img.convert("RGBA")
    shown = items[page * PER_PAGE:(page + 1) * PER_PAGE]
    for i, it in enumerate(shown):
        x0, y0, x1, y1 = _cell(i)
        rgba.alpha_composite(tile(it, 36, accent), (x0 + 22, y0 + 5))
    img = rgba.convert("RGB")
    d = ImageDraw.Draw(img)
    for i, it in enumerate(shown):
        x0, y0, x1, y1 = _cell(i)
        name = it["name"] + (" >" if it["kind"] == "group" else "")
        d.text(((x0 + x1) / 2, y0 + 48), _fit(d, name, font(11, True), 68), font=font(11, True), fill=TEXT, anchor="mm")
    _frame(d, grid_frames(len(shown))[index % PER_PAGE], accent if active else INACTIVE)
    return img


def carousel(items, index, title, s):            # kept for the editor preview of older code paths
    return grid(items, index, title, s)


def options(item: dict, opts: list[dict], index: int, s: dict, top: int = 0, active: bool = True) -> Image.Image:
    img, d = _base(s)
    accent = _rgb(s["accent"])
    rgba = img.convert("RGBA")
    rgba.alpha_composite(tile(item, 16, accent), (5, 2))
    img = rgba.convert("RGB")
    d = ImageDraw.Draw(img)
    d.text((26, 9), _fit(d, item["name"], font(13, True), 168), font=font(13, True), fill=TEXT, anchor="lm")
    d.line([0, 19, W, 19], fill=accent, width=1)
    rows = 4
    for row in range(rows):
        i = top + row
        if i >= len(opts):
            break
        y = 24 + row * 27
        d.text((14, y + 12), _fit(d, opts[i]["label"], font(15, True), 200), font=font(15, True), fill=TEXT, anchor="lm")
        if i == index:
            _frame(d, option_frames(rows)[row], accent if active else INACTIVE)
    if len(opts) > rows:      # in the header: the bottom right is inside the 4th row's frame (screen-side redraws)
        d.text((W - 6, 9), f"{index + 1}/{len(opts)}", font=font(12, True), fill=DIM, anchor="rm")
    return img


def volume(item: dict, level: float | None, muted: bool, s: dict) -> Image.Image:
    img, d = _base(s)
    _header(img, d, item["name"] + " volume", s)
    accent = _rgb(s["accent"])
    if level is None:
        d.text((W / 2, 62), "Not playing audio", font=font(16, True), fill=TEXT, anchor="mm")
        d.text((W / 2, 86), "Start some sound, then try again", font=font(11), fill=DIM, anchor="mm")
        return img
    pct = round(level * 100)
    d.text((W / 2, 56), "Muted" if muted else f"{pct}%", font=font(30, True), fill=TEXT, anchor="mm")
    d.rounded_rectangle([20, 88, W - 20, 100], radius=6, fill=(40, 46, 56))
    if pct:
        d.rounded_rectangle([20, 88, 20 + (W - 40) * pct // 100, 100], radius=6,
                            fill=(110, 116, 128) if muted else accent)
    d.text((W / 2, 120), "turn = volume   press = mute   hold = back", font=font(10), fill=DIM, anchor="mm")
    return img


def message(text: str, sub: str, s: dict, item: dict | None = None) -> Image.Image:
    img, d = _base(s)
    accent = _rgb(s["accent"])
    if item:
        rgba = img.convert("RGBA")
        rgba.alpha_composite(tile(item, 48, accent), (W // 2 - 24, 16))
        img = rgba.convert("RGB")
        d = ImageDraw.Draw(img)
    y = 84 if item else 58
    d.text((W / 2, y), _fit(d, text, font(17, True), 228), font=font(17, True), fill=TEXT, anchor="mm")
    if sub:
        d.text((W / 2, y + 22), _fit(d, sub, font(11), 228), font=font(11), fill=DIM, anchor="mm")
    return img
