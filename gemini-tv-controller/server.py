"""
Gemini TV Backend Server
Pure Python Standard Library (no external dependencies).
Provides local REST APIs for the LG webOS Custom Launcher and Gemini AI TV Assistant.
Mobile web UI removed per user preference.
"""

import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import socket
from tv import LGTV
from agent import GeminiTVAgent, get_api_key, save_api_key
from smartthings import SmartThings
import urllib.request

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
        if self.path == "/" or self.path == "/api":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "service": "Gemini TV Backend",
                "status": "online",
                "has_gemini_key": bool(get_api_key())
            }).encode("utf-8"))

        elif self.path == "/api/status":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = {
                "has_key": bool(get_api_key()),
                "tv": LGTV.get_status()
            }
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif self.path == "/api/smartthings/devices":
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            devices = SmartThings.get_dashboard_devices()
            self.wfile.write(json.dumps(devices).encode("utf-8"))

        elif self.path == "/api/weather":
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

        if self.path == "/api/command":
            prompt = req_data.get("prompt", "")
            res = self.agent.process_command(prompt)
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))

        elif self.path == "/api/key":
            key = req_data.get("api_key", "").strip()
            if key:
                save_api_key(key)
                self.agent.api_key = key
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "has_key": bool(get_api_key())}).encode("utf-8"))

        elif self.path == "/api/smartthings/toggle":
            dev_id = req_data.get("deviceId") or req_data.get("id") or ""
            if dev_id:
                res = SmartThings.toggle_switch(dev_id)
            else:
                res = {"error": "Missing deviceId"}
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(res).encode("utf-8"))

        elif self.path == "/api/quick":
            action = req_data.get("action", "")
            if action == "vol_up":
                LGTV.volume_up(1)
            elif action == "vol_down":
                LGTV.volume_down(1)
            elif action == "mute":
                LGTV.mute()
            elif action == "screen_off":
                LGTV.screen_off()
            elif action == "pc":
                LGTV.switch_input("pc")
            elif action == "home":
                LGTV.launch_app("com.custom.launcher")
            
            self.send_response(200)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        else:
            self.send_response(404)
            self._send_cors_headers()
            self.end_headers()

def run_server():
    server_address = ("0.0.0.0", PORT)
    httpd = HTTPServer(server_address, RequestHandler)
    print(f"Gemini TV Backend running on port {PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()

if __name__ == "__main__":
    run_server()
