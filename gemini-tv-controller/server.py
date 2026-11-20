"""
Gemini TV Backend Server
Pure Python Standard Library (no external dependencies).
Provides REST APIs for the LG webOS Custom Launcher, the Gemini AI TV Assistant
and the Modern LG Hub Android remote (phone/PC in the home network).
The full HTTP reference is docs/API.md.

Access model:
- Listens on all interfaces by default (GEMINI_BIND to change), because the phone app
  talks to it over the LAN.
- Only clients from loopback or private LAN addresses are served; everything else gets 403.
- Browser requests (they carry an Origin header) must come from localhost or a private LAN
  address and must send JSON. The launcher's own origin (file:// pages send "null") is only
  accepted from the TV itself (loopback): any web page can produce "Origin: null" from a
  sandboxed iframe. This blocks web pages from the internet that try to control the TV
  through a browser in the home network (CSRF).
- The Host header must be an IP address, localhost, a single-label name or a local domain
  (.local, .lan, .fritz.box, ...; more via GEMINI_ALLOWED_HOSTS). This stops DNS-rebinding
  pages from reading the GET endpoints.
- Optional shared secret: if GEMINI_TOKEN is set (environment or .env), every client except
  the TV itself (loopback) must send it as header "X-Hub-Token".
- Limits: 64 KB request bodies, 10 s to send a request, 32 connections at once (24 from the
  LAN, so the launcher on the TV always gets through).
"""

import hmac
import io
import ipaddress
import json
import math
import os
import re
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional

from tv import LGTV, APP_MAP, KEY_MAP, ICON_TYPES, apps_version
from agent import GeminiTVAgent, get_api_key, save_api_key, _env_value
import localhome                      # local WiZ/Tapo control (replaces the SmartThings cloud)

SERVER_VERSION = "2.2.0"
SERVICE_NAME = "Modern LG Hub Backend"     # the Android app's discovery looks for "LG Hub"
PORT = int(os.environ.get("GEMINI_PORT", "8080"))
BIND = os.environ.get("GEMINI_BIND", "0.0.0.0")
MAX_BODY = 64 * 1024
SOCKET_TIMEOUT = 15          # s per socket read/write
REQUEST_DEADLINE = 10.0      # s to send request line, headers and body
MAX_CONNECTIONS = 32         # handler threads at once ...
MAX_LAN_CONNECTIONS = 24     # ... of which LAN clients may use this many
WEATHER_TTL = 600.0          # s; the launcher asks every 10 min
WEATHER_STALE_OK = 3 * 3600  # s; serve the last good forecast this long if open-meteo fails
LAUNCHER_APPINFO = "/media/developer/apps/usr/palm/applications/com.custom.launcher/appinfo.json"

_DEVICE_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HDMI_ID = re.compile(r"^com\.webos\.app\.hdmi[1-4]$")
_KEY_NAME = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_WORD = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_API_KEY = re.compile(r"^[A-Za-z0-9_\-]{10,200}$")
_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
_FILE_ORIGIN = re.compile(r"^(null|file://)$")
_HOST_ORIGIN = re.compile(r"^https?://(?:\[([0-9A-Fa-f:.]+)\]|([0-9.]+))(?::\d+)?$")
_LOCAL_DOMAINS = (".local", ".lan", ".home", ".fritz.box", ".internal", ".home.arpa", ".localdomain")


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [server] {msg}", file=sys.stderr, flush=True)


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _is_loopback_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _origin_allowed(origin: str, loopback: bool = False) -> bool:
    """May a browser page with this Origin talk to us? file:// / null only from the TV itself."""
    if _FILE_ORIGIN.match(origin):
        return loopback
    if _LOCAL_ORIGIN.match(origin):
        return True
    m = _HOST_ORIGIN.match(origin)
    return bool(m and _is_private_ip(m.group(1) or m.group(2)))


def _host_allowed(host: Optional[str]) -> bool:
    """Host header check against DNS rebinding (a public name that resolves to the TV)."""
    if not host:
        return True                          # HTTP/1.0 clients may omit it
    h = host.strip().lower().rstrip(".")
    if h.startswith("["):                    # [v6]:port
        h = h[1:h.find("]")] if "]" in h else h[1:]
    elif h.count(":") == 1:                  # name:port / v4:port
        h = h.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if h == "localhost" or "." not in h or h.endswith(_LOCAL_DOMAINS):
        return True
    extra = [x.strip().lower() for x in _env_value("GEMINI_ALLOWED_HOSTS").split(",") if x.strip()]
    return h in extra


def _int_or_none(v) -> Optional[int]:
    """JSON number -> int; None for anything else (bool, string, NaN, Infinity)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return int(v)


def _reject_constant(name):
    raise ValueError(f"invalid JSON constant {name}")


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


# ---- weather (open-meteo, cached) ----------------------------------------------

_weather_lock = threading.Lock()
_weather: Dict[str, Any] = {"data": None, "at": 0.0}


def _coord(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(_env_value(name) or default)
    except ValueError:
        return default
    return v if lo <= v <= hi and math.isfinite(v) else default


def weather_url() -> str:
    lat = _coord("WEATHER_LAT", 52.5200, -90.0, 90.0)
    lon = _coord("WEATHER_LON", 13.4050, -180.0, 180.0)
    tz = _env_value("WEATHER_TZ") or "Europe/Berlin"
    if not re.match(r"^[A-Za-z0-9_/+-]{1,64}$", tz):
        tz = "Europe/Berlin"
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": tz,
    }, safe=",")
    return "https://api.open-meteo.com/v1/forecast?" + q


def get_weather() -> Dict[str, Any]:
    """open-meteo forecast, cached WEATHER_TTL; on failure the last good one (up to 3 h)."""
    with _weather_lock:                        # one fetch at a time; the others take its result
        now = time.monotonic()
        if _weather["data"] is not None and now - _weather["at"] < WEATHER_TTL:
            return _weather["data"]
        try:
            with urllib.request.urlopen(weather_url(), timeout=5) as resp:
                data = json.loads(resp.read(256 * 1024).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("unexpected reply")
            if data.get("current"):
                _weather["data"], _weather["at"] = data, time.monotonic()
            return data
        except Exception as e:
            if _weather["data"] is not None and now - _weather["at"] < WEATHER_STALE_OK:
                return _weather["data"]
            return {"error": f"{type(e).__name__}: {e}"[:200]}


# ---- small helpers ---------------------------------------------------------------

def launcher_version() -> str:
    try:
        with open(LAUNCHER_APPINFO, "r", encoding="utf-8") as f:
            v = json.load(f).get("version")
        return v if isinstance(v, str) else ""
    except Exception:
        return ""


_bg_refresh: Dict[str, Any] = {"thread": None}


def _cached_devices(max_age: float = 30.0):
    """(devices, age) from the smart-home cache without waiting on the LAN; kicks off a refresh
    in the background when the cache is missing or older than localhome's TTL."""
    age = localhome.dashboard_age()
    if age is None or age >= localhome.DASHBOARD_TTL:
        t = _bg_refresh["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_safe_dashboard, name="dash-refresh", daemon=True)
            _bg_refresh["thread"] = t
            t.start()
    if age is None or age > max_age:
        return None, age
    return localhome._cache["dash"], round(age, 1)


def _safe_dashboard():
    try:
        localhome.get_dashboard_devices()
    except Exception as e:
        _log(f"dashboard refresh failed: {type(e).__name__}: {e}")


class _DeadlineReader(io.RawIOBase):
    """Socket reader that gives up once the whole request took longer than REQUEST_DEADLINE
    (a client dripping one byte every few seconds can't hold a handler thread forever)."""

    def __init__(self, sock, deadline: float):
        self.sock, self.deadline = sock, deadline

    def readable(self):
        return True

    def readinto(self, b):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("request took too long")
        self.sock.settimeout(min(left, SOCKET_TIMEOUT))
        return self.sock.recv_into(b)


class RequestHandler(BaseHTTPRequestHandler):
    agent = GeminiTVAgent()
    timeout = SOCKET_TIMEOUT
    server_version = "ModernLGHub/" + SERVER_VERSION

    def setup(self):
        super().setup()
        self.rfile = io.BufferedReader(_DeadlineReader(self.connection, time.monotonic() + REQUEST_DEADLINE))

    def _request_read(self):
        """Request fully read: from now on normal socket timeouts (Gemini may take a while)."""
        try:
            self.connection.settimeout(SOCKET_TIMEOUT)
        except OSError:
            pass

    # ---- access control ------------------------------------------------
    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _is_loopback(self) -> bool:
        return _is_loopback_ip(self._client_ip())

    def _check_access(self) -> bool:
        """Sends the error response itself and returns False if the request is not allowed."""
        if not _is_private_ip(self._client_ip()):
            self._send_json({"error": "Zugriff verweigert"}, 403)
            return False
        if not _host_allowed(self.headers.get("Host")):
            self._send_json({"error": "Zugriff verweigert (Host)"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and not _origin_allowed(origin, self._is_loopback()):
            self._send_json({"error": "Zugriff verweigert (Origin)"}, 403)
            return False
        token = _env_value("GEMINI_TOKEN")
        if token and not self._is_loopback():
            given = self.headers.get("X-Hub-Token") or ""
            if not hmac.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8", "replace")):
                self._send_json({"error": "Missing or wrong X-Hub-Token"}, 401)
                return False
        return True

    def _send_cors_headers(self):
        origin = self.headers.get("Origin")
        if origin and _origin_allowed(origin, self._is_loopback()):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Hub-Token")
            self.send_header("Access-Control-Max-Age", "600")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

    # ---- helpers -------------------------------------------------------
    def _send_json(self, data, status: int = 200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self):
        """Returns (dict, error_string)."""
        if self.headers.get("Origin") is not None:
            # Browsers: JSON only, which forces a CORS preflight (no "simple" cross-site POSTs).
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return None, "Content-Type muss application/json sein"
        if self.headers.get("Transfer-Encoding"):
            return None, "Chunked-Bodies werden nicht unterstützt"
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Ungültige Content-Length"
        if length < 0 or length > MAX_BODY:
            return None, "Anfrage zu groß"
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            return None, "Anfrage unvollständig"
        if not raw:
            return {}, None
        try:
            data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            return None, "Ungültiges JSON"
        if not isinstance(data, dict):
            return None, "JSON muss ein Objekt sein"
        return data, None

    def _internal_error(self, e: Exception, body: dict):
        tb = traceback.format_exc().replace("\n", " | ")[-600:]
        _log(f"{self.command} {urllib.parse.urlparse(self.path).path} failed: {tb}")
        try:
            self._send_json(body, 500)
        except OSError:
            pass

    def log_request(self, code="-", size="-"):
        # No access log: the phone polls every few seconds and the log lives in RAM.
        # Internal errors are logged with their traceback by _internal_error.
        pass

    # ---- handlers ------------------------------------------------------
    def do_OPTIONS(self):
        if not self._check_access():
            return
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._request_read()
        if not self._check_access():
            return
        try:
            url = urllib.parse.urlparse(self.path)
            self._route_get(url.path, urllib.parse.parse_qs(url.query))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._internal_error(e, {"error": f"Interner Fehler: {type(e).__name__}"})

    def do_POST(self):
        if not self._check_access():
            return
        req_data, err = self._read_json_body()
        self._request_read()
        if err:
            return self._send_json({"success": False, "error": err}, 400)
        try:
            self._route_post(urllib.parse.urlparse(self.path).path, req_data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._internal_error(e, {"success": False, "error": f"Interner Fehler: {type(e).__name__}"})
        finally:
            LGTV.invalidate_status()        # whatever it was, the next status poll reads fresh

    def _route_get(self, path: str, query: Optional[Dict[str, list]] = None):
        query = query or {}
        if path in ("/", "/api"):
            self._send_json({
                "service": SERVICE_NAME,
                "version": SERVER_VERSION,
                "launcher_version": launcher_version(),
                "status": "online",
                "has_gemini_key": bool(get_api_key()),
                "has_key": bool(get_api_key()),          # same name as in /api/status
                "token_required": bool(_env_value("GEMINI_TOKEN")),
                "features": ["state", "icon-etag", "smarthome-set", "long-enter", "weather-cache"],
            })
        elif path == "/api/status":
            self._send_json({"has_key": bool(get_api_key()), "tv": LGTV.get_status_cached()})
        elif path == "/api/state":
            devices, age = _cached_devices()
            self._send_json({
                "version": SERVER_VERSION,
                "has_key": bool(get_api_key()),
                "tv": LGTV.get_status_cached(),
                "devices": devices,
                "devices_age": age,
                "apps_version": apps_version(),
            })
        elif path == "/api/apps":
            apps = LGTV.get_launcher_apps()
            self._send_json({"apps": apps, "count": len(apps), "apps_version": apps_version()})
        elif path.startswith("/api/icon/"):
            self._send_icon(urllib.parse.unquote(path[len("/api/icon/"):]))
        elif path == "/api/audio":
            self._send_json(LGTV.get_audio_status())
        elif path == "/api/network":
            self._send_json(LGTV.get_network_info())
        elif path in ("/api/smarthome/devices", "/api/smartthings/devices"):   # old path kept for older launchers
            self._send_json(localhome.get_dashboard_devices())
        elif path == "/api/weather":
            self._send_json(get_weather())
        else:
            self._send_json({"error": "Nicht gefunden"}, 404)

    def _send_icon(self, app_id: str):
        icon_file = LGTV.get_icon_file(app_id) if _APP_ID.match(app_id) and ".." not in app_id else None
        try:
            st = os.stat(icon_file) if icon_file else None
        except OSError:
            st = None
        if st is None:
            return self._send_json({"error": "Nicht gefunden"}, 404)
        etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        ctype = ICON_TYPES.get(os.path.splitext(icon_file)[1].lower(), "application/octet-stream")
        inm = self.headers.get("If-None-Match") or ""
        if etag in [t.strip() for t in inm.split(",")]:
            self.send_response(304)
            self._send_cors_headers()
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return
        with open(icon_file, "rb") as f:
            img_bytes = f.read(8 * 1024 * 1024)
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(img_bytes)))
        self.end_headers()
        self.wfile.write(img_bytes)

    def _bad(self, error: str):
        return self._send_json({"success": False, "error": error}, 400)

    def _route_post(self, path: str, req_data: dict):
        if path == "/api/remote/key":
            key = req_data.get("key", "")
            if not isinstance(key, str) or not _KEY_NAME.match(key.strip()):
                return self._bad("Fehlende oder ungültige Taste")
            self._send_json(LGTV.send_remote_key(key, fast=bool(req_data.get("fast", True))))

        elif path == "/api/remote/text":
            text = req_data.get("text", "")
            if not isinstance(text, str):
                return self._send_json({"success": False, "error": "text muss ein String sein"}, 400)
            self._send_json(LGTV.insert_text(text[:2000], replace=bool(req_data.get("replace", False))))

        elif path == "/api/remote/delete":
            length = _int_or_none(req_data.get("length", 1))
            if length is None and isinstance(req_data.get("length"), str):
                try:
                    length = int(req_data["length"])
                except ValueError:
                    length = None
            length = max(1, min(500, length if length is not None else 1))
            self._send_json(LGTV.delete_text(length=length))

        elif path == "/api/remote/enter":
            self._send_json(LGTV.send_enter())

        elif path == "/api/audio/output":
            output = req_data.get("output", "")
            if not isinstance(output, str) or not _WORD.match(output.strip()):
                return self._bad("Fehlender oder ungültiger Audioausgang")
            self._send_json(LGTV.set_sound_output(output))

        elif path == "/api/audio/toggle":
            self._send_json(LGTV.toggle_audio_output())

        elif path == "/api/launch":
            app_id = req_data.get("id") or req_data.get("appId") or req_data.get("app") or ""
            if not isinstance(app_id, str) or not app_id.strip():
                return self._send_json({"success": False, "error": "App-ID fehlt"}, 400)
            app_id = app_id.strip()
            if app_id.startswith("com.webos.app.hdmi"):
                if not _HDMI_ID.match(app_id):
                    return self._bad("Unbekannter Eingang")
                self._send_json(LGTV.switch_input(app_id))
            elif _APP_ID.match(app_id) or app_id.lower() in APP_MAP:
                self._send_json(LGTV.launch_app(app_id))
            else:
                return self._bad("Ungültige App-ID")

        elif path == "/api/command":
            prompt = req_data.get("prompt", "")
            if not isinstance(prompt, str) or not prompt.strip():
                return self._send_json({"text": "Leere Anfrage.", "actions": []}, 400)
            history = req_data.get("history")
            if not isinstance(history, list):
                history = []
            history = [t for t in history[-50:] if isinstance(t, dict)]
            self._send_json(self.agent.process_command(prompt[:2000], history))

        elif path == "/api/key":
            key = req_data.get("api_key", "")
            key = key.strip() if isinstance(key, str) else ""
            if key and not _API_KEY.match(key):
                return self._send_json({"success": False, "error": "Ungültiges Key-Format"}, 400)
            if key:
                try:
                    save_api_key(key)
                except (OSError, ValueError, UnicodeDecodeError) as e:
                    _log(f"api key not saved: {type(e).__name__}")
                    return self._send_json({"success": False, "error": "Key konnte nicht gespeichert werden",
                                            "has_key": bool(get_api_key())}, 500)
                self.agent.api_key = key
            self._send_json({"success": bool(key), "has_key": bool(get_api_key())})

        elif path in ("/api/smarthome/toggle", "/api/smartthings/toggle", "/api/smarthome/set"):
            dev_id = req_data.get("deviceId") or req_data.get("id") or ""
            if not isinstance(dev_id, str) or not _DEVICE_ID.match(dev_id):
                return self._send_json({"error": "deviceId fehlt oder ist ungültig"}, 400)
            if path == "/api/smarthome/set":
                bri = _int_or_none(req_data.get("brightness"))
                color = localhome.parse_color(req_data.get("color")) if req_data.get("color") else None
                if req_data.get("color") and color is None:
                    return self._send_json({"error": "Ungültige Farbe (erwartet #rrggbb)"}, 400)
                kelvin = _int_or_none(req_data.get("kelvin"))
                state = req_data.get("state", "on")
                if not isinstance(state, (str, bool, int)) or isinstance(state, float):
                    state = "on"
                return self._send_json(localhome.set_switch(dev_id, state, bri, color, kelvin))
            self._send_json(localhome.toggle_switch(dev_id))

        elif path == "/api/picture/mode":
            mode = req_data.get("mode", "game")
            if not isinstance(mode, str) or not _WORD.match(mode.strip()):
                return self._bad("Fehlender oder ungültiger Bildmodus")
            self._send_json(LGTV.set_picture_mode(mode))

        elif path == "/api/quick":
            action = req_data.get("action", "")
            action = action if isinstance(action, str) else ""
            if action.startswith("pic_") and _WORD.match(action[4:]):
                fn = lambda: LGTV.set_picture_mode(action[4:])
            else:
                fn = _quick_actions().get(action)
            if not fn:
                return self._send_json({"success": False, "error": f"Unbekannte Aktion: {action[:40]}"}, 400)
            res = fn() or {}
            ok = bool(res.get("success", False))
            self._send_json(dict(res, action=action, success=ok), 200 if ok else 502)

        else:
            self._send_json({"error": "Nicht gefunden"}, 404)


class ThreadingServer(socketserver.ThreadingMixIn, HTTPServer):
    """One thread per connection, but at most MAX_CONNECTIONS at once (MAX_LAN_CONNECTIONS
    for LAN clients), so a flood from the LAN can't exhaust the TV or lock out the launcher."""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._lan_slots = threading.BoundedSemaphore(MAX_LAN_CONNECTIONS)
        super().__init__(*args, **kwargs)

    def _acquire(self, client_address) -> Optional[list]:
        held = []
        if not _is_loopback_ip(client_address[0] if client_address else ""):
            if not self._lan_slots.acquire(blocking=False):
                return None
            held.append(self._lan_slots)
        if not self._slots.acquire(blocking=False):
            for s in held:
                s.release()
            return None
        held.append(self._slots)
        return held

    def process_request(self, request, client_address):
        held = self._acquire(client_address)
        if held is None:
            try:
                request.settimeout(1.0)
                body = '{"error": "Hub ausgelastet, bitte gleich nochmal"}'.encode()
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        t = threading.Thread(target=self._run, args=(request, client_address, held), daemon=True)
        try:
            t.start()
        except RuntimeError:                  # can't start a thread (out of memory)
            for s in held:
                s.release()
            self.shutdown_request(request)

    def _run(self, request, client_address, held):
        try:
            self.process_request_thread(request, client_address)
        finally:
            for s in held:
                s.release()


def run_server():
    httpd = ThreadingServer((BIND, PORT), RequestHandler)
    print(f"Modern LG Hub Backend {SERVER_VERSION} running on {BIND}:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()


if __name__ == "__main__":
    run_server()
