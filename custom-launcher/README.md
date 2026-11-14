# LG G4 – eigener Startbildschirm + Härtung

TV: LG OLED55G48LW (webOS 10.3.1, gerootet, Homebrew Channel), `root@192.168.178.60`

## Dateikarte (alles in diesem Ordner)

| Pfad | Was |
|---|---|
| `com.custom.launcher/` | **Die App** (Quelltext). `index.html`, `style.css`, `app.js` = Oberfläche; `launcherd.py` = Root-Hilfsdienst; `appinfo.json`, `icon.png`; `data/` nur Vorschau-Daten für den PC |
| `com.custom.launcher_<version>_all.ipk` | Installationspaket, wird von `build-ipk.sh` erzeugt (nicht im Git). Aktuelle Version steht in `com.custom.launcher/appinfo.json` (3.3.2) |
| `../gemini-tv-controller/` | Backend auf dem TV (`server.py`, Port 8080, nur `127.0.0.1`): Gemini-Assistent, Smart-Home-Kacheln (WiZ/Tapo lokal im LAN), Wetter, OLED-aus |
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
7. **Launcher v3** (3.2.x, „Modern Minimalist“): neues Design, Smart-Home-Kacheln (SmartThings über das Backend), Wetter, Gemini-Assistent, Denon-Audio-Modus (schaltet das OLED-Panel aus, Ton läuft weiter).
   - **Home-Taste**: webOS verarbeitet sie selbst (`homeAutoLaunch` bleibt an, sonst reagiert die Fernbedienung nicht). `launcherd.py` leitet LG-Home sofort auf den Launcher um; der Launcher bekommt dann `webOSRelaunch` und springt nach oben. Zurück aus einer App behält die Position.
   - **Home-Taste direkt** (ab 3.3.2): Der Compositor (`surface-manager`) lädt seine Tastenfilter aus configd `com.webos.surfacemanager.keyFilters` und lädt sie bei Änderung live neu. `launcherd.py` erzeugt aus LGs eigener `KeyFilters/systemUi.js` eine Kopie (`/var/lib/webosbrew/custom-launcher-keyfilter/systemUi.js`), in der nur `launchDefaultApp(home)` durch den Start des Launchers ersetzt ist, und lässt die Filter-Einträge darauf zeigen. Damit startet Home LG-Home gar nicht erst (kein Aufblitzen, Hub nach ~0,15 s). Nur im RAM: ein Neustart, die `.off`-Datei oder `launcherd.py stop` stellen LGs Original wieder her. Erkennt `launcherd.py` die Zeile nach einem Firmware-Update nicht, bleibt alles beim Original und die Umleitung greift wie bisher.
   - **Achtung**: Niemals `defaultApps.home` (Kategorie `general`) auf `com.custom.launcher` setzen. Dann startet webOS bei Home gar nichts mehr (Home-Taste tot, auch nach Neustart). Richtiger Wert: `{"MembershipApp":"com.webos.app.overlaymembership"}` ohne `home`.
   - **Absicherung**: Der Boot-Hook startet `launcherd.py` neu, falls der Dienst abstürzt (Prüfung jede Minute), und schreibt ins Log, wenn `python3` fehlt. Pausieren deshalb über die `.off`-Datei, nicht über `launcherd.py stop`.
   - **Schnelle Rückkehr**: `launcherd.py` trägt den Launcher in zwei Keep-Alive-Listen ein (configd, nur im RAM, wird bei jedem Start erneut gesetzt): `com.webos.applicationManager.keepAliveApps` (SAM) und `com.webos.memorymanager.KeepAlive` (LGs Speicher-Manager `memchute`, der sonst unter Last den Launcher-Renderer beendet, z. B. bei offenem Spotify; Rückkehr dann ~3 s mit Ladekreis). memchute liest seine Liste nur beim Start, deshalb startet `launcherd.py` ihn einmal neu, wenn der Eintrag fehlt.
   - **Fenster nach vorn**: Mit `handlesRelaunch` muss die App ihr Fenster bei jedem `webOSRelaunch` selbst holen (`PalmSystem.activate()`), nie entprellt. Sonst bleibt nach Home kurz LG-Home und danach gar keine App im Vordergrund.
8. **Smart Home lokal** (3.4.0): Die SmartThings-Cloud ist ersetzt durch direkte LAN-Steuerung im Backend (`localhome.py`). WiZ-Lampen laufen per UDP 38899, Tapo-Steckdosen per KLAP; die Zugangsdaten stehen als `TAPO_EMAIL`/`TAPO_PASSWORD` in der `.env` des Backends und werden bei jedem Handshake neu gelesen. Die Geräte stehen in `devices.json` und werden per MAC erkannt; ändert die Fritzbox eine IP, findet das Backend sie per Broadcast bzw. ARP-Tabelle wieder (`ip-cache.json`). Die Kacheln zeigen Zustand und Raum. Gemini schaltet einzelne Geräte, ganze Räume („Wohnzimmer aus“) und die Helligkeit. Test auf dem TV: `python3 localhome.py status`.
9. **Startseite im Netflix-Stil** (3.6.0): Oben stehen fest Uhr und Pills, darunter ein Billboard. Es beschreibt groß, was gerade fokussiert ist: App (mit Logo, Beschreibung, „zuletzt vor …“), Lampe (Raum, Zustand, Helligkeit), Raum, Wetter, Gemini oder Audio, mit Hintergrundlicht in der passenden Farbe. Darunter liegen Reihen: „Zuletzt & Favoriten“, Smart Home (Geräte des Hauptraums einzeln, eine Kachel pro weiterem Raum, „Display aus“) und die App-Kategorien Eingänge, Streaming, TV & Mediatheken, Musik & Gaming und Werkzeuge (`CATEGORIES` in `app.js`). Die fokussierte Reihe gleitet unter das Billboard, lange Reihen gleiten seitlich. Die fokussierte Kachel wächst, die Nachbarn rücken zur Seite. Smart-Home-Schalten ist optimistisch (die Kachel schaltet sofort), Lampen leuchten in ihrer echten Farbe.
   - **Performance**: Auf dem TV gemessen (Tasten per CDP, Frame-Zeiten per rAF). Große halbtransparente Ebenen, die beim Navigieren mitlaufen, verdoppeln die Frame-Zeit (p95 50–67 statt 17 ms). Das Hintergrundlicht ist deshalb in die (statische) obere Ebene gemalt und wechselt erst 650 ms nach dem letzten Tastendruck, wenn sich nichts mehr bewegt. Alles, was sich bewegt, läuft über transform/opacity. Im Tasten-Handler gibt es keine erzwungenen Layouts (~1 ms pro Taste). Ergebnis: p95 16,8 ms beim Durchnavigieren.

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
