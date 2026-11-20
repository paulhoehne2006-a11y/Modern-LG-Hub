"""
Gemini AI Agent with Function Calling for LG webOS TV Control.
Uses the Google Gemini API via pure standard library urllib.
The model is configurable via GEMINI_MODEL (environment or .env), the request timeout via
GEMINI_TIMEOUT (seconds, default 12).

One round trip per question: Gemini either answers with text or asks for tool calls; the
tools are executed here and summarised in German if Gemini sent no text of its own.
The API key travels only in the x-goog-api-key header and is never logged or returned.
"""

import json
import socket
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional
from tv import LGTV
import localhome

ENV_FILE = localhome.ENV_FILE

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_TIMEOUT = 12.0
MAX_TOOL_CALLS = 8        # function calls executed per answer (a model loop can't flood the TV)
MAX_REPLY_BYTES = 1 << 20

# kept for server.py and older callers
_env_value = localhome.env_value


def get_api_key() -> str:
    return localhome.env_value("GEMINI_API_KEY")

def get_model() -> str:
    return localhome.env_value("GEMINI_MODEL") or DEFAULT_MODEL

def get_timeout() -> float:
    try:
        t = float(localhome.env_value("GEMINI_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        t = DEFAULT_TIMEOUT
    return max(3.0, min(55.0, t))

SUGGEST_TAG = "NACHFRAGEN:"
MAX_HISTORY = 12          # turns (user + model) sent back for context

def split_suggestions(text: str):
    """Removes the 'NACHFRAGEN: a | b | c' line Gemini appends; returns (text, [suggestions])."""
    lines = (text or "").rstrip().split("\n")
    for i in range(len(lines) - 1, max(len(lines) - 4, -1), -1):
        raw = lines[i].strip().strip("*_ ")
        if raw.upper().startswith(SUGGEST_TAG):
            items = [s.strip().strip("*_\"' ") for s in raw[len(SUGGEST_TAG):].split("|")]
            items = [s[:60] for s in items if s][:3]
            return "\n".join(lines[:i] + lines[i + 1:]).strip(), items
    return (text or "").strip(), []

def _to_int(val, default: int = 1, lo: int = 1, hi: int = 20) -> int:
    try:
        n = int(float(val))
    except (TypeError, ValueError, OverflowError):
        n = default
    return max(lo, min(hi, n))

def _text(val, limit: int = 200) -> str:
    """A tool argument as a short single string (the model may send numbers, lists, null)."""
    if val is None:
        return ""
    if not isinstance(val, str):
        val = str(val)
    return " ".join(val.split())[:limit]

def save_api_key(key: str) -> None:
    key = "".join(key.split())  # no whitespace/newlines -> no .env line injection
    localhome.set_env_value("GEMINI_API_KEY", key)

def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret and text else text


TOOLS_DECLARATION = [
    {
        "name": "launch_app",
        "description": "Startet eine installierte App auf dem TV (z. B. Netflix, YouTube, Spotify, Prime Video, Disney+, Apple TV, Moonlight, Webbrowser, Homebrew Channel).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Name der App (z.B. 'netflix', 'youtube', 'spotify', 'prime', 'disney', 'appletv', 'moonlight', 'browser', 'home')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "switch_input",
        "description": "Wechselt den Video-Eingang des Fernsehers (z. B. auf PC, Konsole, HDMI 1, HDMI 2, HDMI 3, HDMI 4).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "input_name": {
                    "type": "STRING",
                    "description": "Eingangsname (z.B. 'pc', 'hdmi1', 'hdmi2', 'hdmi3', 'hdmi4')"
                }
            },
            "required": ["input_name"]
        }
    },
    {
        "name": "volume_up",
        "description": "Erhöht die Lautstärke des Fernsehers um eine angegebene Anzahl an Stufen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "steps": {
                    "type": "INTEGER",
                    "description": "Anzahl der Lautstärkeschritte (Standard: 1, z.B. 2 oder 5)"
                }
            }
        }
    },
    {
        "name": "volume_down",
        "description": "Verringert die Lautstärke des Fernsehers um eine angegebene Anzahl an Stufen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "steps": {
                    "type": "INTEGER",
                    "description": "Anzahl der Lautstärkeschritte (Standard: 1, z.B. 2 oder 5)"
                }
            }
        }
    },
    {
        "name": "toggle_mute",
        "description": "Schaltet den Ton des Fernsehers stumm oder hebt die Stummschaltung auf.",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },
    {
        "name": "screen_off",
        "description": "Schaltet nur das OLED-Display aus (Bildschirm aus), während der Ton oder Musik (z. B. Spotify) im Hintergrund weiterläuft.",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },
    {
        "name": "screen_on",
        "description": "Schaltet das OLED-Display wieder ein.",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },
    {
        "name": "screensaver",
        "description": "Aktiviert den Bildschirmschoner des Fernsehers.",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },
    {
        "name": "media_control",
        "description": "Steuert die Medienwiedergabe (Play, Pause, Stop, Vorlauf, Rücklauf).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "'play', 'pause', 'stop', 'forward' oder 'rewind'"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "get_tv_status",
        "description": "Fragt den aktuellen Status des Fernsehers ab (aktuelle App im Vordergrund, Power-Status).",
        "parameters": {
            "type": "OBJECT",
            "properties": {}
        }
    },
    {
        "name": "show_toast",
        "description": "Zeigt eine Text-Benachrichtigung (Toast) für einige Sekunden auf dem TV-Bildschirm an.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "message": {
                    "type": "STRING",
                    "description": "Der anzuzeigende Text"
                }
            },
            "required": ["message"]
        }
    },
    {
        "name": "control_smart_device",
        "description": ("Schaltet Smart-Home-Geräte im Heimnetz (WiZ-Lampen, Tapo-Steckdosen) an/aus und dimmt Lampen. "
                        "Ein Raumname allein schaltet alle Geräte im Raum. (Räume und Geräte werden aus devices.json ergänzt.)"),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "device_name": {
                    "type": "STRING",
                    "description": "Gerät und/oder Raum, z.B. 'Wohnzimmer', 'Deckenlampe', 'Bar im Wohnzimmer', 'Bett', 'Büro'"
                },
                "state": {
                    "type": "STRING",
                    "description": "'on' (einschalten) oder 'off' (ausschalten)"
                },
                "brightness": {
                    "type": "INTEGER",
                    "description": "Optional: Helligkeit 10-100 % (nur Lampen, schaltet sie dabei ein)"
                }
            },
            "required": ["device_name", "state"]
        }
    }
]


def _smart_home_description() -> str:
    """Tool description with the rooms and devices from devices.json (the model needs the names)."""
    base = ("Schaltet Smart-Home-Geräte im Heimnetz (WiZ-Lampen, Tapo-Steckdosen) an/aus und dimmt Lampen. "
            "Ein Raumname allein schaltet alle Geräte im Raum.")
    rooms: Dict[str, List[str]] = {}
    for d in localhome.load_devices():
        rooms.setdefault(d["room"] or "Ohne Raum", []).append(d["name"])
    if not rooms:
        return base
    return base + " Räume: " + ", ".join(f"{r} ({', '.join(n)})" for r, n in rooms.items()) + "."


def tools_declaration() -> List[Dict[str, Any]]:
    out = []
    for t in TOOLS_DECLARATION:
        if t["name"] == "control_smart_device":
            t = dict(t, description=_smart_home_description())
        out.append(t)
    return out


def _failed(res) -> bool:
    return isinstance(res, dict) and (res.get("success") is False or bool(res.get("error")))


def execute_tool(name: str, args: dict) -> dict:
    """Dispatches tool call to LGTV or the local smart-home controller."""
    if name == "control_smart_device":
        dev = _text(args.get("device_name"), 100)
        st = _text(args.get("state") or "on", 10).lower()
        bri = args.get("brightness")
        bri = _to_int(bri, 100, 10, 100) if isinstance(bri, (int, float)) and not isinstance(bri, bool) else None
        if bri is not None:
            st = "on"
        if not dev:
            return {"error": "Kein Gerätename angegeben"}
        matched = localhome.find_devices(dev)
        if not matched:
            return {"error": f"Gerät '{dev}' nicht gefunden"}
        with ThreadPoolExecutor(max_workers=min(8, len(matched))) as pool:
            results = list(pool.map(lambda d: localhome.set_switch(d["mac"], st, bri), matched))
        names = [localhome.display_name(d) for d in matched]
        failed = [n for n, r in zip(names, results) if r.get("status") != "ok"]
        on = st in ("on", "an", "ein", "true", "1")
        word = (f"{bri} %" if bri is not None else "AN") if on else "AUS"
        LGTV.toast(f"Smart Home: {', '.join(names)} -> {word}" + (f" (Fehler: {', '.join(failed)})" if failed else ""))
        out = {"devices": names, "state": "on" if on else "off", "success": not failed}
        if failed:
            out["failed"] = {n: r.get("error") for n, r in zip(names, results) if r.get("status") != "ok"}
        return out
    if name == "launch_app":
        app = _text(args.get("app_name"), 100)
        if not app:
            return {"error": "Kein App-Name angegeben"}
        res = LGTV.launch_app(app)
        LGTV.toast(f"Gemini: Starte {app.capitalize()}")
        return res
    elif name == "switch_input":
        inp = _text(args.get("input_name"), 40) or "pc"
        res = LGTV.switch_input(inp)
        LGTV.toast(f"Gemini: Eingang {inp.upper()}")
        return res
    elif name == "volume_up":
        steps = _to_int(args.get("steps", 1))
        return LGTV.volume_up(steps)
    elif name == "volume_down":
        steps = _to_int(args.get("steps", 1))
        return LGTV.volume_down(steps)
    elif name == "toggle_mute":
        return LGTV.mute()
    elif name == "screen_off":
        LGTV.toast("Gemini: Bildschirm aus")
        return LGTV.screen_off()
    elif name == "screen_on":
        return LGTV.screen_on()
    elif name == "screensaver":
        return LGTV.screensaver()
    elif name == "media_control":
        act = _text(args.get("action"), 20) or "play"
        return LGTV.media_control(act)
    elif name == "get_tv_status":
        return LGTV.get_status()
    elif name == "show_toast":
        msg = _text(args.get("message"), 200)
        if not msg:
            return {"error": "Kein Text angegeben"}
        return LGTV.toast(msg)
    return {"error": f"Unbekannte Funktion: {_text(name, 60)}"}


def _summary(tool: str, args: dict) -> str:
    """German half-sentence for an executed tool call ('Netflix gestartet')."""
    if tool == "launch_app":
        return f"{_text(args.get('app_name'), 60).capitalize()} gestartet"
    if tool == "switch_input":
        return f"auf {(_text(args.get('input_name'), 40) or 'pc').upper()} umgeschaltet"
    if tool in ("volume_down", "volume_up"):
        st = _to_int(args.get("steps", 1))
        verb = "gesenkt" if tool == "volume_down" else "erhöht"
        return f"die Lautstärke um {st} Stufe{'n' if st > 1 else ''} {verb}"
    if tool == "toggle_mute":
        return "die Stummschaltung umgeschaltet"
    if tool == "screen_off":
        return "den Bildschirm ausgeschaltet (Ton läuft weiter)"
    if tool == "screen_on":
        return "den Bildschirm wieder eingeschaltet"
    if tool == "screensaver":
        return "den Bildschirmschoner gestartet"
    if tool == "show_toast":
        return f"'{_text(args.get('message'), 80)}' auf dem TV angezeigt"
    if tool == "control_smart_device":
        bri = args.get("brightness")
        if isinstance(bri, (int, float)) and not isinstance(bri, bool):
            state_de = f"auf {_to_int(bri, 100, 10, 100)} % gestellt"
        else:
            state_de = "eingeschaltet" if _text(args.get("state"), 10).lower() == "on" else "ausgeschaltet"
        return f"{_text(args.get('device_name'), 60) or 'Gerät'} {state_de}"
    if tool == "media_control":
        return f"Wiedergabe: {_text(args.get('action'), 20)}"
    if tool == "get_tv_status":
        return "den Status des Fernsehers abgefragt"
    return _text(tool, 60)


class GeminiTVAgent:
    def __init__(self, api_key: str = None):
        # An explicit key (set by /api/key) wins; otherwise the current .env value is used on
        # every request, so editing .env takes effect without a restart.
        self.api_key = api_key or None

    def _key(self) -> str:
        return self.api_key or get_api_key()

    def process_command(self, user_prompt: str, history: List[Dict[str, str]] = None) -> Dict[str, Any]:
        """Sends user command to Gemini with function calling, executes tools and returns response.

        history: earlier turns of this conversation, [{"role": "user"|"model", "text": "..."}],
        so follow-up questions have context. Only text is kept (no tool calls).
        Never raises; every failure comes back as a German "text".
        """
        api_key = self._key()
        if not api_key:
            return {
                "text": "Kein Gemini API-Key hinterlegt. Bitte trage deinen kostenlosen API-Key ein (erhältlich unter https://aistudio.google.com/).",
                "actions": [],
                "needs_key": True
            }

        model = get_model()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model, safe='.-_')}:generateContent"
        
        system_instruction = (
            "Du bist Gemini, der persönliche KI-Assistent auf einem High-End LG OLED G4 TV. "
            "Du kannst sowohl allgemeine Fragen kompetent und prägnant beantworten (z. B. Filmempfehlungen, Allgemeinwissen, Wetter, Rezepte, Erklärungen) "
            "als auch den Fernseher und Smart-Home-Geräte direkt über die bereitgestellten Funktionen steuern. "
            "Wenn der Nutzer den Fernseher steuern will (z. B. 'schalte auf den PC', 'starte YouTube', 'mach leiser', 'schalte TV-Licht ein', 'mach das Bild aus'), rufe die entsprechenden Funktionen auf. "
            "Formatiere deine Antworten übersichtlich und gut lesbar für einen Fernsehbildschirm (Absätze, Aufzählungspunkte, prägnant auf Deutsch). "
            "Der Nutzer kann nicht tippen, nur Vorschläge anklicken. Beende deshalb jede Text-Antwort mit genau einer letzten Zeile "
            f"im Format '{SUGGEST_TAG} Nachfrage 1 | Nachfrage 2 | Nachfrage 3': 2 bis 3 kurze, konkrete Nachfragen "
            "aus Sicht des Nutzers (je höchstens 40 Zeichen), die direkt an deine Antwort anknüpfen."
        )

        contents = []
        for turn in (history or [])[-MAX_HISTORY:]:
            if not isinstance(turn, dict):
                continue
            role = "model" if turn.get("role") == "model" else "user"
            text = str(turn.get("text") or "")[:4000]
            if text:
                contents.append({"role": role, "parts": [{"text": text}]})
        contents.append({"role": "user", "parts": [{"text": user_prompt}]})

        payload = {
            "contents": contents,
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "tools": [
                {
                    "functionDeclarations": tools_declaration()
                }
            ]
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            # Key goes in a header, not the URL, so it never shows up in error strings or logs.
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key}
        )

        try:
            with urllib.request.urlopen(req, timeout=get_timeout()) as resp:
                result = json.loads(resp.read(MAX_REPLY_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read(4096).decode("utf-8", errors="ignore")[:2000]
            except Exception:
                err_body = ""
            if e.code == 404:
                hint = f"Modell '{model}' nicht gefunden. Setze GEMINI_MODEL in der .env auf ein gültiges Modell."
            elif e.code in (400, 401, 403):
                hint = "Bitte prüfe deinen API-Key."
            elif e.code == 429:
                hint = "Kontingent erschöpft, bitte später erneut versuchen."
            else:
                hint = "Gemini-Dienst nicht verfügbar."
            return {
                "text": f"Fehler bei der Gemini-Verbindung: HTTP {e.code}. {hint}",
                "details": _scrub(err_body, api_key),
                "actions": []
            }
        except (socket.timeout, TimeoutError):
            return {"text": "Gemini hat nicht rechtzeitig geantwortet. Bitte versuche es gleich noch einmal.",
                    "actions": []}
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                return {"text": "Gemini hat nicht rechtzeitig geantwortet. Bitte versuche es gleich noch einmal.",
                        "actions": []}
            return {"text": "Keine Verbindung zu Gemini. Ist der Fernseher mit dem Internet verbunden?",
                    "actions": []}
        except (ValueError, UnicodeDecodeError):
            return {"text": "Gemini hat eine unlesbare Antwort geschickt. Bitte versuche es noch einmal.",
                    "actions": []}
        except Exception as e:
            return {
                "text": f"Verbindungsfehler: {type(e).__name__}",
                "actions": []
            }

        # Parse response
        candidates = result.get("candidates") if isinstance(result, dict) else None
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            reason = ""
            if isinstance(result, dict) and isinstance(result.get("promptFeedback"), dict):
                reason = str(result["promptFeedback"].get("blockReason") or "")
            if reason:
                return {"text": "Gemini hat diese Anfrage abgelehnt. Formuliere sie bitte anders.", "actions": []}
            return {"text": "Keine Antwort von Gemini erhalten.", "actions": []}

        content = candidates[0].get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            parts = []

        executed_actions = []
        model_text = ""

        # Check for function calls
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("functionCall"), dict):
                if len(executed_actions) >= MAX_TOOL_CALLS:
                    continue
                fc = part["functionCall"]
                fn_name = _text(fc.get("name"), 60)
                fn_args = fc.get("args") or {}
                if not isinstance(fn_args, dict):
                    fn_args = {}
                try:
                    res = execute_tool(fn_name, fn_args)
                except Exception as e:
                    res = {"error": f"{fn_name} fehlgeschlagen: {type(e).__name__}"}
                executed_actions.append({"tool": fn_name, "args": fn_args, "result": res})
            elif isinstance(part.get("text"), str) and not part.get("thought"):
                model_text += part["text"]

        model_text, suggestions = split_suggestions(model_text)

        if executed_actions and not model_text:
            done = [_summary(a["tool"], a["args"]) for a in executed_actions if not _failed(a["result"])]
            failed = [_summary(a["tool"], a["args"]) for a in executed_actions if _failed(a["result"])]
            if done:
                model_text = f"Alles klar! Ich habe {', und '.join(done)}."
            if failed:
                errs = [str(a["result"].get("error") or "") for a in executed_actions
                        if _failed(a["result"]) and a["result"].get("error")]
                model_text = (model_text + " " if model_text else "") + \
                    f"Nicht geklappt: {', '.join(failed)}" + (f" ({'; '.join(errs[:3])})" if errs else "") + "."
        elif not executed_actions and not model_text:
            reason = str(candidates[0].get("finishReason") or "")
            if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"):
                model_text = "Gemini hat diese Anfrage abgelehnt. Formuliere sie bitte anders."
            else:
                model_text = "Gemini hat keine Antwort geliefert. Bitte versuche es noch einmal."

        return {
            "text": _scrub(model_text.strip(), api_key),
            "actions": executed_actions,
            "suggestions": suggestions,
            "prompt": user_prompt
        }
