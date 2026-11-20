"""
LG webOS TV Controller (Dual-Mode: Local on TV or Remote via SSH)
Supports app launching, inputs, volume, OLED screen off, notifications, media keys,
audio output switching (Denon AVR / TV Speaker / BT), direct text input, and launcher sync.
"""

import os
import re
import subprocess
import json
import base64
import time
import glob
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional, List, Tuple

TV_HOST = os.environ.get("TV_HOST", "root@192.168.178.60")
if TV_HOST.startswith("-"):  # would be parsed as an ssh option
    raise ValueError("TV_HOST must not start with '-'")
IS_LOCAL = os.path.exists("/usr/sbin/luna-send") or os.path.exists("/usr/bin/luna-send")

# accept-new: trust the TV's key on first connect, but refuse if it later changes (MITM).
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=2"
]
# SSH multiplexing needs Unix sockets, which Windows OpenSSH does not support.
if os.name != "nt":
    SSH_OPTS = [
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=/tmp/ssh_mux_%h_%p",
        "-o", "ControlPersist=60",
    ] + SSH_OPTS

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

# The URI is part of a remote shell command in SSH mode: only plain luna:// paths are accepted.
_LUNA_URI = re.compile(r"^luna://[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$")
APP_DIR = "/media/developer/apps/usr/palm/applications/com.custom.launcher"
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEV_APP_DIR = os.path.join(_HERE, "..", "custom-launcher", "com.custom.launcher")
USAGE_FILE = "/var/lib/webosbrew/custom-launcher-usage.json"
USAGE_HALF_LIFE = 45 * 86400        # same decay as launcherd.py
ICON_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".gif": "image/gif"}     # no SVG: it could carry script


def luna_call(uri: str, payload: dict, timeout: int = 5) -> dict:
    """Executes a Luna call either locally or via SSH with multiplexing. Never raises."""
    if not _LUNA_URI.match(uri):
        return {"returnValue": False, "error": "invalid luna uri"}
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
    if not _LUNA_URI.match(uri):
        return False
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

STATUS_TTL = 1.0            # s; see LGTV.get_status_cached
_status_lock = threading.Lock()
_status_cache: Dict[str, Any] = {"data": None, "at": 0.0, "gen": 0}
_pool = ThreadPoolExecutor(max_workers=6)   # only runs single luna calls (never nested)
_net_cache: Dict[str, Any] = {}


def _luna_many(calls: List[Tuple[str, dict]], timeout: int = 5) -> List[dict]:
    """Run several luna_call()s in parallel; results in the same order."""
    futures = [_pool.submit(luna_call, uri, payload, timeout) for uri, payload in calls]
    out = []
    for f in futures:
        try:
            out.append(f.result(timeout=timeout + 3))
        except Exception as e:
            out.append({"returnValue": False, "error": type(e).__name__})
    return out


def _audio_status(sound_out: dict, vol_info: dict) -> Dict[str, Any]:
    output_val = sound_out.get("soundOutput", "external_optical")
    if not isinstance(output_val, str):
        output_val = "external_optical"
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


class LGTV:
    @staticmethod
    def get_status() -> Dict[str, Any]:
        """Returns power state, screen state, foreground app, audio state, and network info.

        The four luna calls run in parallel: the answer takes as long as the slowest one,
        not as long as all four together (each spawns a luna-send on a busy TV).
        """
        fg, power, sound_out, vol_info = _luna_many([
            ("luna://com.webos.applicationManager/getForegroundAppInfo", {}),
            ("luna://com.webos.service.tvpower/power/getPowerState", {}),
            ("luna://com.webos.service.audio/getSoundOutput", {}),
            ("luna://com.webos.service.audio/getVolume", {}),
        ])
        power_state = power.get("state", "Unknown")
        is_screen_off = (str(power_state).lower() in ["screen off", "screen_off", "display off"])
        audio = _audio_status(sound_out, vol_info)
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
    def get_status_cached(max_age: float = STATUS_TTL) -> Dict[str, Any]:
        """get_status(), shared by requests that arrive within max_age seconds of each other.

        Phones poll every few seconds; several pollers (app, widget, notification) then cost
        one set of luna calls instead of one each. Only one refresh runs at a time; callers
        that arrive meanwhile wait for it. invalidate_status() after anything that changes
        the TV's state, so the next poll shows the change.
        """
        hit = _status_cache["data"]
        if hit is not None and time.monotonic() - _status_cache["at"] < max_age:
            return hit
        with _status_lock:
            hit = _status_cache["data"]
            if hit is not None and time.monotonic() - _status_cache["at"] < max_age:
                return hit
            gen = _status_cache["gen"]
            data = LGTV.get_status()
            if _status_cache["gen"] == gen:
                _status_cache["data"], _status_cache["at"] = data, time.monotonic()
            return data

    @staticmethod
    def foreground_app(max_age: float = 3.0) -> str:
        """App in front; from the status cache if it is younger than max_age (the phone polls
        status every 3 s), so a long OK costs no extra luna call within its 1.5 s deadline."""
        hit = _status_cache["data"]
        if hit is not None and time.monotonic() - _status_cache["at"] < max_age:
            return str(hit.get("current_app") or "")
        fg = luna_call("luna://com.webos.applicationManager/getForegroundAppInfo", {})
        return str(fg.get("appId") or "")

    @staticmethod
    def invalidate_status() -> None:
        _status_cache["gen"] += 1
        _status_cache["data"] = None

    @staticmethod
    def send_remote_key(key: str, fast: bool = True) -> Dict[str, Any]:
        """Sends remote control key (UP, DOWN, LEFT, RIGHT, OK, BACK, HOME, etc.)."""
        normalized = KEY_MAP.get(key.lower().strip(), key.upper().strip())
        if normalized in ("LONG_ENTER", "OK_LONG"):
            # A held OK cannot be sent over the network input (it only knows press+release).
            # The hub opens its options menu on INFO instead; any other app gets a plain OK.
            normalized = "INFO" if LGTV.foreground_app() == "com.custom.launcher" else "ENTER"
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
        name = input_name.lower().strip()
        # Accept full webOS ids too (the Android remote sends "com.webos.app.hdmi2").
        app_id = name if name.startswith("com.webos.app.hdmi") else INPUT_MAP.get(name, "com.webos.app.hdmi1")
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
    def set_picture_mode(mode: str) -> Dict[str, Any]:
        """Sets picture mode: game, filmMaker, cinema, expert1 (ISF Dark), expert2 (ISF Bright), standard."""
        mode_map = {
            "game": "game",
            "filmmaker": "filmMaker",
            "cinema": "cinema",
            "isf_dark": "expert1",
            "isf_bright": "expert2",
            "standard": "standard"
        }
        pic_mode = mode_map.get(mode.lower().strip(), mode)
        res = luna_call("luna://com.webos.settingsservice/setSystemSettings", {
            "category": "picture",
            "settings": {"pictureMode": pic_mode}
        })
        return {"action": "set_picture_mode", "mode": pic_mode, "success": res.get("returnValue", False)}

    @staticmethod
    def get_audio_status() -> Dict[str, Any]:
        """Returns sound output and volume details."""
        sound_out, vol_info = _luna_many([
            ("luna://com.webos.service.audio/getSoundOutput", {}),
            ("luna://com.webos.service.audio/getVolume", {}),
        ])
        return _audio_status(sound_out, vol_info)

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
        ok = send_key("ENTER", fast=True)
        return {"action": "send_enter", "success": ok}

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
        """Retrieves network interfaces and MAC addresses for Wake-on-LAN.

        MACs don't change while the backend runs: once found, they are remembered."""
        if _net_cache.get("macs"):
            macs = dict(_net_cache["macs"])
            return {"mac_addresses": macs, "primary_mac": macs.get("eth0") or macs.get("wlan0") or ""}
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
            wired = res.get("wired") if isinstance(res.get("wired"), dict) else {}
            wifi = res.get("wifi") if isinstance(res.get("wifi"), dict) else {}
            if wired.get("macAddress"):
                macs["eth0"] = wired.get("macAddress")
            if wifi.get("macAddress"):
                macs["wlan0"] = wifi.get("macAddress")
        macs = {k: v for k, v in macs.items() if isinstance(v, str)}
        if macs:
            _net_cache["macs"] = dict(macs)

        return {
            "mac_addresses": macs,
            "primary_mac": macs.get("eth0") or macs.get("wlan0") or ""
        }

    @staticmethod
    def get_launcher_apps() -> List[Dict[str, Any]]:
        """Returns the apps and inputs the launcher shows: PC (HDMI 1) first, then by usage.

        The list is launcherd.py's data/apps.json; usage comes from its usage statistics
        (decayed launch counts, same half-life as the launcher uses).
        """
        apps_data = []

        # Check paths for Modern LG Hub data
        for cp in _apps_json_paths():
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
        apps_data = [a for a in apps_data if isinstance(a, dict)] if isinstance(apps_data, list) else []

        # Fallback to luna listApps if custom-launcher data not present
        if not apps_data:
            res = luna_call("luna://com.webos.applicationManager/listApps", {})
            raw_apps = res.get("apps", [])
            for a in raw_apps if isinstance(raw_apps, list) else []:
                if not isinstance(a, dict):
                    continue
                app_id = a.get("id")
                if not a.get("visible", True) or not app_id:
                    continue
                apps_data.append({
                    "id": app_id,
                    "title": a.get("title", app_id),
                    "icon": a.get("largeIcon") or a.get("icon", "")
                })

        # PC (HDMI 1) always comes first; the launcher itself is not listed
        pc = {"id": "com.webos.app.hdmi1", "title": "PC (HDMI 1)", "type": "input", "alias": "pc"}

        usage_map = load_usage()

        merged = [pc]

        for a in apps_data:
            aid = a.get("id", "")
            if aid in ["com.webos.app.hdmi1", "com.custom.launcher"]:
                continue
            item = dict(a)
            item["usage"] = usage_map.get(aid, 0.0)
            merged.append(item)

        # Sort remaining apps by usage descending (stable: ties keep the apps.json order)
        main_list = merged[1:]
        main_list.sort(key=lambda it: it.get("usage", 0.0), reverse=True)

        return [merged[0]] + main_list

    @staticmethod
    def get_icon_file(app_id: str) -> Optional[str]:
        """Finds the icon launcherd.py copied for app_id (any image type), or None.

        app_id must already be validated (no path separators)."""
        if not app_id or "/" in app_id or "\\" in app_id or app_id.startswith("."):
            return None
        for d in _icon_dirs():
            for ext in ICON_TYPES:
                p = os.path.join(d, app_id + ext)
                if os.path.isfile(p):
                    return p
        # PC preview: the design fixtures keep one folder per app
        p = os.path.join(_DEV_APP_DIR, "..", "design", "fixtures", "assets", app_id, "icon.png")
        if os.path.isfile(p):
            return p
        return None


def _apps_json_paths() -> List[str]:
    return [os.path.join(APP_DIR, "data", "apps.json"), os.path.join(_DEV_APP_DIR, "data", "apps.json")]


def _icon_dirs() -> List[str]:
    return [os.path.join(APP_DIR, "data", "icons"), os.path.join(_DEV_APP_DIR, "data", "icons")]


def apps_version() -> int:
    """mtime (s) of the app list the launcher uses; changes whenever launcherd rewrites it. 0 = none."""
    for p in _apps_json_paths():
        try:
            return int(os.stat(p).st_mtime)
        except OSError:
            continue
    return 0


def load_usage(now: Optional[float] = None) -> Dict[str, float]:
    """{app id: decayed launch count} from launcherd's statistics.

    launcherd writes {"updatedAt": ms, "usage": {id: {"n": count, "t": last use (s)}}};
    a flat {id: number} map (older format) is accepted too.
    """
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    table = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
    now = time.time() if now is None else now
    out: Dict[str, float] = {}
    for app, v in table.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[app] = float(v)
        elif isinstance(v, dict) and isinstance(v.get("n"), (int, float)) and isinstance(v.get("t"), (int, float)):
            age = max(0.0, now - float(v["t"]))
            out[app] = round(float(v["n"]) * 0.5 ** (age / USAGE_HALF_LIFE), 4)
    return out
