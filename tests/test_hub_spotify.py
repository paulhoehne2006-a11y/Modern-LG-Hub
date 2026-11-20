"""Tests for the Spotify ambilight: JPEG DC reader, colour + calibration, the engine's state machine
(against a fake WiZ bulb on 127.0.0.1 and a scripted Spotify client) and the REST endpoints.
Nothing leaves the host; no real lamp or Spotify account is touched."""
import base64
import http.client
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

import jpeg_dc  # noqa: E402
import localhome  # noqa: E402
import spotify_ambient as sa  # noqa: E402

# 64x64 test covers made with Pillow (quality 70, 4:2:0). Pillow's own mean colour in the comments.
# Pillow mean (169.8, 88.3, 36.4)
ORANGE_BASELINE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEEx"
    "NDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozv/wAARCABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
    "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwC9RRRXxx9sFFFFABRRRQAUUUUAc+vjTTW6"
    "QXX/AHwv/wAVUi+LtPbpDc/98r/jXCRVbir35YCij56GYV3vY7RfE1k3SKf/AL5X/GpV1+0bpHN/3yP8a5KKrkVc0sJTR1Qx"
    "lVnSrrFu3RJfyH+NSLqMLdFf8h/jWFFVuLtXNKjBHTCvN7mut1G3QN+VSK4bpmqEVXIq5pRSOqEm9zy2KrcVVIqtxV9TM+Tp"
    "lyKrkVU4quRVxTO6mXIqtxdqqRVbi7VxTO6mXIquRVTiq5FXFM7qZ5bFVuKqkVW4q+nmfK0y5FVyKqcVXIq4pndTLkVW4u1V"
    "Iqtxdq4pndTLkVXIqpxVcirimd1M/9k=")
# Pillow mean (23.3, 122.5, 117.4)
TEAL_PROGRESSIVE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEEx"
    "NDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozv/wgARCABAAEADASIAAhEBAxEB/8QAGgABAQADAQEAAAAAAAAAAAAAAwACBAUGAf/EABkBAAIDAQAA"
    "AAAAAAAAAAAAAAADAQIFBP/aAAwDAQACEAMQAAABxq6MGqCqCqDSy46K0urlzVsrfy00lW19FZT55DRWwqksoVDS3Oqktkee"
    "Q0VrKpLKFQ0tzqpLZH//xAAbEAEBAAIDAQAAAAAAAAAAAAAAAQITEBIUQP/aAAgBAQABBQL4PVg9ODfi3Ytsd47cRERERERE"
    "RERERERERERERERERER//8QAGREBAQEAAwAAAAAAAAAAAAAAAAIBAwQg/9oACAEDAQE/AfG9eG8MtjG4pSlKUpT/xAAZEQAC"
    "AwEAAAAAAAAAAAAAAAAAEQESIDD/2gAIAQIBAT8BxaRj5f/EABoQAAICAwAAAAAAAAAAAAAAAAAxAUECQFD/2gAIAQEABj8C"
    "0FkKRSWXzP/EABsQAAMAAgMAAAAAAAAAAAAAAAABEUHRQFBR/9oACAEBAAE/IeBfQimkvpKYFcC2GJH6J3qQAAAAAA//2gAM"
    "AwEAAgADAAAAEPfffRuBm1al6lal6v/EABoRAAMAAwEAAAAAAAAAAAAAAAABMREgMJH/2gAIAQMBAT8Q0XmfRWC8FKcQD//E"
    "ABYRAQEBAAAAAAAAAAAAAAAAAAEAIP/aAAgBAgEBPxDBA4UMRERERf/EAB0QAAIDAAIDAAAAAAAAAAAAAAExAJHREUBQYfD/"
    "2gAIAQEAAT8Q6AS+j3AtW2BahsC1Y2BKobAlSIiEBTnxIAAAAAAf/9k=")
# Pillow mean (94.6, 94.6, 96.6)
GREY_BASELINE = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEEx"
    "NDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozv/wAARCABAAEADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
    "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwAooopgFFFFABRRRQAUUUUAYY8WWDdIbj/v"
    "lf8AGpB4nsm6RT/98r/jXGx1ajoA6xfENo3SOb/vkf409dbtm6JL+Q/xrmY6tR0AdAuqwN0ST8h/jUi38TdFf8hWLHVqOgDT"
    "W5RugapFcN0zVKOrUdAHnUdWo6qx1ajoAsx1ajqrHVqOgC1HVqOqsdWo6ALUdWo6qx1ajoA86jq1HVWOrUdAFmOrUdVY6tR0"
    "AWo6tR1Vjq1HQBajq1HVWOrUdAH/2Q==")


def mean(px):
    return tuple(sum(p[i] for p in px) / len(px) for i in range(3))


class JpegDcTest(unittest.TestCase):
    def test_block_means_match_a_full_decoder(self):
        for data, ref, tol in ((ORANGE_BASELINE, (169.8, 88.3, 36.4), 1.0),
                               (TEAL_PROGRESSIVE, (23.3, 122.5, 117.4), 2.5),   # first DC scan only
                               (GREY_BASELINE, (94.6, 94.6, 96.6), 1.0)):
            px = jpeg_dc.dc_pixels(data)
            self.assertEqual(len(px), 64)
            for got, want in zip(mean(px), ref):
                self.assertAlmostEqual(got, want, delta=tol)

    def test_garbage_raises(self):
        for data in (b"", b"not a jpeg", ORANGE_BASELINE[:300]):
            with self.assertRaises((jpeg_dc.JpegError, IndexError)):
                jpeg_dc.dc_pixels(data)


class ColourTest(unittest.TestCase):
    CAL = {"saturation": 1.0, "brightness": 35, "hue_shift": 0}

    def test_dominant_colour_prefers_saturated_parts(self):
        c = sa.dominant_color(jpeg_dc.dc_pixels(ORANGE_BASELINE))
        self.assertIsNotNone(c)
        r, g, b = c
        self.assertGreater(r, 180)       # the orange, not the dark grey half
        self.assertLess(b, 70)
        t = sa.dominant_color(jpeg_dc.dc_pixels(TEAL_PROGRESSIVE))
        self.assertGreater(t[1], t[0] + 80)

    def test_small_vivid_accent_beats_large_dull_area(self):
        # Drake "More Life": a big warm photo, a small blue title; Spotify's lyrics background is blue
        photo = [(183.0, 128.0, 90.0)] * 150 + [(80.0, 59.0, 45.0)] * 60
        title = [(10.0, 70.0, 120.0)] * 40
        black = [(3.0, 3.0, 3.0)] * 750
        r, g, b = sa.dominant_color(photo + title + black)
        self.assertGreater(b, r + 60)
        old = sa._mean_color(photo + title + black)                         # the old average: brown or grey, never blue
        self.assertTrue(old is None or old[0] > old[2])

    def test_grey_cover_is_neutral(self):
        self.assertIsNone(sa.dominant_color(jpeg_dc.dc_pixels(GREY_BASELINE)))
        self.assertIsNone(sa.dominant_color([]))
        target = sa.calibrate(None, self.CAL)
        self.assertEqual(target["kelvin"], sa.NEUTRAL_KELVIN)
        self.assertEqual(target["dimming"], 35)

    def test_calibration(self):
        red = (200.0, 20.0, 20.0)
        self.assertEqual(sa.calibrate(red, self.CAL)["rgb"][0], 255)              # full value, brightness separate
        green = sa.calibrate(red, dict(self.CAL, hue_shift=120))["rgb"]
        self.assertEqual(max(range(3), key=lambda i: green[i]), 1)
        white = sa.calibrate(red, dict(self.CAL, saturation=0.0))["rgb"]
        self.assertEqual(white, (255, 255, 255))
        vivid = sa.calibrate((200.0, 120.0, 120.0), dict(self.CAL, saturation=3.0))["rgb"]
        self.assertEqual(min(vivid), 0)                                            # saturation capped at 100 %
        self.assertEqual(sa.calibrate(red, dict(self.CAL, brightness=3))["dimming"], 10)

    def test_settings_are_clamped(self):
        c = sa._clean({"mode": "disco", "saturation": 9, "brightness": 0, "hue_shift": -999, "lamp": "AA:BB"})
        self.assertEqual((c["mode"], c["saturation"], c["brightness"], c["hue_shift"], c["lamp"]),
                         ("tv", 3.0, 10, -180, ""))
        self.assertEqual(sa._clean({"saturation": True})["saturation"], sa.DEFAULTS["saturation"])

    def test_restore_params(self):
        self.assertEqual(sa.restore_params({"state": True, "dimming": 40, "temp": 2700, "sceneId": 0}),
                         {"state": True, "dimming": 40, "temp": 2700})
        self.assertEqual(sa.restore_params({"state": True, "dimming": 80, "sceneId": 4, "speed": 50}),
                         {"state": True, "dimming": 80, "sceneId": 4, "speed": 50})
        self.assertEqual(sa.restore_params({"state": True, "dimming": 60, "r": 1, "g": 2, "b": 3, "c": 0, "w": 0}),
                         {"state": True, "dimming": 60, "r": 1, "g": 2, "b": 3, "c": 0, "w": 0})


class FakeWiz(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.pilot = {"state": True, "dimming": 40, "temp": 2700, "sceneId": 0}
        self.sets = []
        self.silent = False
        self.running = True
        self.start()

    def run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            msg = json.loads(data)
            if msg["method"] == "setPilot":
                self.sets.append(dict(msg["params"]))
                p = msg["params"]
                if "r" in p or "temp" in p or "sceneId" in p:
                    for k in ("r", "g", "b", "c", "w", "temp", "sceneId"):
                        self.pilot.pop(k, None)
                self.pilot.update(p)
            if self.silent:
                continue
            if msg["method"] == "getPilot":
                reply = {"method": "getPilot", "result": dict(self.pilot)}
            elif msg["method"] == "setPilot":
                reply = {"method": "setPilot", "result": {"success": True}}
            else:
                continue
            self.sock.sendto(json.dumps(reply).encode(), addr)

    def stop(self):
        self.running = False
        self.join(1)
        self.sock.close()


class FakeClient:
    def __init__(self):
        self.reply = None
        self.error = None
        self.calls = 0

    def has_login(self):
        return True

    def player(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


def playing(track="t1", cover="https://i.scdn.co/image/a", dev_type="TV", is_playing=True):
    return {"is_playing": is_playing, "device": {"name": "Fernseher", "type": dev_type},
            "item": {"id": track, "name": "Song " + track, "artists": [{"name": "X"}],
                     "album": {"images": [{"url": cover + "640", "width": 640}, {"url": cover, "width": 300},
                                          {"url": cover + "64", "width": 64}]}}}


COLOURS = {"https://i.scdn.co/image/a": (240.0, 120.0, 40.0), "https://i.scdn.co/image/b": (20.0, 60.0, 230.0)}


class EngineTest(unittest.TestCase):
    MAC = "0a0000000002"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-sp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wiz = FakeWiz()
        self.addCleanup(self.wiz.stop)
        with open(os.path.join(self.tmp, "devices.json"), "w", encoding="utf-8") as f:
            json.dump({"devices": [
                {"mac": "0a0000000001", "kind": "wiz", "room": "Wohnzimmer", "name": "Fernseher Bar 1", "ip": "127.0.0.1"},
                {"mac": self.MAC, "kind": "wiz", "room": "Zimmer", "name": "Fernseher Bar", "ip": "127.0.0.1"},
            ]}, f)
        patches = [
            mock.patch.object(localhome, "DEVICES_FILE", os.path.join(self.tmp, "devices.json")),
            mock.patch.object(localhome, "IP_CACHE_FILE", os.path.join(self.tmp, "ip-cache.json")),
            mock.patch.object(localhome, "WIZ_PORT", self.wiz.port),
            mock.patch.object(localhome, "rediscover", lambda *a, **k: {}),
            mock.patch.object(sa, "CONFIG_FILE", os.path.join(self.tmp, "spotify-ambient.json")),
            mock.patch.object(sa, "FADE_TIME", 0.05),
            mock.patch.object(sa, "PAUSE_GRACE", 0.0),
            mock.patch.object(sa, "fetch_cover_color", lambda url: COLOURS.get(url)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        localhome._file_cache.clear()
        self.client = FakeClient()
        self.eng = sa.Ambient(client=self.client)
        self.addCleanup(self.eng.sender.close)

    def settle(self):
        time.sleep(0.15)          # fire-and-forget datagrams reach the fake bulb

    def last_rgb(self):
        p = [s for s in self.wiz.sets if "r" in s][-1]
        return (p["r"], p["g"], p["b"]), p["dimming"]

    def test_default_lamp_is_the_exact_name(self):
        self.assertEqual(self.eng.status()["lamp"]["id"], self.MAC)

    def test_song_colour_fade_and_restore(self):
        self.client.reply = playing("t1")
        self.eng.step()
        self.settle()
        rgb, dim = self.last_rgb()
        self.assertEqual(dim, 35)
        self.assertGreater(rgb[0], 200)
        self.assertGreaterEqual(len(self.wiz.sets), sa.FADE_STEPS)          # faded in steps
        self.assertTrue(self.eng.active)
        self.assertEqual(sa.load_config()["saved"]["temp"], 2700)            # old state remembered on disk
        st = self.eng.status()
        self.assertEqual(st["state"], "active")
        self.assertEqual(st["track"]["name"], "Song t1")

        n = len(self.wiz.sets)
        self.eng.step()                                                     # same song: nothing sent
        self.assertEqual(len(self.wiz.sets), n)

        self.client.reply = playing("t2", cover="https://i.scdn.co/image/b")
        self.eng.step()
        self.settle()
        self.assertGreater(self.last_rgb()[0][2], 200)                       # blue now

        self.client.reply = playing("t2", cover="https://i.scdn.co/image/b", is_playing=False)
        self.eng.step()                                                      # starts the grace period
        self.eng.step()
        self.settle()
        self.assertEqual(self.wiz.sets[-1], {"state": True, "dimming": 40, "temp": 2700})
        self.assertFalse(self.eng.active)
        self.assertNotIn("saved", sa.load_config())

    def test_tv_mode_ignores_other_devices(self):
        self.client.reply = playing("t1", dev_type="Smartphone")
        self.eng.step()
        self.settle()
        self.assertEqual(self.wiz.sets, [])
        self.assertEqual(self.eng.status()["state"], "idle")
        self.eng.set_mode("all")
        self.eng.step()
        self.settle()
        self.assertTrue(self.wiz.sets)

    def test_tv_mode_with_a_configured_device_name(self):
        with self.eng.lock:
            self.eng.cfg["tv_device"] = "Anderer TV"
        self.client.reply = playing("t1")
        self.eng.step()
        self.assertEqual(self.wiz.sets, [])

    def test_lamp_off_is_left_alone(self):
        self.wiz.pilot["state"] = False
        self.client.reply = playing("t1")
        self.eng.step()
        self.settle()
        self.assertEqual(self.wiz.sets, [])
        self.assertEqual(self.eng.status()["state"], "lamp_off")
        self.client.reply = playing("t2")                                   # next song looks again
        self.wiz.pilot["state"] = True
        self.eng.step()
        self.settle()
        self.assertTrue(self.wiz.sets)

    def test_manual_change_hands_the_lamp_over(self):
        self.client.reply = playing("t1")
        self.eng.step()
        self.eng.manual_change(self.MAC)
        self.eng.step()
        self.assertTrue(self.eng.override)
        n = len(self.wiz.sets)
        self.client.reply = playing("t2", cover="https://i.scdn.co/image/b")
        self.eng.step()
        self.client.reply = None                                            # stopped
        self.eng.step()
        self.eng.step()
        self.settle()
        self.assertEqual(len(self.wiz.sets), n)                             # no colour, no restore
        self.assertFalse(self.eng.override)

    def test_lamp_switched_off_between_songs_is_override(self):
        self.client.reply = playing("t1")
        self.eng.step()
        self.wiz.pilot["state"] = False
        n = len(self.wiz.sets)
        self.client.reply = playing("t2", cover="https://i.scdn.co/image/b")
        self.eng.step()
        self.assertTrue(self.eng.override)
        self.assertEqual(len(self.wiz.sets), n)

    def test_mode_off_gives_the_lamp_back(self):
        self.client.reply = playing("t1")
        self.eng.step()
        self.eng.set_mode("off")
        delay = self.eng.step()
        self.settle()
        self.assertEqual(self.wiz.sets[-1], {"state": True, "dimming": 40, "temp": 2700})
        self.assertGreaterEqual(delay, 60)
        self.assertEqual(self.client.calls, 1)                               # no polling while off

    def test_calibration_preview_while_nothing_plays(self):
        self.client.reply = None
        self.eng.set_calibration({"brightness": 80, "hue_shift": 30})
        self.eng.step()
        self.settle()
        self.assertEqual(self.last_rgb()[1], 80)
        self.assertEqual(self.eng.status()["state"], "preview")
        self.eng.preview_until = time.time() - 1                              # hold time over
        self.eng.step()
        self.settle()
        self.assertEqual(self.wiz.sets[-1], {"state": True, "dimming": 40, "temp": 2700})

    def test_pending_restore_after_restart(self):
        sa.save_config(dict(sa.DEFAULTS, saved={"state": True, "dimming": 55, "sceneId": 6, "mac": self.MAC}))
        eng = sa.Ambient(client=self.client)
        self.addCleanup(eng.sender.close)
        self.client.reply = None
        eng.step()
        self.assertEqual(self.wiz.sets[-1], {"state": True, "dimming": 55, "sceneId": 6})
        self.assertNotIn("saved", sa.load_config())

    def test_unreachable_lamp_on_restore_is_retried(self):
        self.client.reply = playing("t1")
        self.eng.step()
        self.wiz.silent = True
        self.client.reply = None
        self.eng.step()
        self.eng.step()
        self.assertIn("saved", sa.load_config())
        self.assertEqual(self.eng.status()["state"], "error")
        self.wiz.silent = False
        self.eng.retry_restore_at = 0
        self.eng.step()
        self.assertNotIn("saved", sa.load_config())

    def test_spotify_errors(self):
        self.assertEqual(self.eng._spotify_failed(sa.SpotifyError("ratelimit", "429", 17.0), 0), 17.0)
        self.assertEqual(self.eng._spotify_failed(sa.SpotifyError("network"), 0), sa.BACKOFF[0])
        self.assertEqual(self.eng._spotify_failed(sa.SpotifyError("network"), 99), sa.BACKOFF[-1])
        self.assertEqual(self.eng._spotify_failed(sa.SpotifyError("login", "abgelaufen"), 0), 30.0)
        self.assertEqual(self.eng.status()["state"], "login")

    def test_long_outage_gives_the_lamp_back(self):
        self.client.reply = playing("t1")
        self.eng.step()
        self.eng._spotify_failed(sa.SpotifyError("network"), 0)
        self.assertTrue(self.eng.active)
        self.eng.paused_since -= sa.NO_NET_RESTORE + 1
        self.eng._spotify_failed(sa.SpotifyError("network"), 1)
        self.assertFalse(self.eng.active)

    def test_worker_survives_exceptions(self):
        self.client.error = RuntimeError("boom")
        self.eng.start()
        time.sleep(0.2)
        self.assertTrue(self.eng.thread.is_alive())
        self.assertEqual(self.eng.status()["state"], "error")
        self.eng.stopping = True
        self.eng.wake.set()


class SpotifyClientTest(unittest.TestCase):
    def test_429_reads_retry_after(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "42"}, None)
        self.assertEqual(sa._retry_after(err), 42.0)
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        self.assertEqual(sa._retry_after(err), 30.0)

    def test_cover_takes_the_300px_image(self):
        imgs = {"images": [{"url": "https://x/640", "width": 640}, {"url": "https://x/300", "width": 300},
                           {"url": "https://x/64", "width": 64}]}
        self.assertEqual(sa._cover_url(imgs), "https://x/300")
        self.assertEqual(sa._cover_url({"images": [{"url": "https://x/64", "width": 64}]}), "https://x/64")
        self.assertEqual(sa._cover_url({}), "")


class RestTest(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.tmp = tempfile.mkdtemp(prefix="hub-sp-rest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "devices.json"), "w", encoding="utf-8") as f:
            json.dump({"devices": [{"mac": "0a0000000002", "kind": "wiz", "room": "Z", "name": "Fernseher Bar", "ip": "10.9.9.9"},
                                   {"mac": "0a0000000003", "kind": "tapo", "room": "Z", "name": "Bett", "ip": "10.9.9.8"}]}, f)
        for p in (mock.patch.object(localhome, "DEVICES_FILE", os.path.join(self.tmp, "devices.json")),
                  mock.patch.object(localhome, "IP_CACHE_FILE", os.path.join(self.tmp, "ip-cache.json")),
                  mock.patch.object(localhome, "ENV_FILE", os.path.join(self.tmp, ".env")),
                  mock.patch.object(sa, "CONFIG_FILE", os.path.join(self.tmp, "spotify-ambient.json"))):
            p.start()
            self.addCleanup(p.stop)
        localhome._file_cache.clear()
        eng = sa.Ambient(client=FakeClient())
        p = mock.patch.object(sa, "_engine", eng)
        p.start()
        self.addCleanup(p.stop)
        self.eng = eng
        self.httpd = server.ThreadingServer(("127.0.0.1", 0), server.RequestHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Content-Type": "application/json"} if body is not None else {})
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        return r.status, data

    def test_status_mode_calibration_lamp(self):
        st, data = self.req("GET", "/api/spotify/ambient")
        self.assertEqual(st, 200)
        self.assertEqual((data["mode"], data["calibration"]["brightness"]), ("tv", 35))
        self.assertEqual(data["lamp"]["id"], "0a0000000002")
        self.assertEqual(self.req("POST", "/api/spotify/ambient/mode", {"mode": "all"})[1]["mode"], "all")
        self.assertEqual(self.req("POST", "/api/spotify/ambient/mode", {"mode": "x"})[0], 400)
        st, data = self.req("POST", "/api/spotify/ambient/calibration", {"saturation": 2.5, "brightness": 500, "hue_shift": -20})
        self.assertEqual((st, data["calibration"]), (200, {"saturation": 2.5, "brightness": 100, "hue_shift": -20}))
        self.assertEqual(self.req("POST", "/api/spotify/ambient/calibration", {"brightness": "hell"})[0], 400)
        self.assertEqual(sa.load_config()["mode"], "all")
        self.assertEqual(self.req("POST", "/api/spotify/ambient/lamp", {"id": "0a0000000003"})[0], 400)   # a plug
        self.assertEqual(self.req("POST", "/api/spotify/ambient/lamp", {"id": "0a0000000002"})[0], 200)
        self.assertEqual(self.req("POST", "/api/spotify/ambient/nope", {})[0], 404)

    def test_requests_never_wait_for_the_worker(self):
        t = time.time()
        for _ in range(5):
            self.req("POST", "/api/spotify/ambient/calibration", {"brightness": 50})
        self.assertLess(time.time() - t, 2.0)


if __name__ == "__main__":
    unittest.main()
