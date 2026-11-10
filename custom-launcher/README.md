# LG G4 – eigener Startbildschirm + Härtung

TV: LG OLED55G48LW (webOS 10.3.1, gerootet, Homebrew Channel), `root@192.168.178.60`

## Dateikarte (alles in diesem Ordner)

| Pfad | Was |
|---|---|
| `com.custom.launcher/` | **Die App** (Quelltext). `index.html`, `style.css`, `app.js` = Oberfläche; `launcherd.py` = Root-Hilfsdienst; `appinfo.json`, `icon.png`; `data/` nur Vorschau-Daten für den PC |
| `com.custom.launcher_2.1.0_all.ipk` | Fertiges Installationspaket (aktuell, 2.1.0). Ältere `..._2.0.0_`/`..._1.0.0_` sind alte Stände |
| `audit/review.md`, `audit/qa.md` | Code-Review und TV-Test der Agents (Fehlerlisten, alle wichtigen behoben) |
| `boot/99-custom-launcher` | Boot-Hook, wird auf dem TV nach `/var/lib/webosbrew/init.d/` kopiert |
| `build-ipk.sh` | Baut das .ipk aus `com.custom.launcher/` |
| `tools/deploy.sh` | **Ein Befehl für alles**: bauen, installieren, Boot-Hook setzen, Dienst neu starten |
| `tools/tv.sh`, `luna.sh`, `key.sh`, `tvshot.sh` | Hilfen: Befehl auf dem TV (mit TTY), Luna-Aufruf, Fernbedienungstaste senden, TV-Bildschirmfoto |
| `tools/cdp.sh` | Web-Inspector des TVs: JS in der Launcher-Seite ausführen, Tasten, Screenshots |
| `tools/localshot.sh`, `mkdata.js` | Launcher am PC in Headless-Edge rendern/bedienen (Vorschau mit echten TV-Daten) |
| `design/fixtures/` | Vom TV kopierte Original-Icons, App-Metadaten, LG-Schriften (33 MB, nur für die PC-Vorschau) |
| `audit/` | Bestandsaufnahme des TVs: `privacy.md`, `bloat.md`, `boot.md`, `research.md`, Rohdaten in `raw/`, `blocklist/` |
| `README.md` | diese Datei |

Auf dem TV liegt:
- App: `/media/developer/apps/usr/palm/applications/com.custom.launcher/` (`data/` dort wird vom Dienst erzeugt)
- Boot-Hook: `/var/lib/webosbrew/init.d/99-custom-launcher` (Sicherung des alten: `/var/lib/webosbrew/backup-custom-launcher/99-autostart.v1`)
- Dienst-Log: `/tmp/custom-launcher.log` (nur RAM, wird beim Neustart gelöscht)

## Was gemacht wurde

1. **Launcher v1** (Schwarz, Uhr, Kacheln) gebaut, per SSH installiert.
2. **Home-Taste ersetzt**: LGs Startbildschirm (`com.webos.app.home`) ist Boot-Abhängigkeit und bleibt installiert; ein Root-Dienst (`launcherd.py`) hört auf App-Ereignisse und öffnet bei jedem Erscheinen sofort den eigenen Launcher (gemessen ca. 0,3–0,4 s, kein Polling mehr).
3. **Launcher v2** komplett neu im Apple-TV-Stil: Favoritenreihe (PC/HDMI 1, HDMI 2, YouTube) + App-Raster, Karten in der Farbe des App-Icons, Fokus mit Skalierung/Glanz/Glow, Schwebelicht in Fokusfarbe, Zeigerkippen für die Magic Remote, OK lang drücken = Menü (Öffnen / Favorit / Verschieben / Ausblenden), „Mehr“-Menü, OLED-Schutz (Pixelverschiebung, Abdunkeln nach 2 min, echtes Schwarz).
3b. **Reihenfolge nach Nutzung** (v2.1): Der Root-Dienst zählt, welche App wie oft nach vorn kommt (auch über Fernbedienungstasten wie „Netflix“) und schreibt `data/usage.json`. Die oberste Reihe zeigt die 5 meistgenutzten, darunter folgt der Rest absteigend. Alte Nutzung verblasst (Halbwertszeit 45 Tage). Startreihenfolge: Netflix, Spotify, PC/HDMI 1, YouTube, Prime Video. Per langem OK lässt sich eine App oben **anheften** (bleibt vorn, verschiebbar). Zähler-Datei (überlebt Neuinstallation): `/var/lib/webosbrew/custom-launcher-usage.json` – löschen = Zähler zurücksetzen (danach Dienst neu starten).
4. **Bloat ausgeblendet** (nichts deinstalliert): LG Channels, Gallery+, Sport, Startseiten-Hub, Kamera, Multi View, Alexa, Travel+, Gaming Portal, Public Value, ungenutzte HDMI-Eingänge, Platzhalter-Apps. Alles unter „Mehr → Ausgeblendete Apps“ wiederherstellbar.
5. **Telnet geschlossen**: Die offene Root-Shell ohne Passwort (Port 23) ist abgeschaltet (`/var/luna/preferences/webosbrew_telnet_disabled`). SSH (nur Schlüssel) bleibt.
6. **Audit** (Datenschutz, Bloat, Boot): Tracker-Domains (407) und die Werbe-/ACR-/Sprach-Dienste sind durch „Life's Good Without Spying“ bereits geblockt; Ergebnis siehe `audit/privacy.md`.

## Bedienung / Notausstieg

LG-Startbildschirm wieder wie original (sofort, ohne Neustart):
```bash
ssh root@192.168.178.60 "touch /var/lib/webosbrew/custom-launcher.off"
```
Wieder einschalten: `rm /var/lib/webosbrew/custom-launcher.off`

Neu installieren/aktualisieren: `tools/deploy.sh`
Dienst-Status: `ssh root@192.168.178.60 "python3 /media/developer/apps/usr/palm/applications/com.custom.launcher/launcherd.py status"`
Telnet wieder erlauben: `rm /var/luna/preferences/webosbrew_telnet_disabled` + Neustart

## Bewusst offen gelassen (deine Entscheidung)

- Live Plus / Werbe-Einwilligungen in den TV-Einstellungen stehen noch auf „an“ (Server sind geblockt, aber der Schalter ist an) → in den TV-Einstellungen ausschalten.
- Firmware-Updates sind absichtlich geblockt (`domains.updates=on`) → keine Sicherheitsupdates.
- Apps deinstallieren (Platzhalter-Stubs, Travel+, Gaming Portal): nur auf Wunsch.
