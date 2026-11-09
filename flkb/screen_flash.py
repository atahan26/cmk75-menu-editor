"""Screen-chip (RY5050) firmware update through the keyboard chip - mirrors the vendor app's ABe.oledUpgrade.

THIS IS THE ONLY MODULE ALLOWED TO SEND THE SCREEN-UPDATE COMMANDS (0x30, 0xB0, 0x31, 0xB1 and raw data
packets). Everything else in flkb keeps them blocked through protocol.FORBIDDEN.

Sequence (vendor app, confirmed in the bundle):
  1. 0x30 55 AA 55 AA            ask the screen app to restart into its bootloader          (BIT7 checksum)
  2. 0xB0 (poll, up to 5 x 3 s)  until the reply starts with 0xB0 = bootloader ready          (BIT7)
  3. 0x31 count16                number of 64-byte packets that follow                       (BIT7)
  4. raw 64-byte packets          firmwareOledFile[0x10000:] (the bootloader part is never sent) (no checksum)
  5. 0xB1 sum32                  32-bit sum of every byte sent; success if reply[1] == 0x55  (BIT7)
"""
from __future__ import annotations

import struct
import time

from . import protocol
from .transport import Keyboard

APP_OFFSET = 0x10000              # the vendor sends file[65536:]
PACKET = 64


class FlashError(Exception):
    pass


def _feature(kb: Keyboard, packet64: bytes) -> None:
    sent = kb.dev.send_feature_report(bytes([protocol.REPORT_ID]) + packet64)
    if sent < 0:
        raise FlashError(f"send_feature_report failed: {kb.dev.error()}")


def _command(kb: Keyboard, body: bytes) -> None:
    pkt = bytearray(PACKET)
    pkt[:len(body)] = body
    pkt[7] = protocol.bit7_checksum(pkt)
    _feature(kb, bytes(pkt))


def _reply(kb: Keyboard) -> bytes:
    r = bytes(kb.dev.get_feature_report(protocol.REPORT_ID, PACKET + 1))
    return r[1:] if len(r) == PACKET + 1 else r


def screen_version(kb: Keyboard) -> tuple[int, int]:
    """(program version, resource version) of the screen chip, e.g. (0x0101, 0)."""
    r = kb.read(0xAD, delay_s=0.3)
    if r[0] != 0xAD:
        raise FlashError(f"unexpected version reply {r[:6].hex(' ')}")
    return r[2] << 8 | r[1], r[4] << 8 | r[3]


def bootloader_probe(kb: Keyboard) -> bytes:
    """Send only 0xB0 (no 0x30, nothing written). A screen chip sitting in its bootloader answers B0 ..."""
    _command(kb, bytes([0xB0]))
    time.sleep(0.1)
    return _reply(kb)


def check_image(file_bytes: bytes) -> bytes:
    """Return the app part that would be sent, after sanity checks on the whole firmwareOledFile."""
    if len(file_bytes) <= APP_OFFSET + 0x1000 or len(file_bytes) > 0x40000:
        raise FlashError(f"unexpected file size {len(file_bytes)}")
    app = file_bytes[APP_OFFSET:]
    # every Yichip app here starts with "ldr r0, [pc, #...]; bx r0" vector stubs
    if struct.unpack_from("<H", app, 2)[0] != 0x4700 or (app[1] & 0xF8) != 0x48:
        raise FlashError("app part does not start with the expected vector stubs")
    return app


def flash_app(kb: Keyboard, file_bytes: bytes, *, i_understand_the_risk: bool = False, log=print,
              packet_delay: float = 0.012, block_delay: float = 0.2, start_delay: float = 1.0) -> None:
    """Pacing: the first attempt (no delays, 3 ms/packet) got B1 AA = checksum not confirmed; the vendor app's
    pipeline is much slower, and the bootloader writes flash as data arrives."""
    if not i_understand_the_risk:
        raise FlashError("refusing to flash without explicit confirmation")
    app = check_image(file_bytes)
    count = (len(app) + PACKET - 1) // PACKET
    total = sum(app)                      # vendor: sum of every byte of the sent part
    log(f"app part: {len(app)} bytes, {count} packets, sum32 0x{total & 0xFFFFFFFF:08X}")

    log("1/5 asking the screen chip to enter its bootloader (0x30 55 AA 55 AA)")
    _command(kb, bytes([0x30, 0x55, 0xAA, 0x55, 0xAA, 0, 0]))

    log("2/5 waiting for the bootloader (0xB0)")
    for attempt in range(5):
        time.sleep(3.0)
        _command(kb, bytes([0xB0]))
        time.sleep(0.1)
        r = _reply(kb)
        log(f"    attempt {attempt + 1}: reply {r[:4].hex(' ')}")
        if r[:2] == b"\xb0\x01":            # b0 01 = bootloader ready. b0 00 = the app still answering (or a
            break                            # stale reply from an earlier probe): keep waiting
    else:
        raise FlashError("bootloader did not answer; nothing was written. Power-cycle the keyboard.")

    log(f"3/5 announcing {count} packets (0x31)")
    _command(kb, bytes([0x31, count & 0xFF, count >> 8 & 0xFF]))
    time.sleep(start_delay)

    log("4/5 sending the firmware")
    for i in range(count):
        chunk = app[i * PACKET:(i + 1) * PACKET].ljust(PACKET, b"\x00")
        _feature(kb, chunk)
        time.sleep(packet_delay)
        if (i + 1) % 64 == 0:
            time.sleep(block_delay)          # one 4 KB flash sector per 64 packets
        if i % 100 == 0:
            log(f"    {i}/{count}")
    log(f"    {count}/{count}")

    log("5/5 sending the checksum (0xB1) and waiting for the verdict")
    _command(kb, bytes([0xB1, *struct.pack("<I", total & 0xFFFFFFFF)]))
    time.sleep(1.0)
    r = _reply(kb)
    log(f"    reply {r[:4].hex(' ')}")
    if r[1:2] != b"\x55":
        raise FlashError("the bootloader did not confirm the checksum (expected 0x55). The screen chip stays "
                         "in update mode; run the flash again.")
    log("done: the screen chip accepted the new firmware")
