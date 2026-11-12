"""
Gemini TV Backend Server
Pure Python Standard Library (no external dependencies).
Provides local REST APIs for the LG webOS Custom Launcher, Gemini AI TV Assistant,
and the Modern LG Hub Android Remote for Samsung Galaxy S26 Ultra.
Uses ThreadingHTTPServer for zero-latency concurrent execution.
"""

import os
import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socket
from tv import LGTV
from agent import GeminiTVAgent, get_api_key, save_api_key
from smartthings import SmartThings
import urllib.request
import urllib.parse

PORT = 8080

class RequestHandler(BaseHTTPRequestHandler):
    agent = GeminiTVAgent()

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/api":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "service": "Modern LG Hub Backend",
                "version": "2.0.0",
                "status": "online",
                "has_gemini_key": bool(get_api_key())
            }).encode("utf-8"))

        elif path == "/api/status":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {
                "has_key": bool(get_api_key()),
                "tv": LGTV.get_status()
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif path == "/api/apps":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            apps = LGTV.get_launcher_apps()
            self.wfile.write(json.dumps({"apps": apps, "count": len(apps)}).encode("utf-8"))

        elif path.startswith("/api/icon/"):
            app_id = urllib.parse.unquote(path[len("/api/icon/"):])
            icon_file = LGTV.get_icon_file(app_id)
            if icon_file and os.path.exists(icon_file):
                try:
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
                except Exception:
                    pass
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()

        elif path == "/api/audio":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            audio_info = LGTV.get_audio_status()
            self.wfile.write(json.dumps(audio_info).encode("utf-8"))

        elif path == "/api/network":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            net = LGTV.get_network_info()
            self.wfile.write(json.dumps(net).encode("utf-8"))

        elif path == "/api/smartthings/devices":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            devices = SmartThings.get_dashboard_devices()
            self.wfile.write(json.dumps(devices).encode("utf-8"))

        elif path == "/api/weather":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                w_url = "https://api.open-meteo.com/v1/forecast?latitude=52.5200&longitude=13.4050&current=temperature_2m,apparent_temperature,weather_code&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=Europe%2FBerlin"
                with urllib.request.urlopen(w_url, timeout=5) as resp:
                    w_data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                w_data = {"error": str(e)}
            self.wfile.write(json.dumps(w_data).encode("utf-8"))

        else:
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len)
        try:
            body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            body = raw_body.decode("latin-1", errors="replace")
        try:
            req_data = json.loads(body) if body else {}
        except Exception:
            req_data = {}

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/remote/key":
            key = req_data.get("key", "")
            fast = req_data.get("fast", True)
            res = LGTV.send_remote_key(key, fast=fast)
            self._respond_json(res)

        elif path == "/api/remote/text":
            text = req_data.get("text", "")
            replace = req_data.get("replace", False)
            res = LGTV.insert_text(text, replace=replace)
            self._respond_json(res)

        elif path == "/api/remote/delete":
            length = req_data.get("length", 1)
            res = LGTV.delete_text(length=length)
            self._respond_json(res)

        elif path == "/api/remote/enter":
            res = LGTV.send_enter()
            self._respond_json(res)

        elif path == "/api/audio/output":
            output = req_data.get("output", "")
            res = LGTV.set_sound_output(output)
            self._respond_json(res)

        elif path == "/api/audio/toggle":
            res = LGTV.toggle_audio_output()
            self._respond_json(res)

        elif path == "/api/launch":
            app_id = req_data.get("id") or req_data.get("appId") or req_data.get("app") or ""
            if app_id.startswith("com.webos.app.hdmi"):
                res = LGTV.switch_input(app_id)
            else:
                res = LGTV.launch_app(app_id)
            self._respond_json(res)

        elif path == "/api/command":
            prompt = req_data.get("prompt", "")
            res = self.agent.process_command(prompt)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/key":
            key = req_data.get("api_key", "").strip()
            if key:
                save_api_key(key)
                self.agent.api_key = key
            self._respond_json({"success": True, "has_key": bool(get_api_key())})

        elif path == "/api/smartthings/toggle":
            dev_id = req_data.get("deviceId") or req_data.get("id") or ""
            if dev_id:
                res = SmartThings.toggle_switch(dev_id)
            else:
                res = {"error": "Missing deviceId"}
            self._respond_json(res)

        elif path == "/api/quick":
            action = req_data.get("action", "")
            res = {"success": True, "action": action}
            if action == "vol_up":
                res = LGTV.volume_up(1)
            elif action == "vol_down":
                res = LGTV.volume_down(1)
            elif action == "mute":
                res = LGTV.mute()
            elif action == "screen_off":
                res = LGTV.screen_off()
            elif action == "screen_on":
                res = LGTV.screen_on()
            elif action == "screen_toggle":
                res = LGTV.screen_toggle()
            elif action == "audio_toggle":
                res = LGTV.toggle_audio_output()
            elif action == "power_off":
                res = LGTV.power_off()
            elif action == "pc":
                res = LGTV.switch_input("pc")
            elif action == "home":
                res = LGTV.launch_app("com.custom.launcher")
            
            self._respond_json(res)

        else:
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()

    def _respond_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

def run_server():
    server_address = ("0.0.0.0", PORT)
    httpd = ThreadingHTTPServer(server_address, RequestHandler)
    print(f"Modern LG Hub Backend running on port {PORT} with ThreadingHTTPServer")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()

if __name__ == "__main__":
    run_server()
