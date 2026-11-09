"""CMK75 knob menu: runs the keyboard-screen menu and its browser editor.

usage: python menu_app.py [--no-browser] [--live]
  The knob always drives the menu on the keyboard screen: turn = move, press = choose, hold = first screen.
  Editor: http://127.0.0.1:8475. Needs the patched screen firmware v17+ (menu stored on the screen chip);
  --live: the older upload-per-change menu (firmware v11..v16). Stop with Ctrl+C.
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flkb.menu import config, server  # noqa: E402
from flkb.menu.engine import Engine  # noqa: E402
from flkb.menu.engine_stored import StoredEngine  # noqa: E402

LOG = config.ROOT / "menu.log"


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    try:
        config.ROOT.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-browser", action="store_true", help="do not open the editor in the browser")
    ap.add_argument("--live", action="store_true", help="older live-upload menu (screen firmware v11..v16)")
    args = ap.parse_args()

    if not config.MENU_FILE.exists():
        config.save(config.example_menu())
        log(f"created an example menu in {config.MENU_FILE}")
    engine = Engine(log) if args.live else StoredEngine(log)
    try:
        httpd = server.serve(engine, log)
    except OSError as e:
        log(f"cannot start the editor on port {server.PORT} (is the menu app already running?): {e}")
        return 1
    engine.start()
    url = f"http://127.0.0.1:{server.PORT}/"
    log(f"menu app running. Editor: {url}  Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("stopping")
    finally:
        engine.stop()
        httpd.shutdown()
        engine.out.release()
        engine.out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
