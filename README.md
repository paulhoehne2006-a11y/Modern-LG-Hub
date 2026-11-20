# Modern LG Hub 📺✨

> **Modern, minimalist, high-performance custom launcher and smart control hub for rooted LG webOS Smart TVs.**

Designed for pure speed, zero bloat, elegant dark aesthetics, and seamless hardware integration.

---

## 🌟 Key Features

- **⚡ Blazing Fast Performance (Rock-Solid 60 FPS)**
  - Replaces sluggish stock webOS menus with an ultra-lightweight, zero-bloat interface.
  - Native CSS hardware acceleration with composite-only transforms and opacity (no laggy filters, zero frame drops).
  - Instant micro-interactions and smooth focus transitions.

- **🏠 Persistent Home Button Interception**
  - Pressing the physical **Home** button on your LG Magic Remote reliably brings you to **Modern LG Hub**—never the slow, ad-filled stock webOS home.
  - Event-driven background helper daemon (`launcherd.py`) catches OS lifecycle events in real time without CPU polling.

- **🌙 OLED Screen-Off Mode ("Display aus")**
  - Shuts off the OLED panel instantly to save power and protect against burn-in.
  - Keeps apps and digital audio (e.g., optical Toslink to Denon AVR / Soundbars) running continuously in the background.
  - Wake up anytime with a simple press of any button on the remote.

- **💡 Smart Home Dashboard & Management**
  - Integrated smart device tiles with live status indicators (lights, smart plugs).
  - Toggle devices or whole rooms, set brightness and colour (sliders) and pick the main room directly from your remote.
  - Direct integration with local controller APIs; smart home (WiZ bulbs, Tapo plugs) controlled locally over the LAN, no cloud.

- **🤖 Local Gemini AI TV Assistant**
  - Built-in Gemini assistant (topic chips on the TV, free text/voice from the phone app) for TV commands, smart home and everyday questions.
  - Lightweight Python backend server running locally on the TV.

- **🛡️ Hardened & Privacy-First**
  - Adware, analytics, and telemetry domains blocked (via the "Life's Good Without Spying" Homebrew app).
  - Insecure telnet daemon disabled by default; secure SSH key authentication only.

---

## 🏗️ Architecture

```
modern-lg-hub/
├── custom-launcher/
│   ├── com.custom.launcher/     # Main webOS HTML5/CSS3 application
│   │   ├── app.js               # Performance-optimized UI logic & navigation
│   │   ├── style.css            # Obsidian-dark, micro-typography styling
│   │   ├── index.html           # Minimal DOM layout
│   │   └── launcherd.py         # Root event daemon for Home interception & app sync
│   ├── boot/                    # Boot-time init.d autostart scripts
│   └── tools/                   # Deployment, CDP debugging & automation tools
└── gemini-tv-controller/        # Local Python REST backend & AI assistant
    ├── server.py                # REST API (port 8080, LAN) for launcher + phone app, see docs/API.md
    ├── agent.py                 # Gemini assistant (tool calls)
    ├── tv.py                    # Local Luna bus TV controller (power, apps, screen off)
    ├── localhome.py             # Local smart home control (WiZ UDP, Tapo KLAP), replaces SmartThings
    └── devices.example.json     # Template: copy to devices.json on the TV (MAC, room, name); not in git
```

---

## 🚀 Quick Start & Deployment

### Prerequisites
- Rooted LG webOS TV via Homebrew Channel. Built and tested only on an LG OLED G4 with webOS 10.3.1; the Home-key interception patches firmware-specific files and may not work elsewhere.
- SSH access enabled (`root@<TV_IP>`).

### Deploy to TV
Deploy the launcher and restart the daemon in a single command:

```bash
cd custom-launcher
TV_HOST="root@<TV_IP>" ./tools/deploy.sh
```

This will:
1. Validate syntax locally.
2. Build the `.ipk` package.
3. Install via `appInstallService`.
4. Install the `/var/lib/webosbrew/init.d/99-custom-launcher` boot hook.
5. Launch Modern LG Hub and restart the background daemon.

---

## 🎮 Navigation & Controls (LG Magic Remote)

| Button | Action |
|---|---|
| **D-Pad / Arrows** | Navigate cards and menus |
| **OK / Click** | Launch application or toggle smart device |
| **OK (Long Press)** | Open context menu (Pin, Move, Hide, Manage Smart Home) |
| **Home Button** | Return to Modern LG Hub top screen from anywhere |
| **Back / Exit** | Dismiss menus/modals or jump to top |
| **Magic Pointer** | Point and click on cards, menus and sliders |

---

## ⚙️ Emergency Stock Fallback

If you ever want to temporarily return to the stock LG webOS home screen:
```bash
ssh root@<TV_IP> "touch /var/lib/webosbrew/custom-launcher.off"
```
To re-enable Modern LG Hub:
```bash
ssh root@<TV_IP> "rm /var/lib/webosbrew/custom-launcher.off"
```

---

## 🧪 Tests

```bash
python3 -m unittest discover -s tests -p 'test_hub_*.py'
```

Backend setup (`.env`, `devices.json`) and the full HTTP API: [docs/API.md](docs/API.md).

---

## 📄 License
AGPL v3. See [COPYING](COPYING).
