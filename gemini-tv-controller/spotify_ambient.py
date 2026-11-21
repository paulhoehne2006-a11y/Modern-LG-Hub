"""
Spotify-Licht / Ambilight: one WiZ lamp takes the colour of what the TV shows.

Everything is read on the TV itself, nothing asks the Spotify Web API (that got the app banned):

    Spotify     the Spotify app on the TV is a web app; its page is read through the local web
                inspector (127.0.0.1:9998, the app is marked "inspectable", see tv-setup in
                docs/API.md). It tells whether a track plays (its <video> element), title, artist,
                cover and the background colour Spotify picked for the song (now-playing view).
                Without that colour, the cover's dominant colour is used.
    Screen      mode "ambilight" only, while Spotify does not play: a 320x180 snapshot of the
                screen (com.webos.service.capture/executeOneShot) every POLL_SCREEN s, its
                strongest hue. The hub itself and the home screen do not count.

    worker thread   reads the source, calibrates the colour and fades the lamp (FADE_STEPS steps).
    REST            server.py exposes status(), set_mode(), set_calibration(), set_lamp().

Modes: "off", "spotify" (default), "ambilight" (Spotify first, otherwise the screen).

The lamp is only coloured when it is already on. Its state is read (getPilot) right before the
first change and written back, faded, once the source stops for PAUSE_GRACE s. A change made
through the hub (localhome.set_switch) or a lamp found switched off means "the owner took over":
the lamp is left alone until the source stops.

Settings (mode, calibration, lamp) live in spotify-ambient.json next to this file (gitignored);
the saved lamp state is kept there too, so a restarted backend can still put the lamp back.
Nothing in here ever runs on a request thread: the HTTP handlers only touch memory and wake the
worker.
"""

import base64
import colorsys
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import localhome
import jpeg_dc

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "spotify-ambient.json")

INSPECTOR = ("127.0.0.1", 9998)  # WebAppMgr's web inspector (firewalled to the TV itself)
SPOTIFY_APP = "spotify-beehive"
SCREEN_FILE = "/tmp/spotify-ambient-screen.jpg"
SCREEN_SIZE = (320, 180)
SKIP_APPS = ("com.custom.launcher", "com.webos.app.home", "com.webos.app.homeconnect",
             "com.webos.app.screensaver", "com.webos.app.livetv-screensaver")

POLL_SPOTIFY = 1.0        # s between reads while a track plays (all local, no API)
POLL_SCREEN = 0.5         # s between screen snapshots (ambilight)
POLL_IDLE = 2.0           # s between reads while nothing plays
POLL_OFF = 3600.0         # mode off: sleeps until a setting changes (wake)
POLL_LAMP_OFF = 30.0      # s between lamp checks while the source runs but the lamp is off
LAMP_CHECK = 20.0         # s between "is the lamp still on" checks during ambilight
PAUSE_GRACE = 6.0         # s without a source before the lamp goes back
FADE_STEPS = 5
FADE_TIME = 0.35          # s for a song-change fade
FOLLOW_STEPS = 3          # small fades while the colour follows the screen / Spotify's animation
FOLLOW_TIME = 0.2
CHANGE_MIN = 10           # rgb difference below which nothing is sent
PREVIEW_HOLD = 10.0       # s a calibration preview stays on the lamp when nothing plays
TIMEOUT = 2.0             # s for one inspector / luna call
COVER_TIMEOUT = 8.0
MODES = ("off", "spotify", "ambilight")
OLD_MODES = {"tv": "spotify", "all": "spotify"}
DEFAULTS = {"mode": "spotify", "saturation": 1.4, "brightness": 35, "hue_shift": 0, "lamp": ""}
NEUTRAL_KELVIN = 4000     # grey covers / grey screen
PREVIEW_SAMPLE = (241, 118, 38)   # preview colour before any colour was seen
DEFAULT_LAMP_NAME = "Fernseher Bar"


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [ambient] {msg}", file=sys.stderr, flush=True)


# ---- colour ---------------------------------------------------------------------

def _sat(c) -> float:
    return (max(c) - min(c)) / 255.0


def _lum(c) -> float:
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255.0


HUE_BINS = 24
ACCENT_MIN = 0.012        # hue "mass" per sample a picture needs for an accent colour


def dominant_color(pixels: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float, float]]:
    """The colour Spotify would pick: the strongest hue, not the average of the area.

    Every sample votes for its hue with its HSL saturation squared (damped for near-black and
    near-white), so a small but vivid title (Drake "More Life": blue letters on a big brown photo)
    beats a large dull area, like Spotify's lyrics background does. The colour is the weighted mean
    of that hue +-15 deg. Pictures without a clear hue get their saturation-weighted mean colour
    (_mean_color, the launcher's accentOf rule); None for grey ones."""
    hist = [0.0] * HUE_BINS
    samples = []
    for c in pixels:
        h, l, s = colorsys.rgb_to_hls(*(max(0.0, min(255.0, x)) / 255.0 for x in c))
        if l < 0.04 or l > 0.97 or s < 0.1:
            continue
        w = s * s * min(1.0, l / 0.25) * min(1.0, (1.0 - l) / 0.2)
        b = int(h * HUE_BINS) % HUE_BINS
        hist[b] += w
        samples.append((c, b, w))
    if samples:
        smooth = [hist[i - 1] * 0.5 + hist[i] + hist[(i + 1) % HUE_BINS] * 0.5 for i in range(HUE_BINS)]
        peak = max(range(HUE_BINS), key=lambda i: smooth[i])
        if smooth[peak] / len(pixels) >= ACCENT_MIN:
            acc, ws = [0.0, 0.0, 0.0], 0.0
            for c, b, w in samples:
                if min((b - peak) % HUE_BINS, (peak - b) % HUE_BINS) <= 1:
                    ws += w
                    acc[0] += c[0] * w
                    acc[1] += c[1] * w
                    acc[2] += c[2] * w
            if ws > 0:
                return (acc[0] / ws, acc[1] / ws, acc[2] / ws)
    return _mean_color(pixels)


def _mean_color(pixels: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float, float]]:
    """Saturation-weighted mean colour (same rule as accentOf() in the launcher's app.js).
    None if the picture is grey (too little saturated, very dark or very bright area)."""
    w, acc = 0.0, [0.0, 0.0, 0.0]
    for c in pixels:
        s, l = _sat(c), _lum(c)
        if l < 0.08 or l > 0.95:
            continue
        k = s * s
        w += k
        acc[0] += c[0] * k
        acc[1] += c[1] * k
        acc[2] += c[2] * k
    # app.js wants w >= 3 over 48x48 px; the same share of the samples we have
    if not pixels or w < max(0.05, len(pixels) * 3 / 2304):
        return None
    c = tuple(v / w for v in acc)
    return c if _sat(c) > 0.22 else None


def calibrate(color: Optional[Tuple[float, float, float]], cal: Dict[str, Any]) -> Dict[str, Any]:
    """Target for the lamp: {"rgb": (r,g,b) full value, "dimming": 10..100, "kelvin": int|None}.

    hue_shift rotates the hue, saturation multiplies it, the lamp's own brightness carries the
    level (the rgb always has full value: WiZ scales rgb anyway). Grey -> neutral white."""
    dim = int(max(10, min(100, round(cal["brightness"]))))
    if color is None:
        return {"rgb": localhome._kelvin_rgb(NEUTRAL_KELVIN), "dimming": dim, "kelvin": NEUTRAL_KELVIN}
    h, s, _v = colorsys.rgb_to_hsv(*(max(0.0, min(255.0, x)) / 255.0 for x in color))
    h = (h + cal["hue_shift"] / 360.0) % 1.0
    s = max(0.0, min(1.0, s * cal["saturation"]))
    rgb = tuple(int(round(x * 255)) for x in colorsys.hsv_to_rgb(h, s, 1.0))
    return {"rgb": rgb, "dimming": dim, "kelvin": None}


def hex_rgb(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, round(v)))) for v in rgb)


def parse_css_rgb(s: Any) -> Optional[Tuple[float, float, float]]:
    """'rgb(248, 80, 168)' / 'rgba(...)' -> (248.0, 80.0, 168.0); None for anything else or transparent."""
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$", str(s or "").strip())
    if not m or (m.group(4) is not None and float(m.group(4)) < 0.5):
        return None
    return tuple(max(0.0, min(255.0, float(m.group(i)))) for i in (1, 2, 3))


def _close(a: Optional[Dict[str, Any]], b: Dict[str, Any]) -> bool:
    """Same lamp target, give or take CHANGE_MIN per channel (no datagram for noise)."""
    return bool(a) and a.get("kelvin") == b.get("kelvin") and int(a["dimming"]) == int(b["dimming"]) and \
        max(abs(x - y) for x, y in zip(a["rgb"], b["rgb"])) < CHANGE_MIN


# ---- settings -----------------------------------------------------------------------

def _clean(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    mode = OLD_MODES.get(cfg.get("mode"), cfg.get("mode"))
    if mode in MODES:
        out["mode"] = mode
    for key, lo, hi, cast in (("saturation", 0.0, 3.0, float), ("brightness", 10, 100, int), ("hue_shift", -180, 180, int)):
        v = cfg.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            out[key] = cast(max(lo, min(hi, round(v, 2) if cast is float else round(v))))
    lamp = localhome.norm_mac(cfg.get("lamp") or "")
    out["lamp"] = lamp if localhome.valid_id(lamp) else ""
    if isinstance(cfg.get("saved"), dict):
        out["saved"] = cfg["saved"]
    return out


def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _clean(data if isinstance(data, dict) else {})
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg: Dict[str, Any]) -> None:
    tmp = f"{CONFIG_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        _log(f"settings not saved: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---- WiZ: fire-and-forget sender ------------------------------------------------------

class WizSender:
    """setPilot over UDP without waiting for the answer (a fade step must not take 0.5 s).
    The bulb's answers are drained and dropped."""

    def __init__(self):
        self.sock = None
        self.last_err = 0.0

    def send(self, ip: str, params: Dict[str, Any]) -> None:
        if not ip:
            return
        try:
            if self.sock is None:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.setblocking(False)
            self.sock.sendto(json.dumps({"method": "setPilot", "params": params}).encode(), (ip, localhome.WIZ_PORT))
            while True:                       # drop replies so the receive buffer never fills up
                self.sock.recvfrom(2048)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as e:
            if time.time() - self.last_err > 60:
                self.last_err = time.time()
                _log(f"udp send to {ip} failed: {e}")
            self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def _pilot_params(rgb, dimming: int) -> Dict[str, Any]:
    r, g, b = (int(max(0, min(255, round(v)))) for v in rgb)
    return {"state": True, "r": r, "g": g, "b": b, "dimming": int(max(10, min(100, round(dimming))))}


# ---- the TV as the source ------------------------------------------------------------

class SourceError(Exception):
    """A source could not be read (shown in the status, the engine carries on)."""


def _ws_frame(payload: bytes) -> bytes:
    mask = os.urandom(4)
    n = len(payload)
    head = b"\x81" + (bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536
                      else b"\xff" + struct.pack(">Q", n))
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def cdp_evaluate(ws_url: str, expression: str, timeout: float = TIMEOUT) -> Any:
    """Runtime.evaluate over the inspector's websocket (one short connection, value returned by value)."""
    m = re.match(r"ws://([^/:]+):(\d+)(/.*)$", ws_url)
    if not m:
        raise SourceError("bad inspector url")
    host, port, path = m.group(1), int(m.group(2)), m.group(3)
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise SourceError("inspector closed the connection")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise SourceError("inspector refused the websocket")
        buf = buf.split(b"\r\n\r\n", 1)[1]
        s.sendall(_ws_frame(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                        "params": {"expression": expression, "returnByValue": True}}).encode()))

        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = s.recv(65536)
                if not chunk:
                    raise SourceError("inspector closed the connection")
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out

        deadline = time.time() + timeout
        message = b""
        while time.time() < deadline:
            h = need(2)
            n = h[1] & 127
            if n == 126:
                n = struct.unpack(">H", need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", need(8))[0]
            if h[1] & 0x80:
                need(4)                                   # servers do not mask, but be lenient
            data = need(n)
            if h[0] & 0x0F in (0x8,):
                raise SourceError("inspector closed the websocket")
            if h[0] & 0x0F not in (0x0, 0x1):
                continue                                  # ping/pong/binary: not ours
            message += data
            if not h[0] & 0x80:
                continue                                  # fragmented: wait for the rest
            msg, message = json.loads(message.decode("utf-8")), b""
            if msg.get("id") != 1:
                continue
            res = msg.get("result") or {}
            if "exceptionDetails" in res or "error" in msg:
                raise SourceError("evaluate failed")
            return (res.get("result") or {}).get("value")
        raise SourceError("inspector timeout")


# Runs inside the Spotify app. Only reads; returns a small object.
SPOTIFY_JS = r"""(() => {
  const media = [...document.querySelectorAll('video,audio')].filter(e => !e.loop && e.duration > 20);
  const playing = media.some(e => !e.paused && !e.ended);
  const bg = document.querySelector('.npv-background-color');
  const img = document.querySelector('img[alt="Cover Art"]') ||        // the big visible one is the artist photo
              document.querySelector('img[src*="i.scdn.co/image/"]');
  const title = document.querySelector('.adaptive-title');
  let artist = '';
  if (title) {
    let box = title.parentElement;
    for (let i = 0; i < 3 && box && !artist; i++, box = box.parentElement) {
      const lines = (box.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      const k = lines.indexOf((title.innerText || '').trim());
      if (k >= 0 && lines[k + 1]) artist = lines[k + 1];
    }
  }
  return { playing, bg: bg ? getComputedStyle(bg).backgroundColor : '',
           cover: img && /^https:\/\//.test(img.src) ? img.src : '',
           title: title ? (title.innerText || '').trim() : '', artist };
})()"""


class TvSource:
    """Reads the TV: the Spotify app (web inspector), the foreground app and the screen (luna)."""

    def __init__(self):
        self.page_url = ""

    def _pages(self) -> List[Dict[str, Any]]:
        with urllib.request.urlopen("http://%s:%d/json/list" % INSPECTOR, timeout=TIMEOUT) as r:
            data = json.loads(r.read(512 * 1024).decode("utf-8"))
        return data if isinstance(data, list) else []

    def spotify(self) -> Optional[Dict[str, Any]]:
        """None if the Spotify app is not running (or not inspectable); else
        {"playing", "color" (Spotify's own, or None), "cover", "title", "artist"}."""
        try:
            pages = self._pages()
        except (OSError, ValueError):
            return None                                 # inspector down (TV standby, WebAppMgr restart)
        page = next((p for p in pages if p.get("description") == SPOTIFY_APP), None) or \
            next((p for p in pages if "spotify.com/tvapp" in str(p.get("url") or "")), None)
        if not page or not page.get("webSocketDebuggerUrl"):
            return None
        try:
            v = cdp_evaluate(page["webSocketDebuggerUrl"], SPOTIFY_JS)
        except (OSError, ValueError) as e:
            raise SourceError(f"Spotify-App nicht lesbar ({type(e).__name__})")
        if not isinstance(v, dict):
            raise SourceError("Spotify-App nicht lesbar")
        return {"playing": bool(v.get("playing")), "color": parse_css_rgb(v.get("bg")),
                "cover": str(v.get("cover") or "")[:300], "title": str(v.get("title") or "")[:200],
                "artist": str(v.get("artist") or "")[:200]}

    @staticmethod
    def _luna(uri: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            r = subprocess.run(["luna-send", "-n", "1", uri, json.dumps(payload)], capture_output=True, text=True,
                               timeout=TIMEOUT + 1, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            raise SourceError(f"luna-send: {type(e).__name__}")
        text = r.stdout.strip()
        try:
            return json.loads(text[text.index("{"):]) if "{" in text else {}
        except ValueError:
            return {}

    def foreground(self) -> str:
        return str(self._luna("luna://com.webos.applicationManager/getForegroundAppInfo", {}).get("appId") or "")

    def screen(self) -> Optional[Tuple[float, float, float]]:
        """Strongest colour of a small screen snapshot; None for a grey/black picture."""
        try:
            os.remove(SCREEN_FILE)
        except OSError:
            pass
        res = self._luna("luna://com.webos.service.capture/executeOneShot",
                         {"path": SCREEN_FILE, "method": "DISPLAY", "format": "JPG",
                          "width": SCREEN_SIZE[0], "height": SCREEN_SIZE[1]})
        if not res.get("returnValue"):
            raise SourceError("Bildschirm nicht lesbar")
        try:
            with open(SCREEN_FILE, "rb") as f:
                data = f.read(4 * 1024 * 1024)
            return dominant_color(jpeg_dc.dc_pixels(data))
        except (OSError, ValueError) as e:
            raise SourceError(f"Bildschirm nicht lesbar ({type(e).__name__})")


def fetch_cover_color(url: str) -> Optional[Tuple[float, float, float]]:
    """Dominant colour of the cover at url (None = grey/neutral). Raises OSError/JpegError."""
    if not url.startswith("https://"):
        raise OSError("unexpected cover url")
    with urllib.request.urlopen(url, timeout=COVER_TIMEOUT) as r:
        data = r.read(2 * 1024 * 1024)
    return dominant_color(jpeg_dc.dc_pixels(data))


# ---- the engine -----------------------------------------------------------------------

class Ambient:
    def __init__(self, source: Optional[TvSource] = None, sender: Optional[WizSender] = None):
        self.source = source or TvSource()
        self.sender = sender or WizSender()
        self.lock = threading.Lock()          # cfg, st, the flags set from request threads
        self.wake = threading.Event()
        self.cfg = load_config()
        self.colors: Dict[str, Optional[Tuple[float, float, float]]] = {}     # cover url -> colour
        self.st: Dict[str, Any] = {
            "state": "starting", "message": "", "source": None, "track": None,
            "color": None, "sent": None, "updated": 0.0,
        }
        self.inbox: List[str] = []
        self.active = False            # we own the lamp right now (its old state is in cfg["saved"])
        self.override = False          # owner took over: hands off until the source stops
        self.cur: Optional[Dict[str, Any]] = None    # last target sent {"rgb", "dimming", "kelvin"}
        self.track_id = None           # what is on the lamp: a track, or "screen:<app>"
        self.raw: Optional[Tuple[float, float, float]] = None   # uncalibrated colour on the lamp
        self.have_raw = False
        self.paused_since = 0.0
        self.lamp_checked = 0.0
        self.retry_restore_at = 0.0
        self.preview_until = 0.0
        self.preview_pending = False
        self.cal_dirty = False
        self.last_err = ""
        self.thread: Optional[threading.Thread] = None
        self.stopping = False

    # ---- public (any thread; memory and small files only, never the network) ----
    def status(self) -> Dict[str, Any]:
        with self.lock:
            cfg = self.cfg
            lamp = self._lamp()
            return dict(self.st, mode=cfg["mode"],
                        calibration={k: cfg[k] for k in ("saturation", "brightness", "hue_shift")},
                        lamp={"id": lamp["mac"], "name": localhome.display_name(lamp)} if lamp else None,
                        active=self.active, override=self.override)

    def set_mode(self, mode: str) -> Dict[str, Any]:
        if mode not in MODES:
            raise ValueError("mode")
        with self.lock:
            self.cfg["mode"] = mode
            save_config(self.cfg)
        self.wake.set()
        return self.status()

    def set_calibration(self, values: Dict[str, Any], preview: bool = True) -> Dict[str, Any]:
        with self.lock:
            merged = _clean(dict(self.cfg, **{k: values[k] for k in ("saturation", "brightness", "hue_shift") if k in values}))
            for k in ("saturation", "brightness", "hue_shift"):
                self.cfg[k] = merged[k]
            save_config(self.cfg)
            self.cal_dirty = True
            if preview:
                self.preview_pending = True
                self.preview_until = time.time() + PREVIEW_HOLD
        self.wake.set()
        return self.status()

    def set_lamp(self, mac: str) -> Dict[str, Any]:
        mac = localhome.norm_mac(mac)
        if not any(d["mac"] == mac and d["kind"] == "wiz" for d in localhome.load_devices()):
            raise ValueError("lamp")
        with self.lock:
            if self.cfg.get("lamp") != mac:
                self.inbox.append("lamp_changed")
            self.cfg["lamp"] = mac
            save_config(self.cfg)
        self.wake.set()
        return self.status()

    def manual_change(self, mac: str) -> None:
        """localhome.set_switch hook: the owner switched a device through the hub/Gemini/phone."""
        with self.lock:
            saved = self.cfg.get("saved") or {}
            if saved.get("mac") == localhome.norm_mac(mac):
                self.inbox.append("override")
        self.wake.set()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="spotify-ambient", daemon=True)
        self.thread.start()

    # ---- worker thread only from here on ----
    def _set(self, **kw):
        """Update the status (caller holds self.lock)."""
        self.st.update(kw)
        self.st["updated"] = time.time()

    def _status(self, **kw):
        with self.lock:
            self._set(**kw)

    def _lamp(self) -> Optional[Dict[str, Any]]:
        devs = [d for d in localhome.load_devices() if d["kind"] == "wiz"]
        mac = self.cfg.get("lamp")
        if mac:
            return next((d for d in devs if d["mac"] == mac), None)
        named = [d for d in devs if d["name"].strip().lower() == DEFAULT_LAMP_NAME.lower()]
        return named[0] if named else None

    def _run(self):
        while not self.stopping:
            try:
                delay = self.step()
            except Exception as e:                       # never let the thread die
                _log(f"step failed: {type(e).__name__}: {e}")
                self._status(state="error", message=f"Interner Fehler: {type(e).__name__}")
                delay = 15.0
            self.wake.wait(max(0.1, delay))
            self.wake.clear()

    def _take_inbox(self):
        with self.lock:
            inbox, self.inbox = self.inbox, []
            preview, self.preview_pending = self.preview_pending, False
            cal_dirty, self.cal_dirty = self.cal_dirty, False
            mode = self.cfg["mode"]
        return inbox, preview, cal_dirty, mode

    def _err(self, msg: str) -> None:
        if msg != self.last_err:
            _log(msg)
        self.last_err = msg

    def _read(self, mode: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """(source, why): source = {"kind", "key", "color", "track"}, or None and why nothing runs."""
        why = "Spotify läuft nicht"
        try:
            sp = self.source.spotify()
        except SourceError as e:
            self._err(str(e))
            sp, why = None, str(e)
        if sp is not None and sp["playing"]:
            self.last_err = ""
            color = sp["color"] if sp["color"] is not None else self._cover_color(sp["cover"])
            return {"kind": "spotify", "key": "spotify:" + (sp["cover"] or sp["title"]), "color": color,
                    "track": {"name": sp["title"], "artist": sp["artist"], "cover": sp["cover"]}}, ""
        if sp is not None:
            why = "Spotify pausiert"
        if mode != "ambilight":
            return None, why
        try:
            app = self.source.foreground()
            if not app or app in SKIP_APPS:
                return None, "Startseite"
            color = self.source.screen()
        except SourceError as e:
            self._err(str(e))
            return None, str(e)
        self.last_err = ""
        return {"kind": "screen", "key": "screen:" + app, "color": color, "track": None, "app": app}, ""

    def step(self) -> float:
        """One read of the source and the reaction to it. Returns the delay until the next one."""
        inbox, preview, cal_dirty, mode = self._take_inbox()
        now = time.time()
        if "lamp_changed" in inbox:
            self._deactivate("andere Lampe")
        if "override" in inbox and self.active:
            self._hands_off("Lampe manuell geändert")
        if not self.active and now >= self.retry_restore_at:
            with self.lock:
                pending = bool(self.cfg.get("saved"))
            if pending:                                  # backend restart or a failed restore
                self._restore("ausstehend")
        in_preview = preview or now < self.preview_until

        if mode == "off":
            if in_preview:
                return self._preview(preview)
            self._deactivate("Modus aus")
            self.override = False
            self._status(state="off", message="", source=None, track=None)
            return POLL_OFF

        src, why = self._read(mode)
        if src is None:
            if in_preview:
                return self._preview(preview)
            if self.active or self.override:
                if not self.paused_since:
                    self.paused_since = now
                if now - self.paused_since >= PAUSE_GRACE:
                    self.override = False
                    if not self._deactivate(why):
                        return POLL_IDLE                # keeps the "not reachable" status visible
                else:
                    return max(0.3, min(POLL_SPOTIFY, PAUSE_GRACE - (now - self.paused_since) + 0.1))
            with self.lock:
                restore_pending = bool(self.cfg.get("saved"))
            if restore_pending:
                return POLL_IDLE                        # status keeps saying the lamp could not be reset
            self._status(state="idle", message=why, source=None, track=None)
            return POLL_IDLE
        self.paused_since = 0.0
        poll = POLL_SCREEN if src["kind"] == "screen" else POLL_SPOTIFY
        self._status(source=src["kind"], track=src["track"])

        if self.override:
            self._status(state="override", message="Lampe manuell geändert, pausiert bis die Wiedergabe endet")
            return poll

        new_key = src["key"] != self.track_id
        self.track_id = src["key"]
        self.raw, self.have_raw = src["color"], True

        fresh = False
        if not self.active:
            if not new_key and now - self.lamp_checked < POLL_LAMP_OFF:
                return poll                             # lamp was off/unreachable: look again later
            if not self._activate():
                return poll
            fresh = True
        elif (new_key and src["kind"] == "spotify") or now - self.lamp_checked > LAMP_CHECK:
            self.lamp_checked = now
            if not self._lamp_still_on():
                self._hands_off("Lampe wurde ausgeschaltet")
                return poll

        with self.lock:
            target = calibrate(self.raw, self.cfg)
        slow = fresh or (new_key and src["kind"] == "spotify")
        if slow or cal_dirty or preview or not _close(self.cur, target):
            if slow:
                self._fade_to(target, FADE_STEPS, FADE_TIME, confirm=True)
            else:                                       # following the screen / Spotify's fade: no answer awaited
                self._fade_to(target, FOLLOW_STEPS, FOLLOW_TIME, confirm=cal_dirty or preview)
        self._status(state="active", message="", color=hex_rgb(self.raw) if self.raw else None,
                     sent=hex_rgb(self.cur["rgb"]) if self.cur else None)
        return poll

    def _cover_color(self, url: str) -> Optional[Tuple[float, float, float]]:
        if not url:
            return None
        if url not in self.colors:
            try:
                self.colors[url] = fetch_cover_color(url)
            except Exception as e:
                _log(f"cover not readable ({type(e).__name__}: {e}), using neutral white")
                return None                              # not cached: tried again next time
            if len(self.colors) > 300:
                self.colors.pop(next(iter(self.colors)))
        return self.colors[url]

    def _hands_off(self, why: str) -> None:
        """The owner took the lamp over: forget its old state, leave it alone until the source stops."""
        self.override, self.active, self.cur = True, False, None
        with self.lock:
            self.cfg.pop("saved", None)
            save_config(self.cfg)
            self._set(state="override", message=why + ", pausiert bis die Wiedergabe endet")
        _log(f"hands off: {why}")

    def _getpilot(self, lamp) -> Dict[str, Any]:
        return localhome._with_rediscovery(lamp, lambda d: localhome._wiz_call(d["ip"], "getPilot"))

    def _activate(self) -> bool:
        """Remember the lamp's state and take it over; False if it is off or unreachable."""
        self.lamp_checked = time.time()
        with self.lock:
            lamp = self._lamp()
            pending = self.cfg.get("saved")
        if not lamp:
            self._status(state="error", message="Lampe nicht gefunden (devices.json)")
            return False
        try:
            pilot = self._getpilot(lamp)
        except Exception as e:
            self._status(state="error", message=f"{localhome.display_name(lamp)} nicht erreichbar")
            _log(f"lamp unreachable: {e}")
            return False
        if not pilot.get("state"):
            self._status(state="lamp_off", message=f"{localhome.display_name(lamp)} ist aus")
            return False
        if not (pending and pending.get("mac") == lamp["mac"]):     # a restore still pending keeps the older state
            saved = {k: pilot[k] for k in SAVED_KEYS if k in pilot}
            with self.lock:
                self.cfg["saved"] = dict(saved, mac=lamp["mac"])
                save_config(self.cfg)
        self.cur = {"rgb": localhome.parse_color(localhome._wiz_color(pilot)) or (255, 255, 255),
                    "dimming": int(pilot.get("dimming") or 100), "kelvin": None}
        self.active = True
        localhome.invalidate_cache()
        _log(f"took over {localhome.display_name(lamp)}")
        return True

    def _lamp_still_on(self) -> bool:
        with self.lock:
            lamp = self._lamp()
        try:
            return bool(lamp and self._getpilot(lamp).get("state"))
        except Exception:
            return True                                 # unreachable is not "switched off by the owner"

    def _fade_to(self, target: Dict[str, Any], steps: int, duration: float, confirm: bool = True) -> None:
        with self.lock:
            lamp = self._lamp()
        if not lamp:
            return
        self._fade(lamp, self.cur or target, target["rgb"], target["dimming"], steps, duration)
        final = ({"state": True, "temp": target["kelvin"], "dimming": int(target["dimming"])} if target.get("kelvin")
                 else _pilot_params(target["rgb"], target["dimming"]))
        if confirm:
            try:                                        # last step with an answer: catches a lost datagram
                localhome._wiz_call(lamp["ip"], "setPilot", final, tries=2)
            except Exception as e:
                _log(f"lamp did not confirm: {e}")
                self._status(message=f"{localhome.display_name(lamp)} antwortet nicht")
        else:
            self.sender.send(lamp["ip"], final)
        self.cur = dict(target)

    def _fade(self, lamp, start, rgb, dim, steps=FADE_STEPS, duration=FADE_TIME):
        """steps-1 fire-and-forget intermediate setPilots; the caller sends the exact last one."""
        for i in range(1, steps):
            f = i / steps
            self.sender.send(lamp["ip"], _pilot_params(
                tuple(a + (b - a) * f for a, b in zip(start["rgb"], rgb)),
                start["dimming"] + (dim - start["dimming"]) * f))
            time.sleep(duration / steps)

    def _preview(self, fresh: bool) -> float:
        """Calibration preview with the last colour while nothing plays."""
        if fresh:
            if not self.active and not self._activate():
                self.preview_until = 0.0
                return POLL_IDLE
            with self.lock:
                target = calibrate(self.raw if self.have_raw else PREVIEW_SAMPLE, self.cfg)
            self._fade_to(target, 3, 0.25)
            self._status(state="preview", message="Vorschau", sent=hex_rgb(target["rgb"]))
        left = self.preview_until - time.time()
        if left <= 0:
            self._deactivate("Vorschau beendet")
            return POLL_IDLE
        return min(POLL_IDLE, left + 0.05)

    def _deactivate(self, why: str) -> bool:
        """Give the lamp back if we hold it. False if that failed (retried later)."""
        ok = True
        if self.active:
            _log(f"giving the lamp back ({why})")
            ok = self._restore(why)
        self.track_id = None
        self.paused_since = 0.0
        return ok

    def _restore(self, why: str) -> bool:
        """Fade back to the saved state and write it exactly (scene, white tone, colour, brightness)."""
        with self.lock:
            saved = dict(self.cfg.get("saved") or {})
        self.active = False
        if not saved:
            self.cur = None
            return True
        lamp = next((d for d in localhome.load_devices() if d["mac"] == saved.get("mac")), None)
        if lamp:
            back_rgb = localhome.parse_color(localhome._wiz_color(saved)) or (255, 255, 255)
            back_dim = int(saved.get("dimming") or 100)
            if self.cur:
                self._fade(lamp, self.cur, back_rgb, back_dim)
            params = restore_params(saved)
            try:
                localhome._with_rediscovery(lamp, lambda d: localhome._wiz_call(d["ip"], "setPilot", params, tries=3))
            except Exception as e:
                _log(f"restore failed ({why}): {e}")
                self._status(state="error", message=f"{localhome.display_name(lamp)} nicht erreichbar, alter Zustand bleibt gemerkt")
                self.retry_restore_at = time.time() + 30
                return False                            # "saved" stays: tried again later
            localhome.invalidate_cache()
        with self.lock:
            self.cfg.pop("saved", None)
            save_config(self.cfg)
        self.cur = None
        return True


SAVED_KEYS = ("state", "dimming", "r", "g", "b", "c", "w", "temp", "sceneId", "speed")


def restore_params(saved: Dict[str, Any]) -> Dict[str, Any]:
    """setPilot params that bring a lamp back to a getPilot reading."""
    params: Dict[str, Any] = {"state": bool(saved.get("state", True))}
    if not params["state"]:
        return params
    if isinstance(saved.get("dimming"), int):
        params["dimming"] = max(10, min(100, saved["dimming"]))
    if saved.get("sceneId"):
        params["sceneId"] = saved["sceneId"]
        if saved.get("speed"):
            params["speed"] = saved["speed"]
    elif saved.get("temp"):
        params["temp"] = saved["temp"]
    else:
        for k in ("r", "g", "b", "c", "w"):
            if isinstance(saved.get(k), int):
                params[k] = saved[k]
    return params


# ---- module-level singleton used by server.py ----------------------------------------

_engine: Optional[Ambient] = None
_engine_lock = threading.Lock()


def engine() -> Ambient:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Ambient()
        return _engine


def start() -> None:
    eng = engine()
    localhome.add_switch_listener(eng.manual_change)
    eng.start()


if __name__ == "__main__":
    # python3 spotify_ambient.py color <picture.jpg>   -> dominant + calibrated colour of a file
    # python3 spotify_ambient.py read                  -> what the TV reports right now (on the TV)
    if len(sys.argv) == 3 and sys.argv[1] == "color":
        with open(sys.argv[2], "rb") as f:
            c = dominant_color(jpeg_dc.dc_pixels(f.read()))
        t = calibrate(c, load_config())
        print("dominant", hex_rgb(c) if c else "grau", "-> lamp", hex_rgb(t["rgb"]), f"{t['dimming']} %")
    elif len(sys.argv) == 2 and sys.argv[1] == "read":
        src = TvSource()
        print("spotify", src.spotify())
        print("foreground", src.foreground())
        t0 = time.time()
        c = src.screen()
        print("screen", hex_rgb(c) if c else "grau", f"{(time.time() - t0) * 1000:.0f} ms")
