"""Writes for CMK75, each followed by a read-back check.

Packet layouts mirror tj.setLightSetting / tj.setSLEDParam / tj.setReportRate in the vendor bundle.
The lighting reply (0x87/0x88) uses the same byte layout as the write, so a dump's `raw`
bytes can be written back verbatim to restore.
"""
from __future__ import annotations

from . import reader
from .transport import Keyboard

MAIN, SIDE = 0x07, 0x08
READ_OF = {MAIN: 0x87, SIDE: 0x88}
NAME_TO_MODE = {v: k for k, v in reader.LIGHT_MODES.items()}
SIDE_NAME_TO_MODE = {v: k for k, v in reader.SIDE_LED_MODES.items()}
WAVE = {v: k for k, v in reader.WAVEOP.items()}
MUSIC = {v: k for k, v in reader.MP.items()}
# Modes whose vendor writer encodes a two-way option (ripple / press_action decode one but never write it).
WRITE_TWO_WAY = {"snake", "kaleidoscope", "line_wave", "circle_wave", "fireworks"}
LEVEL_RANGE = range(0, 5)  # brightness/speed 0..4; 4 is the largest value seen on this board (MAXSPEED=4)


class WriteFailed(Exception):
    pass


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) != 6:
        raise ValueError("color must be 'rainbow' or #RRGGBB")
    v = int(c, 16)
    if v == 0xFFFFFF:
        v = 0xFAFFFA  # the vendor app stores white this way
    return v >> 16 & 0xFF, v >> 8 & 0xFF, v & 0xFF


def encode_light(mode: str, brightness: int = 4, speed: int = 2, color: str = "rainbow",
                 option: str | None = None, side: bool = False) -> tuple[int, bytes]:
    """Return (command, 7 payload bytes) exactly as the vendor app builds them."""
    table = SIDE_NAME_TO_MODE if side else NAME_TO_MODE
    if mode not in table:
        raise ValueError(f"unknown {'side ' if side else ''}mode {mode!r}; choose from {sorted(table)}")
    if side and table[mode] >= 20:
        raise ValueError("side-LED music/screen modes are sent on the main lighting command by the "
                         "vendor app; not supported yet")
    if brightness not in LEVEL_RANGE or speed not in LEVEL_RANGE:
        raise ValueError("brightness and speed must be 0..4")
    mode_no = table[mode]
    dazzle = color == "rainbow"
    y = reader.DAZZLE if dazzle else reader.NORMAL      # colour slot
    c = 0 if dazzle else 4                              # colour flag used by music modes
    r, g, b = (0, 0, 0) if dazzle else _rgb(color)

    # Defaults as in the vendor writer: o (speed) = 0, u (brightness) = 0, i (option byte) = 7.
    o, u, i = 0, 0, 7
    if mode == "off":
        pass
    elif mode == "static":
        u, i = brightness, y
    elif mode == "neon":
        o, u = speed, brightness
    elif mode == "wave":
        o, u, i = speed, brightness, WAVE.get(option or "right", 0) << 4 | y
    elif mode in WRITE_TWO_WAY:
        first, _second = reader.TWO_WAY_OPTIONS[mode]
        o, u, i = speed, brightness, (0 if option in (None, first) else 16) | y
    elif mode.startswith("music_follow"):
        u, i = brightness, MUSIC.get(option or "upright", 0) << 4 | c
    elif mode == "screen_color":
        u = 4
    elif mode == "user_picture":
        u, i = brightness, {v: k for k, v in reader.USEROP.items()}.get(option or "1", 0)
        r, g, b = 0, 200, 200
    elif mode == "user_color":
        pass
    else:  # breath, ripple, raindrop, press_action, converge, sine_wave, laser, dazzling, ...
        o, u, i = speed, brightness, y

    cmd = SIDE if side else MAIN
    speed_byte = o if side else reader.MAXSPEED - o
    return cmd, bytes([mode_no, speed_byte, u, i, r, g, b])


def write_light_raw(kb: Keyboard, cmd: int, payload: bytes) -> bytes:
    """Write 7 lighting bytes and confirm the keyboard reports them back."""
    if cmd not in READ_OF or len(payload) != 7:
        raise ValueError("lighting writes take cmd 0x07/0x08 and 7 bytes")
    kb.write(cmd, payload, pre_delay_s=0.5, post_delay_s=0.5)
    back = kb.read(READ_OF[cmd])[1:8]
    if back != payload:
        raise WriteFailed(f"keyboard reports {list(back)} after writing {list(payload)}")
    return back


def set_light(kb: Keyboard, **kw) -> dict:
    cmd, payload = encode_light(**kw)
    write_light_raw(kb, cmd, payload)
    return reader.main_lighting(kb) if cmd == MAIN else reader.side_lighting(kb)


def restore_lighting(kb: Keyboard, dump: dict) -> dict:
    """Write the main and side lighting bytes from a dump back to the keyboard."""
    write_light_raw(kb, MAIN, bytes(dump["lighting"]["raw"]))
    write_light_raw(kb, SIDE, bytes(dump["side_lighting"]["raw"]))
    return {"lighting": reader.main_lighting(kb), "side_lighting": reader.side_lighting(kb)}


def set_clock(kb: Keyboard, when=None) -> str:
    """Set the clock page to `when` (default: PC local time now). Mirrors tj.setOLEDClock:
    packet bytes 8..14 = year high, year low, month, day, hour, minute, second."""
    import datetime as _dt
    t = when or _dt.datetime.now()
    p = bytearray(14)                 # packet bytes 1..14; byte 7 (index 6 here) is the checksum slot
    p[7], p[8] = t.year >> 8 & 0xFF, t.year & 0xFF
    p[9], p[10], p[11], p[12], p[13] = t.month, t.day, t.hour, t.minute, t.second
    kb.write(0x28, bytes(p), post_delay_s=0.05)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def set_report_rate(kb: Keyboard, hz: int, profile: int) -> int | str:
    code = {1000: 1, 500: 2, 250: 4, 125: 8}.get(hz)
    if code is None:
        raise ValueError("polling rate must be 1000, 500, 250 or 125")
    kb.write(0x04, bytes([profile, code]), post_delay_s=1.0)
    got = reader.report_rate(kb, profile)
    if got != hz:
        raise WriteFailed(f"keyboard reports {got} Hz after setting {hz} Hz")
    return got
