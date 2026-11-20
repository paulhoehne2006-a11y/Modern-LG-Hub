"""HTTP contract and access-control tests for gemini-tv-controller/server.py.

A real server runs on an ephemeral port on 127.0.0.1; luna (tv.luna_call/luna_fire) and the
smart-home devices are stubbed, so nothing touches a TV or the LAN. The client address the
server sees can be faked per request with the X-Test-Client header (see _fake_client_ip).

Run: python3 -m unittest discover -s tests -p 'test_hub_*.py'   (or: pytest tests)
"""
import http.client
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "gemini-tv-controller")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import localhome  # noqa: E402
import server  # noqa: E402
import tv  # noqa: E402

ANDROID_FIXTURES = os.environ.get(
    "HUB_ANDROID_FIXTURES", "/home/user/LG-Remote-Android/app/src/test/resources/hub")

LAN = "192.168.178.50"
PUBLIC = "8.8.8.8"
LOOP = "127.0.0.1"


class FakeLuna:
    """Answers luna calls from a table; records every call and fire-and-forget."""

    def __init__(self):
        self.calls = []
        self.fired = []
        self.lock = threading.Lock()
        self.replies = {
            "luna://com.webos.applicationManager/getForegroundAppInfo": {"returnValue": True, "appId": "netflix"},
            "luna://com.webos.service.tvpower/power/getPowerState": {"returnValue": True, "state": "Active"},
            "luna://com.webos.service.audio/getSoundOutput": {"returnValue": True, "soundOutput": "external_optical"},
            "luna://com.webos.service.audio/getVolume": {"returnValue": True, "volume": 23, "muted": False},
            "luna://com.webos.service.tvpower/power/turnOffScreen": {"returnValue": False},
            "luna://com.webos.service.connectionmanager/getStatusInfo": {
                "returnValue": True, "wired": {"macAddress": "02:fc:00:00:00:01"}},
        }

    def call(self, uri, payload, timeout=5):
        with self.lock:
            self.calls.append((uri, payload))
        r = self.replies.get(uri, {"returnValue": True})
        return r(payload) if callable(r) else json.loads(json.dumps(r))

    def fire(self, uri, payload):
        with self.lock:
            self.fired.append((uri, payload))
        return True

    def uris(self):
        return [u for u, _ in self.calls]


def _fake_client_ip(self):
    return self.headers.get("X-Test-Client") or (self.client_address[0] if self.client_address else "")


class HubServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="hubtest-")
        cls.patches = []

        def patch(target, value):
            p = mock.patch(target, value)
            p.start()
            cls.patches.append(p)

        # backend files
        patch("localhome.DEVICES_FILE", os.path.join(cls.tmp, "devices.json"))
        patch("localhome.IP_CACHE_FILE", os.path.join(cls.tmp, "ip-cache.json"))
        patch("localhome.ENV_FILE", os.path.join(cls.tmp, ".env"))
        with open(os.path.join(cls.tmp, "devices.json"), "w", encoding="utf-8") as f:
            json.dump({"devices": [
                {"mac": "0a:00:00:00:00:01", "kind": "wiz", "room": "Wohnzimmer", "name": "Fernseher Bar", "ip": "10.0.0.2"},
                {"mac": "0a0000000003", "kind": "tapo", "room": "Schlafzimmer", "name": "Bett", "ip": "10.0.0.3"},
            ]}, f)
        # launcher data
        app_dir = os.path.join(cls.tmp, "app")
        os.makedirs(os.path.join(app_dir, "data", "icons"))
        with open(os.path.join(app_dir, "data", "apps.json"), "w") as f:
            json.dump({"generatedAt": 1, "apps": [
                {"id": "amazon", "title": "Prime Video", "type": "native", "visible": True, "iconColor": "#0578ff",
                 "icon": "data/icons/amazon.png"},
                {"id": "netflix", "title": "Netflix", "type": "native", "visible": True, "iconColor": "#ffffff",
                 "icon": "data/icons/netflix.png"},
                {"id": "com.custom.launcher", "title": "Home"},
                {"id": "com.webos.app.hdmi1", "title": "HDMI-1"},
            ]}, f)
        with open(os.path.join(app_dir, "data", "icons", "netflix.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        with open(os.path.join(app_dir, "appinfo.json"), "w") as f:
            json.dump({"id": "com.custom.launcher", "version": "9.9.9"}, f)
        patch("tv.APP_DIR", app_dir)
        patch("tv._DEV_APP_DIR", os.path.join(cls.tmp, "nowhere"))
        patch("tv.USAGE_FILE", os.path.join(cls.tmp, "usage.json"))
        patch("server.LAUNCHER_APPINFO", os.path.join(app_dir, "appinfo.json"))
        patch("server.RequestHandler._client_ip", _fake_client_ip)

        cls.httpd = server.ThreadingServer(("127.0.0.1", 0), server.RequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in reversed(cls.patches):
            p.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.luna = FakeLuna()
        p1 = mock.patch("tv.luna_call", self.luna.call)
        p2 = mock.patch("tv.luna_fire", self.luna.fire)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.write_env("")
        for k in ("GEMINI_TOKEN", "GEMINI_API_KEY", "GEMINI_ALLOWED_HOSTS", "WEATHER_LAT", "WEATHER_LON"):
            if k in os.environ:
                p = mock.patch.dict(os.environ, {k: ""})
                p.start()
                self.addCleanup(p.stop)
        tv.LGTV.invalidate_status()
        tv._net_cache.clear()
        localhome.invalidate_cache()
        server._weather.update(data=None, at=0.0)
        # smart-home devices answer from a table instead of the LAN
        self.devices = {"10.0.0.2": {"state": True, "dimming": 55, "r": 255, "g": 40, "b": 0},
                        "10.0.0.3": {"device_on": False}}
        self.dev_calls = []
        p3 = mock.patch("localhome._wiz_call", self._fake_wiz)
        p4 = mock.patch("localhome._tapo", self._fake_tapo)
        p5 = mock.patch("localhome.rediscover", lambda *a, **k: {})
        for p in (p3, p4, p5):
            p.start()
            self.addCleanup(p.stop)
        localhome._cache["caps"].clear()

    # ---- fakes ---------------------------------------------------------------
    def _fake_wiz(self, ip, method, params=None, tries=3):
        self.dev_calls.append((ip, method, params))
        st = self.devices.get(ip)
        if st is None:
            raise OSError(f"no answer from {ip}")
        if method == "getPilot":
            return dict(st)
        if method == "getSystemConfig":
            return {"moduleName": "ESP01_SHRGB_03", "mac": "0a0000000001"}
        if method == "setPilot":
            st.update({"state": params.get("state", st["state"])})
            if "dimming" in params:
                st["dimming"] = params["dimming"]
            return {"success": True}
        raise AssertionError(method)

    def _fake_tapo(self, d):
        test = self

        class S:
            def call(self, method, params=None):
                test.dev_calls.append((d["ip"], method, params))
                st = test.devices.get(d["ip"])
                if st is None:
                    raise OSError("unreachable")
                if method == "get_device_info":
                    return {"device_on": st["device_on"], "mac": "0A-00-00-00-00-03"}
                st["device_on"] = params["device_on"]
                return {}
        return S()

    # ---- helpers -------------------------------------------------------------
    def write_env(self, text):
        with open(localhome.ENV_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        localhome._file_cache.clear()

    def request(self, method, path, body=None, headers=None, client=LOOP, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"X-Test-Client": client}
        h.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        try:
            parsed = json.loads(payload.decode()) if payload else None
        except ValueError:
            parsed = payload
        return resp.status, parsed, resp

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    # ---- access control --------------------------------------------------------
    def test_access_matrix(self):
        cases = [
            # client, headers, expected status
            (LOOP, {}, 200),
            (LAN, {}, 200),
            ("10.1.2.3", {}, 200),
            ("fe80::1", {}, 200),
            ("::ffff:192.168.1.9", {}, 200),
            (PUBLIC, {}, 403),
            ("::ffff:8.8.8.8", {}, 403),
            ("2001:4860::1", {}, 403),
            (LOOP, {"Origin": "null"}, 200),             # the launcher (file://) on the TV
            (LOOP, {"Origin": "file://"}, 200),
            (LAN, {"Origin": "null"}, 403),              # sandboxed iframe of any web page
            (LAN, {"Origin": "file://"}, 403),
            (LAN, {"Origin": "http://192.168.178.20:3000"}, 200),
            (LAN, {"Origin": "http://localhost:8080"}, 200),
            (LAN, {"Origin": "https://evil.example"}, 403),
            (LAN, {"Origin": "http://8.8.8.8"}, 403),
            (LAN, {"Host": "evil.example:8080"}, 403),   # DNS rebinding
            (LAN, {"Host": "192.168.178.60:8080"}, 200),
            (LAN, {"Host": "lgtv.fritz.box:8080"}, 200),
            (LAN, {"Host": "lgtv:8080"}, 200),
            (LAN, {"Host": "[fe80::1]:8080"}, 200),
        ]
        for client, headers, want in cases:
            with self.subTest(client=client, headers=headers):
                status, body, _ = self.get("/api", client=client, headers=headers)
                self.assertEqual(status, want, body)

    def test_allowed_hosts_from_env(self):
        self.write_env("GEMINI_ALLOWED_HOSTS=tv.example.org, other.example\n")
        status, _, _ = self.get("/api", client=LAN, headers={"Host": "tv.example.org:8080"})
        self.assertEqual(status, 200)

    def test_token(self):
        self.write_env("GEMINI_TOKEN=s3cret-token\n")
        status, body, _ = self.get("/api/status", client=LAN)
        self.assertEqual(status, 401)
        self.assertEqual(body, {"error": "Missing or wrong X-Hub-Token"})
        self.assertIn("X-Hub-Token", body["error"])            # Android discovery relies on this
        status, _, _ = self.get("/api/status", client=LAN, headers={"X-Hub-Token": "wrong"})
        self.assertEqual(status, 401)
        status, _, _ = self.get("/api/status", client=LAN, headers={"X-Hub-Token": "s3cret-token"})
        self.assertEqual(status, 200)
        status, _, _ = self.get("/api/status", client=LOOP)     # the TV itself needs no token
        self.assertEqual(status, 200)
        status, body, _ = self.get("/api", client=LAN, headers={"X-Hub-Token": "s3cret-token"})
        self.assertTrue(body["token_required"])
        self.assertNotIn("s3cret-token", json.dumps(body))

    def test_browser_post_needs_json(self):
        status, body, _ = self.request("POST", "/api/remote/key", raw=b'{"key":"UP"}', client=LAN,
                                       headers={"Origin": "http://192.168.178.20", "Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        self.assertIn("application/json", body["error"])
        self.assertEqual(self.luna.fired, [])

    def test_cors_preflight(self):
        status, _, resp = self.request("OPTIONS", "/api/remote/key", client=LOOP,
                                       headers={"Origin": "null", "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "null")
        self.assertEqual(resp.getheader("Access-Control-Allow-Private-Network"), "true")
        status, _, resp = self.request("OPTIONS", "/api/remote/key", client=LAN, headers={"Origin": "null"})
        self.assertEqual(status, 403)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    # ---- body limits --------------------------------------------------------
    def test_body_validation(self):
        status, body, _ = self.request("POST", "/api/remote/key", raw=b"x" * 10,
                                       headers={"Content-Type": "application/json"})
        self.assertEqual((status, body["error"]), (400, "Ungültiges JSON"))
        status, body, _ = self.request("POST", "/api/remote/key", raw=b"[1,2]",
                                       headers={"Content-Type": "application/json"})
        self.assertEqual((status, body["error"]), (400, "JSON muss ein Objekt sein"))
        status, body, _ = self.request("POST", "/api/smarthome/set",
                                       raw=b'{"id":"0a0000000001","brightness":NaN}',
                                       headers={"Content-Type": "application/json"})
        self.assertEqual((status, body["error"]), (400, "Ungültiges JSON"))

    def test_body_too_large(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"POST /api/remote/text HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 70000\r\n\r\n")
        data = b""
        while True:                                   # read until the server closes
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        self.assertIn(b" 400 ", data.split(b"\r\n")[0])
        self.assertIn("Anfrage zu groß".encode(), data)

    def test_slow_client_is_cut_off(self):
        with mock.patch("server.REQUEST_DEADLINE", 0.6):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"GET /api HTTP/1.0\r\n")
            t0 = time.monotonic()
            for _ in range(10):                   # drip a header byte now and then
                try:
                    s.sendall(b"X")
                except OSError:
                    break
                time.sleep(0.2)
                s.settimeout(0.01)
                try:
                    if s.recv(1) == b"":
                        break
                except socket.timeout:
                    pass
                except OSError:
                    break
                s.settimeout(5)
            elapsed = time.monotonic() - t0
            s.close()
        self.assertLess(elapsed, 1.9)

    def test_connection_limit(self):
        with mock.patch("server.MAX_CONNECTIONS", 2), mock.patch("server.MAX_LAN_CONNECTIONS", 1):
            srv = server.ThreadingServer(("127.0.0.1", 0), server.RequestHandler)
        try:
            lan1 = srv._acquire((LAN, 1))
            self.assertIsNotNone(lan1)
            self.assertIsNone(srv._acquire((LAN, 2)))            # LAN share used up
            loop1 = srv._acquire((LOOP, 3))
            self.assertIsNotNone(loop1)                           # the TV still gets in
            self.assertIsNone(srv._acquire((LOOP, 4)))            # total used up
            for s in lan1 + loop1:
                s.release()
            self.assertIsNotNone(srv._acquire((LAN, 5)))
        finally:
            srv.server_close()

    def test_busy_server_answers_503(self):
        srv = server.ThreadingServer(("127.0.0.1", 0), server.RequestHandler)
        srv._slots = threading.BoundedSemaphore(1)
        srv._slots.acquire()                                      # all slots taken
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            conn.request("GET", "/api")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 503)
            self.assertEqual(json.loads(resp.read()), {"error": "Hub ausgelastet, bitte gleich nochmal"})
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()

    # ---- routes -------------------------------------------------------------
    def test_root(self):
        status, body, _ = self.get("/api")
        self.assertEqual(status, 200)
        self.assertIn("LG Hub", body["service"])                 # Android discovery
        self.assertEqual(body["status"], "online")
        self.assertEqual(body["version"], server.SERVER_VERSION)
        self.assertEqual(body["launcher_version"], "9.9.9")
        self.assertFalse(body["has_gemini_key"])
        self.assertFalse(body["has_key"])
        self.assertIsInstance(body["features"], list)
        self.assertEqual(self.get("/")[0], 200)

    def test_status_contract(self):
        status, body, _ = self.get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["has_key"], False)
        t = body["tv"]
        self.assertEqual(t["power"], "Active")
        self.assertFalse(t["screen_off"])
        self.assertEqual(t["current_app"], "netflix")
        self.assertEqual(t["audio"]["soundOutput"], "external_optical")
        self.assertEqual(t["audio"]["volume"], 23)
        self.assertTrue(t["audio"]["is_denon_optical"])
        self.assertIn("primary_mac", t["network"])        # the test host's own MAC or the luna fallback
        self.assertTrue(t["success"])
        self._compare_fixture("status.json", body)

    def test_status_is_shared_and_invalidated(self):
        self.get("/api/status")
        n = len(self.luna.calls)
        self.get("/api/status")                                   # within STATUS_TTL: no new luna calls
        self.assertEqual(len(self.luna.calls), n)
        self.post("/api/remote/key", {"key": "UP"})               # any POST invalidates
        self.get("/api/status")
        self.assertGreater(len(self.luna.calls), n)

    def test_status_concurrent_pollers_share_one_read(self):
        def slow(payload):
            time.sleep(0.3)
            return {"returnValue": True, "appId": "youtube.leanback.v4"}
        self.luna.replies["luna://com.webos.applicationManager/getForegroundAppInfo"] = slow
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.get("/api/status")[1])) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        fg = [u for u in self.luna.uris() if u.endswith("getForegroundAppInfo")]
        self.assertEqual(len(fg), 1)
        self.assertTrue(all(r["tv"]["current_app"] == "youtube.leanback.v4" for r in results))

    def test_state_endpoint(self):
        status, body, _ = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["tv"]["current_app"], "netflix")
        self.assertIn("devices", body)
        self.assertIn("apps_version", body)
        self.assertGreater(body["apps_version"], 0)
        # the first call only starts the smart-home read in the background; later ones carry it
        deadline = time.time() + 5
        while time.time() < deadline:
            body = self.get("/api/state")[1]
            if body["devices"]:
                break
            time.sleep(0.05)
        self.assertEqual([d["id"] for d in body["devices"]], ["0a0000000001", "0a0000000003"])
        self.assertIsInstance(body["devices_age"], float)

    def test_apps_contract_and_usage_order(self):
        now = int(time.time())
        with open(tv.USAGE_FILE, "w") as f:
            json.dump({"updatedAt": now * 1000, "usage": {"netflix": {"n": 4.0, "t": now},
                                                          "amazon": {"n": 1.0, "t": now}}}, f)
        status, body, _ = self.get("/api/apps")
        self.assertEqual(status, 200)
        ids = [a["id"] for a in body["apps"]]
        self.assertEqual(ids, ["com.webos.app.hdmi1", "netflix", "amazon"])
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["apps"][0], {"id": "com.webos.app.hdmi1", "title": "PC (HDMI 1)",
                                           "type": "input", "alias": "pc"})
        self.assertAlmostEqual(body["apps"][1]["usage"], 4.0, places=2)
        self.assertEqual(body["apps"][1]["iconColor"], "#ffffff")
        self._compare_fixture("apps.json", body, list_key="apps")

    def test_icon(self):
        status, body, resp = self.get("/api/icon/netflix")
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "image/png")
        etag = resp.getheader("ETag")
        self.assertTrue(etag)
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nfake")
        status, _, resp = self.get("/api/icon/netflix", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        for bad in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "amazon", "%2e%2e", "a/b", ".hidden"):
            with self.subTest(bad=bad):
                self.assertEqual(self.get("/api/icon/" + bad)[0], 404)

    def test_remote_key(self):
        status, body, _ = self.post("/api/remote/key", {"key": "up"})
        self.assertEqual((status, body), (200, {"action": "send_key", "key": "UP", "success": True}))
        self.assertEqual(self.luna.fired[-1], ("luna://com.webos.service.networkinput/sendSpecialKey", {"key": "UP"}))
        status, body, _ = self.post("/api/remote/key", {"key": "LONG_ENTER"})     # netflix in front -> OK
        self.assertEqual(body["key"], "ENTER")
        self.luna.replies["luna://com.webos.applicationManager/getForegroundAppInfo"] = {
            "returnValue": True, "appId": "com.custom.launcher"}
        status, body, _ = self.post("/api/remote/key", {"key": "LONG_ENTER"})     # hub in front -> INFO
        self.assertEqual(body["key"], "INFO")
        for bad in ({}, {"key": ""}, {"key": 5}, {"key": "UP\"}"}, {"key": "x" * 40}, {"key": "A B"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("/api/remote/key", bad)[0], 400)

    def test_long_enter_uses_fresh_status(self):
        self.luna.replies["luna://com.webos.applicationManager/getForegroundAppInfo"] = {
            "returnValue": True, "appId": "com.custom.launcher"}
        tv.LGTV.invalidate_status()
        self.get("/api/status")                                   # the phone's poll fills the cache
        n = len(self.luna.calls)
        self.assertEqual(tv.LGTV.send_remote_key("LONG_ENTER")["key"], "INFO")
        self.assertEqual(len(self.luna.calls), n)                 # no extra luna call

    def test_text_delete_enter(self):
        status, body, _ = self.post("/api/remote/text", {"text": "hallo", "replace": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.luna.calls[-1], ("luna://com.webos.service.ime/insertText", {"text": "hallo", "replace": 1}))
        self.assertEqual(self.post("/api/remote/text", {"text": 5})[0], 400)
        self.post("/api/remote/delete", {"length": 9999})
        self.assertEqual(self.luna.calls[-1][1], {"length": 500})
        self.post("/api/remote/delete", {"length": "3"})
        self.assertEqual(self.luna.calls[-1][1], {"length": 3})
        self.post("/api/remote/delete", {"length": "x"})
        self.assertEqual(self.luna.calls[-1][1], {"length": 1})
        status, body, _ = self.post("/api/remote/enter")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"action": "send_enter", "success": True})

    def test_launch(self):
        status, body, _ = self.post("/api/launch", {"id": "com.webos.app.hdmi2"})
        self.assertEqual((status, body), (200, {"input": "com.webos.app.hdmi2", "app_id": "com.webos.app.hdmi2",
                                                "success": True}))
        self._compare_fixture("launch_hdmi2.json", body)
        status, body, _ = self.post("/api/launch", {"appId": "netflix"})
        self.assertEqual(status, 200)
        self.assertEqual(body["app_id"], "netflix")
        status, body, _ = self.post("/api/launch", {"app": "Amazon Prime"})       # friendly names still work
        self.assertEqual(body["app_id"], "amazon")
        for bad in ({}, {"id": 3}, {"id": "com.webos.app.hdmi9"}, {"id": "com.webos.app.hdmi1; reboot"},
                    {"id": "a b c"}, {"id": "../x"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post("/api/launch", bad)[0], 400)

    def test_quick(self):
        status, body, _ = self.post("/api/quick", {"action": "vol_up"})
        self.assertEqual((status, body), (200, {"action": "vol_up", "steps": 1, "success": True}))
        self._compare_fixture("quick_vol_up.json", body)
        status, body, _ = self.post("/api/quick", {"action": "nope"})
        self.assertEqual((status, body), (400, {"success": False, "error": "Unbekannte Aktion: nope"}))
        self._compare_fixture("quick_unknown.json", body)
        status, body, _ = self.post("/api/quick", {"action": "screen_off"})       # TV refuses
        self.assertEqual((status, body), (502, {"action": "screen_off", "success": False}))
        self._compare_fixture("quick_screen_off_refused.json", body)
        status, body, _ = self.post("/api/quick", {"action": "pic_game"})
        self.assertEqual(status, 200)
        self.assertEqual(self.luna.calls[-1][1]["settings"], {"pictureMode": "game"})
        self.assertEqual(self.post("/api/quick", {"action": "pic_a\"b"})[0], 400)
        self.assertEqual(self.post("/api/quick", {"action": ["x"]})[0], 400)

    def test_audio_and_picture(self):
        status, body, _ = self.post("/api/audio/output", {"output": "denon"})
        self.assertEqual(body["output"], "external_optical")
        self.assertEqual(self.post("/api/audio/output", {"output": "x\"; rm"})[0], 400)
        self.assertEqual(self.post("/api/audio/output", {})[0], 400)
        status, body, _ = self.post("/api/audio/toggle")
        self.assertEqual(body["output"], "tv_speaker")
        status, body, _ = self.get("/api/audio")
        self.assertEqual(body["volume"], 23)
        status, body, _ = self.post("/api/picture/mode", {"mode": "filmmaker"})
        self.assertEqual(body["mode"], "filmMaker")
        self.assertEqual(self.post("/api/picture/mode", {"mode": 1})[0], 400)

    def test_command_without_key(self):
        status, body, _ = self.post("/api/command", {"prompt": "Hallo"})
        self.assertEqual(status, 200)
        self.assertTrue(body["needs_key"])
        self._compare_fixture("command_no_key.json", body)
        status, body, _ = self.post("/api/command", {"prompt": "  "})
        self.assertEqual((status, body), (400, {"text": "Leere Anfrage.", "actions": []}))

    def test_key_save(self):
        self.write_env("TAPO_EMAIL=a@b.c\nTAPO_PASSWORD=pw")            # no trailing newline
        status, body, _ = self.post("/api/key", {"api_key": "bad key!"})
        self.assertEqual(status, 400)
        status, body, _ = self.post("/api/key", {"api_key": "AIzaTestKey_0123456789"})
        self.assertEqual((status, body), (200, {"success": True, "has_key": True}))
        with open(localhome.ENV_FILE) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["TAPO_EMAIL=a@b.c", "TAPO_PASSWORD=pw", "GEMINI_API_KEY=AIzaTestKey_0123456789"])
        self.assertEqual(os.stat(localhome.ENV_FILE).st_mode & 0o777, 0o600)
        status, body, _ = self.get("/api")
        self.assertTrue(body["has_gemini_key"])
        self.assertNotIn("AIzaTestKey", json.dumps(body))
        server.RequestHandler.agent.api_key = None

    def test_smarthome(self):
        status, body, _ = self.get("/api/smarthome/devices")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 2)
        lamp, plug = body
        self.assertEqual(lamp["id"], "0a0000000001")
        self.assertEqual(lamp["state"], "on")
        self.assertEqual(lamp["brightness"], 55)
        self.assertEqual(lamp["color"], "#ff2800")
        self.assertEqual(lamp["capabilities"], ["brightness", "color", "white"])
        self.assertTrue(lamp["online"])
        self.assertEqual(plug["type"], "plug")
        self.assertEqual(plug["state"], "off")
        for key in ("id", "name", "room", "raw_name", "type", "state", "brightness", "color", "kelvin",
                    "capabilities", "online"):
            self.assertIn(key, lamp)
        self._compare_fixture("smarthome_devices.json", body, top_list=True)
        self.assertEqual(self.get("/api/smartthings/devices")[0], 200)              # old path

        status, body, _ = self.post("/api/smarthome/toggle", {"deviceId": "0a0000000003"})
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["new_state"], "on")
        status, body, _ = self.post("/api/smartthings/toggle", {"id": "0a0000000003"})
        self.assertEqual(body["new_state"], "off")
        status, body, _ = self.post("/api/smarthome/set", {"id": "0a0000000001", "state": "on", "brightness": 30.7})
        self.assertEqual(body["status"], "ok")
        self.assertEqual(self.dev_calls[-1], ("10.0.0.2", "setPilot", {"state": True, "dimming": 30}))
        status, body, _ = self.post("/api/smarthome/set", {"id": "0a0000000001", "color": "#00ff00"})
        self.assertEqual(self.dev_calls[-1][2], {"state": True, "r": 0, "g": 255, "b": 0})
        status, body, _ = self.post("/api/smarthome/set", {"id": "0a0000000001", "kelvin": 99999})
        self.assertEqual(self.dev_calls[-1][2], {"state": True, "temp": 6500})
        self.assertEqual(self.post("/api/smarthome/set", {"id": "0a0000000001", "color": "red"})[0], 400)
        self.assertEqual(self.post("/api/smarthome/set", {"id": "../x"})[0], 400)
        self.assertEqual(self.post("/api/smarthome/toggle", {})[0], 400)
        status, body, _ = self.post("/api/smarthome/set", {"id": "aabbccddeeff"})
        self.assertEqual(body, {"status": "error", "error": "Unbekanntes Gerät"})
        # offline device: error text, still 200 (the clients read "status")
        del self.devices["10.0.0.3"]
        status, body, _ = self.post("/api/smarthome/toggle", {"deviceId": "0a0000000003"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "error")
        self.assertIn("nicht erreichbar", body["error"])
        status, body, _ = self.post("/api/smarthome/set", {"id": "0a0000000003", "state": "on"})
        self.assertEqual(body["status"], "error")
        self.assertTrue(body["error"].startswith("Schlafzimmer Bett nicht erreichbar"))

    def test_weather_cached_and_configurable(self):
        self.write_env("WEATHER_LAT=52.52\nWEATHER_LON=13.405\nWEATHER_TZ=Europe/Berlin\n")
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(url, timeout=None):
            seen.append(url)
            return Resp(json.dumps({"current": {"temperature_2m": 4.3}, "daily": {}}).encode())

        with mock.patch("server.urllib.request.urlopen", fake_urlopen):
            status, body, _ = self.get("/api/weather")
            self.assertEqual(body["current"]["temperature_2m"], 4.3)
            self.get("/api/weather")
        self.assertEqual(len(seen), 1)
        self.assertIn("latitude=52.52", seen[0])
        self.assertIn("longitude=13.405", seen[0])
        # failure later: the last good forecast is served
        server._weather["at"] -= server.WEATHER_TTL + 1

        def failing(url, timeout=None):
            raise OSError("down")
        with mock.patch("server.urllib.request.urlopen", failing):
            status, body, _ = self.get("/api/weather")
        self.assertEqual(body["current"]["temperature_2m"], 4.3)
        server._weather.update(data=None, at=0.0)
        with mock.patch("server.urllib.request.urlopen", failing):
            status, body, _ = self.get("/api/weather")
        self.assertEqual(status, 200)
        self.assertIn("error", body)

    def test_weather_defaults(self):
        url = server.weather_url()
        self.assertIn("latitude=52.52", url)
        self.assertIn("longitude=13.405", url)
        self.write_env("WEATHER_LAT=999\nWEATHER_TZ=$(reboot)\n")
        url = server.weather_url()
        self.assertIn("latitude=52.52", url)
        self.assertIn("timezone=Europe%2FBerlin", url)

    def test_weather_place_from_env_only(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value.read.return_value = b'{"current": {"temperature_2m": 3}}'
        server._weather["data"] = None
        with mock.patch("server.urllib.request.urlopen", return_value=fake):
            self.assertNotIn("place", self.get("/api/weather")[1])
            server._weather["data"] = None
            self.write_env("WEATHER_PLACE=Musterstadt\n")
            self.assertEqual(self.get("/api/weather")[1].get("place"), "Musterstadt")
        server._weather["data"] = None

    def test_not_found_and_internal_error(self):
        self.assertEqual(self.get("/api/nope")[0], 404)
        self.assertEqual(self.post("/api/nope")[0], 404)
        with mock.patch("tv.LGTV.get_audio_status", side_effect=RuntimeError("boom")), \
                mock.patch("server._log") as log:
            status, body, _ = self.get("/api/audio")
        self.assertEqual((status, body), (500, {"error": "Interner Fehler: RuntimeError"}))
        self.assertIn("RuntimeError", log.call_args[0][0])        # traceback goes to the log only

    def test_network(self):
        status, body, _ = self.get("/api/network")
        self.assertEqual(status, 200)
        self.assertIn("primary_mac", body)

    # ---- fixtures from the Android app (optional) --------------------------------
    def _compare_fixture(self, name, body, list_key=None, top_list=False):
        """The hub answer must have every field the Android fixture has (same JSON types)."""
        path = os.path.join(ANDROID_FIXTURES, name)
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            fixture = json.load(f)

        def same_shape(fx, real, where):
            if isinstance(fx, dict):
                self.assertIsInstance(real, dict, where)
                for k, v in fx.items():
                    if k in ("error",) and name.startswith("smarthome"):
                        continue
                    self.assertIn(k, real, f"{where}.{k} missing (fixture {name})")
                    if v is not None and real[k] is not None:
                        same_shape(v, real[k], f"{where}.{k}")
            elif isinstance(fx, list):
                self.assertIsInstance(real, list, where)
                if fx and real:
                    same_shape(fx[0], real[0], where + "[0]")
            elif isinstance(fx, bool):
                self.assertIsInstance(real, bool, where)
            elif isinstance(fx, (int, float)):
                self.assertIsInstance(real, (int, float), where)
            else:
                self.assertIsInstance(real, type(fx), where)

        if top_list:
            same_shape(fixture[0], body[0], name)
        elif list_key:
            self.assertIn(list_key, body)
            app = next(a for a in fixture[list_key] if a["id"] == "netflix")
            mine = next(a for a in body[list_key] if a["id"] == "netflix")
            for k in ("id", "title", "type", "visible", "iconColor", "usage"):
                self.assertIn(k, mine)
                self.assertIsInstance(mine[k], type(app[k]) if not isinstance(app[k], float) else (int, float))
            self.assertIn("count", body)
        else:
            same_shape(fixture, body, name)


if __name__ == "__main__":
    unittest.main()
