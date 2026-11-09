"""Menu file (menu/menu.json next to kb.py): what the keyboard-screen menu shows and what each entry does.

{
  "version": 1,
  "settings": {...},                     # timings, colours (see DEFAULT_SETTINGS)
  "items": [Item, ...]
}
Item   = {"id", "kind", "name", "icon", "color", "target", "browser", "process", "options": [Option], "children": [Item]}
         kind: app | website | folder | file | command | group
         icon: file name inside menu/icons/ (PNG) or "" for a coloured letter tile
         target: exe / .lnk path (app), URL (website), path (folder/file), command line (command)
         browser: exe path to open a website with ("" = default browser)
         process: exe file name used to find the app's windows and audio (e.g. "chrome.exe"); "" = from target
Option = {"label", "action", "value"}
         action: launch | open | open_in | close | mute | volume | keys | command
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "menu"
MENU_FILE = ROOT / "menu.json"
ICON_DIR = ROOT / "icons"

KINDS = ("app", "website", "folder", "file", "command", "action", "group")
ACTIONS = {
    "launch": "Launch / switch to",
    "open": "Open",
    "open_in": "Open in browser...",
    "close": "Close",
    "mute": "Mute / unmute",
    "volume": "Volume (older live menu only)",
    "keys": "Send keys",
    "command": "Run command",
    "system": "Built-in action",
}
DEFAULT_SETTINGS = {
    "open_hold_ms": 700,        # knob held this long opens the menu (shorter = normal mute)
    "double_click_ms": 0,       # > 0: two presses within this many ms = first page (presses then wait that long)
    "idle_close_s": 0,          # > 0: knob goes back to volume after this long without input (an upload,
                                #      so one light blink); 0 = only when you hold the knob at the top
    "set_clock_on_start": True,
    "screen_nav": True,         # firmware v13+: the screen chip moves the frame itself on knob turns
    "nav_sync_ms": 0,         # > 0: this long after the last knob turn, re-send the frame position (one tiny
                                #      upload; guards against a missed knob event). 0 = never
    "accent": "#2EA8FF",
    "background": "#0E1116",
}


def default_options(kind: str) -> list[dict]:
    if kind == "app":
        return [{"label": "Launch / switch", "action": "launch", "value": ""},
                {"label": "Mute / unmute", "action": "mute", "value": ""},
                {"label": "Close", "action": "close", "value": ""}]
    if kind == "website":
        return [{"label": "Open", "action": "open", "value": ""}]
    if kind in ("folder", "file"):
        return [{"label": "Open", "action": "open", "value": ""}]
    if kind == "command":
        return [{"label": "Run", "action": "command", "value": ""}]
    if kind == "action":
        return [{"label": "Run", "action": "system", "value": "media_playpause"}]
    return []


def new_item(kind: str, name: str = "", target: str = "") -> dict:
    return {"id": uuid.uuid4().hex[:10], "kind": kind, "name": name or kind.title(), "icon": "", "color": "",
            "target": target, "browser": "", "process": "", "options": default_options(kind), "children": []}


def example_menu() -> dict:
    items = [new_item("website", "YouTube", "https://www.youtube.com"),
             new_item("folder", "Downloads", str(Path.home() / "Downloads")),
             new_item("command", "Calculator", "calc.exe")]
    items[0]["color"], items[1]["color"], items[2]["color"] = "#E53935", "#43A047", "#8E24AA"
    return {"version": 1, "settings": dict(DEFAULT_SETTINGS), "items": items}


def normalise(menu: dict) -> dict:
    """Fill in missing fields so older or hand-edited files keep working."""
    menu = dict(menu or {})
    menu["version"] = 1
    menu["settings"] = {**DEFAULT_SETTINGS, **(menu.get("settings") or {})}

    def fix(item: dict) -> dict:
        base = new_item(item.get("kind", "app") if item.get("kind") in KINDS else "app")
        base.update({k: v for k, v in item.items() if k in base})
        base["options"] = [{"label": str(o.get("label", "")), "action": o.get("action", "open"),
                            "value": str(o.get("value", ""))}
                           for o in base["options"] if o.get("action") in ACTIONS]
        base["children"] = [fix(c) for c in base.get("children") or []] if base["kind"] == "group" else []
        return base
    menu["items"] = [fix(i) for i in menu.get("items") or []]
    return menu


def load() -> dict:
    if not MENU_FILE.exists():
        return normalise(example_menu())
    return normalise(json.loads(MENU_FILE.read_text(encoding="utf-8")))


def save(menu: dict) -> dict:
    menu = normalise(menu)
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = MENU_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(menu, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, MENU_FILE)           # never leave a half-written menu file
    return menu


def icon_path(name: str) -> Path | None:
    if not name:
        return None
    p = (ICON_DIR / Path(name).name)     # only file names inside menu/icons/
    return p if p.exists() else None
