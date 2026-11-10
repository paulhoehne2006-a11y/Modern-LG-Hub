"""
LG webOS TV Controller (Dual-Mode: Local on TV or Remote via SSH)
Supports app launching, inputs, volume, OLED screen off, notifications and media keys.
"""

import os
import subprocess
import json
import base64
import time
from typing import Dict, Any, Optional

TV_HOST = os.environ.get("TV_HOST", "root@192.168.178.60")
IS_LOCAL = os.path.exists("/usr/sbin/luna-send") or os.path.exists("/usr/bin/luna-send")

# SSH Multiplexing configuration
# Reduced persist time to prevent stale sockets if TV reboots
SSH_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/ssh_mux_%h_%p",
    "-o", "ControlPersist=60",
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=2" # Faster timeout for failing connections
]

APP_MAP = {
    "netflix": "netflix",
    "youtube": "youtube.leanback.v4",
    "spotify": "spotify-beehive",
    "prime": "amazon",
    "amazon": "amazon",
    "amazon prime": "amazon",
    "disney": "com.disney.disneyplus-prod",
    "disney+": "com.disney.disneyplus-prod",
    "apple tv": "com.apple.appletv",
    "appletv": "com.apple.appletv",
    "apple": "com.apple.appletv",
    "moonlight": "com.limelight.webos",
    "homebrew": "org.webosbrew.hbchannel",
    "browser": "com.webos.app.browser",
    "web": "com.webos.app.browser",
    "joyn": "joyn",
    "rtl": "com.netrtl.tvnow",
    "rtl+": "com.netrtl.tvnow",
    "magentatv": "de.telekom.magentatv",
    "sky": "com.skygo.app.de.q",
    "sky q": "com.skygo.app.de.q",
    "hd+": "hdp.opapp",
    "home": "com.custom.launcher",
    "launcher": "com.custom.launcher",
    "startbildschirm": "com.custom.launcher",
    "ard": "ard.mediathek",
    "ard mediathek": "ard.mediathek",
    "xbox": "com.microsoft.xbox"
}

INPUT_MAP = {
    "pc": "com.webos.app.hdmi1",
    "computer": "com.webos.app.hdmi1",
    "hdmi 1": "com.webos.app.hdmi1",
    "hdmi1": "com.webos.app.hdmi1",
    "hdmi 2": "com.webos.app.hdmi2",
    "hdmi2": "com.webos.app.hdmi2",
    "hdmi 3": "com.webos.app.hdmi3",
    "hdmi3": "com.webos.app.hdmi3",
    "hdmi 4": "com.webos.app.hdmi4",
    "hdmi4": "com.webos.app.hdmi4"
}

def luna_call(uri: str, payload: dict, timeout: int = 5) -> dict:
    """Executes a Luna call either locally or via SSH with multiplexing."""
    payload_json = json.dumps(payload)
    if IS_LOCAL:
        cmd = ["luna-send", "-i", "-n", "1", uri, payload_json]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = res.stdout.strip()
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        return json.loads(line)
                    except Exception:
                        pass
            return {"returnValue": res.returncode == 0, "raw": out}
        except Exception as e:
            return {"returnValue": False, "error": str(e)}
    else:
        b64 = base64.b64encode(payload_json.encode('utf-8')).decode('utf-8')
        # Ensure timeout is respected in ssh call
        cmd = ["ssh"] + SSH_OPTS + [TV_HOST,
            f'luna-send -n 1 {uri} "$(echo {b64} | base64 -d)"'
        ]
        try:
            # Shortened timeout to detect connection issues faster
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 1)
            for line in res.stdout.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        return json.loads(line)
                    except Exception:
                        pass
            return {"returnValue": False, "raw": res.stdout.strip()}
        except Exception as e:
            return {"returnValue": False, "error": str(e)}

def send_key(key: str) -> bool:
    """Sends a special remote key (e.g. VOLUMEUP, VOLUMEDOWN, MUTE, PLAY, PAUSE)."""
    res = luna_call("luna://com.webos.service.networkinput/sendSpecialKey", {"key": key})
    return res.get("returnValue", False)

class LGTV:
    @staticmethod
    def get_status() -> Dict[str, Any]:
        """Returns power state and foreground app."""
        # Optimization: Fetch both in parallel if possible, but keeping sequential for reliability
        fg = luna_call("luna://com.webos.applicationManager/getForegroundAppInfo", {})
        power = luna_call("luna://com.webos.service.tvpower/power/getPowerState", {})
        return {
            "power": power.get("state", "Unknown"),
            "current_app": fg.get("appId", "Unknown"),
            "success": fg.get("returnValue", False)
        }

    @staticmethod
    def launch_app(app_name: str) -> Dict[str, Any]:
        """Launches an app by friendly name or exact appId."""
        app_id = APP_MAP.get(app_name.lower().strip(), app_name.strip())
        res = luna_call("luna://com.webos.applicationManager/launch", {"id": app_id})
        return {
            "app": app_name,
            "app_id": app_id,
            "success": res.get("returnValue", False),
            "response": res
        }

    @staticmethod
    def switch_input(input_name: str) -> Dict[str, Any]:
        """Switches HDMI input (e.g. 'pc', 'hdmi1', 'hdmi2')."""
        app_id = INPUT_MAP.get(input_name.lower().strip(), "com.webos.app.hdmi1")
        res = luna_call("luna://com.webos.applicationManager/launch", {"id": app_id})
        return {
            "input": input_name,
            "app_id": app_id,
            "success": res.get("returnValue", False)
        }

    @staticmethod
    def volume_up(steps: int = 1) -> Dict[str, Any]:
        """Increases volume by N steps."""
        steps = max(1, min(steps, 20))
        for _ in range(steps):
            send_key("VOLUMEUP")
            # Removed sleep here to see if TV can handle it.
            # If unreliable, set to 0.02
        return {"action": "volume_up", "steps": steps, "success": True}

    @staticmethod
    def volume_down(steps: int = 1) -> Dict[str, Any]:
        """Decreases volume by N steps."""
        steps = max(1, min(steps, 20))
        for _ in range(steps):
            send_key("VOLUMEDOWN")
        return {"action": "volume_down", "steps": steps, "success": True}

    @staticmethod
    def mute() -> Dict[str, Any]:
        """Toggles mute."""
        ok = send_key("MUTE")
        return {"action": "mute_toggle", "success": ok}

    @staticmethod
    def screen_off() -> Dict[str, Any]:
        """Turns off OLED panel while audio/apps keep running."""
        res = luna_call("luna://com.webos.service.tvpower/power/turnOffScreen", {})
        return {"action": "screen_off", "success": res.get("returnValue", False)}

    @staticmethod
    def screen_on() -> Dict[str, Any]:
        """Turns the OLED panel back on."""
        res = luna_call("luna://com.webos.service.tvpower/power/turnOnScreen", {})
        return {"action": "screen_on", "success": res.get("returnValue", False)}

    @staticmethod
    def screensaver() -> Dict[str, Any]:
        """Starts the screensaver."""
        res = luna_call("luna://com.webos.service.tvpower/power/turnOnScreenSaver", {})
        return {"action": "screensaver", "success": res.get("returnValue", False)}

    @staticmethod
    def media_control(action: str) -> Dict[str, Any]:
        """Media controls: play, pause, stop, forward, rewind."""
        action_map = {
            "play": "PLAY",
            "pause": "PAUSE",
            "stop": "STOP",
            "forward": "FASTFORWARD",
            "rewind": "REWIND"
        }
        key = action_map.get(action.lower().strip(), "PLAY")
        ok = send_key(key)
        return {"action": action, "key": key, "success": ok}

    @staticmethod
    def toast(message: str) -> Dict[str, Any]:
        """Shows a brief toast notification on screen."""
        res = luna_call("luna://com.webos.notification/createToast", {"message": message})
        return {"toast": message, "success": res.get("returnValue", False)}

    @staticmethod
    def power_off() -> Dict[str, Any]:
        """Turns the TV into standby."""
        res = luna_call("luna://com.webos.service.tvpower/power/powerOff", {})
        return {"action": "power_off", "success": res.get("returnValue", False)}
