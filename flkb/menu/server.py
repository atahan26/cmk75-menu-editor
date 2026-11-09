"""Local web server for the menu editor (127.0.0.1 only).

Menu entries can run commands, so the API must not be reachable by other websites open in the same browser:
every /api/ request needs the custom header X-FLKB: 1 (a cross-site page cannot send it without a CORS
preflight, which this server never approves) and a Host header naming this server (blocks DNS rebinding).
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from . import config, winapps

WEB = Path(__file__).resolve().parent / "web"
PORT = 8475


def _save_icon(item_id: str, img: Image.Image) -> str:
    config.ICON_DIR.mkdir(parents=True, exist_ok=True)
    img = img.convert("RGBA")
    img.thumbnail((128, 128), Image.LANCZOS)
    name = re.sub(r"[^A-Za-z0-9_-]", "", item_id)[:32] or "icon"
    path = config.ICON_DIR / f"{name}.png"
    img.save(path)
    return path.name


def make_handler(engine, log):
    class Handler(BaseHTTPRequestHandler):
        server_version = "flkb-menu"

        def log_message(self, fmt, *args):      # keep the console quiet
            pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode("utf-8"))

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20_000_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _api_allowed(self):
            if not self._host_ok() or self.headers.get("X-FLKB") != "1":
                self._json({"error": "forbidden"}, 403)
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?")[0]
            if path == "/":
                path = "/index.html"
            if path.startswith("/icons/"):
                p = config.icon_path(path[7:])
                return self._send(200, p.read_bytes(), "image/png") if p else self._send(404, b"", "text/plain")
            if path.startswith("/api/"):
                if not self._api_allowed():
                    return
                try:
                    if path == "/api/menu":
                        return self._json(engine.menu)
                    if path == "/api/apps":
                        return self._json(winapps.installed_apps())
                    if path == "/api/browsers":
                        return self._json(winapps.installed_browsers())
                    if path == "/api/meta":
                        from .actions import SYSTEM
                        return self._json({"actions": config.ACTIONS, "kinds": config.KINDS,
                                           "system": {k: v[0] for k, v in SYSTEM.items()},
                                           "connected": engine.out.kb is not None, "open": engine.is_open})
                except Exception as e:
                    return self._json({"error": str(e)}, 500)
                return self._json({"error": "not found"}, 404)
            f = (WEB / path.lstrip("/")).resolve()
            if WEB in f.parents and f.is_file():
                return self._send(200, f.read_bytes(), mimetypes.guess_type(f.name)[0] or "text/plain")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._api_allowed():
                return
            path = self.path.split("?")[0]
            try:
                body = self._body()
                if path == "/api/menu":                  # Upload: save, then show it on the keyboard
                    menu = config.save(body)
                    engine.apply_menu(menu, show=True)
                    log("menu uploaded from the editor")
                    return self._json(menu)
                if path == "/api/preview":
                    img = engine.preview(body.get("menu") or engine.menu, body.get("screen", "home"))
                    buf = io.BytesIO()
                    img.resize((480, 270), Image.NEAREST).save(buf, "PNG")
                    return self._send(200, buf.getvalue(), "image/png")
                if path == "/api/icon/fetch":            # icon from the program / shortcut / website
                    kind, target, item_id = body.get("kind"), body.get("target", ""), body.get("id", "icon")
                    img = winapps.icon_from_website(target) if kind == "website" else winapps.icon_from_file(target)
                    if img is None:
                        return self._json({"error": "no icon found"}, 404)
                    return self._json({"icon": _save_icon(item_id, img)})
                if path == "/api/icon/upload":           # icon file chosen by the user
                    data = base64.b64decode(body["data"].split(",", 1)[-1])
                    img = Image.open(io.BytesIO(data))
                    return self._json({"icon": _save_icon(body.get("id", "icon"), img)})
            except Exception as e:
                log(f"editor request {path} failed: {e}")
                return self._json({"error": str(e)}, 500)
            self._json({"error": "not found"}, 404)

    return Handler


def serve(engine, log=print) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(engine, log))
    threading.Thread(target=httpd.serve_forever, name="menu-editor", daemon=True).start()
    return httpd
