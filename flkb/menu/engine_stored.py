"""Menu engine for the stored menu (screen firmware v17+).

The menu lives in the screen chip's flash and the screen chip navigates it by itself (knob turn = move, past the
last cell = next screen, press = open a group / option list / Back). The knob always drives the menu (no volume
mode; volume can be a menu entry). This engine only:
  - writes the menu into the screen's flash when it changed (editor Upload, or at start),
  - follows the position from the same knob keys and runs the action of a pressed entry,
  - hold = back to the first screen (one tiny upload).
No uploads while navigating, so the keyboard lights are left alone.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback

from .. import transport
from . import actions, config, stored
from .knob import Knob
from .screen_out import PID, VID, ScreenOut


class StoredEngine:
    def __init__(self, log=print):
        self.log = log
        self.menu = config.load()
        self.events: queue.Queue = queue.Queue()
        self.out = ScreenOut(log)
        self.knob = Knob(self.events.put, self.s["open_hold_ms"])
        self.knob.menu_open = True            # menu only: turning and pressing always belong to the menu
        self.pages = stored.build(self.menu)
        self.cursor = stored.Cursor(self.pages)
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self.thread = None
        self.shown = False                    # the screen shows our menu (False after a replug / at start)
        self.last_turn_at = 0.0
        self.next_check = 0.0

    @property
    def s(self):
        return self.menu["settings"]

    @property
    def is_open(self):
        return True

    # --- lifecycle
    def start(self):
        self.knob.start()
        self.thread = threading.Thread(target=self._loop, name="menu-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.knob.stop()

    # --- keyboard
    def _with_kb(self, what: str, fn) -> bool:
        with self.out.lock:
            try:
                fn(self.out._connect())
                return True
            except Exception as e:
                if "not found" not in str(e) and "not available" not in str(e):
                    self.log(f"{what} failed: {e}")
                self.out._drop(e)
                self.shown = False
                return False

    def _write(self, force: bool = False) -> bool:
        ok = self._with_kb("writing the menu to the screen",
                           lambda kb: stored.write_menu(kb, self.pages, self.s, force=force, log=self.log))
        if ok:
            self.cursor.go(0, 0)
            self.shown = True
        return ok

    def _home(self) -> bool:
        ok = self._with_kb("showing the menu", lambda kb: stored.show(kb, 0, 0))
        if ok:
            self.cursor.go(0, 0)
            self.shown = True
        return ok

    # --- called by the editor server
    def apply_menu(self, menu: dict, show: bool = True):
        with self.lock:
            self.menu = config.normalise(menu)
            self.knob.hold_ms = self.s["open_hold_ms"]
            self.pages = stored.build(self.menu)
            self.cursor = stored.Cursor(self.pages)
            self._write()

    def preview(self, menu: dict, screen: str = "home"):
        m = config.normalise(menu)
        pages = stored.build(m)
        page, cell = stored.locate(pages, screen[5:]) if screen.startswith("item:") else (0, 0)
        return stored.picture(pages[page], cell, m["settings"])

    # --- events
    def _event(self, ev):
        kind = ev[0]
        if kind == "turn":
            when = ev[2] if len(ev) > 2 else time.time()
            if when - self.last_turn_at < 0.04:
                self.log(f"knob: two steps only {(when - self.last_turn_at) * 1000:.0f} ms apart")
            self.last_turn_at = when
            self.cursor.turn(ev[1])
        elif kind == "click":
            act = self.cursor.press()
            if act:
                self._run(*act)
        elif kind == "hold":
            self._home()

    def _run(self, item, opt):
        try:
            status = actions.RUN[opt["action"]](item, opt.get("value", ""))
            self.log(f"{opt['label']} -> {item['name']}: {status}")
        except Exception as e:
            self.log(f"action {opt['action']} on {item['name']} failed: {e}")

    def _check_plugged(self):
        """Every 2 s: is the keyboard still in the device list? (Only lists USB devices; nothing is sent, so
        the lights are not touched.) Unplugged -> forget the connection; it is redrawn when it comes back."""
        now = time.time()
        if now < self.next_check:
            return
        self.next_check = now + 2
        if self.out.kb is not None and transport.find_config_interface(VID, PID) is None:
            self.log("keyboard unplugged")
            with self.out.lock:
                self.out._drop("unplugged")
            self.shown = False

    # --- loop
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
            self._write()                         # only changed sectors (usually none), then the first screen
        while not self._stop.is_set():
            try:
                ev = self.events.get(timeout=0.5)
            except queue.Empty:
                ev = None
            with self.lock:
                try:
                    if ev is not None:
                        self._event(ev)
                    self._check_plugged()
                    if not self.shown:
                        self._write()             # keyboard (re)connected: the screen chip restarted
                except Exception:
                    self.log("menu error:\n" + traceback.format_exc())
