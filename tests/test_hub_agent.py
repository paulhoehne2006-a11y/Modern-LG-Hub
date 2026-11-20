"""Tests for gemini-tv-controller/agent.py with the Gemini HTTP call and the TV stubbed."""
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "gemini-tv-controller")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import agent  # noqa: E402
import localhome  # noqa: E402

KEY = "AIzaSECRET_test_key_123456"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply(parts, finish="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}]}


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-agent-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, attr in (("devices.json", "DEVICES_FILE"), ("ip-cache.json", "IP_CACHE_FILE"), (".env", "ENV_FILE")):
            p = mock.patch.object(localhome, attr, os.path.join(self.tmp, name))
            p.start()
            self.addCleanup(p.stop)
        localhome._file_cache.clear()
        with open(localhome.DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump({"devices": [
                {"mac": "000000000001", "kind": "wiz", "room": "Wohnzimmer", "name": "Licht"},
                {"mac": "000000000002", "kind": "tapo", "room": "Büro", "name": "Steckdose"},
            ]}, f)
        with open(localhome.ENV_FILE, "w") as f:
            f.write(f"GEMINI_API_KEY={KEY}\nGEMINI_MODEL=test-model\n")
        for k in ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_TIMEOUT"):
            if os.environ.get(k):
                p = mock.patch.dict(os.environ, {k: ""})
                p.start()
                self.addCleanup(p.stop)
        self.tv = mock.MagicMock()
        self.tv.launch_app.return_value = {"app": "netflix", "app_id": "netflix", "success": True}
        self.tv.volume_down.return_value = {"action": "volume_down", "steps": 3, "success": True}
        self.tv.screen_off.return_value = {"action": "screen_off", "success": False}
        self.tv.toast.return_value = {"success": True}
        p = mock.patch.object(agent, "LGTV", self.tv)
        p.start()
        self.addCleanup(p.stop)
        self.requests = []

    def gemini(self, answer=None, exc=None):
        def fake(req, timeout=None):
            self.requests.append((req, timeout))
            if exc is not None:
                raise exc
            return Resp(json.dumps(answer).encode())
        return mock.patch("agent.urllib.request.urlopen", fake)

    def ask(self, prompt="hi", history=None, answer=None, exc=None):
        with self.gemini(answer, exc):
            return agent.GeminiTVAgent().process_command(prompt, history or [])

    def test_no_key(self):
        with open(localhome.ENV_FILE, "w") as f:
            f.write("")
        localhome._file_cache.clear()
        res = agent.GeminiTVAgent().process_command("hi")
        self.assertTrue(res["needs_key"])
        self.assertEqual(res["actions"], [])

    def test_text_answer_with_suggestions(self):
        res = self.ask(answer=reply([{"text": "Hallo!\n**NACHFRAGEN: Wetter? | Film-Tipp | Licht an**"}]))
        self.assertEqual(res["text"], "Hallo!")
        self.assertEqual(res["suggestions"], ["Wetter?", "Film-Tipp", "Licht an"])
        req, timeout = self.requests[0]
        self.assertIn("/models/test-model:generateContent", req.full_url)
        self.assertNotIn(KEY, req.full_url)                      # key only in the header
        self.assertEqual(req.get_header("X-goog-api-key"), KEY)
        self.assertEqual(timeout, agent.DEFAULT_TIMEOUT)

    def test_request_shape_history_and_tools(self):
        history = [{"role": "user", "text": "a"}, {"role": "model", "text": "b"}, "junk", {"role": "x", "text": ""}] \
            + [{"role": "user", "text": str(i)} for i in range(20)]
        self.ask("jetzt", history=history, answer=reply([{"text": "ok"}]))
        body = json.loads(self.requests[0][0].data)
        self.assertEqual(body["contents"][-1], {"role": "user", "parts": [{"text": "jetzt"}]})
        self.assertLessEqual(len(body["contents"]), agent.MAX_HISTORY + 1)
        decl = {t["name"]: t for t in body["tools"][0]["functionDeclarations"]}
        self.assertIn("Wohnzimmer (Licht)", decl["control_smart_device"]["description"])
        self.assertIn("Büro (Steckdose)", decl["control_smart_device"]["description"])

    def test_tool_calls_and_summary(self):
        res = self.ask(answer=reply([{"functionCall": {"name": "launch_app", "args": {"app_name": "netflix"}}},
                                     {"functionCall": {"name": "volume_down", "args": {"steps": "3"}}}]))
        self.tv.launch_app.assert_called_once_with("netflix")
        self.tv.volume_down.assert_called_once_with(3)
        self.assertEqual([a["tool"] for a in res["actions"]], ["launch_app", "volume_down"])
        self.assertEqual(res["text"], "Alles klar! Ich habe Netflix gestartet, und die Lautstärke um 3 Stufen gesenkt.")

    def test_failed_tool_is_reported(self):
        res = self.ask(answer=reply([{"functionCall": {"name": "screen_off", "args": {}}}]))
        self.assertIn("Nicht geklappt", res["text"])
        self.assertIn("Bildschirm ausgeschaltet", res["text"])
        self.assertNotIn("Alles klar", res["text"])

    def test_bad_arguments_do_not_crash(self):
        res = self.ask(answer=reply([
            {"functionCall": {"name": "launch_app", "args": {"app_name": 42}}},
            {"functionCall": {"name": "launch_app", "args": None}},
            {"functionCall": {"name": "volume_up", "args": {"steps": 1e300}}},
            {"functionCall": {"name": "show_toast", "args": {"message": ["x"] * 500}}},
            {"functionCall": {"name": "nope", "args": {}}},
            {"functionCall": "garbage"},
            "garbage",
            {"text": 5},
        ]))
        self.assertEqual(len(res["actions"]), 5)
        self.tv.launch_app.assert_called_once_with("42")
        self.tv.volume_up.assert_called_once_with(20)
        self.assertLessEqual(len(self.tv.toast.call_args_list[-1][0][0]), 200)
        self.assertIn("Unbekannte Funktion", json.dumps(res["actions"][-1]["result"], ensure_ascii=False))

    def test_tool_call_cap(self):
        parts = [{"functionCall": {"name": "volume_up", "args": {"steps": 20}}}] * 50
        res = self.ask(answer=reply(parts))
        self.assertEqual(len(res["actions"]), agent.MAX_TOOL_CALLS)
        self.assertEqual(self.tv.volume_up.call_count, agent.MAX_TOOL_CALLS)

    def test_tool_exception_is_contained(self):
        self.tv.launch_app.side_effect = RuntimeError("luna down")
        res = self.ask(answer=reply([{"functionCall": {"name": "launch_app", "args": {"app_name": "x"}}}]))
        self.assertIn("fehlgeschlagen", res["actions"][0]["result"]["error"])
        self.assertIn("Nicht geklappt", res["text"])

    def test_smart_home_tool(self):
        with mock.patch.object(localhome, "set_switch", return_value={"status": "ok"}) as sw:
            res = self.ask(answer=reply([{"functionCall": {"name": "control_smart_device",
                                                           "args": {"device_name": "Wohnzimmer", "state": "off"}}}]))
        sw.assert_called_once_with("000000000001", "off", None)
        self.assertEqual(res["actions"][0]["result"], {"devices": ["Wohnzimmer Licht"], "state": "off", "success": True})
        self.assertEqual(res["text"], "Alles klar! Ich habe Wohnzimmer ausgeschaltet.")
        with mock.patch.object(localhome, "set_switch", return_value={"status": "error", "error": "weg"}):
            res = self.ask(answer=reply([{"functionCall": {"name": "control_smart_device",
                                                           "args": {"device_name": "Steckdose", "brightness": 250}}}]))
        self.assertFalse(res["actions"][0]["result"]["success"])
        self.assertIn("Nicht geklappt", res["text"])

    def test_http_errors(self):
        cases = [(404, "test-model"), (400, "API-Key"), (403, "API-Key"), (429, "Kontingent"), (503, "nicht verfügbar")]
        for code, word in cases:
            with self.subTest(code=code):
                err = urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(
                    json.dumps({"error": {"message": f"bad key {KEY}"}}).encode()))
                res = self.ask(exc=err)
                self.assertIn(f"HTTP {code}", res["text"])
                self.assertIn(word, res["text"])
                self.assertNotIn(KEY, json.dumps(res))           # never echo the key
                self.assertEqual(res["actions"], [])

    def test_network_failures_are_german(self):
        for exc, word in ((socket.timeout("t"), "rechtzeitig"), (TimeoutError(), "rechtzeitig"),
                          (urllib.error.URLError(socket.timeout()), "rechtzeitig"),
                          (urllib.error.URLError(OSError("no route")), "Internet"),
                          (RuntimeError("?"), "Verbindungsfehler")):
            with self.subTest(exc=exc):
                res = self.ask(exc=exc)
                self.assertIn(word, res["text"])

    def test_malformed_replies(self):
        for answer, word in (({}, "Keine Antwort"), ({"candidates": "x"}, "Keine Antwort"),
                             ({"promptFeedback": {"blockReason": "SAFETY"}}, "abgelehnt"),
                             (reply([], finish="SAFETY"), "abgelehnt"),
                             ({"candidates": [{"content": None}]}, "keine Antwort"),
                             ([1, 2], "Keine Antwort")):
            with self.subTest(answer=answer):
                res = self.ask(answer=answer)
                self.assertIn(word, res["text"])
        with mock.patch("agent.urllib.request.urlopen", lambda req, timeout=None: Resp(b"not json")):
            res = agent.GeminiTVAgent().process_command("hi")
        self.assertIn("unlesbare", res["text"])

    def test_timeout_setting(self):
        with open(localhome.ENV_FILE, "a") as f:
            f.write("GEMINI_TIMEOUT=999\n")
        localhome._file_cache.clear()
        self.assertEqual(agent.get_timeout(), 55.0)
        with mock.patch.dict(os.environ, {"GEMINI_TIMEOUT": "abc"}):
            self.assertEqual(agent.get_timeout(), agent.DEFAULT_TIMEOUT)

    def test_explicit_key_wins_and_env_is_live(self):
        a = agent.GeminiTVAgent()
        self.assertEqual(a._key(), KEY)
        with open(localhome.ENV_FILE, "w") as f:
            f.write("GEMINI_API_KEY=AIzaOTHER_key_000000000\n")
        localhome._file_cache.clear()
        self.assertEqual(a._key(), "AIzaOTHER_key_000000000")   # .env edited: no restart needed
        a.api_key = "explicit_key_0000000"
        self.assertEqual(a._key(), "explicit_key_0000000")

    def test_save_api_key(self):
        agent.save_api_key(" new\nkey ")
        self.assertEqual(agent.get_api_key(), "newkey")
        with open(localhome.ENV_FILE) as f:
            self.assertIn("GEMINI_MODEL=test-model", f.read())

    def test_split_suggestions(self):
        self.assertEqual(agent.split_suggestions("a\nb"), ("a\nb", []))
        self.assertEqual(agent.split_suggestions(None), ("", []))
        text, sug = agent.split_suggestions("x\nNACHFRAGEN: " + " | ".join(["y" * 100] * 5))
        self.assertEqual(text, "x")
        self.assertEqual(len(sug), 3)
        self.assertEqual(len(sug[0]), 60)


if __name__ == "__main__":
    unittest.main()
