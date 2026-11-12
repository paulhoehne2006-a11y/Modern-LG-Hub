"""
LG webOS TV Controller (Dual-Mode: Local on TV or Remote via SSH)
Supports app launching, inputs, volume, OLED screen off, notifications, media keys,
audio output switching (Denon AVR / TV Speaker / BT), direct text input, and launcher sync.
"""

import os
import subprocess
import json
import base64
import time
import glob
from typing import Dict, Any, Optional, List

TV_HOST = os.environ.get("TV_HOST", "root@192.168.178.60")
IS_LOCAL = os.path.exists("/usr/sbin/luna-send") or os.path.exists("/usr/bin/luna-send")

# SSH Multiplexing configuration
SSH_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/ssh_mux_%h_%p",
    "-o", "ControlPersist=60",
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=2"
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

KEY_MAP = {
    "up": "UP",
    "down": "DOWN",
    "left": "LEFT",
    "right": "RIGHT",
    "ok": "ENTER",
    "enter": "ENTER",
    "select": "ENTER",
    "back": "BACK",
    "home": "HOME",
    "exit": "EXIT",
    "info": "INFO",
    "menu": "MENU",
    "settings": "MENU",
    "asterisk": "ASTERISK",
    "vol_up": "VOLUMEUP",
    "volume_up": "VOLUMEUP",
    "volumeup": "VOLUMEUP",
    "vol_down": "VOLUMEDOWN",
    "volume_down": "VOLUMEDOWN",
    "volumedown": "VOLUMEDOWN",
    "mute": "MUTE",
    "ch_up": "CHANNELUP",
    "channel_up": "CHANNELUP",
    "channelup": "CHANNELUP",
    "ch_down": "CHANNELDOWN",
    "channel_down": "CHANNELDOWN",
    "channeldown": "CHANNELDOWN",
    "play": "PLAY",
    "pause": "PAUSE",
    "stop": "STOP",
    "forward": "FASTFORWARD",
    "fastforward": "FASTFORWARD",
    "rewind": "REWIND",
    "red": "RED",
    "green": "GREEN",
    "yellow": "YELLOW",
    "blue": "BLUE",
    "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
    "5": "5", "6": "6", "7": "7", "8": "8", "9": "9"
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
        cmd = ["ssh"] + SSH_OPTS + [TV_HOST,
            f'luna-send -n 1 {uri} "$(echo {b64} | base64 -d)"'
        ]
        try:
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

def luna_fire(uri: str, payload: dict) -> bool:
    """Fires a Luna call asynchronously without waiting for reply (sub-millisecond return)."""
    payload_json = json.dumps(payload)
    try:
        if IS_LOCAL:
            subprocess.Popen(["luna-send", "-n", "1", uri, payload_json],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, close_fds=True)
            return True
        else:
            b64 = base64.b64encode(payload_json.encode('utf-8')).decode('utf-8')
            cmd = ["ssh"] + SSH_OPTS + [TV_HOST,
                f'luna-send -n 1 {uri} "$(echo {b64} | base64 -d)"'
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, close_fds=True)
            return True
    except Exception:
        return False

def send_key(key: str, fast: bool = False) -> bool:
    """Sends a special remote key (e.g. VOLUMEUP, ENTER, UP, DOWN)."""
    normalized = KEY_MAP.get(key.lower().strip(), key.upper().strip())
    payload = {"key": normalized}
    if fast:
        return luna_fire("luna://com.webos.service.networkinput/sendSpecialKey", payload)
    res = luna_call("luna://com.webos.service.networkinput/sendSpecialKey", payload)
    return res.get("returnValue", False)

class LGTV:
    @staticmethod
    def get_status() -> Dict[str, Any]:
        """Returns power state, screen state, foreground app, audio state, and network info."""
        fg = luna_call("luna://com.webos.applicationManager/getForegroundAppInfo", {})
        power = luna_call("luna://com.webos.service.tvpower/power/getPowerState", {})
        power_state = power.get("state", "Unknown")
        is_screen_off = (power_state.lower() in ["screen off", "screen_off", "display off"])
        audio = LGTV.get_audio_status()
        net = LGTV.get_network_info()

        return {
            "power": power_state,
            "screen_off": is_screen_off,
            "current_app": fg.get("appId", "Unknown"),
            "audio": audio,
            "network": net,
            "success": fg.get("returnValue", False) or power.get("returnValue", False)
        }

    @staticmethod
    def send_remote_key(key: str, fast: bool = True) -> Dict[str, Any]:
        """Sends remote control key (UP, DOWN, LEFT, RIGHT, OK, BACK, HOME, etc.)."""
        normalized = KEY_MAP.get(key.lower().strip(), key.upper().strip())
        ok = send_key(normalized, fast=fast)
        return {"action": "send_key", "key": normalized, "success": ok}

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
            send_key("VOLUMEUP", fast=True)
        return {"action": "volume_up", "steps": steps, "success": True}

    @staticmethod
    def volume_down(steps: int = 1) -> Dict[str, Any]:
        """Decreases volume by N steps."""
        steps = max(1, min(steps, 20))
        for _ in range(steps):
            send_key("VOLUMEDOWN", fast=True)
        return {"action": "volume_down", "steps": steps, "success": True}

    @staticmethod
    def mute() -> Dict[str, Any]:
        """Toggles mute."""
        ok = send_key("MUTE", fast=False)
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
    def screen_toggle() -> Dict[str, Any]:
        """Toggles OLED screen on/off."""
        power = luna_call("luna://com.webos.service.tvpower/power/getPowerState", {})
        state = power.get("state", "").lower()
        if "screen off" in state or "display off" in state:
            return LGTV.screen_on()
        else:
            return LGTV.screen_off()

    @staticmethod
    def get_audio_status() -> Dict[str, Any]:
        """Returns sound output and volume details."""
        sound_out = luna_call("luna://com.webos.service.audio/getSoundOutput", {})
        vol_info = luna_call("luna://com.webos.service.audio/getVolume", {})

        output_val = sound_out.get("soundOutput", "external_optical")
        is_denon = "optical" in output_val.lower()
        is_tv = "speaker" in output_val.lower() and not is_denon

        return {
            "soundOutput": output_val,
            "volume": vol_info.get("volume", 0),
            "muted": vol_info.get("muted", False),
            "is_denon_optical": is_denon,
            "is_tv_speaker": is_tv,
            "display_title": "Denon AVR-1803 (Optisch)" if is_denon else ("TV-Lautsprecher" if is_tv else output_val)
        }

    @staticmethod
    def set_sound_output(output: str) -> Dict[str, Any]:
        """Sets sound output (external_optical, tv_speaker, tv_speaker_and_external_optical, bt_soundbar)."""
        target = output.strip().lower()
        mapping = {
            "denon": "external_optical",
            "optical": "external_optical",
            "external_optical": "external_optical",
            "tv": "tv_speaker",
            "speaker": "tv_speaker",
            "tv_speaker": "tv_speaker",
            "both": "tv_speaker_and_external_optical",
            "tv_optical": "tv_speaker_and_external_optical",
            "bluetooth": "bt_soundbar",
            "bt": "bt_soundbar"
        }
        dest = mapping.get(target, target)
        res = luna_call("luna://com.webos.service.audio/changeSoundOutput", {"output": dest})
        return {"action": "change_sound_output", "output": dest, "success": res.get("returnValue", False)}

    @staticmethod
    def toggle_audio_output() -> Dict[str, Any]:
        """Toggles between Denon AVR-1803 (external_optical) and TV Speakers (tv_speaker)."""
        cur = LGTV.get_audio_status()
        new_target = "tv_speaker" if cur.get("is_denon_optical") else "external_optical"
        return LGTV.set_sound_output(new_target)

    @staticmethod
    def insert_text(text: str, replace: bool = False) -> Dict[str, Any]:
        """Sends keyboard text directly to webOS active input field."""
        payload = {"text": text, "replace": 1 if replace else 0}
        res = luna_call("luna://com.webos.service.ime/insertText", payload)
        return {"action": "insert_text", "text": text, "success": res.get("returnValue", False)}

    @staticmethod
    def delete_text(length: int = 1) -> Dict[str, Any]:
        """Deletes characters in active input field."""
        payload = {"length": length}
        res = luna_call("luna://com.webos.service.ime/deleteCharacters", payload)
        return {"action": "delete_text", "length": length, "success": res.get("returnValue", False)}

    @staticmethod
    def send_enter() -> Dict[str, Any]:
        """Sends enter key to active input or app."""
        luna_call("luna://com.webos.service.ime/sendEnterKey", {})
        return send_key("ENTER", fast=True)

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
        ok = send_key(key, fast=True)
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

    @staticmethod
    def get_network_info() -> Dict[str, Any]:
        """Retrieves network interfaces and MAC addresses for Wake-on-LAN."""
        macs = {}
        # Try local sysfs interfaces
        for iface_path in glob.glob("/sys/class/net/*"):
            iface = os.path.basename(iface_path)
            if iface in ["eth0", "wlan0", "lan0"]:
                addr_file = os.path.join(iface_path, "address")
                if os.path.exists(addr_file):
                    try:
                        with open(addr_file, "r") as f:
                            mac = f.read().strip()
                            if mac and mac != "00:00:00:00:00:00":
                                macs[iface] = mac
                    except Exception:
                        pass

        # If empty (e.g. running remote or restricted), query connectionmanager via Luna
        if not macs:
            res = luna_call("luna://com.webos.service.connectionmanager/getStatusInfo", {})
            wired = res.get("wired", {})
            wifi = res.get("wifi", {})
            if wired.get("macAddress"):
                macs["eth0"] = wired.get("macAddress")
            if wifi.get("macAddress"):
                macs["wlan0"] = wifi.get("macAddress")

        return {
            "mac_addresses": macs,
            "primary_mac": macs.get("eth0") or macs.get("wlan0") or ""
        }

    @staticmethod
    def get_launcher_apps() -> List[Dict[str, Any]]:
        """Returns sorted apps and inputs matching Modern LG Hub."""
        apps_data = []

        # Check paths for Modern LG Hub data
        candidate_paths = [
            "/media/developer/apps/usr/palm/applications/com.custom.launcher/data/apps.json",
            os.path.join(os.path.dirname(__file__), "..", "custom-launcher", "com.custom.launcher", "data", "apps.json")
        ]
        
        for cp in candidate_paths:
            if os.path.exists(cp):
                try:
                    with open(cp, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                        if isinstance(raw, list):
                            apps_data = raw
                        elif isinstance(raw, dict) and "apps" in raw:
                            apps_data = raw["apps"]
                        if apps_data:
                            break
                except Exception:
                    pass

        # Fallback to luna listApps if custom-launcher data not present
        if not apps_data:
            res = luna_call("luna://com.webos.applicationManager/listApps", {})
            raw_apps = res.get("apps", [])
            for a in raw_apps:
                app_id = a.get("id")
                if not a.get("visible", True) or not app_id:
                    continue
                apps_data.append({
                    "id": app_id,
                    "title": a.get("title", app_id),
                    "icon": a.get("largeIcon") or a.get("icon", "")
                })

        # Add top inputs (HDMI 1 is PC)
        inputs = [
            {"id": "com.webos.app.hdmi1", "title": "PC (HDMI 1)", "type": "input", "alias": "pc"},
            {"id": "com.webos.app.hdmi2", "title": "HDMI 2", "type": "input", "alias": "hdmi2"},
            {"id": "com.custom.launcher", "title": "Modern LG Hub", "type": "home", "alias": "home"}
        ]

        # Merge usage count if available
        usage_file = "/var/lib/webosbrew/custom-launcher-usage.json"
        usage_map = {}
        if os.path.exists(usage_file):
            try:
                with open(usage_file, "r") as f:
                    usage_map = json.load(f)
            except Exception:
                pass

        merged = []
        # Prepend PC input
        merged.append(inputs[0])

        for a in apps_data:
            aid = a.get("id", "")
            if aid in ["com.webos.app.hdmi1", "com.custom.launcher"]:
                continue
            item = dict(a)
            item["usage"] = usage_map.get(aid, 0.0)
            merged.append(item)

        # Sort remaining apps by usage descending
        def sort_key(it):
            return it.get("usage", 0.0)

        main_list = merged[1:]
        main_list.sort(key=sort_key, reverse=True)

        return [merged[0]] + main_list

    @staticmethod
    def get_icon_file(app_id: str) -> Optional[str]:
        """Finds icon path for app on disk."""
        search_dirs = [
            "/media/developer/apps/usr/palm/applications/com.custom.launcher/data/icons",
            os.path.join(os.path.dirname(__file__), "..", "custom-launcher", "com.custom.launcher", "data", "icons"),
            os.path.join(os.path.dirname(__file__), "..", "custom-launcher", "design", "fixtures", "assets", app_id)
        ]
        
        for d in search_dirs:
            p = os.path.join(d, f"{app_id}.png")
            if os.path.exists(p):
                return p
            p_icon = os.path.join(d, "icon.png")
            if os.path.exists(p_icon):
                return p_icon
            
        return None
