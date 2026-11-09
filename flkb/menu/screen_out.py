"""Keyboard-screen output for the menu: sends only what changed, keeps the firmware's live hold alive,
reconnects after the keyboard is unplugged. One instance owns the keyboard's config interface."""
from __future__ import annotations

import threading
import time

from PIL import Image, ImageChops

from .. import reader, screen, transport, writer

VID, PID, CMK75_ID = 0x3151, 0x4015, 1323
KEEPALIVE_S = 20          # firmware live hold is 30 s; refresh well before it runs out
RETRY_S = 3               # while the keyboard is missing, look for it this often


class ScreenOut:
    def __init__(self, log=print):
        self.log = log
        self.kb = None
        self.last = None            # what the screen shows (as far as we know)
        self.last_layout = b""
        self.busy = (0.0, 0.0)      # (start, end) of the last upload: the keyboard chip was busy relaying it
        self.tainted = []           # areas the screen chip may have drawn itself (press flash): always resend
        self.lock = threading.RLock()
        self.active = True          # menu always on: keep the hold alive
        self.rect_mode = True       # firmware v9+: send changed rectangles only
        self.last_send = 0.0
        self.next_try = 0.0
        self.missing_logged = False

    def _connect(self):
        if self.kb:
            return self.kb
        if time.time() < self.next_try:
            raise RuntimeError("keyboard not available")
        self.next_try = time.time() + RETRY_S
        itf = transport.find_config_interface(VID, PID)
        if not itf:
            if not self.missing_logged:
                self.log("keyboard not found; waiting for it")
                self.missing_logged = True
            raise RuntimeError("keyboard not found")
        kb = transport.Keyboard(itf)
        if reader.model_id(kb) != CMK75_ID:
            kb.close()
            raise RuntimeError("not the CMK75")
        self.kb, self.last, self.missing_logged = kb, None, False
        self.log("keyboard connected")
        return kb

    def _drop(self, err):
        if self.kb:
            self.log(f"keyboard connection lost: {err}")
            try:
                self.kb.close()
            except Exception:
                pass
        self.kb, self.last, self.last_layout = None, None, b""

    def set_clock(self) -> str | None:
        with self.lock:
            try:
                return writer.set_clock(self._connect())
            except Exception as e:
                self._drop(e)
                return None

    def show(self, img: Image.Image, full: bool = False, layout: bytes = b"") -> bool:
        """Send the changed part of img (240 x 135), compressed, plus a layout record (firmware v11: where the
        selectable frames are). Returns False if the keyboard is unavailable."""
        img = img.convert("RGB")
        with self.lock:
            try:
                kb = self._connect()
                if full or self.last is None:
                    rects = [(0, 0, screen.W, screen.H)]
                else:
                    rects = screen.dirty_rects(self.last, img)
                    if not rects and layout == self.last_layout:
                        return True               # nothing changed: no upload (tainted areas wait for the next)
                    rects += self.tainted
                t0 = time.time()
                r = screen.upload_rects(kb, img, rects, extra=layout)
                self.log(f"upload {r['bytes']} B ({'full page' if full else str(len(rects)) + ' areas'}) "
                         f"in {r['seconds']:.2f} s")
                self.busy = (t0, time.time())
                self.tainted = []
                self.last, self.last_layout, self.last_send = img, layout, time.time()
                return True
            except Exception as e:
                self._drop(e)
                return False

    def taint(self, rects):
        """These areas may not show what we think (the screen chip drew there): include them in the next upload."""
        with self.lock:
            self.tainted += [r for r in rects if r[2] > r[0] and r[3] > r[1]]

    def was_busy_at(self, t: float, margin: float = 0.15) -> bool:
        """True if t falls in (or just around) the last upload: knob messages to the screen chip may have been
        dropped by the keyboard chip then, so the screen's own frame position cannot be trusted."""
        start, end = self.busy
        return start - margin <= t <= end + margin

    def assume(self, img: Image.Image, layout: bytes = b""):
        """The screen chip changed the picture by itself (frame moved on a knob event): remember it as shown."""
        with self.lock:
            if self.last is not None:
                self.last, self.last_layout = img.convert("RGB"), layout

    def sync(self, layout: bytes) -> bool:
        """Send only the layout record: the screen chip repaints every frame at the PC's position."""
        with self.lock:
            try:
                r = screen.upload_rects(self._connect(), self.last, self.tainted, extra=layout)
                self.log(f"upload {r['bytes']} B (frame re-sync) in {r['seconds']:.2f} s")
                self.tainted = []
                self.last_layout = layout
                return True
            except Exception as e:
                self._drop(e)
                return False

    def keepalive(self):
        """Nothing to do: firmware v10 keeps the live picture until release() (no periodic uploads, so the
        keyboard lights are left alone while the knob is idle)."""

    def release(self):
        """Give the screen back to the firmware's pages (clock), e.g. when the menu app quits."""
        with self.lock:
            try:
                if self.kb:
                    screen.release_live(self.kb)
            except Exception as e:
                self.log(f"could not release the screen: {e}")

    def close(self):
        with self.lock:
            if self.kb:
                self.kb.close()
                self.kb = None
