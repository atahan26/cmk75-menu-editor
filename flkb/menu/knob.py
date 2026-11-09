"""Knob gestures from a low-level keyboard hook. Only the knob's three keys are looked at:
Volume up / Volume down (turning) and Mute (pressing). Every other key passes straight through.

Menu closed: turning passes through (normal volume). Pressing is held back until release: a short press is
re-sent as a normal Mute, a press held for `hold_ms` opens the menu instead.
Menu open: all three keys are swallowed and become menu events.
Events (to on_event): ("turn", +1 | -1), ("click",), ("hold",)   [hold = open when closed, back when open]
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

from .actions import INJECTED_MARK

VK_MUTE, VK_DOWN, VK_UP = 0xAD, 0xAE, 0xAF
WH_KEYBOARD_LL, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP, WM_QUIT = 13, 0x100, 0x101, 0x104, 0x105, 0x12
LLKHF_INJECTED = 0x10

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class Knob:
    def __init__(self, on_event, hold_ms: int = 700):
        self.on_event = on_event
        self.hold_ms = hold_ms
        self.menu_open = False            # set by the menu engine
        self._down = False
        self._held = False
        self._timer = None
        self._thread = None
        self._tid = None
        self._hook = None
        self._proc = HOOKPROC(self._callback)    # keep a reference: the hook must outlive this call

    # --- hook (runs on the hook thread; must return quickly)
    def _callback(self, code, wparam, lparam):
        if code >= 0:
            k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if k.vkCode in (VK_MUTE, VK_DOWN, VK_UP) and not (k.flags & LLKHF_INJECTED and k.dwExtraInfo == INJECTED_MARK):
                down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if self._handle(k.vkCode, down):
                    return 1                  # swallowed
        return user32.CallNextHookEx(self._hook, code, wparam, lparam)

    def _handle(self, vk: int, down: bool) -> bool:
        if vk in (VK_UP, VK_DOWN):
            if not self.menu_open:
                return False
            if down:
                self._emit(("turn", 1 if vk == VK_UP else -1, time.time()))
            return True
        # the knob press (Mute): always held back, decided on release or after hold_ms
        if down:
            if not self._down:                # ignore auto-repeat
                self._down, self._held = True, False
                self._timer = threading.Timer(self.hold_ms / 1000, self._hold_fired)
                self._timer.daemon = True
                self._timer.start()
            return True
        if self._timer:
            self._timer.cancel()
        was_held, self._down = self._held, False
        if not was_held:
            if self.menu_open:
                self._emit(("click",))
            else:
                threading.Thread(target=self._send_mute, daemon=True).start()
        return True

    def _hold_fired(self):
        if self._down:
            self._held = True
            self._emit(("hold",))

    @staticmethod
    def _send_mute():
        user32.keybd_event(VK_MUTE, 0, 0, INJECTED_MARK)
        user32.keybd_event(VK_MUTE, 0, 2, INJECTED_MARK)

    def _emit(self, ev):
        self.on_event(ev)                 # the engine passes queue.put: fast, ordered, thread-safe

    # --- thread with the message loop the hook needs
    def start(self):
        ready = threading.Event()

        def run():
            self._tid = kernel32.GetCurrentThreadId()
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
            ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
            user32.UnhookWindowsHookEx(self._hook)
        self._thread = threading.Thread(target=run, name="knob-hook", daemon=True)
        self._thread.start()
        ready.wait(5)
        if not self._hook:
            raise RuntimeError("could not install the keyboard hook")

    def stop(self):
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
