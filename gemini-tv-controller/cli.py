"""
CLI interface for Gemini TV Controller
Usage: py cli.py "Mach YouTube an und Lautstärke leiser"
"""

import sys
from agent import GeminiTVAgent

def main():
    if len(sys.argv) < 2:
        print("Usage: py cli.py \"<dein befehl>\"")
        print("Beispiel: py cli.py \"Schalte auf PC um und mach leiser\"")
        sys.exit(1)

    prompt = " ".join(sys.argv[1:])
    print(f"[*] Sende Befehl an Gemini: '{prompt}'...")
    agent = GeminiTVAgent()
    res = agent.process_command(prompt)
    print("\n[+] Gemini Antwort:")
    print(res.get("text", ""))
    if res.get("actions"):
        print("\n[+] Ausgeführte TV-Aktionen:")
        for act in res["actions"]:
            print(f"  - {act['tool']}: {act.get('args', {})}")

if __name__ == "__main__":
    main()
