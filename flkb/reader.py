"""Read-only decoders for CMK75 (yc500_k247_soc, vendor class chain tIe -> ABe -> tj -> ... -> nE).

Every decoder mirrors a named method in the vendor bundle; the method is noted on each function.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

from . import hid_usage
from .transport import Keyboard

DATA = Path(__file__).resolve().parent / "data"

# ---- constants from the vendor class `tj` (most specific definition wins) ----
MAXSPEED = 4
NORMAL, DAZZLE = 7, 8          # color slot 7 = custom RGB, 8 = rainbow ("dazzle")
COMMONCOLOR = {0: 0xFF0000, 1: 0xFF8000, 2: 0xFFFF00, 3: 0x00FF00, 4: 0x00FFFF, 5: 0x0000FF, 6: 0xFF00FF}
WAVEOP = {0: "right", 1: "left", 2: "down", 3: "up"}
MP = {0: "upright", 1: "separate", 2: "intersect"}
USEROP = {0: "1", 16: "2", 32: "3", 48: "4", 64: "5"}

LIGHT_MODES = {
    0: "off", 1: "static", 2: "breath", 3: "neon", 4: "wave", 5: "ripple", 6: "raindrop",
    7: "snake", 8: "press_action", 9: "converge", 10: "sine_wave", 11: "kaleidoscope",
    12: "line_wave", 13: "user_picture", 14: "laser", 15: "circle_wave", 16: "dazzling",
    17: "rain_down", 18: "meteor", 19: "press_action_off", 20: "music_follow_3",
    21: "screen_color", 22: "music_follow_2", 23: "train", 24: "fireworks", 66: "user_color",
}
TWO_WAY_OPTIONS = {  # mode -> (option when high nibble == 0, otherwise)
    "neon": ("default", "random"), "ripple": ("full", "single"), "snake": ("z", "return"),
    "press_action": ("on_to_off", "off_to_on"), "kaleidoscope": ("out", "in"),
    "line_wave": ("right", "left"), "circle_wave": ("anti_clockwise", "clockwise"),
    "fireworks": ("right", "left"),
}
NO_COLOR = {"off", "neon", "user_picture", "screen_color", "user_color"}
SIDE_LED_MODES = {0: "off", 1: "static", 2: "breath", 3: "neon", 4: "wave", 5: "snake",
                  20: "music_follow_3", 21: "screen_color", 22: "music_follow_2"}

# English labels for the vendor's function-key names (function_keys.json is in Chinese).
FUNCTION_LABELS = {
    "上一曲": "Previous track", "下一曲": "Next track", "停止": "Stop", "播放/暂停": "Play/Pause",
    "播放器": "Media player", "火力": "Rapid fire", "静音": "Mute", "音量减": "Volume down",
    "音量加": "Volume up", "计算器": "Calculator", "fn": "Fn", "邮件": "Mail",
    "我的电脑": "My computer", "搜索": "Search", "主页": "Browser home", "亮度减": "Brightness down",
    "亮度加": "Brightness up", "呼出siri": "Siri", "压枪": "Recoil control",
    "开关压枪(持续)": "Recoil control (hold)", "开关压枪(切换)": "Recoil control (toggle)",
    "枪械切换加": "Weapon next", "枪械切换减": "Weapon previous", "枪械切换(循环)": "Weapon cycle",
    "切换输入法": "Switch input language", "缩小": "Zoom out", "放大": "Zoom in",
    "麦克风开关": "Mic toggle", "返回": "Back", "fn锁屏": "Fn lock", "锁屏": "Lock screen",
    "r_FN": "Right Fn", "音量<->键盘亮度": "Volume/brightness swap 1",
    "音量<->键盘亮度2": "Volume/brightness swap 2", "音量<->键盘亮度3": "Volume/brightness swap 3",
    "刷新": "Refresh", "程序打开1": "Open program 1", "程序打开2": "Open program 2",
    "程序打开3": "Open program 3",
}
FUNCTION_TYPES = {2, 3, 6, 8, 10, 13, 14, 18, 19, 22}
# Firmware functions missing from the vendor tables. Sources: testing on the real CMK75 (2026-11-03)
# and the CMK75 manual (FCC ID 2A4V8-CK247), matched to the codes found in the Fn layer.
BUILTIN_LABELS = {
    (19, 0, 0, 0): "Screen: next page",          # Fn+Insert (tested): picture -> animation -> clock
    (19, 3, 0, 0): "Screen: colour test",        # Fn+knob press (tested): red, green, blue, white
    (19, 2, 0, 0): "Screen: on/off",             # Fn+\ (manual); user saw the key lights flash
    (14, 0, 0, 0): "Bluetooth device 1 (hold 3 s: pair)",
    (14, 0, 1, 0): "Bluetooth device 2 (hold 3 s: pair)",
    (14, 0, 2, 0): "Bluetooth device 3 (hold 3 s: pair)",
    (14, 1, 0, 0): "2.4G: pair receiver",
    (13, 6, 0, 0): "Lighting: custom slot 1",
    (13, 6, 1, 0): "Lighting: custom slot 2",
    (13, 6, 2, 0): "Lighting: custom slot 3",
    (13, 6, 128, 0): "Lighting: save recorded custom",
    (13, 2, 1, 0): "Lighting: brightness up",
    (13, 2, 2, 0): "Lighting: brightness down",
    (13, 3, 1, 0): "Lighting: speed slower",
    (13, 3, 2, 0): "Lighting: speed faster",
    (13, 1, 0, 0): "Lighting: colour switch",
    (13, 1, 3, 1): "Lighting: static",
    (10, 5, 3, 0): "Lighting: mode switch",
    (10, 3, 0, 0): "Win key lock",
    (10, 8, 0, 0): "Battery level query (Bluetooth)",
}
MACRO_MODES = {0: "repeat_times", 1: "on_off", 2: "touch_repeat"}


def _load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _function_names() -> dict[tuple, str]:
    # Some codes have two vendor names (e.g. [10,13,0,0] is both "Fn lock" and a volume swap); keep both.
    names: dict[tuple, list[str]] = {}
    for k, v in _load("function_keys.json").items():
        names.setdefault(tuple(v), []).append(FUNCTION_LABELS.get(k, k))
    names.setdefault((10, 13, 0, 0), []).insert(0, "Fn lock")  # fn锁屏 is defined outside oL/uL
    return {code: " / ".join(dict.fromkeys(n)) for code, n in names.items()}


_FUNCS = _function_names()
LAYOUT = [list(s) for s in _load("layout_cmk75.json")["slots"]]


# ---------------------------------------------------------------- simple reads
def firmware_version(kb: Keyboard) -> int:            # tj.getFirmwareVersion (0x80)
    return struct.unpack_from("<H", kb.read(0x80), 1)[0]


def model_id(kb: Keyboard) -> int:                     # driver device id (0x8F)
    return struct.unpack_from("<I", kb.read(0x8F), 1)[0]


def profile_and_mode(kb: Keyboard) -> dict:            # tP.getCurrentProfile / tP.getMode (0x85)
    r = kb.read(0x85, bytes([0]))
    return {"profile": r[1], "mode": "normal" if r[2] == 0 else "gaming", "raw_mode": r[2]}


def report_rate(kb: Keyboard, profile: int) -> int | str:   # tj.getReportRate (0x84)
    r = kb.read(0x84, bytes([profile]))
    return {1: 1000, 2: 500, 4: 250, 8: 125}.get(r[2], f"unknown({r[2]})")


def keyboard_options(kb: Keyboard, profile: int) -> dict:   # tj.getKeyboardOption (0x86)
    r = kb.read(0x86, bytes([profile]))
    a, b, c = r[2], r[3], r[4]
    return {
        "win_key_lock": bool(a & 1), "system": "mac" if a & 2 else "win",
        "wasd_arrow_swap": bool(a & 8), "main_led_off": bool(a & 16), "side_led_off": bool(a & 32),
        "keyboard_mode": bool(a & 64), "keyboard_lock": bool(a & 128),
        "fn_key_matrix": bool(b & 1), "power_save": bool(c), "raw": [a, b, c],
    }


def debounce(kb: Keyboard) -> int:                     # tj.getDeBounce (0x91)
    return kb.read(0x91)[2]


def sleep_times(kb: Keyboard) -> dict:                 # tj.getSleepTime (0x92), raw seconds
    r = kb.read(0x92)
    u16 = lambda i: r[i + 1] << 8 | r[i]
    return {"bluetooth_s": u16(1), "wireless_2_4g_s": u16(3),
            "deep_bluetooth_s": u16(5), "deep_2_4g_s": u16(7)}


def auto_os(kb: Keyboard) -> bool:                     # ABe.getAutoOsen (0x97)
    return bool(kb.read(0x97)[1])


# ---------------------------------------------------------------- lighting
def _color(c: int, rgb: int):
    if rgb == 0xFAFFFA:
        rgb = 0xFFFFFF
    if c == DAZZLE:
        return "rainbow"
    if c != NORMAL and c in COMMONCOLOR:
        rgb = COMMONCOLOR[c]
    return f"#{rgb:06X}"


def _light(r: bytes, modes: dict, speed_inverted: bool) -> dict:
    mode_no, speed, bright, opt_byte = r[1], r[2], r[3], r[4]
    rgb = r[5] << 16 | r[6] << 8 | r[7]
    c, hi = opt_byte & 0x0F, opt_byte >> 4
    mode = modes.get(mode_no, f"unknown({mode_no})")
    out = {"mode": mode, "brightness": bright,
           "speed": (MAXSPEED - speed) if speed_inverted else speed}
    if mode in TWO_WAY_OPTIONS:
        out["option"] = TWO_WAY_OPTIONS[mode][0 if hi == 0 else 1]
    elif mode == "wave":
        out["option"] = WAVEOP.get(hi, "right")
    elif mode.startswith("music_follow"):
        out["option"] = MP.get(hi, "upright")
        out["color"] = "rainbow" if c == 0 else f"#{rgb:06X}"
    elif mode == "user_picture":
        out["option"] = USEROP.get(opt_byte, "1")
    if mode not in NO_COLOR and "color" not in out:
        out["color"] = _color(c, rgb)
    out["raw"] = list(r[1:8])
    return out


def main_lighting(kb: Keyboard) -> dict:               # tj.getLightSetting (0x87)
    return _light(kb.read(0x87), LIGHT_MODES, speed_inverted=True)


def side_lighting(kb: Keyboard) -> dict:               # tj.getSLEDParam (0x88)
    return _light(kb.read(0x88), SIDE_LED_MODES, speed_inverted=False)


# ---------------------------------------------------------------- keymap
def _paged(kb: Keyboard, cmd: int, index: int, pages: int) -> bytes:
    return b"".join(kb.read(cmd, bytes([index, p])) for p in range(pages))


def describe(code: list[int]) -> dict:                 # mirrors the vendor decoder DL
    t, m, k, k2 = code
    if code == [0, 0, 0, 0]:
        return {"type": "disabled"}
    if code == [0, 0, 1, 0]:
        return {"type": "unknown", "raw": code}
    if t == 0:
        if tuple(code) in _FUNCS:
            return {"type": "function", "name": _FUNCS[tuple(code)]}
        parts = ([hid_usage.MODIFIERS.get(m, f"mod 0x{m:02X}")] if m else []) + [hid_usage.name(k)]
        if k2:
            parts.append(hid_usage.name(k2))
        return {"type": "key" if len(parts) == 1 else "combo", "name": "+".join(parts)}
    if t == 1:
        return {"type": "mouse", "raw": code}
    if t in FUNCTION_TYPES:
        if tuple(code) in _FUNCS:
            return {"type": "function", "name": _FUNCS[tuple(code)]}
        if tuple(code) in BUILTIN_LABELS:
            return {"type": "builtin", "name": BUILTIN_LABELS[tuple(code)]}
        return {"type": "builtin", "raw": code}  # firmware function the vendor tables don't name
    if t == 9:
        return {"type": "macro", "macro_index": k, "macro_mode": MACRO_MODES.get(m, f"mode {m}")}
    if t == 21:
        return {"type": "gamepad", "raw": code}
    return {"type": "unknown", "raw": code}


def _label(d: dict) -> str:
    return d.get("name") or (f"macro #{d['macro_index']} ({d['macro_mode']})" if d["type"] == "macro"
                             else d["type"] if "raw" not in d else f"{d['type']} {d['raw']}")


def _decode_matrix(buf: bytes, fn_layer: bool = False) -> list[dict]:
    """Main layer: `changed` = differs from the CMK75 default.
    Fn layer: there is no default to compare with, so `assigned` = has an Fn action."""
    rows = []
    for slot, default in enumerate(LAYOUT):
        cur = list(buf[slot * 4: slot * 4 + 4])
        if default == [0, 0, 0, 0] and cur == [0, 0, 0, 0]:
            continue  # empty position in this layout
        d = describe(cur)
        row = {"slot": slot, "key": _label(describe(default)), "current": _label(d), "raw": cur}
        if fn_layer:
            row["assigned"] = cur != [0, 0, 0, 0]
        else:
            row["changed"] = cur != default
        if d["type"] == "macro":
            row["macro_index"] = d["macro_index"]
        rows.append(row)
    return rows


def keymap(kb: Keyboard, profile: int) -> list[dict]:  # tP._getKeyMatrix (0x89 x 8 pages)
    return _decode_matrix(_paged(kb, 0x89, profile, 8))


def fn_layer(kb: Keyboard, index: int = 0) -> list[dict]:   # nE._getFnKeyMatrix (0x90 x 8 pages)
    return _decode_matrix(_paged(kb, 0x90, index, 8), fn_layer=True)


# ---------------------------------------------------------------- macros
def macro(kb: Keyboard, index: int) -> dict:           # zE.getMacro (0x8B x 4) + nE.buffToMacroEvents
    buf = b"".join(kb.read(0x8B, bytes([index, p])) for p in range(4))
    events, repeat = [], struct.unpack_from("<H", buf, 0)[0]
    a, n = 6, buf[2:6]
    while a < len(buf):
        if n[0] == 249:
            events.append({"type": "mouse_move", "dx": n[2], "dy": n[3]})
            if n[1]:
                delay = n[1] >> 1
            else:
                delay = struct.unpack_from("<H", buf, a)[0]
                a += 2
        else:
            action = "down" if n[1] & 0x80 else "up"
            if 4 <= n[0] <= 239:
                events.append({"type": "key", "action": action, "key": hid_usage.name(n[0])})
            else:
                events.append({"type": "mouse_button", "action": action, "raw": n[0]})
            if n[1] & 0x7F:
                delay = n[1] & 0x7F
                a -= 2
            else:
                delay = struct.unpack_from("<H", n, 2)[0]
        if delay:
            events.append({"type": "delay_ms", "value": delay})
        n = buf[a:a + 4]
        if n == b"\x00\x00\x00\x00" or len(n) < 4:
            break
        a += 4
    return {"index": index, "repeat_count": repeat, "events": events}


# ---------------------------------------------------------------- everything
def dump(kb: Keyboard) -> dict:
    pm = profile_and_mode(kb)
    p = pm["profile"]
    km = keymap(kb, p)
    fn = fn_layer(kb, 0)
    macro_ids = sorted({r["macro_index"] for r in km + fn if "macro_index" in r})
    return {
        "model_id": model_id(kb),
        "firmware_version": firmware_version(kb),
        **pm,
        "report_rate_hz": report_rate(kb, p),
        "keyboard_options": keyboard_options(kb, p),
        "debounce": debounce(kb),
        "sleep": sleep_times(kb),
        "auto_os_detect": auto_os(kb),
        "lighting": main_lighting(kb),
        "side_lighting": side_lighting(kb),
        "keymap": km,
        "fn_layer": fn,
        "macros": [macro(kb, i) for i in macro_ids],
    }
