"""
Gemini AI Agent with Function Calling for LG webOS TV Control.
Uses Google Gemini API (gemini-2.0-flash or gemini-1.5-flash) via pure standard library urllib.
"""

import os
import json
import urllib.request
import urllib.error
from typing import Dict, Any, List
from tv import LGTV

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

def get_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key and os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    return key

def save_api_key(key: str) -> None:
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
        dev = args.get("device_name", "").lower()
        st = args.get("state", "on").lower()
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
        steps = int(args.get("steps", 1))
        return LGTV.volume_up(steps)
    elif name == "volume_down":
        steps = int(args.get("steps", 1))
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

    def process_command(self, user_prompt: str) -> Dict[str, Any]:
        """Sends user command to Gemini with function calling, executes tools and returns response."""
        if not self.api_key:
            return {
                "text": "Kein Gemini API-Key hinterlegt. Bitte trage deinen kostenlosen API-Key ein (erhältlich unter https://aistudio.google.com/).",
                "actions": [],
                "needs_key": True
            }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={self.api_key}"
        
        system_instruction = (
            "Du bist Gemini, der persönliche KI-Assistent auf einem High-End LG OLED G4 TV. "
            "Du kannst sowohl allgemeine Fragen kompetent und prägnant beantworten (z. B. Filmempfehlungen, Allgemeinwissen, Wetter, Rezepte, Erklärungen) "
            "als auch den Fernseher und Smart-Home-Geräte direkt über die bereitgestellten Funktionen steuern. "
            "Wenn der Nutzer den Fernseher steuern will (z. B. 'schalte auf den PC', 'starte YouTube', 'mach leiser', 'schalte TV-Licht ein', 'mach das Bild aus'), rufe die entsprechenden Funktionen auf. "
            "Formatiere deine Antworten übersichtlich und gut lesbar für einen Fernsehbildschirm (Absätze, Aufzählungspunkte, prägnant auf Deutsch)."
        )

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}]
                }
            ],
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
            headers={"Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            return {
                "text": f"Fehler bei der Gemini-Verbindung: HTTP {e.code}. Bitte prüfe deinen API-Key.",
                "details": err_body,
                "actions": []
            }
        except Exception as e:
            return {
                "text": f"Verbindungsfehler: {e}",
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
                fn_args = fc.get("args", {})
                res = execute_tool(fn_name, fn_args)
                executed_actions.append({"tool": fn_name, "args": fn_args, "result": res})
            elif "text" in part:
                model_text += part["text"]

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
                    st = args.get("steps", 1)
                    summaries.append(f"die Lautstärke um {st} Stufe{'n' if st > 1 else ''} gesenkt")
                elif t == "volume_up":
                    st = args.get("steps", 1)
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
            "prompt": user_prompt
        }
