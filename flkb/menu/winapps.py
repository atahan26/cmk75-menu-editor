"""Windows helpers for the menu editor: installed apps, browsers, shortcut targets, icons."""
from __future__ import annotations

import ctypes
import html.parser
import io
import os
import re
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

from PIL import Image

START_MENUS = [Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
               Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
SKIP = re.compile(r"uninstall|readme|help|documentation|website|release notes|license", re.I)


def resolve_lnk(path: str) -> dict:
    """Target, arguments and icon location of a .lnk shortcut (via WScript.Shell)."""
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitialize()          # editor requests run on their own threads
    except OSError:
        pass
    shell = comtypes.client.CreateObject("WScript.Shell", dynamic=True)   # late-bound: TargetPath etc.
    sc = shell.CreateShortcut(str(path))
    return {"target": sc.TargetPath or "", "args": sc.Arguments or "", "icon": sc.IconLocation or ""}


def real_process(target: str, args: str = "") -> str:
    """exe name of the running app. Squirrel installers (Discord, Slack, Teams...) start the app through
    Update.exe --processStart App.exe: the app's process is App.exe, not Update.exe."""
    m = re.search(r"--processStart(?:AndWait)?\s+\"?([^\"\s]+\.exe)", args or "", re.I)
    if m:
        return m.group(1)
    return Path(target).name if target else ""


def installed_apps() -> list[dict]:
    """Start-menu shortcuts: [{name, path (the .lnk), target, process}] sorted by name."""
    seen, out = set(), []
    for root in START_MENUS:
        if not root.exists():
            continue
        for lnk in root.rglob("*.lnk"):
            name = lnk.stem
            if SKIP.search(name) or name.lower() in seen:
                continue
            try:
                info = resolve_lnk(str(lnk))
            except Exception:
                info = {"target": "", "args": "", "icon": ""}
            target = info["target"]
            if target and not target.lower().endswith(".exe"):
                continue                         # documents, URLs, folders: not apps
            seen.add(name.lower())
            out.append({"name": name, "path": str(lnk), "target": target,
                        "process": real_process(target, info["args"])})
    try:
        for app in store_apps():
            if app["name"].lower() not in seen:
                seen.add(app["name"].lower())
                out.append(app)
    except Exception:
        pass
    return sorted(out, key=lambda a: a["name"].lower())


# --- Microsoft Store (packaged) apps: WhatsApp, Spotify from the Store, Xbox... ------------------------------
# They have no .lnk in the Start menu. Started by "shell:AppsFolder\<AUMID>"; the exe and logo are in the
# package's AppxManifest.xml.
APPS_FOLDER = "shell:AppsFolder\\"
_PS_STORE = (
    "$pk=@{}; Get-AppxPackage | ForEach-Object { $pk[$_.PackageFamilyName]=$_.InstallLocation };"
    "@(Get-StartApps | Where-Object { $_.AppID -like '*!*' } | ForEach-Object {"
    " [pscustomobject]@{name=$_.Name; aumid=$_.AppID; dir=$pk[$_.AppID.Split('!')[0]]} }) | ConvertTo-Json -Compress"
)


def _ps_json(script: str):
    import json
    import subprocess
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],   # app names can be Turkish
                       capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
    return data if isinstance(data, list) else [data]


def _manifest_app(pkg_dir: str, app_id: str) -> dict:
    """{exe, logo} of one <Application Id=...> in a package manifest ('' when not found)."""
    import xml.etree.ElementTree as ET
    root = ET.parse(Path(pkg_dir) / "AppxManifest.xml").getroot()
    for el in root.iter():
        if el.tag.endswith("}Application") and el.get("Id") == app_id:
            exe = el.get("Executable") or ""
            logo = ""
            for v in el.iter():
                if v.tag.endswith("}VisualElements"):
                    logo = v.get("Square44x44Logo") or v.get("Square150x150Logo") or ""
                    break
            return {"exe": "" if "$" in exe else Path(exe.replace("\\", "/")).name, "logo": logo}
    return {"exe": "", "logo": ""}


def store_apps() -> list[dict]:
    out = []
    for a in _ps_json(_PS_STORE):
        if not a.get("name") or not a.get("aumid") or SKIP.search(a["name"]):
            continue
        info = {"exe": "", "logo": ""}
        if a.get("dir"):
            try:
                info = _manifest_app(a["dir"], a["aumid"].split("!", 1)[1])
            except Exception:
                pass
        out.append({"name": a["name"], "path": APPS_FOLDER + a["aumid"], "target": APPS_FOLDER + a["aumid"],
                    "process": info["exe"]})
    return out


def store_logo(aumid: str) -> Path | None:
    """Biggest plain logo file of a packaged app (logo.png -> logo.targetsize-256.png, logo.scale-200.png...)."""
    pfn, app_id = aumid.split("!", 1)
    rows = _ps_json(f"@(Get-AppxPackage | Where-Object {{ $_.PackageFamilyName -eq '{pfn.replace(chr(39), '')}' }}"
                    " | Select-Object InstallLocation) | ConvertTo-Json -Compress")
    if not rows or not rows[0].get("InstallLocation"):
        return None
    pkg = Path(rows[0]["InstallLocation"])
    logo = _manifest_app(str(pkg), app_id)["logo"]
    if not logo:
        return None
    base = pkg / logo
    if base.exists():
        return base
    best, best_score = None, -1
    for f in base.parent.glob(base.stem + ".*" + base.suffix):
        q = f.name[len(base.stem):].lower()
        if "altform" in q or "contrast" in q:
            continue                          # keep the normal coloured logo
        m = re.search(r"(?:targetsize|scale)-(\d+)", q)
        score = int(m.group(1)) * (1 if "targetsize" in q else 0.44) if m else 0
        if score > best_score:
            best, best_score = f, score
    return best


BROWSERS = {
    "Chrome": [r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
               r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
               r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
    "Edge": [r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
             r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"],
    "Firefox": [r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe", r"%PROGRAMFILES(X86)%\Mozilla Firefox\firefox.exe"],
    "Brave": [r"%PROGRAMFILES%\BraveSoftware\Brave-Browser\Application\brave.exe",
              r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"],
    "Opera": [r"%LOCALAPPDATA%\Programs\Opera\opera.exe"],
    "Opera GX": [r"%LOCALAPPDATA%\Programs\Opera GX\opera.exe"],
    "Vivaldi": [r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe"],
}


def installed_browsers() -> list[dict]:
    out = []
    for name, candidates in BROWSERS.items():
        for c in candidates:
            p = os.path.expandvars(c)
            if "%" not in p and Path(p).exists():
                out.append({"name": name, "path": p})
                break
    return out


# --- icons from programs ------------------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


user32.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                                        wintypes.UINT, wintypes.UINT]
user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
user32.DestroyIcon.argtypes = [wintypes.HICON]
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def _hicon_to_image(hicon, size: int) -> Image.Image:
    hdc = gdi32.CreateCompatibleDC(None)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, hbmp)
    try:
        user32.DrawIconEx(hdc, 0, 0, hicon, size, size, 0, None, 3)        # DI_NORMAL
        raw = ctypes.string_at(bits, size * size * 4)
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
    img = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
    if img.getchannel("A").getextrema()[1] == 0:            # old icons without alpha: treat as opaque
        img.putalpha(255)
    return img.copy()


def icon_from_file(path: str, size: int = 128) -> Image.Image | None:
    """Best available icon of an .exe / .dll / .lnk / .ico (follows shortcuts)."""
    path, index = str(path), 0
    if path.lower().startswith(APPS_FOLDER.lower()):
        logo = store_logo(path[len(APPS_FOLDER):])
        return Image.open(logo).convert("RGBA") if logo else None
    if path.lower().endswith(".lnk"):
        info = resolve_lnk(path)
        loc = info["icon"]
        if loc and not loc.startswith(","):
            file, _, idx = loc.rpartition(",")
            path, index = os.path.expandvars(file or loc), int(idx or 0) if (idx or "0").lstrip("-").isdigit() else 0
        else:
            path = info["target"] or path
    if path.lower().endswith((".ico", ".png")):
        img = Image.open(path)
        return img.convert("RGBA")
    for s in (size, 64, 48, 32):
        hicon, got = wintypes.HICON(), wintypes.UINT()
        n = user32.PrivateExtractIconsW(path, index, s, s, ctypes.byref(hicon), ctypes.byref(got), 1, 0)
        if n and hicon:
            try:
                return _hicon_to_image(hicon, s)
            finally:
                user32.DestroyIcon(hicon)
    return None


# --- website icons ------------------------------------------------------------------------------------------
class _IconLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        rel = (a.get("rel") or "").lower()
        if tag == "link" and "icon" in rel and a.get("href"):
            size = 0
            m = re.search(r"(\d+)x\d+", a.get("sizes") or "")
            if m:
                size = int(m.group(1))
            if "apple-touch" in rel:
                size = max(size, 180)
            self.links.append((size, a["href"]))


def _get(url: str, limit: int = 2_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fl-keyboard menu editor)"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read(limit)


def icon_from_website(url: str) -> Image.Image | None:
    """The site's largest declared icon (apple-touch-icon / link rel=icon), else /favicon.ico."""
    if "://" not in url:
        url = "https://" + url
    candidates = []
    try:
        page = _get(url).decode("utf-8", "replace")
        p = _IconLinks()
        p.feed(page)
        candidates = [urllib.parse.urljoin(url, h) for _, h in sorted(p.links, reverse=True)]
    except Exception:
        pass
    parts = urllib.parse.urlsplit(url)
    candidates.append(f"{parts.scheme}://{parts.netloc}/favicon.ico")
    for c in candidates:
        try:
            data = _get(c)
            img = Image.open(io.BytesIO(data))
            if getattr(img, "n_frames", 1) > 1 or img.format == "ICO":
                sizes = sorted(img.info.get("sizes", []) or [img.size])
                if img.format == "ICO" and sizes:
                    img.size = sizes[-1]
            img = img.convert("RGBA")
            if img.width >= 16:
                return img
        except Exception:
            continue
    return None
