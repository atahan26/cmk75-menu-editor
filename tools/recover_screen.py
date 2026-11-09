"""Bring the keyboard screen back when it is black and the knob only changes the volume.

usage: python tools/recover_screen.py [--force] [firmware.bin]
  Checks the screen chip (read-only). If it is stuck in its bootloader (update mode, no app running) it
  reflashes the known-good build (default: firmware/patched/firmwareOledFile_cmk75_v19_nonet.bin) and checks
  that the app runs again. If the app is already running it does nothing unless --force is given.
  Stops the menu app first (only one program may use the keyboard) and starts it again afterwards.
  The menu itself lives in a separate flash area and is not touched.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flkb import screen_flash as sf, transport  # noqa: E402

DEFAULT_FW = ROOT / "firmware" / "patched" / "firmwareOledFile_cmk75_v19_nonet.bin"
VID, PID = 0x3151, 0x4015


def menu_app_pids() -> list[int]:
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*menu_app.py*' } "
          "| ForEach-Object { $_.ProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    return [int(x) for x in out.split() if x.isdigit()]


def stop_menu_app() -> bool:
    pids = menu_app_pids()
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    if pids:
        time.sleep(2)
    return bool(pids)


def start_menu_app() -> None:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(pythonw), str(ROOT / "menu_app.py"), "--no-browser"], cwd=ROOT,
                     creationflags=0x08000000)       # CREATE_NO_WINDOW


def state(kb) -> str:
    """'app' (screen app running), 'bootloader' (update mode) or 'unknown'."""
    probe = sf.bootloader_probe(kb)[:2]
    if probe == b"\xb0\x01":
        return "bootloader"
    if probe == b"\xb0\x00":
        return "app"
    return "unknown"


def main() -> int:
    args = sys.argv[1:]
    force = "--force" in args
    files = [a for a in args if not a.startswith("--")]
    fw = Path(files[0]) if files else DEFAULT_FW
    if not fw.exists():
        print(f"firmware file not found: {fw}")
        return 1

    was_running = stop_menu_app()
    try:
        itf = transport.find_config_interface(VID, PID)
        if itf is None:
            print("keyboard not found: plug it into this PC (not the Mac) and run again")
            return 1
        kb = transport.Keyboard(itf)
        try:
            st = state(kb)
            print(f"screen chip: {st}")
            if st == "app" and not force:
                print("the screen app is running; nothing to do (use --force to reflash anyway)")
                return 0
            print(f"flashing {fw.name} ...")
            sf.flash_app(kb, fw.read_bytes(), i_understand_the_risk=True, log=lambda m: print(" ", m, flush=True))
            time.sleep(4)
            ver = sf.screen_version(kb)
            st = state(kb)
            print(f"after the flash: version {ver}, state {st}")
            if st != "app":
                print("the screen app is NOT running yet. Unplug the keyboard for 15 s and run this again.")
                return 1
            print("done: the screen app is running")
            return 0
        finally:
            kb.close()
    finally:
        if was_running:
            start_menu_app()
            print("menu app started again")


if __name__ == "__main__":
    raise SystemExit(main())
