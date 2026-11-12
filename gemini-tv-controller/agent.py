"""
Gemini AI Agent with Function Calling for LG webOS TV Control.
Uses the Google Gemini API via pure standard library urllib.
The model is configurable via GEMINI_MODEL (environment or .env).
"""

import os
import json
import urllib.request
import urllib.error
from typing import Dict, Any, List
from tv import LGTV

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

DEFAULT_MODEL = "gemini-3.8-flash"

def _env_value(name: str) -> str:
    val = os.environ.get(name, "")
    if not val and os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(name + "="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    return val

def get_api_key() -> str:
    return _env_value("GEMINI_API_KEY")

def get_model() -> str:
    return _env_value("GEMINI_MODEL") or DEFAULT_MODEL

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
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))

def save_api_key(key: str) -> None:
    key = "".join(key.split())  # no whitespace/newlines -> no .env line injection
    lines = []
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = [l for l in f if not l.startswith("GEMINI_API_KEY=")]
        except Exception:
            lines = []
    lines.append(f"GEMINI_API_KEY={key.strip()}\n")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)

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
        "description": "Steuert SmartHome-Geräte (SmartThings: Lampen, Lichter, Steckdosen, Schalter).",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "device_name": {
                    "type": "STRING",
                    "description": "Name des Geräts (z.B. 'TV-Licht' / 'TV-Leiste', 'Wohnzimmer', 'Deckenlicht' / 'Decke Arbeitszimmer', 'Schreibtisch', 'Bett', 'Büro', 'Bar')"
                },
                "state": {
                    "type": "STRING",
                    "description": "'on' (einschalten) oder 'off' (ausschalten)"
                }
            },
            "required": ["device_name", "state"]
        }
    }
]

from smartthings import SmartThings

def execute_tool(name: str, args: dict) -> dict:
    """Dispatches tool call to LGTV or SmartThings controller."""
    if name == "control_smart_device":
        dev = str(args.get("device_name") or "").strip().lower()
        st = str(args.get("state") or "on").strip().lower()
        if not dev:
            return {"error": "Kein Gerätename angegeben"}
        # Find matching device ID
        devices = SmartThings.get_dashboard_devices()
        matched = None
        for d in devices:
            if dev in d["name"].lower() or dev in d["raw_name"].lower():
                matched = d
                break
        if not matched and "tv" in dev:
            matched = next((d for d in devices if "tv" in d["name"].lower()), None)
        if matched:
            res = SmartThings.set_switch(matched["id"], st)
            LGTV.toast(f"SmartHome: {matched['name']} -> {st.upper()}")
            return {"device": matched["name"], "state": st, "success": True, "res": res}
        return {"error": f"Gerät '{dev}' nicht in SmartThings gefunden"}
    if name == "launch_app":
        app = args.get("app_name", "")
        res = LGTV.launch_app(app)
        LGTV.toast(f"Gemini: Starte {app.capitalize()}")
        return res
    elif name == "switch_input":
        inp = args.get("input_name", "pc")
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
        act = args.get("action", "play")
        return LGTV.media_control(act)
    elif name == "get_tv_status":
        return LGTV.get_status()
    elif name == "show_toast":
        msg = args.get("message", "")
        return LGTV.toast(msg)
    return {"error": f"Unbekannte Funktion: {name}"}

class GeminiTVAgent:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or get_api_key()

    def process_command(self, user_prompt: str, history: List[Dict[str, str]] = None) -> Dict[str, Any]:
        """Sends user command to Gemini with function calling, executes tools and returns response.

        history: earlier turns of this conversation, [{"role": "user"|"model", "text": "..."}],
        so follow-up questions have context. Only text is kept (no tool calls).
        """
        if not self.api_key:
            return {
                "text": "Kein Gemini API-Key hinterlegt. Bitte trage deinen kostenlosen API-Key ein (erhältlich unter https://aistudio.google.com/).",
                "actions": [],
                "needs_key": True
            }

        model = get_model()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        
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
                    "functionDeclarations": TOOLS_DECLARATION
                }
            ]
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            # Key goes in a header, not the URL, so it never shows up in error strings or logs.
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        )

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")[:2000]
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
                "details": err_body,
                "actions": []
            }
        except Exception as e:
            return {
                "text": f"Verbindungsfehler: {type(e).__name__}",
                "actions": []
            }

        # Parse response
        candidates = result.get("candidates", [])
        if not candidates:
            return {"text": "Keine Antwort von Gemini erhalten.", "actions": []}

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])

        executed_actions = []
        model_text = ""

        # Check for function calls
        for part in parts:
            if "functionCall" in part:
                fc = part["functionCall"]
                fn_name = fc.get("name")
                fn_args = fc.get("args") or {}
                if not isinstance(fn_args, dict):
                    fn_args = {}
                try:
                    res = execute_tool(fn_name, fn_args)
                except Exception as e:
                    res = {"error": f"{fn_name} fehlgeschlagen: {type(e).__name__}"}
                executed_actions.append({"tool": fn_name, "args": fn_args, "result": res})
            elif "text" in part:
                model_text += part["text"]

        model_text, suggestions = split_suggestions(model_text)

        if executed_actions and not model_text:
            summaries = []
            for a in executed_actions:
                t = a["tool"]
                args = a.get("args", {})
                if t == "launch_app":
                    app_label = args.get("app_name", "").capitalize()
                    summaries.append(f"{app_label} gestartet")
                elif t == "switch_input":
                    inp_label = args.get("input_name", "").upper()
                    summaries.append(f"auf {inp_label} umgeschaltet")
                elif t == "volume_down":
                    st = _to_int(args.get("steps", 1))
                    summaries.append(f"die Lautstärke um {st} Stufe{'n' if st > 1 else ''} gesenkt")
                elif t == "volume_up":
                    st = _to_int(args.get("steps", 1))
                    summaries.append(f"die Lautstärke um {st} Stufe{'n' if st > 1 else ''} erhöht")
                elif t == "toggle_mute":
                    summaries.append("die Stummschaltung umgeschaltet")
                elif t == "screen_off":
                    summaries.append("den Bildschirm ausgeschaltet (Ton läuft weiter)")
                elif t == "screen_on":
                    summaries.append("den Bildschirm wieder eingeschaltet")
                elif t == "screensaver":
                    summaries.append("den Bildschirmschoner gestartet")
                elif t == "show_toast":
                    summaries.append(f"'{args.get('message', '')}' auf dem TV angezeigt")
                elif t == "control_smart_device":
                    state_de = "eingeschaltet" if args.get('state') == 'on' else "ausgeschaltet"
                    summaries.append(f"{args.get('device_name', 'Gerät')} {state_de}")
                elif t == "media_control":
                    summaries.append(f"Wiedergabe: {args.get('action', '')}")
                else:
                    summaries.append(t)
            model_text = f"Alles klar! Ich habe {', und '.join(summaries)}."

        return {
            "text": model_text.strip(),
            "actions": executed_actions,
            "suggestions": suggestions,
            "prompt": user_prompt
        }
