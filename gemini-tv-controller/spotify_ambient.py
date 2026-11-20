"""
Spotify ambilight: one WiZ lamp takes the dominant colour of the cover of the song that is
playing on Spotify and fades over on every song change.

    worker thread   polls GET /me/player every POLL_PLAYING s (Spotify Web API, PKCE token from
                    spotify_login.py), computes the colour of the smallest cover once per track,
                    applies the calibration and fades the lamp over FADE_STEPS steps.
    REST            server.py exposes status(), set_mode(), set_calibration(), set_lamp().

Modes: "off", "tv" (only when the active Spotify device is a TV, default), "all".

The lamp is only coloured when it is already on. Its state is read (getPilot) right before the
first change and written back, faded, once playback pauses or stops for PAUSE_GRACE s. A change
made through the hub (localhome.set_switch) or a lamp found switched off at a song change means
"the owner took over": the lamp is left alone until playback ends.

Settings (mode, calibration, lamp) live in spotify-ambient.json next to this file (gitignored);
the saved lamp state is kept there too, so a restarted backend can still put the lamp back.
Nothing in here ever runs on a request thread: the HTTP handlers only touch memory and wake the
worker.
"""

import colorsys
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import localhome
import jpeg_dc
import spotify_login

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "spotify-ambient.json")
API = spotify_login.API

POLL_PLAYING = 4.0        # s between player polls while something plays
POLL_IDLE = 5.0           # s between polls while nothing plays
POLL_LAMP_OFF = 30.0      # s between lamp checks while music plays but the lamp is off
PAUSE_GRACE = 6.0         # s of pause/stop before the lamp goes back
FADE_STEPS = 10
FADE_TIME = 1.0           # s for a song-change fade
PREVIEW_HOLD = 10.0       # s a calibration preview stays on the lamp when nothing plays
NO_NET_RESTORE = 60.0     # s without internet before the lamp goes back
BACKOFF = (5, 10, 20, 40, 60)   # s after network errors
HTTP_TIMEOUT = 8.0
MODES = ("off", "tv", "all")
DEFAULTS = {"mode": "tv", "saturation": 1.4, "brightness": 35, "hue_shift": 0, "lamp": "", "tv_device": ""}
NEUTRAL_KELVIN = 4000     # grey covers
PREVIEW_SAMPLE = (241, 118, 38)   # preview colour before any cover was seen
DEFAULT_LAMP_NAME = "Fernseher Bar"


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [spotify] {msg}", file=sys.stderr, flush=True)


# ---- colour ---------------------------------------------------------------------

def _sat(c) -> float:
    return (max(c) - min(c)) / 255.0


def _lum(c) -> float:
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255.0


def dominant_color(pixels: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float, float]]:
    """Saturation-weighted mean colour (same rule as accentOf() in the launcher's app.js).
    None if the artwork is grey (too little saturated, very dark or very bright area)."""
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
    level (the rgb always has full value: WiZ scales rgb anyway). Grey covers -> neutral white."""
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


# ---- settings -----------------------------------------------------------------------

def _clean(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    if cfg.get("mode") in MODES:
        out["mode"] = cfg["mode"]
    for key, lo, hi, cast in (("saturation", 0.0, 3.0, float), ("brightness", 10, 100, int), ("hue_shift", -180, 180, int)):
        v = cfg.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            out[key] = cast(max(lo, min(hi, round(v, 2) if cast is float else round(v))))
    lamp = localhome.norm_mac(cfg.get("lamp") or "")
    out["lamp"] = lamp if localhome.valid_id(lamp) else ""
    if isinstance(cfg.get("tv_device"), str):
        out["tv_device"] = cfg["tv_device"].strip()[:100]
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


# ---- Spotify --------------------------------------------------------------------------

class SpotifyError(Exception):
    def __init__(self, kind: str, msg: str = "", retry_after: float = 0.0):
        super().__init__(msg or kind)
        self.kind, self.retry_after = kind, retry_after     # kind: login | ratelimit | network | api


class SpotifyClient:
    def __init__(self):
        self.token: Optional[Dict[str, Any]] = None
        self.token_sig = None

    def _token_file_sig(self):
        try:
            st = os.stat(spotify_login.TOKEN_FILE)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def has_login(self) -> bool:
        return self._token_file_sig() is not None and bool(spotify_login.client_id())

    def _access_token(self, force: bool = False) -> str:
        sig = self._token_file_sig()
        if sig is None:
            raise SpotifyError("login", "Kein Spotify-Login (spotify_login.py ausführen)")
        if self.token is None or sig != self.token_sig:
            self.token, self.token_sig = spotify_login.load_token(), sig
        if not self.token:
            raise SpotifyError("login", "Spotify-Token unlesbar (spotify_login.py erneut ausführen)")
        if not force and self.token.get("access_token") and time.time() < float(self.token.get("expires_at") or 0):
            return self.token["access_token"]
        cid = spotify_login.client_id()
        if not cid:
            raise SpotifyError("login", "SPOTIFY_CLIENT_ID fehlt in .env")
        try:
            self.token = spotify_login.token_request({"grant_type": "refresh_token",
                                                      "refresh_token": self.token["refresh_token"],
                                                      "client_id": cid}, timeout=HTTP_TIMEOUT)
            self.token_sig = self._token_file_sig()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401):
                raise SpotifyError("login", "Spotify-Login abgelaufen (spotify_login.py erneut ausführen)")
            if e.code == 429:
                raise SpotifyError("ratelimit", "Spotify bremst (429)", _retry_after(e))
            raise SpotifyError("api", f"Token-Erneuerung: HTTP {e.code}")
        except (OSError, ValueError) as e:
            raise SpotifyError("network", f"Spotify nicht erreichbar ({type(e).__name__})")
        return self.token["access_token"]

    def player(self) -> Optional[Dict[str, Any]]:
        """GET /me/player; None when nothing is active (204)."""
        for attempt in (0, 1):
            req = urllib.request.Request(API + "/me/player?additional_types=episode",
                                         headers={"Authorization": "Bearer " + self._access_token(force=attempt == 1)})
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                    raw = r.read(1024 * 1024)
                    if r.status == 204 or not raw.strip():
                        return None
                    data = json.loads(raw.decode("utf-8"))
                    return data if isinstance(data, dict) else None
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue                                     # token revoked/expired early: refresh once
                if e.code == 429:
                    raise SpotifyError("ratelimit", "Spotify bremst (429)", _retry_after(e))
                if e.code in (401, 403):
                    raise SpotifyError("login", f"Spotify verweigert den Zugriff (HTTP {e.code})")
                raise SpotifyError("api", f"Spotify-API: HTTP {e.code}")
            except (OSError, ValueError) as e:
                raise SpotifyError("network", f"Spotify nicht erreichbar ({type(e).__name__})")
        raise SpotifyError("login", "Spotify-Login ungültig")


def _retry_after(e: urllib.error.HTTPError) -> float:
    try:
        return max(1.0, min(3600.0, float(e.headers.get("Retry-After") or 30)))
    except (TypeError, ValueError):
        return 30.0


def _cover_url(item: Dict[str, Any]) -> str:
    imgs = (item.get("album") or {}).get("images") or item.get("images") or []
    imgs = [i for i in imgs if isinstance(i, dict) and isinstance(i.get("url"), str)]
    if not imgs:
        return ""
    return min(imgs, key=lambda i: i.get("width") or 10 ** 6)["url"]


def fetch_cover_color(url: str) -> Optional[Tuple[float, float, float]]:
    """Dominant colour of the cover at url (None = grey/neutral). Raises OSError/JpegError."""
    if not url.startswith("https://"):
        raise OSError("unexpected cover url")
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        data = r.read(2 * 1024 * 1024)
    return dominant_color(jpeg_dc.dc_pixels(data))


# ---- the engine -----------------------------------------------------------------------

class Ambient:
    def __init__(self, client: Optional[SpotifyClient] = None, sender: Optional[WizSender] = None):
        self.client = client or SpotifyClient()
        self.sender = sender or WizSender()
        self.lock = threading.Lock()          # cfg, st, the flags set from request threads
        self.wake = threading.Event()
        self.cfg = load_config()
        self.colors: Dict[str, Optional[Tuple[float, float, float]]] = {}     # cover url -> colour
        self.st: Dict[str, Any] = {
            "state": "starting", "message": "", "track": None, "device": None, "is_playing": False,
            "color": None, "sent": None, "updated": 0.0,
        }
        self.inbox: List[str] = []
        self.active = False            # we own the lamp right now (its old state is in cfg["saved"])
        self.override = False          # owner took over: hands off until playback ends
        self.cur: Optional[Dict[str, Any]] = None    # last target sent {"rgb", "dimming", "kelvin"}
        self.track_id = None           # track whose colour is (or should be) on the lamp
        self.raw: Optional[Tuple[float, float, float]] = None   # uncalibrated colour of that track
        self.have_raw = False
        self.paused_since = 0.0
        self.lamp_checked = 0.0
        self.retry_restore_at = 0.0
        self.preview_until = 0.0
        self.preview_pending = False
        self.cal_dirty = False
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
                        active=self.active, override=self.override,
                        login=self.client.has_login())

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
        fails = 0
        while not self.stopping:
            try:
                delay = self.step()
                fails = 0
            except SpotifyError as e:
                delay = self._spotify_failed(e, fails)
                fails = fails + 1 if e.kind == "network" else 0
            except Exception as e:                       # never let the thread die
                _log(f"step failed: {type(e).__name__}: {e}")
                self._status(state="error", message=f"Interner Fehler: {type(e).__name__}")
                delay = 15.0
            self.wake.wait(max(0.2, delay))
            self.wake.clear()

    def _spotify_failed(self, e: SpotifyError, fails: int) -> float:
        self._status(state="login" if e.kind == "login" else "error", message=str(e))
        if e.kind == "login":
            self._deactivate("kein Login")
            return 30.0                                  # the token file is looked at again then
        if e.kind == "ratelimit":
            _log(f"rate limited, waiting {e.retry_after:.0f} s")
            return e.retry_after
        if e.kind == "network":
            # no internet: keep the colour a while, but don't leave the lamp coloured for good
            if self.active and not self.paused_since:
                self.paused_since = time.time()
            if self.active and time.time() - self.paused_since > NO_NET_RESTORE:
                self._deactivate("keine Verbindung")
            if fails == 0:
                _log(str(e))
            return BACKOFF[min(fails, len(BACKOFF) - 1)]
        return 30.0

    def _take_inbox(self):
        with self.lock:
            inbox, self.inbox = self.inbox, []
            preview, self.preview_pending = self.preview_pending, False
            cal_dirty, self.cal_dirty = self.cal_dirty, False
            mode = self.cfg["mode"]
        return inbox, preview, cal_dirty, mode

    def step(self) -> float:
        """One poll and the reaction to it. Returns the delay until the next one."""
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
            self._status(state="off", message="", track=None, device=None, is_playing=False)
            return 3600.0                               # sleeps until a setting changes (wake)

        if not self.client.has_login():
            raise SpotifyError("login", "Kein Spotify-Login (spotify_login.py ausführen)")

        p = self.client.player()
        item = (p or {}).get("item") or None
        dev = (p or {}).get("device") or {}
        playing = bool(p and p.get("is_playing") and item)
        match = playing and self._device_matches(dev, mode)
        self._status(is_playing=bool(p and p.get("is_playing")),
                     device={"name": dev.get("name"), "type": dev.get("type")} if dev else None,
                     track=_track_info(item) if item else None)

        if not match:
            if in_preview:
                return self._preview(preview)
            if self.active or self.override:
                if not self.paused_since:
                    self.paused_since = now
                if now - self.paused_since >= PAUSE_GRACE:
                    self.override = False
                    if not self._deactivate("Wiedergabe pausiert" if p else "Wiedergabe gestoppt"):
                        return POLL_PLAYING             # keeps the "not reachable" status visible
                else:
                    return max(0.5, min(POLL_PLAYING, PAUSE_GRACE - (now - self.paused_since) + 0.1))
            with self.lock:
                restore_pending = bool(self.cfg.get("saved"))
            if restore_pending:
                return POLL_PLAYING                     # status keeps saying the lamp could not be reset
            why = "Läuft auf einem anderen Gerät" if playing else ("Pausiert" if p else "Nichts läuft")
            self._status(state="idle", message=why)
            return POLL_PLAYING if p else POLL_IDLE
        self.paused_since = 0.0

        if self.override:
            self._status(state="override", message="Lampe manuell geändert, pausiert bis die Wiedergabe endet")
            return POLL_PLAYING

        tid = item.get("id") or item.get("uri") or item.get("name")
        new_track = tid != self.track_id
        if new_track:
            self.raw, self.have_raw = self._cover_color(item), True
            self.track_id = tid
        if self.active and not (new_track or cal_dirty or preview):
            return POLL_PLAYING

        fresh = False
        if not self.active:
            if not new_track and now - self.lamp_checked < POLL_LAMP_OFF:
                return POLL_PLAYING                     # lamp was off/unreachable: look again later
            if not self._activate():
                return POLL_PLAYING
            fresh = True
        elif new_track and not self._lamp_still_on():
            self._hands_off("Lampe wurde ausgeschaltet")
            return POLL_PLAYING

        with self.lock:
            target = calibrate(self.raw, self.cfg)
        slow = new_track or fresh
        self._fade_to(target, FADE_STEPS if slow else 3, FADE_TIME if slow else 0.25)
        self._status(state="active", message="", color=hex_rgb(self.raw) if self.raw else None,
                     sent=hex_rgb(target["rgb"]))
        return POLL_PLAYING

    def _cover_color(self, item) -> Optional[Tuple[float, float, float]]:
        url = _cover_url(item)
        if url not in self.colors:
            try:
                self.colors[url] = fetch_cover_color(url) if url else None
            except Exception as e:
                _log(f"cover not readable ({type(e).__name__}: {e}), using neutral white")
                return None                              # not cached: tried again with the next track
            if len(self.colors) > 300:
                self.colors.pop(next(iter(self.colors)))
        return self.colors[url]

    def _device_matches(self, dev: Dict[str, Any], mode: str) -> bool:
        if mode == "all":
            return True
        with self.lock:
            name = self.cfg.get("tv_device") or ""
        if name:
            return str(dev.get("name") or "") == name
        return str(dev.get("type") or "").lower() == "tv"

    def _hands_off(self, why: str) -> None:
        """The owner took the lamp over: forget its old state, leave it alone until playback ends."""
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

    def _fade_to(self, target: Dict[str, Any], steps: int, duration: float) -> None:
        with self.lock:
            lamp = self._lamp()
        if not lamp:
            return
        self._fade(lamp, self.cur or target, target["rgb"], target["dimming"], steps, duration)
        final = ({"state": True, "temp": target["kelvin"], "dimming": int(target["dimming"])} if target.get("kelvin")
                 else _pilot_params(target["rgb"], target["dimming"]))
        try:                                            # last step with an answer: catches a lost datagram
            localhome._wiz_call(lamp["ip"], "setPilot", final, tries=2)
        except Exception as e:
            _log(f"lamp did not confirm: {e}")
            self._status(message=f"{localhome.display_name(lamp)} antwortet nicht")
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
        """Calibration preview with the last cover colour while nothing (matching) plays."""
        if fresh:
            if not self.active and not self._activate():
                self.preview_until = 0.0
                return POLL_PLAYING
            with self.lock:
                target = calibrate(self.raw if self.have_raw else PREVIEW_SAMPLE, self.cfg)
            self._fade_to(target, 3, 0.25)
            self._status(state="preview", message="Vorschau", sent=hex_rgb(target["rgb"]))
        left = self.preview_until - time.time()
        if left <= 0:
            self._deactivate("Vorschau beendet")
            return POLL_PLAYING
        return min(POLL_PLAYING, left + 0.05)

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


def _track_info(item: Dict[str, Any]) -> Dict[str, Any]:
    artists = item.get("artists") or []
    show = item.get("show") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "artist": ", ".join(a.get("name", "") for a in artists if isinstance(a, dict)) or show.get("name") or "",
        "cover": _cover_url(item),
    }


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
    # python3 spotify_ambient.py color <cover.jpg>   -> dominant + calibrated colour of a file
    if len(sys.argv) == 3 and sys.argv[1] == "color":
        with open(sys.argv[2], "rb") as f:
            c = dominant_color(jpeg_dc.dc_pixels(f.read()))
        t = calibrate(c, load_config())
        print("dominant", hex_rgb(c) if c else "grau", "-> lamp", hex_rgb(t["rgb"]), f"{t['dimming']} %")
