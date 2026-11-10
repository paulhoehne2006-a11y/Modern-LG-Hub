"""
Gemini TV Backend Server
Pure Python Standard Library (no external dependencies).
Provides local REST APIs for the LG webOS Custom Launcher and Gemini AI TV Assistant.
Mobile web UI removed per user preference.

Security model: the launcher talks to this server via http://127.0.0.1:8080, so by
default the server only listens on loopback. Set GEMINI_BIND=0.0.0.0 to expose it on
the LAN (not recommended: there is no authentication).
"""

import os
import re
import json
import socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from tv import LGTV
from agent import GeminiTVAgent, get_api_key, save_api_key
from smartthings import SmartThings
import urllib.request

PORT = int(os.environ.get("GEMINI_PORT", "8080"))
BIND = os.environ.get("GEMINI_BIND", "127.0.0.1")
MAX_BODY = 64 * 1024
SOCKET_TIMEOUT = 30

# The launcher runs from file:// (Origin "null"); the PC preview runs on localhost.
_ALLOWED_ORIGIN = re.compile(r"^(null|file://|https?://(localhost|127\.0\.0\.1)(:\d+)?)$")
_DEVICE_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")

QUICK_ACTIONS = {
    "vol_up": lambda: LGTV.volume_up(1),
    "vol_down": lambda: LGTV.volume_down(1),
    "mute": LGTV.mute,
    "screen_off": LGTV.screen_off,
    "pc": lambda: LGTV.switch_input("pc"),
    "home": lambda: LGTV.launch_app("com.custom.launcher"),
}


class RequestHandler(BaseHTTPRequestHandler):
    agent = GeminiTVAgent()
    timeout = SOCKET_TIMEOUT

    # ---- helpers -------------------------------------------------------
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or bool(_ALLOWED_ORIGIN.match(origin))

    def _send_cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and _ALLOWED_ORIGIN.match(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data, status: int = 200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self):
        """Returns (dict, error_string)."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            # Forces a CORS preflight for browsers, so foreign web pages cannot fire "simple" requests.
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
        if not self._origin_ok():
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._send_json({"error": "Forbidden origin"}, 403)
        try:
            self._route_get()
        except Exception as e:
            self._send_json({"error": f"Interner Fehler: {type(e).__name__}"}, 500)

    def do_POST(self):
        if not self._origin_ok():
            return self._send_json({"error": "Forbidden origin"}, 403)
        req_data, err = self._read_json_body()
        if err:
            return self._send_json({"success": False, "error": err}, 400)
        try:
            self._route_post(req_data)
        except Exception as e:
            self._send_json({"success": False, "error": f"Interner Fehler: {type(e).__name__}"}, 500)

    def _route_get(self):
        if self.path in ("/", "/api"):
            self._send_json({
                "service": "Gemini TV Backend",
                "status": "online",
                "has_gemini_key": bool(get_api_key()),
            })
        elif self.path == "/api/status":
            self._send_json({"has_key": bool(get_api_key()), "tv": LGTV.get_status()})
        elif self.path == "/api/smartthings/devices":
            self._send_json(SmartThings.get_dashboard_devices())
        elif self.path == "/api/weather":
            try:
                w_url = "https://api.open-meteo.com/v1/forecast?latitude=52.5200&longitude=13.4050&current=temperature_2m,apparent_temperature,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Europe%2FBerlin"
                with urllib.request.urlopen(w_url, timeout=5) as resp:
                    w_data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                w_data = {"error": str(e)}
            self._send_json(w_data)
        else:
            self._send_json({"error": "Not found"}, 404)

    def _route_post(self, req_data: dict):
        if self.path == "/api/command":
            prompt = req_data.get("prompt", "")
            if not isinstance(prompt, str) or not prompt.strip():
                return self._send_json({"text": "Leere Anfrage.", "actions": []}, 400)
            self._send_json(self.agent.process_command(prompt))

        elif self.path == "/api/key":
            key = req_data.get("api_key", "")
            key = key.strip() if isinstance(key, str) else ""
            if key and not re.match(r"^[A-Za-z0-9_\-]{10,200}$", key):
                return self._send_json({"success": False, "error": "Ungültiges Key-Format"}, 400)
            if key:
                save_api_key(key)
                self.agent.api_key = key
            self._send_json({"success": bool(key), "has_key": bool(get_api_key())})

        elif self.path == "/api/smartthings/toggle":
            dev_id = req_data.get("deviceId") or req_data.get("id") or ""
            if not isinstance(dev_id, str) or not _DEVICE_ID.match(dev_id):
                return self._send_json({"error": "Missing or invalid deviceId"}, 400)
            self._send_json(SmartThings.toggle_switch(dev_id))

        elif self.path == "/api/quick":
            action = req_data.get("action", "")
            fn = QUICK_ACTIONS.get(action) if isinstance(action, str) else None
            if not fn:
                return self._send_json({"success": False, "error": f"Unbekannte Aktion: {action}"}, 400)
            res = fn() or {}
            ok = bool(res.get("success", False))
            self._send_json({"success": ok, "action": action, "result": res}, 200 if ok else 502)

        else:
            self._send_json({"error": "Not found"}, 404)


class ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server():
    httpd = ThreadingServer((BIND, PORT), RequestHandler)
    print(f"Gemini TV Backend running on {BIND}:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()


if __name__ == "__main__":
    run_server()
