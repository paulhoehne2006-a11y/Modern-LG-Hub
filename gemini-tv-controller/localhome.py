"""
Local smart-home control over the LAN (no cloud): WiZ bulbs (UDP 38899) and
TP-Link Tapo plugs (KLAP over HTTP). Replaces the SmartThings cloud client.

Devices are listed in devices.json and identified by MAC; their IPs are learned
automatically (WiZ broadcast / ARP table) and cached in ip-cache.json, so DHCP
changes don't break anything.

Tapo needs the TP-Link account e-mail/password for the KLAP handshake (it never
leaves the LAN): TAPO_EMAIL / TAPO_PASSWORD in .env, read on every handshake.
"""

import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES_FILE = os.path.join(HERE, "devices.json")
IP_CACHE_FILE = os.path.join(HERE, "ip-cache.json")
ENV_FILE = os.path.join(HERE, ".env")

WIZ_PORT = 38899
DASHBOARD_TTL = 5.0         # s; the launcher polls the dashboard regularly
REDISCOVER_EVERY = 60.0     # s; at most one LAN rediscovery per minute ...
REDISCOVER_MAX = 600.0      # s; ... backing off to one per 10 min while it finds nothing new
DASHBOARD_WAIT = 20.0       # s; longest a request waits for another thread's dashboard refresh

_MAC_RE = re.compile(r"^[0-9a-f]{12}$")
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_lock = threading.Lock()                 # ip-cache file, KLAP session table, file caches
_discover_lock = threading.Lock()        # one LAN rediscovery at a time
_refresh_lock = threading.Lock()         # one dashboard refresh at a time
# dash/dash_at: last dashboard; gen: bumped by every switch so an older, slower refresh can't
# overwrite the cache with the state from before the switch.
_cache: Dict[str, Any] = {"dash": None, "dash_at": 0.0, "gen": 0, "discovered_at": 0.0,
                          "discover_every": REDISCOVER_EVERY, "caps": {}}
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
KELVIN_MIN, KELVIN_MAX = 2200, 6500     # white range every WiZ white/colour bulb accepts


def norm_mac(mac: str) -> str:
    return re.sub(r"[^0-9a-f]", "", str(mac or "").lower())


def valid_id(dev_id) -> bool:
    return isinstance(dev_id, str) and bool(_MAC_RE.match(dev_id))


def _log(msg: str) -> None:
    print(f"[localhome] {msg}", file=sys.stderr, flush=True)


# ---- small files, re-read only when they change -------------------------------

_file_cache: Dict[str, Any] = {}


def _read_cached(path: str, parse):
    """parse(open file) for path, cached until the file's mtime/size changes. Raises like open()."""
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    hit = _file_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    with open(path, "r", encoding="utf-8") as f:
        value = parse(f)
    with _lock:
        _file_cache[path] = (sig, value)
    return value


def _parse_env(f) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in f:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and k not in out:             # first definition wins (as before)
            out[k] = v.strip().strip('"').strip("'")
    return out


def env_value(key: str) -> str:
    """Setting from the process environment, else from .env next to the backend ("" if unset).

    The .env is parsed only when it changed, so this is cheap enough for every request.
    """
    val = os.environ.get(key, "")
    if val:
        return val
    try:
        return _read_cached(ENV_FILE, _parse_env).get(key, "")
    except (OSError, UnicodeDecodeError):
        return ""


def set_env_value(key: str, value: str) -> None:
    """Replace/add KEY=value in .env, keeping every other line. Atomic, mode 0600 (it holds secrets).

    Raises OSError/UnicodeDecodeError if the existing file can't be read: then nothing is written,
    because rewriting it from an empty list would drop the Tapo login and everything else.
    """
    if not re.match(r"^[A-Z][A-Z0-9_]*$", key) or any(c in value for c in "\r\n\0"):
        raise ValueError("invalid .env entry")
    with _lock:
        lines: List[str] = []
        if os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = [l for l in f.read().splitlines() if not l.strip().startswith(key + "=")]
        lines.append(f"{key}={value}")
        tmp = f"{ENV_FILE}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, ENV_FILE)
            _file_cache.pop(ENV_FILE, None)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


# ---- device list & IP cache -------------------------------------------------

def _parse_devices(f):
    raw = json.load(f).get("devices", [])
    return raw if isinstance(raw, list) else []


def load_devices() -> List[Dict[str, Any]]:
    try:
        raw = _read_cached(DEVICES_FILE, _parse_devices)
    except Exception as e:
        _log(f"devices.json unreadable: {e}")
        return []
    ips = _load_ip_cache()
    out = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        mac = norm_mac(d.get("mac"))
        if not valid_id(mac) or d.get("kind") not in ("wiz", "tapo"):
            continue
        out.append({
            "mac": mac,
            "kind": d["kind"],
            "room": str(d.get("room") or ""),
            "name": str(d.get("name") or mac),
            "ip": ips.get(mac) or str(d.get("ip") or ""),
        })
    return out


def _parse_ip_cache(f) -> Dict[str, str]:
    data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and _IPV4_RE.match(v)}


def _load_ip_cache() -> Dict[str, str]:
    try:
        return dict(_read_cached(IP_CACHE_FILE, _parse_ip_cache))
    except Exception:
        return {}


def _remember_ip(mac: str, ip: str) -> None:
    cache = _load_ip_cache()
    if cache.get(mac) == ip:
        return
    cache[mac] = ip
    with _lock:
        tmp = f"{IP_CACHE_FILE}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, IP_CACHE_FILE)
            _file_cache.pop(IP_CACHE_FILE, None)
        except Exception as e:
            _log(f"ip cache not written: {e}")
    _log(f"{mac} is now at {ip}")


def _device(dev_id: str) -> Optional[Dict[str, Any]]:
    mac = norm_mac(dev_id)
    return next((d for d in load_devices() if d["mac"] == mac), None)


def _subnet_prefix() -> str:
    """'a.b.c.' of the TV's own LAN address (the route towards HUB_GATEWAY, default: the default route)."""
    gw = env_value("HUB_GATEWAY") or "1.1.1.1"   # UDP connect only picks the route, nothing is sent
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((gw, 9))                 # UDP connect sends nothing, it only picks the route
        return s.getsockname()[0].rsplit(".", 1)[0] + "."
    except Exception:
        return gw.rsplit(".", 1)[0] + "." if _IPV4_RE.match(gw) and gw != "1.1.1.1" else "192.168.1."
    finally:
        if s is not None:
            s.close()


def _scan_lan() -> Dict[str, str]:
    """{mac: ip} seen on the LAN right now: WiZ broadcast replies, then the kernel ARP table. ~3.5 s."""
    found: Dict[str, str] = {}
    # WiZ answers a broadcast with its MAC.
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
        msg = b'{"method":"getSystemConfig","params":{}}'
        end = time.time() + 2.0
        next_send = 0.0
        while time.time() < end:
            if time.time() >= next_send:
                s.sendto(msg, ("255.255.255.255", WIZ_PORT))
                next_send = time.time() + 0.5
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                mac = norm_mac(json.loads(data).get("result", {}).get("mac"))
            except Exception:
                continue
            if valid_id(mac):
                found[mac] = addr[0]
    except Exception as e:
        _log(f"wiz discovery failed: {e}")
    finally:
        if s is not None:
            s.close()
    # Everything else (Tapo): touch every address so the kernel ARP table fills up.
    s = None
    try:
        prefix = _subnet_prefix()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(1, 255):
            try:
                s.sendto(b"", (prefix + str(i), 9))
            except OSError:
                pass
        s.close()
        s = None
        time.sleep(1.5)
        with open("/proc/net/arp", "r") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[2] != "0x0":
                    mac = norm_mac(parts[3])
                    if valid_id(mac) and mac != "000000000000":
                        found.setdefault(mac, parts[0])
    except Exception as e:
        _log(f"arp discovery failed: {e}")
    finally:
        if s is not None:
            s.close()
    return found


def rediscover(force: bool = False, min_interval: Optional[float] = None) -> Dict[str, str]:
    """Find the current IPs of all devices (WiZ broadcast + ARP table) and update ip-cache.json.

    Rate-limited: returns {} without scanning if the last scan is too recent. The interval
    starts at REDISCOVER_EVERY and doubles (up to REDISCOVER_MAX) while scans find no new IP,
    so a lamp that is simply switched off at the wall doesn't cause a subnet sweep every minute.
    min_interval overrides that back-off (used when the owner actively switches a device).
    Concurrent callers wait for a running scan instead of starting their own; afterwards the
    new IPs are in the ip cache (see _with_rediscovery).
    """
    with _discover_lock:
        now = time.time()
        every = _cache["discover_every"] if min_interval is None else min_interval
        if not force and now - _cache["discovered_at"] < every:
            return {}
        _cache["discovered_at"] = now
        found = _scan_lan()
        known = {d["mac"]: d["ip"] for d in load_devices()}
        moved = False
        for mac, ip in found.items():
            if mac in known and known[mac] != ip:
                _remember_ip(mac, ip)
                moved = True
        _cache["discover_every"] = (REDISCOVER_EVERY if moved
                                    else min(_cache["discover_every"] * 2, REDISCOVER_MAX))
        return found


# ---- WiZ (UDP JSON) ------------------------------------------------------------

def _wiz_call(ip: str, method: str, params: Optional[dict] = None, tries: int = 3) -> Dict[str, Any]:
    if not ip:
        raise OSError("no IP")
    msg = json.dumps({"id": 1, "method": method, "params": params or {}}).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(tries):
            s.sendto(msg, (ip, WIZ_PORT))
            deadline = time.time() + 0.5
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                s.settimeout(left)
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    break
                if addr[0] != ip:
                    continue
                try:
                    res = json.loads(data)
                except ValueError:
                    continue                    # garbage datagram: keep waiting for the real answer
                if not isinstance(res, dict):
                    continue
                if "error" in res:
                    raise OSError(f"WiZ error: {res['error']}")
                if res.get("method") == method:
                    result = res.get("result")
                    return result if isinstance(result, dict) else {}
    finally:
        s.close()
    raise OSError(f"no answer from {ip}")


# WiZ scenes without own colour info -> approximate colour temperature
_SCENE_KELVIN = {6: 2200, 9: 4000, 10: 2700, 11: 2700, 12: 4200, 13: 6500, 14: 2200, 16: 2200,
                 18: 2400, 29: 3000, 30: 3500, 31: 2700, 32: 3200}


def _kelvin_rgb(k: float):
    """Colour of a white light at k kelvin (Tanner Helland's approximation)."""
    import math
    t = max(1000.0, min(40000.0, float(k))) / 100.0
    r = 255.0 if t <= 66 else 329.698727446 * ((t - 60) ** -0.1332047592)
    g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * ((t - 60) ** -0.0755148492)
    b = 255.0 if t >= 66 else (0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307)
    return tuple(int(max(0, min(255, v))) for v in (r, g, b))


def _wiz_color(r: dict) -> str:
    if all(isinstance(r.get(c), int) for c in ("r", "g", "b")) and (r["r"] or r["g"] or r["b"]):
        rgb = (r["r"], r["g"], r["b"])
    elif isinstance(r.get("temp"), (int, float)):
        rgb = _kelvin_rgb(r["temp"])
    else:
        rgb = _kelvin_rgb(_SCENE_KELVIN.get(r.get("sceneId"), 2700))
    return "#%02x%02x%02x" % rgb


def _wiz_caps(d) -> List[str]:
    """What a WiZ bulb can do, from its module name (…SHRGB… colour, …SHTW… tunable white, …SHDW… dim only).

    Asked once per bulb and cached; unknown modules are treated as full colour bulbs."""
    caps = _cache["caps"].get(d["mac"])
    if caps is None:
        name = str(_wiz_call(d["ip"], "getSystemConfig", tries=2).get("moduleName") or "").upper()
        if "RGB" in name or not name:
            caps = ["brightness", "color", "white"]
        elif "TW" in name:
            caps = ["brightness", "white"]
        else:
            caps = ["brightness"]
        _cache["caps"][d["mac"]] = caps
    return caps


def _wiz_status(d) -> Dict[str, Any]:
    r = _wiz_call(d["ip"], "getPilot")
    out = {"state": "on" if r.get("state") else "off", "brightness": r.get("dimming"), "color": _wiz_color(r)}
    if isinstance(r.get("temp"), (int, float)) and r["temp"]:
        out["kelvin"] = int(r["temp"])
    try:
        out["capabilities"] = _wiz_caps(d)
    except Exception:
        out["capabilities"] = ["brightness", "color", "white"]
    return out


def parse_color(value) -> Optional[tuple]:
    """'#ff8800' / 'ff8800' -> (255, 136, 0); anything else -> None."""
    m = _HEX_RE.match(str(value or "").strip())
    if not m:
        return None
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _wiz_set(d, on: Optional[bool] = None, brightness: Optional[int] = None,
             color: Optional[tuple] = None, kelvin: Optional[int] = None) -> None:
    params: Dict[str, Any] = {}
    if on is not None:
        params["state"] = bool(on)
    if brightness is not None:
        params["dimming"] = max(10, min(100, int(brightness)))   # WiZ accepts 10..100
        params.setdefault("state", True)
    if color is not None:
        params["r"], params["g"], params["b"] = (max(0, min(255, int(c))) for c in color)
        params.setdefault("state", True)
    elif kelvin is not None:
        params["temp"] = max(KELVIN_MIN, min(KELVIN_MAX, int(kelvin)))
        params.setdefault("state", True)
    _wiz_call(d["ip"], "setPilot", params)


# ---- Tapo (KLAP) -------------------------------------------------------------

class _AES:
    """AES-128-CBC with PKCS7 via the system libcrypto (no Python crypto package on the TV)."""
    _lib = None

    @classmethod
    def lib(cls):
        if cls._lib is None:
            lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
            lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
            lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
            lib.EVP_aes_128_cbc.restype = ctypes.c_void_p
            for fn in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
                getattr(lib, fn).argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_char_p, ctypes.c_char_p]
            for fn in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
                getattr(lib, fn).argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
                                             ctypes.c_char_p, ctypes.c_int]
            for fn in ("EVP_EncryptFinal_ex", "EVP_DecryptFinal_ex"):
                getattr(lib, fn).argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
            cls._lib = lib
        return cls._lib

    @classmethod
    def run(cls, encrypt: bool, key: bytes, iv: bytes, data: bytes) -> bytes:
        lib = cls.lib()
        pre = "EVP_Encrypt" if encrypt else "EVP_Decrypt"
        ctx = lib.EVP_CIPHER_CTX_new()
        try:
            if not getattr(lib, pre + "Init_ex")(ctx, lib.EVP_aes_128_cbc(), None, key, iv):
                raise OSError("AES init failed")
            buf = ctypes.create_string_buffer(len(data) + 32)
            n1, n2 = ctypes.c_int(0), ctypes.c_int(0)
            if not getattr(lib, pre + "Update")(ctx, buf, ctypes.byref(n1), data, len(data)):
                raise OSError("AES update failed")
            tail = ctypes.create_string_buffer(32)
            if not getattr(lib, pre + "Final_ex")(ctx, tail, ctypes.byref(n2)):
                raise OSError("AES final failed (wrong key?)")
            return buf.raw[:n1.value] + tail.raw[:n2.value]
        finally:
            lib.EVP_CIPHER_CTX_free(ctx)


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


_env_value = env_value        # old name


def _tapo_credentials():
    email, pw = env_value("TAPO_EMAIL"), env_value("TAPO_PASSWORD")
    creds = []
    if email and pw:
        creds.append((email, pw))
    creds += [("test@tp-link.net", "test"), ("", "")]   # plugs not (yet) bound to an account
    return creds


class _KlapSession:
    def __init__(self, ip: str):
        self.ip = ip
        self.lock = threading.Lock()
        self.cookie = None
        self.expires = 0.0

    def _post(self, path: str, body: bytes, timeout: float = 4.0):
        req = urllib.request.Request(f"http://{self.ip}/app/{path}", data=body, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        with _LAN_OPENER.open(req, timeout=timeout) as r:
            return r.read(65536), r.headers.get("Set-Cookie", "")

    def handshake(self) -> None:
        self.cookie = None
        try:
            self._handshake()
        except BaseException:
            self.cookie = None          # never keep a half-made session (cookie without key)
            raise

    def _handshake(self) -> None:
        local = os.urandom(16)
        resp, cookie = self._post("handshake1", local)
        m = re.search(r"TP_SESSIONID=([0-9A-Fa-f]+)", cookie or "")
        if len(resp) != 48 or not m:
            raise OSError("unexpected KLAP handshake1 reply")
        self.cookie = "TP_SESSIONID=" + m.group(1)
        remote, server_hash = resp[:16], resp[16:]
        auth = version = None
        for email, pw in _tapo_credentials():
            v2 = _sha256(hashlib.sha1(email.encode()).digest() + hashlib.sha1(pw.encode()).digest())
            if _sha256(local + remote + v2) == server_hash:
                auth, version = v2, 2
                break
            v1 = hashlib.md5(hashlib.md5(email.encode()).digest() + hashlib.md5(pw.encode()).digest()).digest()
            if _sha256(local + v1) == server_hash:
                auth, version = v1, 1
                break
        if auth is None:
            raise PermissionError("Tapo-Login fehlgeschlagen: TAPO_EMAIL/TAPO_PASSWORD in .env prüfen")
        proof = _sha256(remote + local + auth) if version == 2 else _sha256(remote + auth)
        self._post("handshake2", proof)
        self.key = _sha256(b"lsk" + local + remote + auth)[:16]
        ivf = _sha256(b"iv" + local + remote + auth)
        self.iv, self.seq = ivf[:12], int.from_bytes(ivf[-4:], "big", signed=True)
        self.sig = _sha256(b"ldk" + local + remote + auth)[:28]
        self.expires = time.time() + 20 * 3600

    def _request(self, payload: dict) -> dict:
        self.seq = self.seq + 1 if self.seq < 0x7FFFFFFF else -0x80000000
        seq_b = self.seq.to_bytes(4, "big", signed=True)
        iv = self.iv + seq_b
        enc = _AES.run(True, self.key, iv, json.dumps(payload).encode())
        body = _sha256(self.sig + seq_b + enc) + enc
        resp, _ = self._post(f"request?seq={self.seq}", body)
        return json.loads(_AES.run(False, self.key, iv, resp[32:]))

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        with self.lock:
            for attempt in (0, 1):
                if not self.cookie or time.time() > self.expires:
                    self.handshake()
                try:
                    res = self._request(payload)
                except (urllib.error.HTTPError, OSError, ValueError):
                    if attempt:
                        raise
                    self.cookie = None      # session gone (timeout / plug rebooted): handshake again
                    continue
                if not isinstance(res, dict):
                    raise OSError("unexpected Tapo reply")
                code = res.get("error_code", 0)
                if code == 0:
                    return res.get("result", {})
                if attempt or code not in (-1012, 9999, 1002):
                    raise OSError(f"Tapo error_code {code}")
                self.cookie = None
        raise OSError("Tapo request failed")


# LAN devices are always reached directly, never through an http_proxy from the environment.
_LAN_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_klap: Dict[str, _KlapSession] = {}


def _tapo(d) -> _KlapSession:
    with _lock:
        s = _klap.get(d["mac"])
        if s is None or s.ip != d["ip"]:
            s = _klap[d["mac"]] = _KlapSession(d["ip"])
        return s


def _tapo_status(d) -> Dict[str, Any]:
    r = _tapo(d).call("get_device_info")
    mac = norm_mac(r.get("mac"))
    if mac and mac != d["mac"]:
        raise OSError(f"{d['ip']} is another device ({mac})")
    return {"state": "on" if r.get("device_on") else "off", "brightness": None, "capabilities": []}


def _tapo_set(d, on: Optional[bool] = None, brightness: Optional[int] = None,
              color: Optional[tuple] = None, kelvin: Optional[int] = None) -> None:
    if on is None:
        on = True
    _tapo(d).call("set_device_info", {"device_on": bool(on)})


# ---- public API (same shapes the launcher already uses) ------------------------

def _with_rediscovery(d, fn, eager: bool = False):
    """Run fn(d); if the device doesn't answer, look for its new IP once and retry.

    Retries whenever the ip cache now has another address for the device, also when the
    rediscovery that found it ran in another thread (this call then only waited for it).
    eager: the owner is switching this device right now, so ignore the scan back-off.
    """
    try:
        return fn(d)
    except PermissionError:
        raise
    except Exception as first:
        rediscover(min_interval=REDISCOVER_EVERY if eager else None)
        nd = _device(d["mac"])
        if not nd or not nd["ip"] or nd["ip"] == d["ip"]:
            raise first
        return fn(nd)


def _status(d) -> Dict[str, Any]:
    return _with_rediscovery(d, _wiz_status if d["kind"] == "wiz" else _tapo_status)


def _device_type(d) -> str:
    return "light" if d["kind"] == "wiz" else "plug"


def display_name(d) -> str:
    return f"{d['room']} {d['name']}".strip()


def _read_dashboard() -> List[Dict[str, Any]]:
    devices = load_devices()

    def one(d):
        try:
            return _status(d), None
        except Exception as e:
            return None, str(e)

    if not devices:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(devices))) as pool:
        results = list(pool.map(one, devices))
    out = []
    for d, (st, err) in zip(devices, results):
        out.append({
            "id": d["mac"],
            "name": d["name"],
            "room": d["room"],
            "raw_name": display_name(d),
            "type": _device_type(d),
            "state": st["state"] if st else "off",
            "brightness": st["brightness"] if st else None,
            "color": st.get("color") if st else None,
            "kelvin": st.get("kelvin") if st else None,
            "capabilities": st.get("capabilities", []) if st else [],
            "online": st is not None,
            **({"error": err} if err else {}),
        })
    return out


def get_dashboard_devices(max_age: Optional[float] = None) -> List[Dict[str, Any]]:
    """All devices with their current state, in devices.json order (cached a few seconds).

    Only one refresh runs at a time (an unreachable device makes one take seconds). While it
    runs, other callers get the previous, slightly older list at once instead of piling up
    behind it; only callers with no usable list at all (first call, right after a switch) wait.
    """
    if max_age is None:
        max_age = DASHBOARD_TTL
    dash, at = _cache["dash"], _cache["dash_at"]
    if dash is not None and time.time() - at < max_age:
        return dash
    if dash is not None:
        if not _refresh_lock.acquire(blocking=False):
            return dash                                  # stale-while-revalidate
    elif not _refresh_lock.acquire(timeout=DASHBOARD_WAIT):
        return _cache["dash"] or []
    try:
        dash, at = _cache["dash"], _cache["dash_at"]     # filled by the refresh we waited for?
        if dash is not None and time.time() - at < max_age:
            return dash
        gen = _cache["gen"]
        out = _read_dashboard()
        with _lock:
            if _cache["gen"] == gen:                     # no switch happened meanwhile
                _cache["dash"], _cache["dash_at"] = out, time.time()
        return out
    finally:
        _refresh_lock.release()


def dashboard_age() -> Optional[float]:
    """Seconds since the cached dashboard was read, None if there is none."""
    return None if _cache["dash"] is None else max(0.0, time.time() - _cache["dash_at"])


def invalidate_cache() -> None:
    with _lock:
        _cache["gen"] += 1
        _cache["dash"] = None


_switch_listeners: List[Any] = []


def add_switch_listener(fn) -> None:
    """fn(mac) runs after every switch the owner makes (hub, Gemini, phone app); e.g. the Spotify
    ambilight hands a lamp back to the owner. Not called for the ambilight's own writes."""
    if fn not in _switch_listeners:
        _switch_listeners.append(fn)


def set_switch(dev_id: str, state, brightness: Optional[int] = None,
               color: Optional[tuple] = None, kelvin: Optional[int] = None) -> Dict[str, Any]:
    """Switch a device; for WiZ bulbs optionally also brightness, an RGB colour or a white tone (kelvin)."""
    d = _device(dev_id) if valid_id(norm_mac(dev_id)) else None
    if not d:
        return {"status": "error", "error": "Unbekanntes Gerät"}
    for fn in list(_switch_listeners):
        try:
            fn(d["mac"])
        except Exception as e:
            _log(f"switch listener failed: {e}")
    on = str(state).lower().strip() in ("on", "an", "ein", "true", "1")
    setter = _wiz_set if d["kind"] == "wiz" else _tapo_set
    try:
        _with_rediscovery(d, lambda x: setter(x, on, brightness if on else None,
                                              color if on else None, kelvin if on else None),
                          eager=True)
    except PermissionError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error", "error": f"{display_name(d)} nicht erreichbar ({e})"}
    finally:
        invalidate_cache()
    return {"status": "ok", "state": "on" if on else "off", "new_state": "on" if on else "off"}


def toggle_switch(dev_id: str) -> Dict[str, Any]:
    d = _device(dev_id) if valid_id(norm_mac(dev_id)) else None
    if not d:
        return {"status": "error", "error": "Unbekanntes Gerät"}
    try:
        cur = _with_rediscovery(d, _wiz_status if d["kind"] == "wiz" else _tapo_status, eager=True)["state"]
    except Exception as e:
        return {"status": "error", "error": f"{display_name(d)} nicht erreichbar ({e})"}
    return set_switch(d["mac"], "off" if cur == "on" else "on")


def _words(s: str) -> List[str]:
    s = s.lower().replace("ß", "ss")
    return [w for w in re.split(r"[^0-9a-zäöü]+", s) if w]


_FILLER = {"das", "die", "der", "den", "im", "in", "licht", "lampe", "lampen", "alle", "alles", "von", "mein", "meine"}


def find_devices(query: str) -> List[Dict[str, Any]]:
    """Devices matching a spoken name ('Wohnzimmer', 'Bar im Wohnzimmer', 'Bett').

    A room on its own means every device in that room; otherwise the best-scoring
    device(s) win. Partial words count ('Decke' -> 'Deckenlampe').
    """
    q = _words(query)
    if not q:
        return []
    devices = load_devices()
    rooms = {}
    for d in devices:
        rooms.setdefault(d["room"].lower(), []).append(d)
    # whole room ("Wohnzimmer", "alles im Wohnzimmer", "Licht im Büro" when the room has one light only)
    for room, devs in rooms.items():
        rw = _words(room)
        rest = [w for w in q if not any(w == r or (len(w) >= 4 and r.startswith(w)) for r in rw)]
        if len(rest) < len(q) and not [w for w in rest if w not in _FILLER]:
            return devs

    def score(d):
        words = _words(display_name(d))
        sc = 0
        for w in q:
            if w in words:
                sc += 3
            elif len(w) >= 3 and any(x.startswith(w) or w.startswith(x) and len(x) >= 3 for x in words):
                sc += 2
            elif len(w) >= 4 and any(w in x for x in words):
                sc += 1
            elif any(len(os.path.commonprefix([w, x])) >= 5 for x in words):   # Deckenlicht ~ Deckenlampe
                sc += 1
        return sc

    scored = [(score(d), d) for d in devices]
    best = max((s for s, _ in scored), default=0)
    return [d for s, d in scored if s == best] if best > 0 else []


if __name__ == "__main__":
    # python3 localhome.py [status|discover|on <mac>|off <mac>|find <text>]
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "discover":
        print(json.dumps(rediscover(force=True), indent=1))
    elif cmd in ("on", "off"):
        print(set_switch(sys.argv[2], cmd))
    elif cmd == "find":
        print([display_name(d) for d in find_devices(" ".join(sys.argv[2:]))])
    else:
        for d in get_dashboard_devices(0):
            print(f"{d['id']}  {d['type']:5}  {d['state']:3}  {'ok ' if d['online'] else 'OFF'} "
                  f"{d['room']} / {d['name']}  {d.get('error', '')}")
