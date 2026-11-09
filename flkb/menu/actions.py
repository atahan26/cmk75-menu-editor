"""What menu options do on the PC. Every function returns a short status text for the keyboard screen."""
from __future__ import annotations

import ctypes
import os
import shlex
import subprocess
from ctypes import wintypes
from pathlib import Path

import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
WM_CLOSE, SW_RESTORE, GW_OWNER = 0x0010, 9, 4
INJECTED_MARK = 0xF1CB75                  # dwExtraInfo of keys we inject (our knob hook lets them through)


def process_name(item: dict) -> str:
    """exe name used to find an app's windows and audio session."""
    if item.get("process"):
        return item["process"].lower()
    target = item.get("target", "")
    if target.lower().endswith(".lnk"):
        try:
            from .winapps import real_process, resolve_lnk
            info = resolve_lnk(target)
            return real_process(info["target"], info["args"]).lower()
        except Exception:
            return ""
    if Path(target).name.lower() == "update.exe":           # Squirrel app saved by its target path
        return ""
    return Path(target).name.lower() if target.lower().endswith(".exe") else ""


def _pids(exe: str) -> set[int]:
    return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == exe}


def app_windows(exe: str) -> list[int]:
    pids = _pids(exe)
    found = []

    @EnumWindowsProc
    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER) \
                and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True
    if pids:
        user32.EnumWindows(cb, 0)
    return found


def _focus(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows only lets the foreground app hand over focus; a synthetic Alt tap is the usual way around that.
    user32.keybd_event(0x12, 0, 0, INJECTED_MARK)
    user32.keybd_event(0x12, 0, 2, INJECTED_MARK)
    user32.SetForegroundWindow(hwnd)


def _start(target: str, args: str = "") -> None:
    if args:
        subprocess.Popen([target, *shlex.split(args, posix=False)], close_fds=True)
    else:
        os.startfile(target)


def launch(item: dict, value: str = "") -> str:
    exe = process_name(item)
    wins = app_windows(exe) if exe else []
    if wins:
        _focus(wins[0])
        return "Switched to"
    _start(item["target"], value)
    return "Launching"


def open_target(item: dict, value: str = "") -> str:
    target = value or item.get("target", "")
    if item["kind"] == "website":
        if "://" not in target:
            target = "https://" + target
        if item.get("browser"):
            subprocess.Popen([item["browser"], target], close_fds=True)
        else:
            os.startfile(target)
        return "Opening"
    os.startfile(target)
    return "Opening"


def open_in(item: dict, value: str = "") -> str:
    url = item.get("target", "")
    if "://" not in url:
        url = "https://" + url
    if not value:
        os.startfile(url)
    else:
        subprocess.Popen([value, url], close_fds=True)
    return "Opening"


def close(item: dict, value: str = "") -> str:
    exe = process_name(item)
    wins = app_windows(exe) if exe else []
    for hwnd in wins:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)       # a polite close: the app may still ask to save
    return "Closing" if wins else "Not running"


# --- per-app audio (Windows audio sessions) -----------------------------------------------------------------
def _sessions(exe: str):
    from pycaw.pycaw import AudioUtilities
    out = []
    for s in AudioUtilities.GetAllSessions():
        try:
            if s.Process and s.Process.name().lower() == exe:
                out.append(s.SimpleAudioVolume)
        except psutil.Error:
            continue
    return out


def get_volume(item: dict):
    """(level 0..1, muted) of the app's audio, or (None, False) if it has no audio session."""
    vols = _sessions(process_name(item))
    if not vols:
        return None, False
    return vols[0].GetMasterVolume(), bool(vols[0].GetMute())


def change_volume(item: dict, delta: float):
    vols = _sessions(process_name(item))
    if not vols:
        return None, False
    level = min(1.0, max(0.0, vols[0].GetMasterVolume() + delta))
    for v in vols:
        v.SetMasterVolume(level, None)
    return level, bool(vols[0].GetMute())


def mute(item: dict, value: str = "") -> str:
    vols = _sessions(process_name(item))
    if not vols:
        return "No audio"
    new = 0 if vols[0].GetMute() else 1
    for v in vols:
        v.SetMute(new, None)
    return "Muted" if new else "Unmuted"


# --- keys and commands ----------------------------------------------------------------------------------------
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B,
      "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25,
      "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "playpause": 0xB3,
      "next": 0xB0, "prev": 0xB1, "mute": 0xAD, "volup": 0xAF, "voldown": 0xAE, "printscreen": 0x2C}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})


def send_keys(item: dict, value: str = "") -> str:
    """value like "ctrl+shift+m": press all keys in order, release in reverse."""
    keys = []
    for part in value.lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in VK:
            keys.append(VK[part])
        elif len(part) == 1:
            code = user32.VkKeyScanW(ord(part)) & 0xFF
            keys.append(code)
        else:
            return f"Unknown key: {part}"
    # Like a person pressing a shortcut: keys go down one after another, stay held a moment, then come up.
    # Real scan codes too: apps such as Discord read keybinds at a level where scan code 0 or a combo that
    # lasts microseconds is missed.
    import time as _t
    EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C}
    for k in keys:
        flags = 1 if k in EXTENDED else 0                       # KEYEVENTF_EXTENDEDKEY
        user32.keybd_event(k, user32.MapVirtualKeyW(k, 0) & 0xFF, flags, INJECTED_MARK)
        _t.sleep(0.02)
    _t.sleep(0.06)
    for k in reversed(keys):
        flags = (1 if k in EXTENDED else 0) | 2                 # | KEYEVENTF_KEYUP
        user32.keybd_event(k, user32.MapVirtualKeyW(k, 0) & 0xFF, flags, INJECTED_MARK)
        _t.sleep(0.02)
    return "Sent"


def run_command(item: dict, value: str = "") -> str:
    cmd = value or item.get("target", "")
    subprocess.Popen(cmd, shell=True, close_fds=True)       # the user's own command from their own menu file
    return "Started"


RUN = {"launch": launch, "open": open_target, "open_in": open_in, "close": close, "mute": mute,
       "keys": send_keys, "command": run_command}


# --- built-in actions (option "system", value = key below) --------------------------------------------------
def _mic_toggle() -> str:
    from pycaw.pycaw import AudioUtilities
    mic = AudioUtilities.GetMicrophone()
    if mic is None:
        return "No microphone"
    ep = getattr(mic, "EndpointVolume", None)
    if ep is None:                                   # older pycaw: raw IMMDevice
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume
        ep = cast(mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    new = 0 if ep.GetMute() else 1
    ep.SetMute(new, None)
    return "Mic muted" if new else "Mic on"


def _keys(combo: str):
    return lambda: send_keys({}, combo)


def _open(uri: str):
    def run():
        os.startfile(uri)
        return "Opening"
    return run


def _lock():
    user32.LockWorkStation()
    return "Locked"


def _sleep():
    ctypes.WinDLL("powrprof").SetSuspendState(0, 1, 0)
    return "Sleeping"


SYSTEM = {   # key: (label shown in the editor, function)
    "media_playpause": ("Media: play / pause", _keys("playpause")),
    "media_next": ("Media: next track", _keys("next")),
    "media_prev": ("Media: previous track", _keys("prev")),
    "volume_up": ("System volume up", _keys("volup")),
    "volume_down": ("System volume down", _keys("voldown")),
    "volume_mute": ("System mute", _keys("mute")),
    "mic_mute": ("Microphone mute / unmute", _mic_toggle),
    "screenshot": ("Screenshot (snip)", _keys("win+shift+s")),
    "show_desktop": ("Show desktop", _keys("win+d")),
    "explorer": ("File Explorer", _keys("win+e")),
    "task_manager": ("Task Manager", _keys("ctrl+shift+esc")),
    "lock": ("Lock the PC", _lock),
    "sleep": ("Sleep", _sleep),
    "settings_sound": ("Settings: Sound", _open("ms-settings:sound")),
    "settings_display": ("Settings: Display", _open("ms-settings:display")),
    "settings_bluetooth": ("Settings: Bluetooth", _open("ms-settings:bluetooth")),
}


def system(item: dict, value: str = "") -> str:
    if value not in SYSTEM:
        return "Unknown action"
    return SYSTEM[value][1]() or "Done"


RUN["system"] = system
