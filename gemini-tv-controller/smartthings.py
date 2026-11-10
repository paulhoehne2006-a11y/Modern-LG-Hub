"""
SmartThings REST API Client for LG TV Dashboard and Gemini Agent.
"""

import os
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor
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
_DEVICE_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")

def _valid_id(device_id) -> bool:
    return isinstance(device_id, str) and bool(_DEVICE_ID_RE.match(device_id))

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
        if not _valid_id(device_id):
            return {"error": "Invalid deviceId"}
        return api_request(f"/devices/{device_id}/status")

    @staticmethod
    def send_command(device_id: str, capability: str, command: str, args: list = None) -> Dict[str, Any]:
        """Sends command to a device."""
        if not _valid_id(device_id):
            return {"error": "Invalid deviceId"}
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
        res = SmartThings.send_command(device_id, "switch", cmd)
        SmartThings.invalidate_cache()
        return res

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
        if "error" in status:
            return {"status": "error", "error": status["error"]}
        current = SmartThings._extract_switch_value(status)
        new_state = "off" if current == "on" else "on"
        res = SmartThings.set_switch(device_id, new_state)
        if isinstance(res, dict) and "error" in res:
            return {"status": "error", "error": res["error"]}
        return {"status": "ok", "state": new_state, "new_state": new_state, "result": res}

    @staticmethod
    def get_room_names(location_ids) -> Dict[str, str]:
        """roomId -> room name, cached (rooms rarely change)."""
        now = time.time()
        if _cache["rooms"] is not None and now - _cache["rooms_at"] < ROOMS_TTL:
            return _cache["rooms"]
        rooms = {}
        for loc in location_ids:
            if not _valid_id(loc):
                continue
            res = api_request(f"/locations/{loc}/rooms")
            for r in (res.get("items", []) if isinstance(res, dict) else []):
                if r.get("roomId"):
                    rooms[r["roomId"]] = r.get("name") or ""
        _cache["rooms"], _cache["rooms_at"] = rooms, now
        return rooms

    @staticmethod
    def get_dashboard_devices(max_age: Optional[float] = None) -> List[Dict[str, Any]]:
        """All switchable devices for the TV dashboard (known ones first, in their usual order).

        Result is cached for a few seconds: the launcher polls this regularly and every
        device needs its own status request.
        """
        if max_age is None:
            max_age = DASHBOARD_TTL
        now = time.time()
        if _cache["dash"] is not None and now - _cache["dash_at"] < max_age:
            return _cache["dash"]

        devices = [d for d in SmartThings.get_devices() if _has_switch(d) and _valid_id(d.get("deviceId"))]
        rooms = SmartThings.get_room_names({d.get("locationId") for d in devices if d.get("locationId")})

        def raw_name(d):
            return d.get("label") or d.get("name") or "Gerät"

        # Same name twice -> add the room ("Bett · Schlafzimmer"); still equal -> number them.
        counts: Dict[str, int] = {}
        for d in devices:
            counts[raw_name(d)] = counts.get(raw_name(d), 0) + 1

        def known_rank(d):
            n = raw_name(d)
            return KNOWN_ORDER.index(n) if n in KNOWN_ORDER else len(KNOWN_ORDER)
        devices.sort(key=lambda d: (known_rank(d), raw_name(d).lower()))

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda d: SmartThings.get_device_status(d["deviceId"]), devices))

        results, seen = [], {}
        for d, status in zip(devices, statuses):
            raw = raw_name(d)
            name = ALIASES.get(raw, raw)
            if counts[raw] > 1:
                room = rooms.get(d.get("roomId") or "", "")
                if room and room.lower() not in name.lower():
                    name = f"{name} · {room}"
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} ({seen[name]})"
            results.append({
                "id": d["deviceId"],
                "name": name,
                "raw_name": raw,
                "type": _device_type(d),
                "state": SmartThings._extract_switch_value(status) if "error" not in status else "off",
                "online": "error" not in status,
            })
        _cache["dash"], _cache["dash_at"] = results, now
        return results

    @staticmethod
    def invalidate_cache() -> None:
        _cache["dash"] = None


# ---- dashboard helpers ------------------------------------------------------
DASHBOARD_TTL = 15      # seconds
ROOMS_TTL = 3600
_cache: Dict[str, Any] = {"dash": None, "dash_at": 0.0, "rooms": None, "rooms_at": 0.0}

# Friendly names for devices the launcher already knew; their order is the default tile order.
ALIASES = {
    "TV-Leiste": "TV-Licht",
    "Wohnzimmer-Licht": "Wohnzimmer",
    "Decke Arbeitszimmer": "Deckenlicht",
    "Büro-Licht": "Büro",
}
KNOWN_ORDER = ["TV-Leiste", "Wohnzimmer-Licht", "Decke Arbeitszimmer", "Schreibtisch", "Bett", "Büro-Licht", "Bar"]


def _capabilities(d: dict) -> set:
    return {c.get("id") for comp in d.get("components", []) for c in comp.get("capabilities", [])}


def _has_switch(d: dict) -> bool:
    return "switch" in _capabilities(d)


def _device_type(d: dict) -> str:
    caps = _capabilities(d)
    if "tvChannel" in caps or "mediaPlayback" in caps:
        return "tv"
    if "switchLevel" in caps or "colorControl" in caps:
        return "light"
    return "plug"
