"""HID transport: find the keyboard's vendor config interface and do request/reply reads."""
from __future__ import annotations

import time
from dataclasses import dataclass

import hid

from . import protocol

VENDOR_USAGE_PAGES = (0xFFFF, 0xFFFFF)  # 65535 and 1048575, as listed in the vendor device table


@dataclass
class Interface:
    vid: int
    pid: int
    usage_page: int
    usage: int
    interface: int
    path: bytes
    product: str


def list_interfaces(vid: int | None = None, pid: int | None = None) -> list[Interface]:
    out = []
    for d in hid.enumerate(vid or 0, pid or 0):
        out.append(Interface(d["vendor_id"], d["product_id"], d["usage_page"], d["usage"],
                             d["interface_number"], d["path"], d.get("product_string") or ""))
    return out


def find_config_interface(vid: int, pid: int) -> Interface | None:
    for itf in list_interfaces(vid, pid):
        if itf.usage_page in VENDOR_USAGE_PAGES and itf.usage == 2:
            return itf
    return None


class Keyboard:
    def __init__(self, itf: Interface):
        self.itf = itf
        self.dev = hid.device()
        self.dev.open_path(itf.path)

    def close(self) -> None:
        self.dev.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def write(self, cmd: int, payload: bytes, pre_delay_s: float = 0.01, post_delay_s: float = 0.5) -> None:
        """Send a SET command (must be on protocol.SAFE_WRITES). Delays mirror the vendor app."""
        pkt = protocol.build(cmd, payload, allow_writes=True)
        time.sleep(pre_delay_s)
        sent = self.dev.send_feature_report(bytes([protocol.REPORT_ID]) + pkt)
        if sent < 0:
            raise IOError(f"send_feature_report failed: {self.dev.error()}")
        time.sleep(post_delay_s)

    def read(self, cmd: int, payload: bytes = b"", delay_s: float = 0.02) -> bytes:
        """Send a GET command and return the 64-byte reply (report ID stripped)."""
        pkt = protocol.build(cmd, payload)
        sent = self.dev.send_feature_report(bytes([protocol.REPORT_ID]) + pkt)
        if sent < 0:
            raise IOError(f"send_feature_report failed: {self.dev.error()}")
        time.sleep(delay_s)
        reply = bytes(self.dev.get_feature_report(protocol.REPORT_ID, protocol.PACKET_LEN + 1))
        if not reply:
            raise IOError(f"get_feature_report returned nothing: {self.dev.error()}")
        # Windows hidapi returns the report ID as the first byte.
        return reply[1:] if len(reply) == protocol.PACKET_LEN + 1 else reply
