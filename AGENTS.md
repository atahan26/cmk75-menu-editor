# AGENTS.md: guide for AI coding agents

This file is for any coding agent (Claude Code, Codex, Cursor, Copilot, ...) working in this repository.
Read `README.md` for the user-facing description. Everything below is what you need to change the code safely.

## What this is
A Windows-only Python tool. A browser editor builds a menu; the menu is rendered as 240x135 images and written
to the screen chip of a CMK75 keyboard; the knob drives the menu and the PC runs the chosen action.
Python 3.11+, no test suite, no build step. Dependencies: `requirements.txt`.

## Run
```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe menu_app.py --no-browser    # editor on http://127.0.0.1:8475/
```
Without a connected keyboard the editor and preview still work; hardware calls will fail or be logged.

## Map
- `menu_app.py`: entry point, argument parsing, start/stop of engine and server.
- `flkb/menu/config.py`: menu schema (`KINDS`, `ACTIONS`, defaults, `example_menu()`), load/save of `menu/menu.json`.
- `flkb/menu/actions.py`: executes an option (launch, open, keys, volume, mute, command, built-in system actions).
- `flkb/menu/engine_stored.py`, `engine.py`: menu logic (stored-on-chip mode and the older live mode).
- `flkb/menu/render.py`: draws the screen images with Pillow.
- `flkb/menu/server.py` and `flkb/menu/web/`: editor HTTP API (port 8475, localhost only) and the page.
- `flkb/menu/winapps.py`, `knob.py`, `screen_out.py`, `stored.py`: Windows app discovery, knob input, screen output, flash layout.
- `flkb/protocol.py`, `transport.py`, `reader.py`, `writer.py`, `screen.py`, `screen_flash.py`: keyboard HID protocol and flashing.
- `tools/`: `recover_screen.py` (reflash a stuck screen chip), `build_patch.py` (patch a vendor screen firmware), `autostart.py`.

## Common tasks
- **New action type:** add it to `ACTIONS` in `config.py`, implement it in `actions.py`, and expose it in
  `flkb/menu/web/app.js` (option dropdown). Keep the option shape `{"label", "action", "value"}`.
- **New entry kind:** add it to `KINDS` in `config.py`, handle it in `actions.py`, `render.py` and the editor's add dialog.
- **UI change:** edit the three files in `flkb/menu/web/`; no build tool is involved.

## Safety rules (follow these)
1. **Never flash or write to the keyboard without the user's explicit confirmation.** That includes
   `tools/recover_screen.py`, the output of `tools/build_patch.py`, and anything calling `screen_flash`.
   Flashing can brick a screen chip.
2. `flkb/protocol.py` blocks commands that touch firmware or flash (`UnsafeCommand`). Do not weaken or bypass that guard.
3. Prefer read-only checks first. Only one program may use the keyboard at a time, so stop `menu_app.py` before other tools.
4. **Never commit** vendor firmware (`*.bin`), disassemblies, flash dumps, `devices.json`, `menu/` (personal paths, icons)
   or `.env`. They are in `.gitignore`; keep it that way.
5. The editor server must stay bound to `127.0.0.1`. It can launch programs, so never expose it on the network.
6. Do not add telemetry or network calls. The tool is meant to run fully locally.

## Style
Small, readable Python with type hints and `from __future__ import annotations`. Match the surrounding code.
Windows paths and PowerShell are expected; do not add cross-platform shims unless asked.
