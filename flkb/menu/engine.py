"""Menu state machine: knob events in, screens out, actions run.

The menu is always on the keyboard screen while the app runs. The knob has two modes:
  volume mode (grey highlight): turn = volume, press = mute, hold = start driving the menu
  menu mode (blue highlight):   turn = move, press = choose, hold = back (at the top: back to volume mode),
                                double-press = first page; no input for idle_close_s = back to volume mode
"""
from __future__ import annotations

import queue
import threading
import time
import traceback

from .. import screen
from . import actions, config, render
from .knob import Knob
from .screen_out import ScreenOut



class Engine:
    def __init__(self, log=print):
        self.log = log
        self.menu = config.load()
        self.events: queue.Queue = queue.Queue()
        self.out = ScreenOut(log)
        self.knob = Knob(self.events.put, self.menu["settings"]["open_hold_ms"])
        self.stack: list[dict] = [self._home()]
        self.active = False              # menu mode (knob drives the menu)
        self.last_input = 0.0
        self.pending_click = None        # time of a press that may still become a double-press
        self.error_until = 0.0
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.thread = None
        self.dirty = False               # redraw at the end of the loop pass (merges quick knob turns)
        self.nav_moved_at = 0.0          # last frame move done by the screen chip (re-sync after it)
        self.resync_due = False          # a turn happened near an upload: re-sync once the knob is quiet
        self.last_turn = 0.0
        self.last_turn_at = 0.0

    # --- lifecycle
    def start(self):
        self.knob.start()
        self.thread = threading.Thread(target=self._loop, name="menu-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.knob.stop()

    @property
    def s(self):
        return self.menu["settings"]

    @property
    def is_open(self):
        return self.active

    # --- called by the editor server
    def apply_menu(self, menu: dict, show: bool = True):
        with self.lock:
            self.menu = config.normalise(menu)
            self.knob.hold_ms = self.s["open_hold_ms"]
            self.stack = [self._home()]
            self._set_active(False)
            self._draw(full=True)

    def preview(self, menu: dict, screen: str = "home"):
        """Image of a screen for the editor preview (does not touch the keyboard)."""
        m = config.normalise(menu)
        s = m["settings"]
        if screen.startswith("item:"):
            it = self._find(m["items"], screen[5:])
            if it:
                if it["kind"] == "group":
                    return render.grid(it["children"], 0, it["name"], s)
                if len(it["options"]) > 1:
                    return render.options(it, it["options"], 0, s)
                idx = next((i for i, x in enumerate(m["items"]) if x is it), 0)
                return render.grid(m["items"], idx, "Menu", s)
        return render.grid(m["items"], 0, "Menu", s)

    @staticmethod
    def _find(items, item_id):
        for it in items:
            if it["id"] == item_id:
                return it
            hit = Engine._find(it.get("children") or [], item_id)
            if hit:
                return hit
        return None

    # --- state
    def _home(self):
        return {"type": "list", "items": self.menu["items"], "index": 0, "title": "Menu"}

    def _set_active(self, on: bool):
        self.active = on
        self.knob.menu_open = on
        self.pending_click = None

    def _frame_image(self):
        f = self.stack[-1]
        if f["type"] == "list":
            return render.grid(f["items"], f["index"], f["title"], self.s, self.active)
        if f["type"] == "options":
            return render.options(f["item"], f["item"]["options"], f["index"], self.s, f["top"], self.active)
        level, muted = actions.get_volume(f["item"])
        return render.volume(f["item"], level, muted, self.s)

    def _layout(self) -> bytes:
        """Layout record for the current screen: where the selectable frames are (the screen chip moves the
        frame itself on knob events while the menu drives the knob)."""
        f = self.stack[-1]
        acc, bg = render._rgb(self.s["accent"]), render._rgb(self.s["background"], (14, 17, 22))
        cells, index = [], 0
        if f["type"] == "list" and f["items"]:
            page = f["index"] // render.PER_PAGE
            n = min(render.PER_PAGE, len(f["items"]) - page * render.PER_PAGE)
            cells, index = render.grid_frames(n), f["index"] % render.PER_PAGE
        elif f["type"] == "options" and f["item"]["options"]:
            rows = min(4, len(f["item"]["options"]) - f["top"])
            cells, index = render.option_frames(rows), f["index"] - f["top"]
        on = self.active and bool(cells) and not self.error_until and bool(self.s.get("screen_nav"))
        return screen.layout_record(on, index, cells, render.FRAME_T, acc, bg, render.INACTIVE, wrap=self._wraps())

    def _wraps(self) -> bool:
        """The whole list is on the screen: the screen chip may wrap around by itself (firmware v16)."""
        f = self.stack[-1]
        if f["type"] == "list":
            return 0 < len(f["items"]) <= render.PER_PAGE
        if f["type"] == "options":
            return 0 < len(f["item"]["options"]) <= 4
        return False

    def _draw(self, full: bool = False):
        if full:
            self.out.show(self._frame_image(), full=True, layout=self._layout())
            self.dirty = False
        else:
            self.dirty = True

    def _to_volume_mode(self):
        self.stack = [self._home()]
        self._set_active(False)
        self._draw()

    def _error(self, text, sub, item=None):
        self.error_until = time.time() + 1.5
        self.out.show(render.message(text, sub, self.s, item), layout=self._layout())   # frames off

    # --- events
    def _event(self, ev):
        kind = ev[0]
        self.last_input = time.time()
        if self.error_until:
            self.error_until = 0.0
            self._draw()
        if kind == "hold":
            if not self.active:
                self._set_active(True)
                self._draw()
            elif len(self.stack) > 1:
                self.pending_click = None
                self.stack.pop()
                self._draw()
            else:
                self._to_volume_mode()
            return
        if not self.active:
            return
        if kind == "turn":
            gap = (ev[2] if len(ev) > 2 else time.time()) - self.last_turn_at
            if gap < 0.04:
                self.log(f"knob: two steps only {gap * 1000:.0f} ms apart (direction {ev[1]:+d})")
            self.last_turn_at = ev[2] if len(ev) > 2 else time.time()
            self.last_turn = time.time()
            self._turn(ev[1], ev[2] if len(ev) > 2 else time.time())
        elif kind == "click":
            now = time.time()
            if self.s.get("screen_nav"):
                self.out.taint(self._frame_bars())    # the screen chip flashes the frame on a press
            if not self.s["double_click_ms"]:
                self._select()                    # no double-press: act at once
            elif self.pending_click and now - self.pending_click <= self.s["double_click_ms"] / 1000:
                self.pending_click = None
                self.stack = [self._home()]       # double-press: first page
                self._draw()
            else:
                self.pending_click = now

    def _turn(self, step, when: float = 0.0):
        if self.s.get("screen_nav") and self.out.was_busy_at(when, margin=1.5):
            # The keyboard chip may hold knob messages for the screen while it relays an upload and deliver them
            # late, after our layout reset: re-send the position once the knob is quiet.
            self.resync_due = True
        f = self.stack[-1]
        if f["type"] == "list" and f["items"]:
            new = f["index"] + step
            same_page = (0 <= new < len(f["items"]) and new // render.PER_PAGE == f["index"] // render.PER_PAGE)                 or self._wraps()
            f["index"] = new % len(f["items"])
            if same_page and not self.dirty and self.s.get("screen_nav") and not self.out.was_busy_at(when):
                self._screen_moved()          # the screen chip moved the frame itself (knob event)
                return
        elif f["type"] == "options" and f["item"]["options"]:
            n = len(f["item"]["options"])
            new = f["index"] + step
            visible = (0 <= new < n and f["top"] <= new < f["top"] + 4) or self._wraps()
            f["index"] = new % n
            if f["index"] < f["top"]:
                f["top"] = f["index"]
            elif f["index"] >= f["top"] + 4:
                f["top"] = f["index"] - 3
            if visible and not self.dirty and self.s.get("screen_nav") and not self.out.was_busy_at(when):
                self._screen_moved()
                return
        elif f["type"] == "volume":
            actions.change_volume(f["item"], 0.05 * step)
        self._draw()

    def _frame_bars(self):
        """Pixel areas of every selectable frame on the current screen (where the screen chip draws)."""
        f, t = self.stack[-1], render.FRAME_T
        if f["type"] == "list" and f["items"]:
            n = min(render.PER_PAGE, len(f["items"]) - (f["index"] // render.PER_PAGE) * render.PER_PAGE)
            cells = render.grid_frames(n)
        elif f["type"] == "options" and f["item"]["options"]:
            cells = render.option_frames(min(4, len(f["item"]["options"]) - f["top"]))
        else:
            return []
        bars = []
        for x, y, w, h in cells:
            bars += [(x, y, x + w, y + t), (x, y + h - t, x + w, y + h), (x, y, x + t, y + h), (x + w - t, y, x + w, y + h)]
        return bars

    def _screen_moved(self):
        """The frame move was drawn by the screen chip from the keyboard's knob event: no upload, just keep
        our copy of the screen in step, and re-sync the position once the knob is quiet."""
        self.out.assume(self._frame_image(), self._layout())
        self.nav_moved_at = time.time()

    def _select(self):
        f = self.stack[-1]
        if f["type"] == "list":
            if not f["items"]:
                return
            it = f["items"][f["index"]]
            if it["kind"] == "group":
                self.stack.append({"type": "list", "items": it["children"], "index": 0, "title": it["name"]})
            elif len(it["options"]) == 1:
                self._run(it, it["options"][0])
                return
            elif it["options"]:
                self.stack.append({"type": "options", "item": it, "index": 0, "top": 0})
            else:
                self._error("No options", "add some in the editor", it)
                return
            self._draw()
        elif f["type"] == "options":
            self._run(f["item"], f["item"]["options"][f["index"]])
        elif f["type"] == "volume":
            actions.mute(f["item"])
            self._draw()

    def _run(self, item, opt):
        if opt["action"] == "volume":
            self.stack.append({"type": "volume", "item": item})
            self._draw()
            return
        try:
            status = actions.RUN[opt["action"]](item, opt.get("value", ""))
        except Exception as e:
            self.log(f"action {opt['action']} on {item['name']} failed: {e}")
            self._error("Failed", str(e)[:60], item)
            return
        self.log(f"{opt['label']} -> {item['name']}: {status}")
        self._draw()                          # no confirmation screen; the knob stays in menu mode

    # --- loop
    def _take_events(self) -> list:
        """Wait up to 0.1 s for a knob event, then take every other one already queued (fast turning
        becomes one redraw instead of one per detent)."""
        try:
            evs = [self.events.get(timeout=0.1)]
        except queue.Empty:
            return []
        while True:
            try:
                evs.append(self.events.get_nowait())
            except queue.Empty:
                return evs

    def _loop(self):
        try:
            import comtypes
            comtypes.CoInitialize()               # per-app audio uses COM on this thread
        except Exception:
            pass
        if self.s.get("set_clock_on_start"):
            t = self.out.set_clock()
            self.log(f"keyboard clock set to {t}" if t else "could not set the keyboard clock (not connected?)")
        with self.lock:
            self._draw(full=True)
        while not self._stop.is_set():
            evs = self._take_events()
            with self.lock:
                try:
                    for ev in evs:
                        self._event(ev)
                    now = time.time()
                    if self.pending_click and now - self.pending_click > self.s["double_click_ms"] / 1000:
                        self.pending_click = None
                        self._select()
                    if self.error_until and now > self.error_until:
                        self.error_until = 0.0
                        self._draw()
                    if self.active and self.s["idle_close_s"] and now - self.last_input > self.s["idle_close_s"]:
                        self._to_volume_mode()
                    if self.out.last is None:      # keyboard came back (replugged): redraw everything
                        self._draw(full=True)
                    if self.dirty:
                        self.dirty = False
                        self.nav_moved_at = 0.0
                        self.out.show(self._frame_image(), layout=self._layout())
                    if self.resync_due and not self.dirty and now - self.last_turn > 0.4:
                        self.resync_due = False
                        self.nav_moved_at = 0.0
                        self.out.sync(self._layout())     # frames repainted at our position
                    sync_ms = self.s.get("nav_sync_ms", 0)
                    if self.nav_moved_at and sync_ms and now - self.nav_moved_at > sync_ms / 1000:
                        self.nav_moved_at = 0.0
                        self.out.sync(self._layout())
                except Exception:
                    self.log("menu error:\n" + traceback.format_exc())
