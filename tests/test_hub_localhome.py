"""Protocol and concurrency tests for gemini-tv-controller/localhome.py.

WiZ bulbs are played by a UDP socket on 127.0.0.1, a Tapo plug by a small KLAP HTTP server
(same crypto as the real plug, via the module's own libcrypto binding). Nothing leaves the host.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "gemini-tv-controller")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import localhome  # noqa: E402


def _have_libcrypto():
    try:
        localhome._AES.lib()
        return True
    except OSError:
        return False


class TempFilesMixin:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="hub-lh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, attr in (("devices.json", "DEVICES_FILE"), ("ip-cache.json", "IP_CACHE_FILE"), (".env", "ENV_FILE")):
            p = mock.patch.object(localhome, attr, os.path.join(self.tmp, name))
            p.start()
            self.addCleanup(p.stop)
        localhome._file_cache.clear()
        localhome.invalidate_cache()
        localhome._cache.update(discovered_at=0.0, discover_every=localhome.REDISCOVER_EVERY)
        localhome._cache["caps"].clear()
        localhome._klap.clear()
        for k in ("TAPO_EMAIL", "TAPO_PASSWORD", "HUB_GATEWAY"):
            if os.environ.get(k):
                p = mock.patch.dict(os.environ, {k: ""})
                p.start()
                self.addCleanup(p.stop)

    def write_devices(self, devices):
        with open(localhome.DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump({"devices": devices}, f)

    def write_env(self, text):
        with open(localhome.ENV_FILE, "w", encoding="utf-8") as f:
            f.write(text)


# ---- WiZ ---------------------------------------------------------------------------

class FakeWiz(threading.Thread):
    def __init__(self, module="ESP01_SHRGB_03", garbage_first=False, silent=False, error=False):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.pilot = {"state": True, "dimming": 40, "temp": 2700, "sceneId": 0}
        self.module, self.garbage_first, self.silent, self.error = module, garbage_first, silent, error
        self.received = []
        self.running = True

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            msg = json.loads(data)
            self.received.append(msg)
            if self.silent:
                continue
            if self.garbage_first:
                self.sock.sendto(b"\xff not json", addr)
                self.sock.sendto(b"[1,2,3]", addr)
            if self.error:
                reply = {"method": msg["method"], "error": {"code": -32601, "message": "Method not found"}}
            elif msg["method"] == "getPilot":
                reply = {"method": "getPilot", "result": dict(self.pilot)}
            elif msg["method"] == "getSystemConfig":
                reply = {"method": "getSystemConfig", "result": {"mac": "0a0000000001", "moduleName": self.module}}
            elif msg["method"] == "setPilot":
                self.pilot.update(msg["params"])
                reply = {"method": "setPilot", "result": {"success": True}}
            else:
                continue
            self.sock.sendto(json.dumps(reply).encode(), addr)

    def stop(self):
        self.running = False
        self.join(1)
        self.sock.close()


class WizTest(TempFilesMixin, unittest.TestCase):
    def start_wiz(self, **kw):
        wiz = FakeWiz(**kw)
        wiz.start()
        self.addCleanup(wiz.stop)
        p = mock.patch.object(localhome, "WIZ_PORT", wiz.port)
        p.start()
        self.addCleanup(p.stop)
        return wiz

    def dev(self):
        return {"mac": "0a0000000001", "kind": "wiz", "room": "R", "name": "L", "ip": "127.0.0.1"}

    def test_status_and_caps(self):
        self.start_wiz(module="ESP03_SHTW1C_01")
        st = localhome._wiz_status(self.dev())
        self.assertEqual(st["state"], "on")
        self.assertEqual(st["brightness"], 40)
        self.assertEqual(st["kelvin"], 2700)
        self.assertEqual(st["capabilities"], ["brightness", "white"])
        self.assertRegex(st["color"], r"^#[0-9a-f]{6}$")

    def test_garbage_datagrams_are_ignored(self):
        self.start_wiz(garbage_first=True)
        self.assertEqual(localhome._wiz_call("127.0.0.1", "getPilot")["dimming"], 40)

    def test_set(self):
        wiz = self.start_wiz()
        localhome._wiz_set(self.dev(), on=True, brightness=5, color=(300, -4, 7))
        self.assertEqual(wiz.received[-1]["params"], {"state": True, "dimming": 10, "r": 255, "g": 0, "b": 7})
        localhome._wiz_set(self.dev(), on=True, kelvin=1000)
        self.assertEqual(wiz.received[-1]["params"], {"state": True, "temp": 2200})

    def test_no_answer_is_bounded(self):
        self.start_wiz(silent=True)
        t0 = time.monotonic()
        with self.assertRaises(OSError):
            localhome._wiz_call("127.0.0.1", "getPilot", tries=2)
        self.assertLess(time.monotonic() - t0, 1.6)

    def test_error_reply(self):
        self.start_wiz(error=True)
        with self.assertRaises(OSError):
            localhome._wiz_call("127.0.0.1", "getPilot", tries=1)

    def test_no_ip(self):
        with self.assertRaises(OSError):
            localhome._wiz_call("", "getPilot")


# ---- Tapo KLAP -------------------------------------------------------------------

class FakeTapo:
    """Minimal KLAP v2 plug: handshake1/2, encrypted request, sessions that can be dropped."""

    def __init__(self, email, password, mac="0A-00-00-00-00-03"):
        self.auth = localhome._sha256(hashlib.sha1(email.encode()).digest() + hashlib.sha1(password.encode()).digest())
        self.mac = mac
        self.on = False
        self.sessions = {}
        self.handshakes = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                m = re.search(r"TP_SESSIONID=([0-9A-F]+)", self.headers.get("Cookie") or "")
                sid = m.group(1) if m else None
                if self.path == "/app/handshake1":
                    fake.handshakes += 1
                    remote = os.urandom(16)
                    sid = os.urandom(8).hex().upper()
                    fake.sessions[sid] = {"local": body, "remote": remote, "ok": False}
                    out = remote + localhome._sha256(body + remote + fake.auth)
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"TP_SESSIONID={sid};TIMEOUT=86400")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                s = fake.sessions.get(sid)
                if s is None:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                local, remote, auth = s["local"], s["remote"], fake.auth
                if self.path == "/app/handshake2":
                    s["ok"] = body == localhome._sha256(remote + local + auth)
                    self.send_response(200 if s["ok"] else 403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/app/request") and s["ok"]:
                    seq = int(self.path.split("seq=")[1])
                    key = localhome._sha256(b"lsk" + local + remote + auth)[:16]
                    iv = localhome._sha256(b"iv" + local + remote + auth)[:12] + seq.to_bytes(4, "big", signed=True)
                    req = json.loads(localhome._AES.run(False, key, iv, body[32:]))
                    if req["method"] == "get_device_info":
                        res = {"error_code": 0, "result": {"device_on": fake.on, "mac": fake.mac}}
                    elif req["method"] == "set_device_info":
                        fake.on = req["params"]["device_on"]
                        res = {"error_code": 0}
                    else:
                        res = {"error_code": -1}
                    enc = localhome._AES.run(True, key, iv, json.dumps(res).encode())
                    out = b"\0" * 32 + enc
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.ip = "127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def reboot(self):
        self.sessions.clear()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@unittest.skipUnless(_have_libcrypto(), "libcrypto not available")
class TapoTest(TempFilesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write_env('TAPO_EMAIL="me@example.org"\nTAPO_PASSWORD=pa55word\n')
        self.plug = FakeTapo("me@example.org", "pa55word")
        self.addCleanup(self.plug.stop)
        self.dev = {"mac": "0a0000000003", "kind": "tapo", "room": "R", "name": "Bett", "ip": self.plug.ip}

    def test_status_and_switch(self):
        self.assertEqual(localhome._tapo_status(self.dev)["state"], "off")
        localhome._tapo_set(self.dev, on=True)
        self.assertTrue(self.plug.on)
        self.assertEqual(localhome._tapo_status(self.dev)["state"], "on")
        self.assertEqual(self.plug.handshakes, 1)          # the session is reused

    def test_plug_reboot_rehandshakes(self):
        localhome._tapo_status(self.dev)
        self.plug.reboot()
        self.assertEqual(localhome._tapo_status(self.dev)["state"], "off")
        self.assertEqual(self.plug.handshakes, 2)

    def test_wrong_credentials(self):
        self.write_env("TAPO_EMAIL=me@example.org\nTAPO_PASSWORD=wrong\n")
        localhome._file_cache.clear()
        with self.assertRaises(PermissionError):
            localhome._tapo_status(self.dev)
        self.assertIsNone(localhome._klap[self.dev["mac"]].cookie)   # no half-made session kept

    def test_other_device_at_that_ip(self):
        self.plug.mac = "11-22-33-44-55-66"
        with self.assertRaises(OSError):
            localhome._tapo_status(self.dev)

    def test_unreachable(self):
        dead = dict(self.dev, ip="127.0.0.1:1")
        with self.assertRaises(OSError):
            localhome._tapo_status(dead)


# ---- rediscovery, dashboard cache --------------------------------------------------

class RediscoveryTest(TempFilesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write_devices([
            {"mac": "aa:aa:aa:aa:aa:01", "kind": "wiz", "room": "Wohnzimmer", "name": "Licht", "ip": "10.0.0.1"},
            {"mac": "aaaaaaaaaa02", "kind": "wiz", "room": "Wohnzimmer", "name": "Licht 2", "ip": "10.0.0.2"},
            {"mac": "aaaaaaaaaa03", "kind": "tapo", "room": "Büro", "name": "Steckdose", "ip": "10.0.0.3"},
            {"mac": "bogus", "kind": "wiz"},
            {"mac": "aaaaaaaaaa04", "kind": "zigbee"},
        ])
        self.scans = 0
        self.lan = {"aaaaaaaaaa01": "10.0.1.1", "aaaaaaaaaa02": "10.0.1.2"}

        def scan():
            self.scans += 1
            time.sleep(0.2)
            return dict(self.lan)
        p = mock.patch.object(localhome, "_scan_lan", scan)
        p.start()
        self.addCleanup(p.stop)

    def test_load_devices_filters_invalid(self):
        devs = localhome.load_devices()
        self.assertEqual([d["mac"] for d in devs], ["aaaaaaaaaa01", "aaaaaaaaaa02", "aaaaaaaaaa03"])

    def test_concurrent_callers_share_one_scan_and_both_retry(self):
        """Two devices moved: the second caller waits for the first one's scan and then retries
        with the new IP from the cache (before, it was rate-limited and gave up)."""
        answers = {"10.0.1.1": "one", "10.0.1.2": "two"}

        def fn(d):
            if d["ip"] in answers:
                return answers[d["ip"]]
            raise OSError("no answer from " + d["ip"])

        devs = {d["mac"]: d for d in localhome.load_devices()}
        out = {}
        threads = [threading.Thread(target=lambda m=m: out.__setitem__(m, localhome._with_rediscovery(devs[m], fn)))
                   for m in ("aaaaaaaaaa01", "aaaaaaaaaa02")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(out, {"aaaaaaaaaa01": "one", "aaaaaaaaaa02": "two"})
        self.assertEqual(self.scans, 1)
        with open(localhome.IP_CACHE_FILE) as f:
            self.assertEqual(json.load(f), {"aaaaaaaaaa01": "10.0.1.1", "aaaaaaaaaa02": "10.0.1.2"})

    def test_rate_limit_and_backoff(self):
        self.lan = {}
        self.assertEqual(localhome.rediscover(), {})          # nothing moved: interval doubles
        self.assertEqual(self.scans, 1)
        self.assertEqual(localhome._cache["discover_every"], 2 * localhome.REDISCOVER_EVERY)
        localhome.rediscover()
        self.assertEqual(self.scans, 1)                         # rate-limited
        localhome._cache["discovered_at"] -= localhome.REDISCOVER_EVERY + 1
        localhome.rediscover()
        self.assertEqual(self.scans, 1)                         # still inside the backed-off interval
        localhome.rediscover(min_interval=localhome.REDISCOVER_EVERY)
        self.assertEqual(self.scans, 2)                         # the owner switching a device ignores it
        for _ in range(10):
            localhome._cache["discovered_at"] = 0.0
            localhome.rediscover()
        self.assertEqual(localhome._cache["discover_every"], localhome.REDISCOVER_MAX)
        self.lan = {"aaaaaaaaaa03": "10.0.9.9"}
        localhome.rediscover(force=True)
        self.assertEqual(localhome._cache["discover_every"], localhome.REDISCOVER_EVERY)

    def test_permission_error_is_not_retried(self):
        def fn(d):
            raise PermissionError("login")
        with self.assertRaises(PermissionError):
            localhome._with_rediscovery(localhome.load_devices()[0], fn)
        self.assertEqual(self.scans, 0)

    def test_dashboard_not_overwritten_by_older_refresh(self):
        """A refresh that started before a switch must not put the old state back into the cache."""
        state = {"on": False}
        started = threading.Event()
        release = threading.Event()

        def status(d):
            if d["mac"] == "aaaaaaaaaa01":
                st = "on" if state["on"] else "off"
                started.set()
                release.wait(2)
                return {"state": st, "brightness": None, "capabilities": []}
            raise OSError("offline")

        with mock.patch.object(localhome, "_status", status), \
                mock.patch.object(localhome, "rediscover", lambda *a, **k: {}):
            t = threading.Thread(target=localhome.get_dashboard_devices)
            t.start()
            started.wait(2)
            state["on"] = True                 # the switch happens ...
            localhome.invalidate_cache()       # ... and set_switch invalidates
            release.set()
            t.join()
            self.assertIsNone(localhome._cache["dash"])
            dash = localhome.get_dashboard_devices()
        self.assertEqual(dash[0]["state"], "on")
        self.assertFalse(dash[2]["online"])
        self.assertIn("offline", dash[2]["error"])

    def test_stale_dashboard_is_served_while_refreshing(self):
        calls = []
        gate = threading.Event()

        def status(d):
            calls.append(d["mac"])
            gate.wait(2)
            return {"state": "on", "brightness": 1, "capabilities": []}

        with mock.patch.object(localhome, "_status", status):
            gate.set()
            first = localhome.get_dashboard_devices()
            gate.clear()
            localhome._cache["dash_at"] -= localhome.DASHBOARD_TTL + 1     # make it stale
            t = threading.Thread(target=localhome.get_dashboard_devices)
            t.start()
            time.sleep(0.1)
            t0 = time.monotonic()
            again = localhome.get_dashboard_devices()                       # refresh in flight
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertIs(again, first)
            gate.set()
            t.join()
        self.assertEqual(len(calls), 6)

    def test_set_switch_unknown_and_invalid(self):
        self.assertEqual(localhome.set_switch("nonsense", "on")["status"], "error")
        self.assertEqual(localhome.set_switch("ffffffffffff", "on")["error"], "Unbekanntes Gerät")


class EnvTest(TempFilesMixin, unittest.TestCase):
    def test_env_value_and_cache(self):
        self.write_env('# comment\nexport A="1"\nB = 2\nA=again\n')
        self.assertEqual(localhome.env_value("A"), "1")
        self.assertEqual(localhome.env_value("B"), "2")
        self.assertEqual(localhome.env_value("C"), "")
        with mock.patch.dict(os.environ, {"C": "from-env"}):
            self.assertEqual(localhome.env_value("C"), "from-env")
        time.sleep(0.01)
        self.write_env("A=changed\n")
        self.assertEqual(localhome.env_value("A"), "changed")

    def test_missing_env(self):
        self.assertEqual(localhome.env_value("A"), "")

    def test_set_env_value_keeps_other_lines(self):
        self.write_env("TAPO_EMAIL=x@y.z\nGEMINI_API_KEY=old\nTAPO_PASSWORD=pw")
        localhome.set_env_value("GEMINI_API_KEY", "new")
        with open(localhome.ENV_FILE) as f:
            self.assertEqual(f.read(), "TAPO_EMAIL=x@y.z\nTAPO_PASSWORD=pw\nGEMINI_API_KEY=new\n")
        self.assertEqual(os.stat(localhome.ENV_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(localhome.env_value("GEMINI_API_KEY"), "new")

    def test_unreadable_env_is_not_overwritten(self):
        with open(localhome.ENV_FILE, "wb") as f:
            f.write(b"TAPO_PASSWORD=\xff\xfe\n")
        with self.assertRaises(UnicodeDecodeError):
            localhome.set_env_value("GEMINI_API_KEY", "x")
        with open(localhome.ENV_FILE, "rb") as f:
            self.assertEqual(f.read(), b"TAPO_PASSWORD=\xff\xfe\n")

    def test_injection_rejected(self):
        with self.assertRaises(ValueError):
            localhome.set_env_value("GEMINI_API_KEY", "a\nTAPO_PASSWORD=x")
        with self.assertRaises(ValueError):
            localhome.set_env_value("bad key", "x")


class MatchingTest(TempFilesMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.write_devices([
            {"mac": "000000000001", "kind": "wiz", "room": "Arbeitszimmer", "name": "Deckenlampe"},
            {"mac": "000000000002", "kind": "wiz", "room": "Arbeitszimmer", "name": "Fernseher Bar"},
            {"mac": "000000000003", "kind": "tapo", "room": "Arbeitszimmer", "name": "Bett"},
            {"mac": "000000000004", "kind": "wiz", "room": "Wohnzimmer", "name": "Fernseher Bar 1"},
            {"mac": "000000000005", "kind": "wiz", "room": "Büro", "name": "Licht"},
        ])

    def names(self, q):
        return [localhome.display_name(d) for d in localhome.find_devices(q)]

    def test_room(self):
        self.assertEqual(len(self.names("Arbeitszimmer")), 3)
        self.assertEqual(self.names("Licht im Büro"), ["Büro Licht"])

    def test_device(self):
        self.assertEqual(self.names("Deckenlicht"), ["Arbeitszimmer Deckenlampe"])
        self.assertEqual(self.names("Bett"), ["Arbeitszimmer Bett"])
        self.assertEqual(self.names(""), [])
        self.assertEqual(self.names("Garage"), [])

    def test_parse_color(self):
        self.assertEqual(localhome.parse_color("#ff8800"), (255, 136, 0))
        self.assertEqual(localhome.parse_color("FF8800"), (255, 136, 0))
        self.assertIsNone(localhome.parse_color("red"))
        self.assertIsNone(localhome.parse_color(None))


if __name__ == "__main__":
    unittest.main()
