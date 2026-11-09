"""CMK75 240x135 colour screen, 16-bit mode (otherSetting.LED.isRgb == "16").

Mirrors the vendor app's drawFrameToKeyboardGIF -> MHr / QHr -> getTFTLCDDataRGBImg / setTFTLCDDataRGBImg:
  * region = bounding box of non-black pixels, right/bottom exclusive (MHr)
  * pixels inside the region, COLUMN by column, top to bottom (QHr)
  * each pixel RGB565, high byte first (QHr: writeUInt16LE then push [1],[0])
  * 0xA5 handshake with region + byte length, retried until reply[1] != 0; ready if reply[1] == 1
  * 0x25 packets: [cmd, frame, frameNum, frameDelay, idx lo, idx hi, len, chk] + 56 data bytes, 1 ms apart
"""
from __future__ import annotations

import time

from PIL import Image

from .transport import Keyboard

W, H = 240, 135
CHUNK = 56
PACKET_DELAY = 0.001             # pause before each 56-byte picture packet (vendor timing; 0 caused glitches?)
HANDSHAKE_SETTLE = 0.1           # pause between an accepted handshake and the data (0.02 gave random glitches)


class ScreenError(Exception):
    pass


def region_of(img: Image.Image) -> tuple[int, int, int, int]:
    bbox = img.convert("RGB").getbbox()   # bbox of non-black pixels, right/bottom exclusive
    return bbox if bbox else (0, 0, 0, 0)


def encode(img: Image.Image, region: tuple[int, int, int, int]) -> bytes:
    img = img.convert("RGB")
    if img.size != (W, H):
        raise ScreenError(f"image must be {W}x{H}, got {img.size}")
    left, top, right, bottom = region
    if right <= left or bottom <= top:
        return bytes(W * H * 2)           # vendor sends a zero-filled buffer for an all-black image
    px = img.load()
    out = bytearray()
    for x in range(left, right):          # column-major
        for y in range(top, bottom):
            r, g, b = px[x, y]
            v = (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)
            out += bytes((v >> 8, v & 0xFF))
    return bytes(out)


def _handshake(kb: Keyboard, frame: int, frames: int, delay: int, region, length: int) -> None:
    left, top, right, bottom = region
    p = bytearray(17)                     # packet bytes 1..17; packet byte 7 is the checksum slot
    p[0], p[1], p[2] = frame, frames, delay
    p[3], p[4] = length & 0xFF, length >> 8 & 0xFF
    p[5] = 0
    p[7], p[8], p[9], p[10] = left & 0xFF, top & 0xFF, right & 0xFF, bottom & 0xFF
    p[11], p[12], p[13], p[14] = left >> 8, top >> 8, right >> 8, bottom >> 8
    p[15], p[16] = length >> 16 & 0xFF, length >> 24 & 0xFF
    busy = 0
    for attempt in range(40):
        kb.write(0xA5, bytes(p), post_delay_s=0.0)
        time.sleep(0.1)
        reply = kb.dev.get_feature_report(0, 65)
        reply = bytes(reply[1:] if len(reply) == 65 else reply)
        if reply and reply[1] != 0:
            if reply[1] == 1:
                return
            if reply[1] == 4 and busy < 20:
                # 4 = screen busy (seen on the CMK75 right after a stored upload; the same request was accepted
                # a few seconds later). Back off and ask again.
                busy += 1
                time.sleep(0.5)
                continue
            raise ScreenError(f"keyboard refused the upload (reply byte 1 = {reply[1]})")
        time.sleep(0.1)
    raise ScreenError("keyboard never reported ready for the upload")


def _send(kb: Keyboard, data: bytes, region, frame: int, delay: int, progress=None) -> int:
    """Handshake + 56-byte data packets for one upload. Returns the packet count."""
    _handshake(kb, frame, 1, delay, region, len(data))
    time.sleep(HANDSHAKE_SETTLE)
    packets = (len(data) + CHUNK - 1) // CHUNK
    for i in range(packets):
        chunk = data[i * CHUNK:(i + 1) * CHUNK]
        payload = bytes([frame, 1, delay, i & 0xFF, i >> 8 & 0xFF, len(chunk), 0]) + chunk.ljust(CHUNK, b"\0")
        kb.write(0x25, payload, pre_delay_s=PACKET_DELAY, post_delay_s=0.0)
        if progress and i % 100 == 0:
            progress(i / packets)
    return packets


def upload(kb: Keyboard, img: Image.Image, progress=None, slot: int = 0) -> dict:
    """Store a still 240x135 image in picture slot `slot` (saved to flash, survives power-off)."""
    region = region_of(img.convert("RGB"))
    data = encode(img, region)
    t0 = time.perf_counter()
    packets = _send(kb, data, region, slot, 0, progress)
    return {"region": region, "bytes": len(data), "packets": packets,
            "seconds": round(time.perf_counter() - t0, 2)}


# Live drawing (needs the patched screen firmware; with stock firmware it simply stores into slot 4).
LIVE_SLOT, LIVE_DELAY = 4, 0x01   # marker: frame count 1 + slot 4 + delay 1 (0x4C is refused by the CMK75)
BLOCK = 4096


def live_strips(region):
    """Split (left, top, right, bottom) into column strips of at most one 4 KB block each."""
    left, top, right, bottom = region
    h = bottom - top
    cols = max(1, BLOCK // (h * 2))
    for x in range(left, right, cols):
        yield (x, top, min(x + cols, right), bottom)


def live_payload(img: Image.Image, region) -> bytes:
    """One live upload: whole columns packed into 4 KB blocks (padded), block n = columns (n-1)*cpb...
    The patched firmware (multi-block live drawing) draws each block at its own column offset."""
    parts = [encode(img, strip) for strip in live_strips(region)]
    # Pad every block but the last: the length field the keyboard forwards is 16 bits, and a full screen of
    # 16 padded blocks would be exactly 65536 bytes.
    data = b"".join(part.ljust(BLOCK, b"\0") for part in parts[:-1]) + parts[-1]
    if len(data) > 0xFFFF:
        raise ScreenError(f"live region too big for one upload ({len(data)} bytes)")
    return data


def upload_live(kb: Keyboard, img: Image.Image, region=(0, 0, W, H)) -> dict:
    """Draw `region` of `img` straight to the screen (patched firmware: no flash writes) as ONE upload.
    The CMK75 keyboard chip merges back-to-back single-block uploads (it only closes an upload after ~0.5 s
    of silence), so the whole region goes through a single handshake, like a stored picture."""
    img = img.convert("RGB")
    t0 = time.perf_counter()
    data = live_payload(img, region)
    packets = _send(kb, data, region, LIVE_SLOT, LIVE_DELAY)
    return {"region": region, "blocks": -(-len(data) // BLOCK), "packets": packets,
            "seconds": round(time.perf_counter() - t0, 2)}


# --- Rectangle mode (firmware v9+): several small rectangles in ONE upload --------------------------------
# Each 4 KB block starts with b"RECT", then records [x, y, w, h, w*h*2 pixel bytes]; a record with w == 0
# (or the end of the block) ends it. Only the changed pixels travel, so a highlight move costs a few KB.
RECT_MAGIC = b"RECT"


def rgb565(colour) -> int:
    r, g, b = colour[:3]
    return (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)


def packbits16(data: bytes) -> bytes:
    """PackBits over 16-bit pixels (firmware v11 record 0xFD): c < 128 -> c + 1 literal pixels follow;
    c > 128 -> the next pixel repeats 257 - c times."""
    px = [data[i:i + 2] for i in range(0, len(data), 2)]
    out, i, n = bytearray(), 0, len(px)
    while i < n:
        j = i + 1
        while j < n and j - i < 128 and px[j] == px[i]:
            j += 1
        if j - i >= 2:
            out.append(257 - (j - i))
            out += px[i]
            i = j
            continue
        j = i
        while j < n and j - i < 128 and not (j + 1 < n and px[j] == px[j + 1]):
            j += 1
        j = max(j, i + 1)
        out.append(j - i - 1)
        out += b"".join(px[i:j])
        i = j
    return bytes(out)


def layout_record(on: bool, index: int, cells, thickness: int, accent, background, inactive,
                  wrap: bool = False) -> bytes:
    """Firmware v11 record 0xFE: where the selectable cells are, so the screen chip can move the frame
    itself when the keyboard chip reports a knob turn. Also repaints all frames."""
    cells = list(cells)[:16]
    out = bytearray([0xFE, (1 if on else 0) | (2 if wrap else 0), index, len(cells), thickness])
    for c in (accent, background, inactive):
        v = rgb565(c)
        out += bytes((v >> 8, v & 0xFF))
    for x, y, w, h in cells:
        out += bytes((x, y, w, h))
    return bytes(out)


def rect_payload(img: Image.Image, rects, extra: bytes = b"", compress: bool = True) -> bytes:
    """Pack (left, top, right, bottom) rectangles of img into rectangle-mode blocks (raw or PackBits records,
    whichever is smaller), followed by `extra` records (e.g. a layout record)."""
    img = img.convert("RGB")
    records = []
    for left, top, right, bottom in rects:
        h = bottom - top
        if h <= 0 or right <= left:
            continue
        cols = max(1, (BLOCK - len(RECT_MAGIC) - 8) // (h * 2))      # columns that fit in one record
        for x in range(left, right, cols):
            x1 = min(x + cols, right)
            raw = encode(img, (x, top, x1, bottom))
            rec = bytes([x, top, x1 - x, h]) + raw
            if compress:
                pb = packbits16(raw)
                if len(pb) + 7 < len(rec) and len(pb) + 7 <= BLOCK - len(RECT_MAGIC):
                    rec = bytes([0xFD, x, top, x1 - x, h, len(pb) & 0xFF, len(pb) >> 8]) + pb
            records.append(rec)
    if extra:
        records.append(extra)
    blocks, cur = [], bytearray(RECT_MAGIC)
    for rec in records:
        if len(cur) + len(rec) > BLOCK:
            blocks.append(bytes(cur).ljust(BLOCK, b"\0"))           # zero w = end of records
            cur = bytearray(RECT_MAGIC)
        cur += rec
    if len(cur) > len(RECT_MAGIC):
        if len(cur) + 4 <= BLOCK:
            cur += bytes(4)               # explicit end record (w = 0): the rest of the screen chip's 4 KB block
        blocks.append(bytes(cur))         # buffer still holds the previous upload's bytes, which must not be read
    data = b"".join(blocks)
    if len(data) > 0xFFFF:
        raise ScreenError(f"too many changed pixels for one upload ({len(data)} bytes)")
    return data


def dirty_rects(old: Image.Image, new: Image.Image, cell: int = 1):
    """Rectangles (left, top, right, bottom) covering every pixel that differs, on a cell grid, merged into runs."""
    from PIL import ImageChops
    diff = ImageChops.difference(old.convert("RGB"), new.convert("RGB")).convert("L").point(lambda v: 255 if v else 0)
    if not diff.getbbox():
        return []
    cw, ch = (W + cell - 1) // cell, (H + cell - 1) // cell
    padded = Image.new("L", (cw * cell, ch * cell), 0)              # whole cells, so each cell is exactly cell x cell
    padded.paste(diff, (0, 0))
    grid = padded.reduce(cell).load()                               # any changed pixel -> non-zero cell
    runs = {}                                                       # (x0, x1) -> list of rows
    rects = []
    for cy in range(ch):
        row, cx = [], 0
        while cx < cw:
            if grid[cx, cy]:
                start = cx
                while cx < cw and grid[cx, cy]:
                    cx += 1
                row.append((start, cx))
            cx += 1
        for span in row:
            runs.setdefault(span, []).append(cy)
    for (x0, x1), rows in runs.items():                              # merge vertically consecutive rows
        start = prev = rows[0]
        for r in rows[1:] + [None]:
            if r is not None and r == prev + 1:
                prev = r
                continue
            rects.append((x0 * cell, start * cell, min(W, x1 * cell), min(H, (prev + 1) * cell)))
            if r is not None:
                start = prev = r
    return rects


def upload_rects(kb: Keyboard, img: Image.Image, rects, extra: bytes = b"", compress: bool = True) -> dict:
    """Draw only `rects` of img (firmware v9+ rectangle mode; v11+ compression and layout), as one upload."""
    t0 = time.perf_counter()
    data = rect_payload(img, rects, extra, compress)
    if not data:
        return {"rects": 0, "bytes": 0, "seconds": 0.0}
    packets = _send(kb, data, (0, 0, W, H), LIVE_SLOT, LIVE_DELAY)
    return {"rects": len(rects), "bytes": len(data), "packets": packets, "seconds": round(time.perf_counter() - t0, 2)}


def release_live(kb: Keyboard) -> None:
    """Firmware v10+: end the live hold now (record x = 255), so the screen goes back to its pages (clock)."""
    data = RECT_MAGIC + bytes([0xFF, 0, 0, 0]) + bytes(4)
    _send(kb, data, (0, 0, W, H), LIVE_SLOT, LIVE_DELAY)
