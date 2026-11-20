"""State-machine tests for custom-launcher/com.custom.launcher/launcherd.py.

Two levels:
* on_life_event and friends called directly with a fake luna bus;
* the real daemon() main loop, with luna-send replaced by in-process fakes and the two
  subscriptions fed through pipes. select() is counted, so a busy loop (the H1 bug from
  audit/review.md) fails the test instead of eating a core on the TV.
Every file the daemon touches is redirected into a temp directory.
"""
import importlib.util
import json
import os
import select as real_select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHERD = os.path.join(ROOT, "custom-launcher", "com.custom.launcher", "launcherd.py")

_spec = importlib.util.spec_from_file_location("launcherd", LAUNCHERD)
launcherd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcherd)

APP = launcherd.APP_ID
HOME = launcherd.LG_HOME
SAM = launcherd.SAM
ORIG_FILTER = ("// LG key filter\nfunction onHome(key) {\n    "
               + launcherd.KEYFILTER_OLD + "\n}\n")


class FakeBus:
    """luna_call/luna_fire replacement: configd, settings, SAM; records everything."""

    def __init__(self, test):
        self.test = test
        self.lock = threading.Lock()
        self.fired = []
        self.calls = []
        self.foreground = "netflix"
        self.list_apps_ok = True
        self.configs = {
            "com.webos.applicationManager.keepAliveApps": ["com.webos.app.home"],
            launcherd.MEMMGR_KEEPALIVE: ["x"],
            launcherd.KEYFILTER_CFG: [{"name": "systemUi", "file": test.orig_filter}, {"file": "/other.js"}],
        }
        self.memchute_keepalive = [APP]
        self.default_apps = {"MembershipApp": "com.webos.app.overlaymembership"}

    def call(self, uri, params=None, timeout=20):
        with self.lock:
            self.calls.append((uri, params))
        if uri == SAM + "getForegroundAppInfo":
            return {"returnValue": True, "appId": self.foreground}
        if uri == SAM + "listApps":
            if not self.list_apps_ok:
                return {"returnValue": False, "errorText": "busy"}
            return {"returnValue": True, "apps": [
                {"id": "netflix", "title": "Netflix", "visible": True},
                {"id": "youtube.leanback.v4", "title": "YouTube", "visible": True},
                {"id": APP, "title": "Home"},
                {"id": "com.webos.app.hdmi1", "title": "HDMI 1", "visible": False}]}
        if uri == SAM + "running":
            return {"returnValue": True, "running": []}
        if uri.endswith("service.config/getConfigs"):
            return {"returnValue": True, "configs": {k: json.loads(json.dumps(self.configs[k]))
                                                     for k in params["configNames"] if k in self.configs}}
        if uri.endswith("service.config/setConfigs"):
            self.configs.update(json.loads(json.dumps(params["configs"])))
            return {"returnValue": True}
        if uri.endswith("memorymanager/getConfigs"):
            return {"returnValue": True, "KeepAlive": list(self.memchute_keepalive)}
        if uri.endswith("getSystemSettings"):
            return {"returnValue": True, "settings": {"defaultApps": dict(self.default_apps)}}
        if uri.endswith("setSystemSettings"):
            self.default_apps = params["settings"]["defaultApps"]
            return {"returnValue": True}
        return {"returnValue": True}

    def fire(self, uri, params):
        with self.lock:
            self.fired.append((time.monotonic(), uri, params))

    def launches(self):
        with self.lock:
            return [p for _, u, p in self.fired if u == SAM + "launch" and p.get("id") == APP]

    def closes(self):
        with self.lock:
            return [p for _, u, p in self.fired if u == SAM + "closeByAppId"]

    def filter_files(self):
        return [f.get("file") for f in self.configs[launcherd.KEYFILTER_CFG]]


class LauncherdBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-ld-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        t = self.tmp
        app_dir = os.path.join(t, "app")
        os.makedirs(app_dir)
        with open(os.path.join(app_dir, "appinfo.json"), "w") as f:
            json.dump({"id": APP}, f)
        self.orig_filter = os.path.join(t, "KeyFilters", "systemUi.js")
        os.makedirs(os.path.dirname(self.orig_filter))
        with open(self.orig_filter, "w") as f:
            f.write(ORIG_FILTER)
        paths = {
            "APP_DIR": app_dir, "DATA_DIR": os.path.join(app_dir, "data"),
            "ICON_DIR": os.path.join(app_dir, "data", "icons"),
            "OFF_FLAG": os.path.join(t, "custom-launcher.off"), "PASS_FLAG": os.path.join(t, "pass"),
            "LOCK_FILE": os.path.join(t, "lock"), "LOG_FILE": os.path.join(t, "log"),
            "REFRESH_LOCK": os.path.join(t, "refresh.lock"), "USAGE_PERSIST": os.path.join(t, "usage.json"),
            "SYSLOG": os.path.join(t, "no-syslog"),
            "KEYFILTER_ORIG": (self.orig_filter,),
            "KEYFILTER_COPY": os.path.join(t, "copy", "systemUi.js"),
        }
        for k, v in paths.items():
            p = mock.patch.object(launcherd, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.bus = FakeBus(self)
        for name, fn in (("luna_call", self.bus.call), ("luna_fire", self.bus.fire),
                         ("kill_stale_subscriptions", lambda: None)):
            p = mock.patch.object(launcherd, name, fn)
            p.start()
            self.addCleanup(p.stop)
        launcherd.KNOWN.clear()
        launcherd.KNOWN.update(["netflix", "youtube.leanback.v4"])
        launcherd._children[:] = []
        launcherd._memchute_restarted = False
        launcherd._keyfilter_warned = False
        launcherd._shrink_seen = False

    def log_text(self):
        try:
            with open(launcherd.LOG_FILE) as f:
                return f.read()
        except OSError:
            return ""

    def touch(self, path):
        with open(path, "w"):
            pass


class LifeEventTest(LauncherdBase):
    def setUp(self):
        super().setUp()
        self.st = launcherd.State()
        self.st.usage = {}

    def ev(self, app, event):
        launcherd.on_life_event(self.st, {"appId": app, "event": event})

    def test_home_launch_redirects_without_closing(self):
        self.ev(HOME, "launch")
        self.assertEqual(len(self.bus.launches()), 1)
        self.assertEqual(self.bus.launches()[0], {"id": APP, "params": {"reason": "home"}})
        self.assertEqual(self.bus.closes(), [])                 # no window yet: nothing to close
        self.assertEqual(len(self.st.burst), 2)
        self.assertEqual(self.st.nag_left, 5)

    def test_home_foreground_redirects_and_closes(self):
        self.ev(HOME, "foreground")
        self.assertTrue(self.st.home_front)
        self.assertEqual(len(self.bus.launches()), 1)
        self.assertEqual(self.bus.closes(), [{"id": HOME}])
        self.ev(APP, "foreground")
        self.assertFalse(self.st.home_front)
        self.assertEqual((self.st.burst, self.st.nag_left), ([], 0))
        self.assertFalse(launcherd.nag_due(self.st))

    def test_pass_flag_and_second_home_press(self):
        self.touch(launcherd.PASS_FLAG)
        self.ev(HOME, "launch")
        self.assertTrue(self.st.allow_home)
        self.assertFalse(os.path.exists(launcherd.PASS_FLAG))
        self.ev(HOME, "foreground")
        self.assertEqual(self.bus.launches(), [])
        self.assertFalse(launcherd.nag_due(self.st))
        self.ev(HOME, "launch")                                 # same press, logged twice: still allowed
        self.assertEqual(self.bus.launches(), [])
        self.st.allow_since -= launcherd.REPRESS_AFTER + 0.1
        self.ev(HOME, "launch")                                 # HOME pressed again on LG home
        self.assertFalse(self.st.allow_home)
        self.assertEqual(len(self.bus.launches()), 1)

    def test_stale_pass_flag_is_ignored(self):
        self.touch(launcherd.PASS_FLAG)
        old = time.time() - launcherd.PASS_SECONDS - 5
        os.utime(launcherd.PASS_FLAG, (old, old))
        self.ev(HOME, "launch")
        self.assertFalse(self.st.allow_home)
        self.assertEqual(len(self.bus.launches()), 1)

    def test_paused(self):
        self.touch(launcherd.OFF_FLAG)
        self.ev(HOME, "foreground")
        self.assertEqual(self.bus.launches(), [])
        self.assertTrue(self.st.home_front)
        self.assertFalse(launcherd.nag_due(self.st))
        os.remove(launcherd.OFF_FLAG)
        os.remove(os.path.join(launcherd.APP_DIR, "appinfo.json"))   # uninstalled = paused too
        self.assertTrue(launcherd.paused())

    def test_usage_counting(self):
        self.ev("netflix", "foreground")
        self.ev("netflix", "foreground")                        # same app again: not counted twice
        self.ev("unknown.app", "foreground")
        self.ev("netflix", "foreground")
        self.assertAlmostEqual(self.st.usage["netflix"]["n"], 2.0, places=3)
        self.assertNotIn("unknown.app", self.st.usage)
        self.st.quiet, self.st.started = 1000, time.time()     # boot quiet period
        self.ev("youtube.leanback.v4", "foreground")
        self.assertNotIn("youtube.leanback.v4", self.st.usage)

    def test_usage_decay_and_load(self):
        u = {}
        launcherd.usage_record(u, "a", 0)
        launcherd.usage_record(u, "a", launcherd.USAGE_HALF_LIFE)
        self.assertAlmostEqual(u["a"]["n"], 1.5)
        launcherd.usage_flush(u)
        self.assertEqual(launcherd.usage_load(), u)
        with open(launcherd.USAGE_PERSIST, "w") as f:
            f.write('{"usage": {"x": {"n": "bad"}, "y": {"n": 2, "t": 5}}}')
        self.assertEqual(launcherd.usage_load(), {"y": {"n": 2.0, "t": 5}})
        os.remove(launcherd.USAGE_PERSIST)
        self.assertIn("netflix", launcherd.usage_load())         # seed on a fresh install

    def test_ignores_incomplete_events(self):
        for ev in ({}, {"appId": HOME}, {"event": "launch"}, {"returnValue": True, "subscribed": True}):
            launcherd.on_life_event(self.st, ev)
        self.assertEqual(self.bus.fired, [])


class HomeKeyTest(LauncherdBase):
    def test_patch_and_restore(self):
        st = launcherd.State()
        launcherd.set_home_key(st, True)
        self.assertTrue(st.home_key_direct)
        self.assertEqual(self.bus.filter_files(), [launcherd.KEYFILTER_COPY, "/other.js"])
        with open(launcherd.KEYFILTER_COPY) as f:
            patched = f.read()
        self.assertIn(launcherd.KEYFILTER_NEW, patched)
        self.assertNotIn(launcherd.KEYFILTER_OLD, patched)
        n = len(self.bus.calls)
        launcherd.set_home_key(st, True)                         # idempotent: no second setConfigs
        self.assertFalse([u for u, _ in self.bus.calls[n:] if u.endswith("setConfigs")])
        launcherd.set_home_key(st, False)
        self.assertFalse(st.home_key_direct)
        self.assertEqual(self.bus.filter_files(), [self.orig_filter, "/other.js"])

    def test_unknown_firmware_keeps_original(self):
        with open(self.orig_filter, "w") as f:
            f.write("function onHome() { somethingElse(); }")
        st = launcherd.State()
        launcherd.set_home_key(st, True)
        self.assertFalse(st.home_key_direct)
        self.assertEqual(self.bus.filter_files(), [self.orig_filter, "/other.js"])
        self.assertIn("not recognised", self.log_text())

    def test_guard_tick_while_paused_undoes_a_leftover_patch(self):
        """A killed daemon leaves the patch in configd; the next one must undo it when paused."""
        self.bus.configs[launcherd.KEYFILTER_CFG][0]["file"] = launcherd.KEYFILTER_COPY
        self.touch(launcherd.OFF_FLAG)
        st = launcherd.State()                                   # fresh daemon: home_key_direct False
        launcherd.guard_tick(st)
        self.assertEqual(self.bus.filter_files(), [self.orig_filter, "/other.js"])
        self.assertFalse([u for u, _ in self.bus.calls if u.endswith("getSystemSettings")])

    def test_guard_tick_active(self):
        self.bus.default_apps["home"] = APP
        st = launcherd.State()
        launcherd.guard_tick(st)
        self.assertNotIn("home", self.bus.default_apps)          # the "Home key dead" trap is undone
        self.assertIn(APP, self.bus.configs["com.webos.applicationManager.keepAliveApps"])
        self.assertIn(APP, self.bus.configs[launcherd.MEMMGR_KEEPALIVE])
        self.assertTrue(st.home_key_direct)

    def test_memchute_restart_is_bounded(self):
        self.bus.memchute_keepalive = ["other"]
        with mock.patch.object(launcherd.subprocess, "call",
                               side_effect=subprocess.TimeoutExpired("systemctl", 30)) as call:
            launcherd.ensure_memchute_keep_alive()
            launcherd.ensure_memchute_keep_alive()               # only once per daemon run
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args[1]["timeout"], launcherd.MEMCHUTE_RESTART_TIMEOUT)
        self.assertIn("rc=timeout", self.log_text())


class DataTest(LauncherdBase):
    def test_refresh_writes_apps_and_known(self):
        launcherd.KNOWN.clear()
        self.assertIs(launcherd.refresh(), True)
        with open(os.path.join(launcherd.DATA_DIR, "apps.json")) as f:
            apps = json.load(f)["apps"]
        self.assertEqual([a["id"] for a in apps], ["com.webos.app.hdmi1", "netflix", "youtube.leanback.v4"])
        self.assertEqual(launcherd.KNOWN, {"com.webos.app.hdmi1", "netflix", "youtube.leanback.v4"})

    def test_refresh_failure_and_shrink_guard(self):
        self.bus.list_apps_ok = False
        self.assertIs(launcherd.refresh(), False)
        apps = [{"id": "app%d" % i, "visible": True} for i in range(20)]
        self.assertIs(launcherd.refresh(apps), True)
        self.assertEqual(launcherd.refresh(apps[:5]), "retry")    # suspiciously small: ask again
        self.assertIs(launcherd.refresh(apps[:5]), True)

    def test_load_known(self):
        launcherd.KNOWN.clear()
        os.makedirs(launcherd.DATA_DIR)
        with open(os.path.join(launcherd.DATA_DIR, "apps.json"), "w") as f:
            json.dump({"apps": [{"id": "netflix"}, {"id": 5}, "x"]}, f)
        launcherd.load_known()
        self.assertEqual(launcherd.KNOWN, {"netflix"})

    def test_subscription_reader_handles_split_json(self):
        sub = launcherd.Subscription("luna://x/y", {})
        r, w = os.pipe()
        sub.fd = r
        sub.decoder = launcherd.codecs.getincrementaldecoder("utf-8")("replace")
        os.write(w, b'junk {"appId": "netf')
        self.assertEqual(sub.read(), [])
        os.write(w, b'lix", "event": "\xc3')
        self.assertEqual(sub.read(), [])
        os.write(w, b'\xa4"}\r\n{"a": 1}')
        self.assertEqual(sub.read(), [{"appId": "netflix", "event": "ä"}, {"a": 1}])
        os.close(w)
        self.assertIsNone(sub.read())
        os.close(r)


# ---- the real main loop -------------------------------------------------------------

class PipeSubscription(launcherd.Subscription):
    registry = {}

    def start(self):
        r, w = os.pipe()
        self.fd, self.w = r, w
        self.buf = ""
        self.decoder = launcherd.codecs.getincrementaldecoder("utf-8")("replace")
        self.proc = None
        PipeSubscription.registry[self.uri] = self

    def stop(self):
        for fd in (getattr(self, "w", None), self.fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd, self.w = None, None

    def send(self, obj):
        os.write(self.w, json.dumps(obj).encode() + b"\r\n")


class CountingSelect:
    def __init__(self):
        self.n = 0

    def select(self, r, w, x, timeout=None):
        self.n += 1
        return real_select.select(r, w, x, timeout)


@unittest.skipUnless(threading.current_thread() is threading.main_thread(), "needs the main thread for signals")
class DaemonLoopTest(LauncherdBase):
    def setUp(self):
        super().setUp()
        PipeSubscription.registry = {}
        self.sel = CountingSelect()
        for name, value in (("Subscription", PipeSubscription), ("select", self.sel),
                            ("USAGE_FLUSH_DELAY", 0.1), ("REFRESH_RETRY", 0.3)):
            p = mock.patch.object(launcherd, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.old_term = signal.getsignal(signal.SIGTERM)
        self.old_int = signal.getsignal(signal.SIGINT)

    def tearDown(self):
        signal.signal(signal.SIGTERM, self.old_term)
        signal.signal(signal.SIGINT, self.old_int)
        signal.set_wakeup_fd(-1)
        super().tearDown()

    def run_daemon(self, scenario, boot=False, limit=30.0):
        """Run daemon() in this (main) thread while scenario(life) drives it from another."""
        errors = []

        def driver():
            try:
                deadline = time.time() + 5
                while SAM + "getAppLifeEvents" not in PipeSubscription.registry and time.time() < deadline:
                    time.sleep(0.02)
                scenario(PipeSubscription.registry[SAM + "getAppLifeEvents"])
            except BaseException as e:          # noqa: B902 - reported after the loop stops
                errors.append(e)
            finally:
                os.kill(os.getpid(), signal.SIGTERM)

        safety = threading.Timer(limit, lambda: os.kill(os.getpid(), signal.SIGTERM))
        safety.start()
        t = threading.Thread(target=driver, daemon=True)
        t.start()
        try:
            self.assertEqual(launcherd.daemon(boot), 0)
        finally:
            safety.cancel()
            t.join(5)
        if errors:
            raise errors[0]

    def idle_selects(self, seconds):
        n = self.sel.n
        time.sleep(seconds)
        return self.sel.n - n

    def wait_for(self, cond, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_redirect_and_no_busy_loop(self):
        def scenario(life):
            time.sleep(0.3)
            life.send({"appId": HOME, "event": "launch"})
            self.assertTrue(self.wait_for(lambda: len(self.bus.launches()) >= 1))
            life.send({"appId": HOME, "event": "foreground"})
            self.assertTrue(self.wait_for(lambda: self.bus.closes()))
            life.send({"appId": APP, "event": "foreground"})
            time.sleep(0.6)
            n = len(self.bus.launches())
            self.assertLessEqual(n, 4)                          # launch, foreground, <= 2 bursts
            self.assertLess(self.idle_selects(1.0), 15)         # idle: no spinning
            self.assertEqual(len(self.bus.launches()), n)       # nothing more once the hub is in front

            # owner opened LG home on purpose (pass flag), it stays in front: no nagging, no spinning
            self.touch(launcherd.PASS_FLAG)
            life.send({"appId": HOME, "event": "launch"})
            life.send({"appId": HOME, "event": "foreground"})
            time.sleep(0.3)
            n = len(self.bus.launches())
            self.assertLess(self.idle_selects(1.5), 15)
            self.assertEqual(len(self.bus.launches()), n)

            # off switch while LG home is in front: no spinning either
            life.send({"appId": "netflix", "event": "foreground"})
            life.send({"appId": HOME, "event": "launch"})
            time.sleep(0.1)
            self.touch(launcherd.OFF_FLAG)
            life.send({"appId": HOME, "event": "foreground"})
            time.sleep(0.2)
            self.assertLess(self.idle_selects(1.5), 15)

        self.run_daemon(scenario)
        self.assertIn("daemon stop", self.log_text())

    def test_nag_retries_while_lg_home_stays(self):
        def scenario(life):
            time.sleep(0.2)
            self.bus.foreground = HOME                          # the watchdog sees it too
            life.send({"appId": HOME, "event": "foreground"})
            time.sleep(0.15 + 5 * 0.8 + 0.5)                    # burst + 5 nags, then it gives up
            n = len(self.bus.launches())
            self.assertIn(n, (1 + 1 + 5, 1 + 1 + 5 + 1))       # foreground, burst, 5 nags (+ watchdog at 1 s)
            self.assertLess(self.idle_selects(1.0), 15)
            self.assertEqual(len(self.bus.launches()), n)       # then it gives up (no endless relaunching)

        self.run_daemon(scenario)

    def test_stop_restores_home_key_and_paused_restart_undoes_patch(self):
        def scenario(life):
            # the guard tick is due 2 s after start; the loop wakes at least every 5 s
            self.assertTrue(self.wait_for(lambda: self.bus.filter_files()[0] == launcherd.KEYFILTER_COPY, 8.0))

        self.run_daemon(scenario)
        self.assertEqual(self.bus.filter_files()[0], self.orig_filter)   # clean stop: LG's original

        # a daemon killed with the patch active, then the owner pauses, the supervisor restarts it
        self.bus.configs[launcherd.KEYFILTER_CFG][0]["file"] = launcherd.KEYFILTER_COPY
        self.touch(launcherd.OFF_FLAG)
        PipeSubscription.registry = {}

        def scenario2(life):
            self.assertTrue(self.wait_for(lambda: self.bus.filter_files()[0] == self.orig_filter, 8.0))

        self.run_daemon(scenario2)

    def test_refresh_retry_and_known_from_disk(self):
        launcherd.KNOWN.clear()
        os.makedirs(launcherd.DATA_DIR)
        with open(os.path.join(launcherd.DATA_DIR, "apps.json"), "w") as f:
            json.dump({"apps": [{"id": "netflix"}]}, f)
        self.bus.list_apps_ok = False

        def list_calls():
            return len([u for u, _ in self.bus.calls if u == SAM + "listApps"])

        def scenario(life):
            time.sleep(0.2)
            life.send({"appId": "netflix", "event": "foreground"})
            self.assertTrue(self.wait_for(lambda: os.path.exists(launcherd.USAGE_PERSIST) and
                                          "netflix" in open(launcherd.USAGE_PERSIST).read() and
                                          json.load(open(launcherd.USAGE_PERSIST))["usage"]["netflix"]["n"] > 3))
            self.assertTrue(self.wait_for(lambda: list_calls() >= 1 + launcherd.REFRESH_RETRIES, 4.0))
            time.sleep(1.5)
            self.assertEqual(list_calls(), 1 + launcherd.REFRESH_RETRIES)   # bounded

        self.run_daemon(scenario)

    def test_subscription_error_reply_resubscribes(self):
        def scenario(life):
            time.sleep(0.2)
            life.send({"returnValue": False, "errorText": "Service does not exist"})
            self.assertTrue(self.wait_for(lambda: "subscription error" in self.log_text()))
            self.assertTrue(self.wait_for(lambda: PipeSubscription.registry[SAM + "getAppLifeEvents"].fd is not None
                                          and PipeSubscription.registry[SAM + "getAppLifeEvents"] is life
                                          and life.fd is not None, 3.0))
            life.send({"appId": HOME, "event": "launch"})
            self.assertTrue(self.wait_for(lambda: len(self.bus.launches()) >= 1))

        self.run_daemon(scenario)


if __name__ == "__main__":
    unittest.main()
