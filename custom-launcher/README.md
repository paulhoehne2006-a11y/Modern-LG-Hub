# LG G4 – eigener Startbildschirm + Härtung

TV: LG OLED G4 (webOS 10.3.1, gerootet, Homebrew Channel), `root@<TV-IP>`

## Dateikarte (alles in diesem Ordner)

| Pfad | Was |
|---|---|
| `com.custom.launcher/` | **Die App** (Quelltext). `index.html`, `style.css`, `app.js` = Oberfläche; `launcherd.py` = Root-Hilfsdienst; `appinfo.json`, `icon.png`; `data/` wird auf dem TV von `launcherd.py` erzeugt (bzw. lokal für die PC-Vorschau per `tools/mkdata.js`, nicht im Git) |
| `com.custom.launcher_<version>_all.ipk` | Installationspaket, wird von `build-ipk.sh` erzeugt (nicht im Git). Aktuelle Version steht in `com.custom.launcher/appinfo.json` (3.9.0) |
| `../gemini-tv-controller/` | Backend auf dem TV (`server.py`, Port 8080, im Heimnetz erreichbar für die Handy-App; Zugriffsmodell und alle Endpunkte in `../docs/API.md`): Gemini-Assistent, Smart-Home-Kacheln (WiZ/Tapo lokal im LAN), Wetter, OLED-aus |
| `boot/99-custom-launcher` | Boot-Hook, wird auf dem TV nach `/var/lib/webosbrew/init.d/` kopiert |
| `build-ipk.sh` | Baut das .ipk aus `com.custom.launcher/` |
| `tools/deploy.sh` | **Ein Befehl für alles**: bauen, installieren, Boot-Hook setzen, Dienst neu starten |
| `tools/tv.sh`, `luna.sh`, `key.sh`, `tvshot.sh` | Hilfen: Befehl auf dem TV (mit TTY), Luna-Aufruf, Fernbedienungstaste senden, TV-Bildschirmfoto |
| `tools/cdp.sh` | Web-Inspector des TVs: JS in der Launcher-Seite ausführen, Tasten, Screenshots |
| `tools/localshot.sh`, `mkdata.js` | Launcher am PC in einem Headless-Chromium/Edge rendern/bedienen (Vorschau mit eigenen lokalen TV-Daten; findet den Browser selbst, sonst `--browser` oder `LOCALSHOT_BROWSER`) |
| `design/fixtures/`, `com.custom.launcher/data/` | Nicht im Git (lokale Dateien): vom TV exportierte App-Icons/Assets von Drittanbietern sowie LGs Systemschriften („LG Smart UI“, vom TV `/usr/share/fonts/`); auf dem PC ohne diese Dateien nutzt die Vorschau Ersatzschriften |
| `README.md` | diese Datei |

Auf dem TV liegt:
- App: `/media/developer/apps/usr/palm/applications/com.custom.launcher/` (`data/` dort wird vom Dienst erzeugt)
- Boot-Hook: `/var/lib/webosbrew/init.d/99-custom-launcher` (Sicherung des alten: `/var/lib/webosbrew/backup-custom-launcher/99-autostart.v1`)
- Dienst-Log: `/tmp/custom-launcher.log` (nur RAM, wird beim Neustart gelöscht)

## Was gemacht wurde

1. **Launcher v1** (Schwarz, Uhr, Kacheln) gebaut, per SSH installiert.
2. **Home-Taste ersetzt**: LGs Startbildschirm (`com.webos.app.home`) ist Boot-Abhängigkeit und bleibt installiert; ein Root-Dienst (`launcherd.py`) hört auf App-Ereignisse und öffnet bei jedem Erscheinen sofort den eigenen Launcher (gemessen ca. 0,3–0,4 s, kein Polling mehr).
3. **Launcher v2** komplett neu im Apple-TV-Stil: Favoritenreihe (PC/HDMI 1, HDMI 2, YouTube) + App-Raster, Karten in der Farbe des App-Icons, Fokus mit Skalierung/Glanz/Glow, Schwebelicht in Fokusfarbe, OK lang drücken = Menü (Öffnen / Favorit / Verschieben / Ausblenden), „Mehr“-Menü, OLED-Schutz (Pixelverschiebung, Abdunkeln nach 1 min ohne Taste – bei offenem Gemini nach 3 min –, Bildschirmschoner nach 4 min, echtes Schwarz).
3b. **Reihenfolge nach Nutzung** (v2.1): Der Root-Dienst zählt, welche App wie oft nach vorn kommt (auch über Fernbedienungstasten wie „Netflix“) und schreibt `data/usage.json`. Die oberste Reihe zeigt die 5 meistgenutzten, darunter folgt der Rest absteigend. Alte Nutzung verblasst (Halbwertszeit 45 Tage). Startreihenfolge: Netflix, Spotify, PC/HDMI 1, YouTube, Prime Video. Per langem OK lässt sich eine App oben **anheften** (bleibt vorn, verschiebbar). Zähler-Datei (überlebt Neuinstallation): `/var/lib/webosbrew/custom-launcher-usage.json` – löschen = Zähler zurücksetzen (danach Dienst neu starten).
4. **Bloat ausgeblendet** (nichts deinstalliert): LG Channels, Gallery+, Sport, Startseiten-Hub, Kamera, Multi View, Alexa, Travel+, Gaming Portal, Public Value, ungenutzte HDMI-Eingänge, Platzhalter-Apps. Alles unter „Mehr → Ausgeblendete Apps“ wiederherstellbar.
5. **Telnet geschlossen**: Die offene Root-Shell ohne Passwort (Port 23) ist abgeschaltet (`/var/luna/preferences/webosbrew_telnet_disabled`). SSH (nur Schlüssel) bleibt.
6. **Audit** (Datenschutz, Bloat, Boot): Tracker-Domains (407) und die Werbe-/ACR-/Sprach-Dienste sind durch „Life's Good Without Spying“ bereits geblockt.
7. **Launcher v3** (3.2.x, „Modern Minimalist“): neues Design, Smart-Home-Kacheln (SmartThings über das Backend), Wetter, Gemini-Assistent, Denon-Audio-Modus (schaltet das OLED-Panel aus, Ton läuft weiter).
   - **Home-Taste**: webOS verarbeitet sie selbst (`homeAutoLaunch` bleibt an, sonst reagiert die Fernbedienung nicht). `launcherd.py` leitet LG-Home sofort auf den Launcher um; der Launcher bekommt dann `webOSRelaunch` und springt nach oben. Zurück aus einer App behält die Position.
   - **Home-Taste direkt** (ab 3.3.2): Der Compositor (`surface-manager`) lädt seine Tastenfilter aus configd `com.webos.surfacemanager.keyFilters` und lädt sie bei Änderung live neu. `launcherd.py` erzeugt aus LGs eigener `KeyFilters/systemUi.js` eine Kopie (`/var/lib/webosbrew/custom-launcher-keyfilter/systemUi.js`), in der nur `launchDefaultApp(home)` durch den Start des Launchers ersetzt ist, und lässt die Filter-Einträge darauf zeigen. Damit startet Home LG-Home gar nicht erst (kein Aufblitzen, Hub nach ~0,15 s). Nur im RAM: ein Neustart, die `.off`-Datei oder `launcherd.py stop` stellen LGs Original wieder her. Erkennt `launcherd.py` die Zeile nach einem Firmware-Update nicht, bleibt alles beim Original und die Umleitung greift wie bisher.
   - **Achtung**: Niemals `defaultApps.home` (Kategorie `general`) auf `com.custom.launcher` setzen. Dann startet webOS bei Home gar nichts mehr (Home-Taste tot, auch nach Neustart). Richtiger Wert: `{"MembershipApp":"com.webos.app.overlaymembership"}` ohne `home`.
   - **Absicherung**: Der Boot-Hook startet `launcherd.py` neu, falls der Dienst abstürzt (Prüfung jede Minute), und schreibt ins Log, wenn `python3` fehlt. Pausieren deshalb über die `.off`-Datei, nicht über `launcherd.py stop`.
   - **Schnelle Rückkehr**: `launcherd.py` trägt den Launcher in zwei Keep-Alive-Listen ein (configd, nur im RAM, wird bei jedem Start erneut gesetzt): `com.webos.applicationManager.keepAliveApps` (SAM) und `com.webos.memorymanager.KeepAlive` (LGs Speicher-Manager `memchute`, der sonst unter Last den Launcher-Renderer beendet, z. B. bei offenem Spotify; Rückkehr dann ~3 s mit Ladekreis). memchute liest seine Liste nur beim Start, deshalb startet `launcherd.py` ihn einmal neu, wenn der Eintrag fehlt.
   - **Fenster nach vorn**: Mit `handlesRelaunch` muss die App ihr Fenster bei jedem `webOSRelaunch` selbst holen (`PalmSystem.activate()`), nie entprellt. Sonst bleibt nach Home kurz LG-Home und danach gar keine App im Vordergrund.
8. **Smart Home lokal** (3.4.0): Die SmartThings-Cloud ist ersetzt durch direkte LAN-Steuerung im Backend (`localhome.py`). WiZ-Lampen laufen per UDP 38899, Tapo-Steckdosen per KLAP; die Zugangsdaten stehen als `TAPO_EMAIL`/`TAPO_PASSWORD` in der `.env` des Backends und werden bei jedem Handshake neu gelesen. Die Geräte stehen in `devices.json` (nur auf dem TV, nicht im Git; Vorlage `devices.example.json`) und werden per MAC erkannt; ändert die Fritzbox eine IP, findet das Backend sie per Broadcast bzw. ARP-Tabelle wieder (`ip-cache.json`). Die Kacheln zeigen Zustand und Raum. Gemini schaltet einzelne Geräte, ganze Räume („Wohnzimmer aus“) und die Helligkeit. Test auf dem TV: `python3 localhome.py status`. Der alte SmartThings-Client (`smartthings.py`, `st_oauth.py`) ist entfernt; auf dem TV liegt er nur noch in `gemini-controller/backup-smartthings/`. Die alten Pfade `/api/smartthings/devices|toggle` antworten weiter (lokal), weil ältere App-Versionen darauf zurückfallen.
9. **Startseite im Netflix-Stil** (3.6.0): Oben stehen fest Uhr und Pills, darunter ein Billboard. Es beschreibt groß, was gerade fokussiert ist: App (mit Logo, Beschreibung, „zuletzt vor …“), Lampe (Raum, Zustand, Helligkeit), Raum, Wetter, Gemini oder Audio, mit Hintergrundlicht in der passenden Farbe. Darunter liegen Reihen: „Zuletzt & Favoriten“, Smart Home (Geräte des Hauptraums einzeln, eine Kachel pro weiterem Raum, „Display aus“) und die App-Kategorien Eingänge, Streaming, TV & Mediatheken, Musik & Gaming und Werkzeuge (`CATEGORIES` in `app.js`). Die fokussierte Reihe gleitet unter das Billboard, lange Reihen gleiten seitlich. Die fokussierte Kachel wächst, die Nachbarn rücken zur Seite. Smart-Home-Schalten ist optimistisch (die Kachel schaltet sofort), Lampen leuchten in ihrer echten Farbe.
   - **Performance**: Auf dem TV gemessen (Tasten per CDP, Frame-Zeiten per rAF). Große halbtransparente Ebenen, die beim Navigieren mitlaufen, verdoppeln die Frame-Zeit (p95 50–67 statt 17 ms). Das Hintergrundlicht ist deshalb in die (statische) obere Ebene gemalt und wechselt erst 650 ms nach dem letzten Tastendruck, wenn sich nichts mehr bewegt. Alles, was sich bewegt, läuft über transform/opacity. Im Tasten-Handler gibt es keine erzwungenen Layouts (~1 ms pro Taste). Ergebnis: p95 16,8 ms beim Durchnavigieren.
   - **Fokus-Wächter** (3.6.1): Auf dem TV beobachtet: Der Hub war sichtbar, hatte aber nicht den Tastaturfokus. Dann gibt der erste Tastendruck nur den Fokus und bewegt nichts („zweimal drücken“). Home, App-Wechsel, Schnellmenü, Toasts, Kaltstart und Neuladen lösen das nicht aus. Die Ursache ist noch offen. Der Hub holt sich den Fokus jetzt selbst zurück, wenn er sichtbar ist und ihn länger als 4 s nicht hat, ohne dass ein Overlay ihn kurz vorher genommen hat (`PalmSystem.activate()` nimmt einem offenen Schnellmenü den Fokus nicht weg). Jeder Fokuswechsel landet in einem kleinen Protokoll: `tools/cdp.sh eval 'window.__launcher.focusLog.join("\n")'` (Zeiten in UTC). Ein „focus (by key?)“ dort ist genau der verschluckte Tastendruck.
   - **Nach Home vorne bleiben** (3.6.2): Nach Home steht der Fokus auf der ersten Karte. Kommt die App-Liste kurz danach und sortiert „Zuletzt“ neu, blieb der Fokus vorher an der alten App hängen und wanderte mit ihr auf Platz 4 (die Reihe war dann seitlich verschoben, „runter“ landete auf der falschen Kachel). Bis zur ersten Bewegung (max. 5 s) bleibt der Fokus jetzt auf Platz 1.
   - **Farbe und Lang-OK vom Handy** (3.7.0): Lampen mit Farbe/Weißton haben im Optionen-Menü den Punkt „Farbe“ (Warm-, Neutralweiß, Tageslicht, 8 Farben, mit Farbpunkt; das Menü bleibt zum Ausprobieren offen). Die Netzwerk-Fernbedienung kennt nur Drücken+Loslassen, kein Halten: Die Handy-App (ab 2.2.0) schickt bei gehaltenem OK `LONG_ENTER` an `/api/remote/key`. Das Backend macht daraus INFO (Code 457), wenn der Hub vorne ist, sonst ein normales OK. Der Hub öffnet bei INFO das Optionen-Menü der fokussierten Kachel. Außerdem liegen Menü, Abdunkelung und Toasts wieder über den Reihen (seit 3.6.0 lagen sie dahinter, weil `#rows`/`#fixed` eigene z-Ebenen haben).
   - **Eigene Farbe** (3.8.0): Ganz oben im Farbmenü steht „Eigene Farbe …“ mit drei Reglern: Farbton (0–355°), Sättigung (Weiß bis volle Farbe) und Weißton (2200–6500 K). ▲▼ wählt den Regler, ◀▶ verstellt ihn (schnell gedrückt oder gehalten in 3er-Schritten), OK schließt, Zurück geht ins Farbmenü. Die Lampe folgt live, sobald der Wert 160 ms ruht (kein Befehl pro Tastendruck). Der Regler, an dem zuletzt gedreht wurde, ist „aktiv“ und bestimmt, ob die Lampe Farbe oder Weißton bekommt. Getestet mit abgefangenem `fetch` (keine echte Lampe geschaltet).
   - **Hintergrundlicht neu** (3.8.2): Das Licht wechselt jetzt zusammen mit dem Billboard-Text (300 ms Ruhe nach der letzten Taste; vorher 650 ms nach dem Text, wirkte nachgeladen) und läuft unter dem Billboard hinter den Reihen weich aus, statt an der Billboard-Kante abzubrechen. Es wird in drei kleine, statische Flächen gemalt (`#glow-top`, `#fixed-fade`, `#glow-low`), nie als eigene Ebene und nie über `#page` (eine CSS-Variable auf `#page` lässt alle Karten neu berechnen: p95 100 ms). Keine Maske über den laufenden Reihen (kostete ~2 Bilder pro Frame). Der Logo-Schatten endet vor der Kante (der alte wurde dort abgeschnitten = feine Linie). Gemessen gegen 3.6.0 auf demselben TV: schnelles Navigieren p95 16,8 ms (gleich), einzelne langsame Schritte 1–2 Bilder beim Text-Wechsel.
   - **Uhr ohne Puls** (3.8.2): Der pulsierende Doppelpunkt hielt den TV dauerhaft bei 60 Bildern/s: ~28 % CPU im WebAppMgr plus ~20 % im Compositor, auch in Ruhe. Jetzt statisch: beide unter 1 %.
   - **Runter von ganz oben** (3.8.1): Von Wetter/Gemini/Audio nach unten landet der Fokus immer auf der ersten Karte links.
   - **Fokus nach Kaltstart**: webOS gibt dem frisch gestarteten Hub den Fokus erst 1–4 s nachdem er sichtbar ist (unabhängig vom Laden der Seite, gemessen). Tasten in dieser Zeit gehen verloren; daran lässt sich nichts ändern. Für den langen Fall (Minuten) protokolliert der Fokus-Wächter jede Episode einmal mit Kontext und versucht nach 4 s `activate()`, nach 10 s einen Neustart von sich selbst mit `refocus` (behält die Ansicht). `deactivate()`+`activate()` darf nicht verwendet werden: das schickt den Hub minutenlang in den Hintergrund.

10. **Überarbeitung** (3.9.0, Backend 2.2.0):
   - **Ein Menüsystem**: Jeder Menüpunkt ist Aktion (Haken für die aktuelle Wahl, Pfeil für Untermenüs), Schalter oder Regler. Helligkeit ist ein Regler statt fünf Einträgen (◀▶, gehalten in 3er-Schritten, sendet 160 ms nach der letzten Änderung, immer nur den neuesten Wert). Die eigene Farbe nutzt dieselben Regler. Alle Menüs gehen auch mit dem Zeiger.
   - **Billboard mit Fakten**: Lampe (Helligkeit, Farbe), Raum (Geräte), Wetter (3 Tage), Eingang (Anschluss). Mehr Abstand zwischen Reihentitel und fokussierter Karte (Fokus 1,12×).
   - **Gemini**: Linien-Icons statt Emoji, erledigte Aktionen als deutsche Etiketten, 45 s Zeitlimit mit „Erneut versuchen“.
   - **Fehlerzustände**: Smart-Home-Server oder Wetter nicht erreichbar, keine Geräte, Gerät offline – jeweils eigene Anzeige statt endlosem „…“.
   - **Behoben**: Pfeiltasten tot nach langem OK auf Gemini; leeres Billboard-Logo; Smart-Home-Antworten in falscher Reihenfolge machten Änderungen rückgängig; Gemini-Fenster wurde nie abgedunkelt.
   - **Backend**: `Origin: null` nur noch vom TV selbst (vorher konnte eine Webseite im Heimnetz den TV steuern), Schutz gegen DNS-Rebinding, Zeit- und Verbindungsgrenzen; `/api/status` parallel und 1 s geteilt; „Zuletzt“-Reihenfolge der Handy-App jetzt wirklich nach Nutzung; `.env` wird sicher geschrieben; Wetter-Ort per `WEATHER_LAT`/`WEATHER_LON`/`WEATHER_TZ`.
   - **launcherd**: Die `.off`-Datei stellt LGs Tastenfilter auch nach einem Absturz des Dienstes wieder her; Neustart von memchute mit Zeitlimit.
   - **Tests**: `python3 -m unittest discover -s tests -p 'test_hub_*.py'` (96 Tests), GitHub Actions in `.github/workflows/ci.yml`. Vor dem ersten Einsatz am TV prüfen: Launcher-Anfragen kommen von 127.0.0.1 mit `Origin: null` (sonst 403), Lampen-Regler mit echter WiZ, Frame-Zeiten beim Billboard-Wechsel.

## Bedienung / Notausstieg

LG-Startbildschirm wieder wie original (innerhalb von ~5 s, ohne Neustart; greift auch, wenn `launcherd.py` vorher abgestürzt ist):
```bash
ssh root@<TV-IP> "touch /var/lib/webosbrew/custom-launcher.off"
```
Wieder einschalten: `rm /var/lib/webosbrew/custom-launcher.off`

Neu installieren/aktualisieren: `export TV_HOST=root@<TV-IP>` (einmal pro Terminal, alle Tools brauchen es), dann `tools/deploy.sh`

Persönliches steht nur auf dem TV, nicht im Git: `gemini-tv-controller/devices.json` (Vorlage `devices.example.json`) und die `.env` (`TAPO_EMAIL`, `TAPO_PASSWORD`, `GEMINI_API_KEY`, `WEATHER_LAT`, `WEATHER_LON`, `WEATHER_PLACE`).
Dienst-Status: `ssh root@<TV-IP> "python3 /media/developer/apps/usr/palm/applications/com.custom.launcher/launcherd.py status"`
Telnet wieder erlauben: `rm /var/luna/preferences/webosbrew_telnet_disabled` + Neustart

## Bewusst offen gelassen (deine Entscheidung)

- Live Plus / Werbe-Einwilligungen in den TV-Einstellungen stehen noch auf „an“ (Server sind geblockt, aber der Schalter ist an) → in den TV-Einstellungen ausschalten.
- Firmware-Updates sind absichtlich geblockt (`domains.updates=on`) → keine Sicherheitsupdates.
- Apps deinstallieren (Platzhalter-Stubs, Travel+, Gaming Portal): nur auf Wunsch.
