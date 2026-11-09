"""Start the knob menu app when you log in to Windows.

usage: python tools/autostart.py [install|remove|status]
  install: puts "CMK75 menu.lnk" in your Startup folder. It runs menu_app.py --no-browser with pythonw
           (no console window); the log goes to menu.log as usual.
  remove:  deletes that shortcut again.
The editor is still at http://127.0.0.1:8475/ while the app runs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
STARTUP = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
LINK = STARTUP / "CMK75 menu.lnk"


def install() -> None:
    if not PYTHONW.exists():
        raise SystemExit(f"not found: {PYTHONW}")
    q = lambda s: str(s).replace("'", "''")      # noqa: E731  (PowerShell single-quote escaping)
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('" + q(LINK) + "');"
        "$s.TargetPath = '" + q(PYTHONW) + "';"
        "$s.Arguments = '\"" + q(ROOT / "menu_app.py") + "\" --no-browser';"
        "$s.WorkingDirectory = '" + q(ROOT) + "';"
        "$s.Description = 'CMK75 knob menu';"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print(f"installed: {LINK}")


def remove() -> None:
    if LINK.exists():
        LINK.unlink()
        print(f"removed: {LINK}")
    else:
        print("not installed")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        install()
    elif cmd == "remove":
        remove()
    elif cmd == "status":
        print(f"installed: {LINK}" if LINK.exists() else "not installed")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
