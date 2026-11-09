"""Packet format for Rongyuan yc3121-family keyboards (FL·ESPORTS, Akko, MonsGeek, ...).

Derived from the vendor app's JS bundle (F. L Driver 2.1.70):
  * 64-byte feature report, sent with HID report ID 0 (65 bytes on the wire).
  * byte 0 = command (FEA_CMD_*). Commands >= 0x80 are reads (GET_*).
  * "BIT7" checksum: byte 7 = 255 - (sum(bytes 0..6) & 0xFF).
  * A read is: send the GET command as a feature report, wait, then get a feature report.
"""
from __future__ import annotations

PACKET_LEN = 64
REPORT_ID = 0

# Reads this tool is allowed to send in the read-only phase. Names are the vendor's.
SAFE_READS: dict[int, str] = {
    0x80: "GET_REV",            # firmware version
    0x83: "GET_BATTERY",
    0x84: "GET_REPORT",         # polling rate (this family uses 0x84)
    0x85: "GET_PROFILE",
    0x86: "GET_KBOPTION",
    0x87: "GET_LEDPARAM",
    0x88: "GET_SLEDPARAM",
    0x89: "GET_KEYMATRIX",
    0x8A: "GET_KEYENABLE",
    0x8B: "GET_MACRO",
    0x8F: "GET_INFOR",          # device info (model id)
    0x90: "GET_FN",
    0x91: "GET_DEBOUNCE",
    0x92: "GET_SLEEPTIME",
    0x97: "GET_AUTOOS_EN",
    0x9D: "GET_KEY_MAGNETISM_MODE",
    0x9F: "GET_MAGNETISM_FAST",
    0xA3: "GET_KEYSTROKE",
    0xAD: "GETOLED_VERSION",    # screen chip: program version + resource (flash) version
    0xE5: "GET_MAGNETISM_BY_ARR",
}

# Never sent by this tool, in any phase: flash erase, bootloader entry, firmware transfer.
FORBIDDEN: frozenset[int] = frozenset(
    {0x2C, 0xAC, 0x30, 0x31, 0x32, 0x40, 0x41, 0x43, 0x44, 0x7F,
     0xB0, 0xB1, 0xB2, 0xC0, 0xC1, 0xC3, 0xC4}
)


# Writes this tool may send, each with the checksum the vendor app uses for it.
# Grown one command at a time, after the matching read decoder is confirmed on the real board.
SAFE_WRITES: dict[int, tuple[str, str]] = {
    0x04: ("SET_REPORT", "bit7"),      # polling rate          (tj.setReportRate, checksum 0)
    0x07: ("SET_LEDPARAM", "bit8"),    # main lighting         (tj.setLightSetting, checksum 1)
    0x08: ("SET_SLEDPARAM", "bit8"),   # side lighting         (tj.setSLEDParam, checksum 1)
    0x28: ("SET_OLEDCLOCK", "bit7"),   # clock page time; RAM only, sent by the app on every connect
    0xA5: ("GETTFTLCDDATA", "bit7"),   # screen upload handshake: region + length (ABe.getTFTLCDDataRGBImg)
    0x25: ("SETTFTLCDDATA", "bit7"),   # screen pixel data, 56 bytes/packet (ABe.setTFTLCDDataRGBImg)
}


class UnsafeCommand(Exception):
    pass


def bit7_checksum(packet: bytes | bytearray) -> int:
    return 255 - (sum(packet[0:7]) & 0xFF)


def bit8_checksum(packet: bytes | bytearray) -> int:
    # Same scheme one byte later, used when bytes 5..7 carry an RGB colour.
    return 255 - (sum(packet[0:8]) & 0xFF)


def build(cmd: int, payload: bytes = b"", *, allow_writes: bool = False) -> bytes:
    """Return the 64-byte packet for `cmd`, with the vendor's checksum for that command."""
    if cmd in FORBIDDEN:
        raise UnsafeCommand(f"command 0x{cmd:02X} touches firmware/flash and is blocked")
    if cmd in SAFE_READS:
        checksum = "bit7"
    elif allow_writes and cmd in SAFE_WRITES:
        checksum = SAFE_WRITES[cmd][1]
    else:
        raise UnsafeCommand(f"command 0x{cmd:02X} is not on the allowed "
                            f"{'write' if allow_writes else 'read'} list")
    if len(payload) > PACKET_LEN - 1:
        raise ValueError("payload too long")
    pkt = bytearray(PACKET_LEN)
    pkt[0] = cmd
    pkt[1:1 + len(payload)] = payload
    if checksum == "bit7":
        pkt[7] = bit7_checksum(pkt)
    else:
        if len(payload) > 7:
            raise ValueError("bit8 packets carry at most 7 payload bytes")
        pkt[8] = bit8_checksum(pkt)
    return bytes(pkt)
