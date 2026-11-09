"""Names for USB HID keyboard usage IDs (HID Usage Tables, page 0x07)."""

_LETTERS = {0x04 + i: chr(ord("A") + i) for i in range(26)}
_DIGITS = {0x1E + i: str((i + 1) % 10) for i in range(10)}
_F1_F12 = {0x3A + i: f"F{i + 1}" for i in range(12)}
_F13_F24 = {0x68 + i: f"F{i + 13}" for i in range(12)}

NAMES: dict[int, str] = {
    **_LETTERS, **_DIGITS, **_F1_F12, **_F13_F24,
    0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab", 0x2C: "Space",
    0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\", 0x32: "NonUS #",
    0x33: ";", 0x34: "'", 0x35: "`", 0x36: ",", 0x37: ".", 0x38: "/",
    0x39: "CapsLock", 0x46: "PrintScreen", 0x47: "ScrollLock", 0x48: "Pause",
    0x49: "Insert", 0x4A: "Home", 0x4B: "PageUp", 0x4C: "Delete", 0x4D: "End",
    0x4E: "PageDown", 0x4F: "Right", 0x50: "Left", 0x51: "Down", 0x52: "Up",
    0x53: "NumLock", 0x54: "KP /", 0x55: "KP *", 0x56: "KP -", 0x57: "KP +",
    0x58: "KP Enter", 0x59: "KP 1", 0x5A: "KP 2", 0x5B: "KP 3", 0x5C: "KP 4",
    0x5D: "KP 5", 0x5E: "KP 6", 0x5F: "KP 7", 0x60: "KP 8", 0x61: "KP 9",
    0x62: "KP 0", 0x63: "KP .", 0x64: "NonUS \\", 0x65: "Menu",
    0x87: "Intl1", 0x88: "Intl2", 0x89: "Intl3", 0x8A: "Intl4", 0x8B: "Intl5",
    0xE0: "LCtrl", 0xE1: "LShift", 0xE2: "LAlt", 0xE3: "LWin",
    0xE4: "RCtrl", 0xE5: "RShift", 0xE6: "RAlt", 0xE7: "RWin",
}

# The vendor stores a combo's modifier as the modifier key's usage ID (uE table).
MODIFIERS = {224: "Ctrl", 225: "Shift", 226: "Alt", 227: "Win"}


def name(usage: int) -> str:
    return NAMES.get(usage, f"0x{usage:02X}")
