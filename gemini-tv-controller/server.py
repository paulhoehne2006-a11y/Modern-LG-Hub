"""
Gemini TV Backend Server
Pure Python Standard Library (no external dependencies).
Provides REST APIs for the LG webOS Custom Launcher, the Gemini AI TV Assistant
and the Modern LG Hub Android remote (phone/PC in the home network).

Access model:
- Listens on all interfaces by default (GEMINI_BIND to change), because the phone app
  talks to it over the LAN.
- Only clients from loopback or private LAN addresses are served; everything else gets 403.
- Browser requests (they carry an Origin header) must come from the launcher (file:// / null),
  localhost or a private LAN address and must send JSON. This blocks web pages from the
  internet that try to control the TV through a browser in the home network (CSRF).
- Optional shared secret: if GEMINI_TOKEN is set (environment or .env), every client except
  the TV itself (loopback) must send it as header "X-Hub-Token".
"""

import os
import re
import json
import ipaddress
import socketserver
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from tv import LGTV
from agent import GeminiTVAgent, get_api_key, save_api_key, _env_value
from smartthings import SmartThings

PORT = int(os.environ.get("GEMINI_PORT", "8080"))
BIND = os.environ.get("GEMINI_BIND", "0.0.0.0")
MAX_BODY = 64 * 1024
SOCKET_TIMEOUT = 30

_DEVICE_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCAL_ORIGIN = re.compile(r"^(null|file://|https?://(localhost|127\.0\.0\.1)(:\d+)?)$")
_HOST_ORIGIN = re.compile(r"^https?://(?:\[([0-9A-Fa-f:.]+)\]|([0-9.]+))(?::\d+)?$")


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _origin_allowed(origin: str) -> bool:
    if _LOCAL_ORIGIN.match(origin):
        return True
    m = _HOST_ORIGIN.match(origin)
    return bool(m and _is_private_ip(m.group(1) or m.group(2)))


def _quick_actions():
    return {
        "vol_up": lambda: LGTV.volume_up(1),
        "vol_down": lambda: LGTV.volume_down(1),
        "mute": LGTV.mute,
        "screen_off": LGTV.screen_off,
        "screen_on": LGTV.screen_on,
        "screen_toggle": LGTV.screen_toggle,
        "audio_toggle": LGTV.toggle_audio_output,
        "power_off": LGTV.power_off,
        "pc": lambda: LGTV.switch_input("pc"),
        "home": lambda: LGTV.launch_app("com.custom.launcher"),
    }


class RequestHandler(BaseHTTPRequestHandler):
    agent = GeminiTVAgent()
    timeout = SOCKET_TIMEOUT

    # ---- access control ------------------------------------------------
    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self._client_ip()).is_loopback
        except ValueError:
            return False

    def _check_access(self) -> bool:
        """Sends the error response itself and returns False if the request is not allowed."""
        if not _is_private_ip(self._client_ip()):
            self._send_json({"error": "Forbidden"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and not _origin_allowed(origin):
            self._send_json({"error": "Forbidden origin"}, 403)
            return False
        token = _env_value("GEMINI_TOKEN")
        if token and not self._is_loopback() and self.headers.get("X-Hub-Token") != token:
            self._send_json({"error": "Missing or wrong X-Hub-Token"}, 401)
            return False
        return True

    def _send_cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and _origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Hub-Token")

    # ---- helpers -------------------------------------------------------
    def _send_json(self, data, status: int = 200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # kept for the endpoints added for the phone app
    _respond_json = _send_json

    def _read_json_body(self):
        """Returns (dict, error_string)."""
        if self.headers.get("Origin") is not None:
            # Browsers: JSON only, which forces a CORS preflight (no "simple" cross-site POSTs).
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return None, "Content-Type must be application/json"
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Invalid Content-Length"
        if length < 0 or length > MAX_BODY:
            return None, "Body too large"
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}, None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "Invalid JSON"
        if not isinstance(data, dict):
            return None, "JSON body must be an object"
        return data, None

    # ---- handlers ------------------------------------------------------
    def do_OPTIONS(self):
        if not self._check_access():
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._check_access():
            return
        try:
            self._route_get(urllib.parse.urlparse(self.path).path)
        except Exception as e:
            self._send_json({"error": f"Interner Fehler: {type(e).__name__}"}, 500)

    def do_POST(self):
        if not self._check_access():
            return
        req_data, err = self._read_json_body()
        if err:
            return self._send_json({"success": False, "error": err}, 400)
        try:
            self._route_post(urllib.parse.urlparse(self.path).path, req_data)
        except Exception as e:
            self._send_json({"success": False, "error": f"Interner Fehler: {type(e).__name__}"}, 500)

    def _route_get(self, path: str):
        if path in ("/", "/api"):
            self._send_json({
                "service": "Modern LG Hub Backend",
                "version": "2.1.0",
                "status": "online",
                "has_gemini_key": bool(get_api_key()),
            })
        elif path == "/api/status":
            self._send_json({"has_key": bool(get_api_key()), "tv": LGTV.get_status()})
        elif path == "/api/apps":
            apps = LGTV.get_launcher_apps()
            self._send_json({"apps": apps, "count": len(apps)})
        elif path.startswith("/api/icon/"):
            app_id = urllib.parse.unquote(path[len("/api/icon/"):])
            icon_file = LGTV.get_icon_file(app_id) if _APP_ID.match(app_id) and ".." not in app_id else None
            if icon_file and os.path.exists(icon_file):
                with open(icon_file, "rb") as f:
                    img_bytes = f.read()
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(img_bytes)))
                self.end_headers()
                self.wfile.write(img_bytes)
                return
            self._send_json({"error": "Not found"}, 404)
        elif path == "/api/audio":
            self._send_json(LGTV.get_audio_status())
        elif path == "/api/network":
            self._send_json(LGTV.get_network_info())
        elif path == "/api/smartthings/devices":
            self._send_json(SmartThings.get_dashboard_devices())
        elif path == "/api/weather":
            try:
                w_url = "https://api.open-meteo.com/v1/forecast?latitude=52.5200&longitude=13.4050&current=temperature_2m,apparent_temperature,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Europe%2FBerlin"
                with urllib.request.urlopen(w_url, timeout=5) as resp:
                    w_data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                w_data = {"error": str(e)}
            self._send_json(w_data)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _route_post(self, path: str, req_data: dict):
        if path == "/api/remote/key":
            key = str(req_data.get("key", ""))
            self._send_json(LGTV.send_remote_key(key, fast=bool(req_data.get("fast", True))))

        elif path == "/api/remote/text":
            text = req_data.get("text", "")
            if not isinstance(text, str):
                return self._send_json({"success": False, "error": "text must be a string"}, 400)
            self._send_json(LGTV.insert_text(text[:2000], replace=bool(req_data.get("replace", False))))

        elif path == "/api/remote/delete":
            try:
                length = max(1, min(500, int(req_data.get("length", 1))))
            except (TypeError, ValueError):
                length = 1
            self._send_json(LGTV.delete_text(length=length))

        elif path == "/api/remote/enter":
            self._send_json(LGTV.send_enter())

        elif path == "/api/audio/output":
            self._send_json(LGTV.set_sound_output(str(req_data.get("output", ""))))

        elif path == "/api/audio/toggle":
            self._send_json(LGTV.toggle_audio_output())

        elif path == "/api/launch":
            app_id = req_data.get("id") or req_data.get("appId") or req_data.get("app") or ""
            if not isinstance(app_id, str) or not app_id:
                return self._send_json({"success": False, "error": "Missing app id"}, 400)
            if app_id.startswith("com.webos.app.hdmi"):
                self._send_json(LGTV.switch_input(app_id))
            else:
                self._send_json(LGTV.launch_app(app_id))

        elif path == "/api/command":
            prompt = req_data.get("prompt", "")
            if not isinstance(prompt, str) or not prompt.strip():
                return self._send_json({"text": "Leere Anfrage.", "actions": []}, 400)
            history = req_data.get("history")
            if not isinstance(history, list):
                history = []
            history = [t for t in history if isinstance(t, dict)]
            self._send_json(self.agent.process_command(prompt[:2000], history))

        elif path == "/api/key":
            key = req_data.get("api_key", "")
            key = key.strip() if isinstance(key, str) else ""
            if key and not re.match(r"^[A-Za-z0-9_\-]{10,200}$", key):
                return self._send_json({"success": False, "error": "Ungültiges Key-Format"}, 400)
            if key:
                save_api_key(key)
                self.agent.api_key = key
            self._send_json({"success": bool(key), "has_key": bool(get_api_key())})

        elif path == "/api/smartthings/toggle":
            dev_id = req_data.get("deviceId") or req_data.get("id") or ""
            if not isinstance(dev_id, str) or not _DEVICE_ID.match(dev_id):
                return self._send_json({"error": "Missing or invalid deviceId"}, 400)
            self._send_json(SmartThings.toggle_switch(dev_id))

        elif path == "/api/picture/mode":
            self._send_json(LGTV.set_picture_mode(str(req_data.get("mode", "game"))))

        elif path == "/api/quick":
            action = req_data.get("action", "")
            action = action if isinstance(action, str) else ""
            if action.startswith("pic_"):
                fn = lambda: LGTV.set_picture_mode(action[4:])
            else:
                fn = _quick_actions().get(action)
            if not fn:
                return self._send_json({"success": False, "error": f"Unbekannte Aktion: {action}"}, 400)
            res = fn() or {}
            ok = bool(res.get("success", False))
            self._send_json(dict(res, action=action, success=ok), 200 if ok else 502)

        else:
            self._send_json({"error": "Not found"}, 404)


class ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server():
    httpd = ThreadingServer((BIND, PORT), RequestHandler)
    print(f"Modern LG Hub Backend running on {BIND}:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()


if __name__ == "__main__":
    run_server()
