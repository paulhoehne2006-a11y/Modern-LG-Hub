"""
SmartThings REST API Client for LG TV Dashboard and Gemini Agent.
"""

import os
import json
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

def get_token() -> str:
    token = os.environ.get("SMARTTHINGS_TOKEN", "")
    if not token and os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SMARTTHINGS_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    return token

BASE_URL = "https://api.smartthings.com/v1"

def api_request(endpoint: str, method: str = "GET", data: Optional[dict] = None) -> Any:
    token = get_token()
    if not token:
        return {"error": "No SmartThings token configured"}

    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {"status": resp.status}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        return {"error": f"HTTP {e.code}", "details": err}
    except Exception as e:
        return {"error": str(e)}

class SmartThings:
    @staticmethod
    def get_devices() -> List[Dict[str, Any]]:
        """Returns list of devices."""
        res = api_request("/devices")
        return res.get("items", [])

    @staticmethod
    def get_device_status(device_id: str) -> Dict[str, Any]:
        """Returns current status of a device."""
        return api_request(f"/devices/{device_id}/status")

    @staticmethod
    def send_command(device_id: str, capability: str, command: str, args: list = None) -> Dict[str, Any]:
        """Sends command to a device."""
        cmd = {
            "component": "main",
            "capability": capability,
            "command": command
        }
        if args is not None:
            cmd["arguments"] = args
        return api_request(f"/devices/{device_id}/commands", method="POST", data={"commands": [cmd]})

    @staticmethod
    def set_switch(device_id: str, state: str) -> Dict[str, Any]:
        """Sets switch to 'on' or 'off'."""
        state = state.lower().strip()
        cmd = "on" if state in ["on", "an", "ein", "true"] else "off"
        return SmartThings.send_command(device_id, "switch", cmd)

    @staticmethod
    def _extract_switch_value(status: dict) -> str:
        main = status.get("components", {}).get("main", {})
        sw = main.get("switch", {})
        if isinstance(sw, dict):
            inner = sw.get("switch", {})
            if isinstance(inner, dict):
                val = inner.get("value")
                if val in ["on", "off"]:
                    return val
            val = sw.get("value")
            if val in ["on", "off"]:
                return val
        return "off"

    @staticmethod
    def toggle_switch(device_id: str) -> Dict[str, Any]:
        """Toggles switch based on current status."""
        status = SmartThings.get_device_status(device_id)
        current = SmartThings._extract_switch_value(status)
        new_state = "off" if current == "on" else "on"
        res = SmartThings.set_switch(device_id, new_state)
        return {"status": "ok", "state": new_state, "new_state": new_state, "result": res}

    @staticmethod
    def get_dashboard_devices() -> List[Dict[str, Any]]:
        """Returns key devices formatted for the TV dashboard."""
        devices = SmartThings.get_devices()
        results = []
        
        # Priority mapping
        targets = [
            ("TV-Leiste", "TV-Licht", "light"),
            ("Wohnzimmer-Licht", "Wohnzimmer", "light"),
            ("Decke Arbeitszimmer", "Deckenlicht", "light"),
            ("Schreibtisch", "Schreibtisch", "plug"),
            ("Bett", "Bett", "plug"),
            ("Büro-Licht", "Büro", "light"),
            ("Bar", "Bar", "light")
        ]

        device_dict = {d.get("label") or d.get("name"): d for d in devices}

        for match_name, display_name, icon_type in targets:
            if match_name in device_dict:
                d = device_dict[match_name]
                d_id = d.get("deviceId")
                status = SmartThings.get_device_status(d_id)
                switch_val = SmartThings._extract_switch_value(status)
                results.append({
                    "id": d_id,
                    "name": display_name,
                    "raw_name": match_name,
                    "type": icon_type,
                    "state": switch_val
                })
        return results
