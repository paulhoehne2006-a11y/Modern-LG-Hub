# Modern LG Hub backend – HTTP API

Backend: `gemini-tv-controller/server.py` (Python standard library only), runs as root on the TV,
port **8080** (`GEMINI_PORT`), bound to all interfaces (`GEMINI_BIND`, default `0.0.0.0`).
Clients: the launcher on the TV (`http://127.0.0.1:8080`, page origin `file://` → `Origin: null`)
and the Android remote over the LAN. Backend version: `2.2.0` (`/api` → `version`); it is
versioned separately from the launcher app (`launcher_version`, from the installed `appinfo.json`).

All bodies are JSON (`application/json; charset=utf-8`). Responses carry `Cache-Control: no-store`
(except icons). Error texts are German, except the token error (clients match its wording).

## Access model

Checked in this order for every request (including `OPTIONS`):

| Check | Rule | Failure |
|---|---|---|
| Client address | loopback, private (RFC 1918 / ULA), or link-local; IPv4-mapped IPv6 is unwrapped | `403 {"error": "Zugriff verweigert"}` |
| `Host` header | absent, an IP literal, `localhost`, a single-label name, a name ending in `.local .lan .home .fritz.box .internal .home.arpa .localdomain`, or listed in `GEMINI_ALLOWED_HOSTS` (comma separated) – stops DNS rebinding | `403 {"error": "Zugriff verweigert (Host)"}` |
| `Origin` header (browsers) | `null` / `file://` **only from loopback** (the launcher; any web page can send `null` from a sandboxed iframe); `http(s)://localhost`, `127.0.0.1`, `[::1]` or a private IP, any port | `403 {"error": "Zugriff verweigert (Origin)"}` |
| Token | if `GEMINI_TOKEN` is set: every non-loopback client sends header `X-Hub-Token` (constant-time compare) | `401 {"error": "Missing or wrong X-Hub-Token"}` (exact text; the Android discovery looks for `X-Hub-Token`) |

Without `GEMINI_TOKEN`, **every device in the LAN can use every endpoint** (keys, apps, power off,
lamps, Gemini, storing a new Gemini key). That is the documented intent for a home network; set a
token if the LAN has guests/IoT devices you don't trust.

Browser POSTs (any request with `Origin`) must use `Content-Type: application/json`, which forces a
CORS preflight. Allowed origins get `Access-Control-Allow-Origin: <origin>`, methods `GET, POST,
OPTIONS`, headers `Content-Type, X-Hub-Token`, `Access-Control-Max-Age: 600`, and
`Access-Control-Allow-Private-Network: true` when the preflight asks for it.

Limits: request body ≤ 64 KB (`400 "Anfrage zu groß"`), no chunked bodies, the whole request
(line, headers, body) must arrive within 10 s (connection closed), 15 s per socket read/write,
at most 32 handler threads, of which LAN clients may hold 24 (the launcher on the TV always gets
a slot); beyond that `503 {"error": "Hub ausgelastet, bitte gleich nochmal"}`. JSON with
`NaN`/`Infinity` is rejected. Unknown paths: `404 {"error": "Nicht gefunden"}`. Unhandled
exceptions: `500 {"error": "Interner Fehler: <ExceptionType>"}` (POST: plus `"success": false`);
the traceback goes to the server log only. There is no access log (the log lives in RAM).

Common 400 shape for POST validation errors: `{"success": false, "error": "<German text>"}`.
Exceptions: smart-home routes answer `{"error": ...}`, `/api/command` answers `{"text": ..., "actions": []}`.

## GET endpoints

### `GET /api` (also `/`)
```json
{"service": "Modern LG Hub Backend", "version": "2.2.0", "launcher_version": "3.8.2",
 "status": "online", "has_gemini_key": true, "has_key": true, "token_required": false,
 "features": ["state", "icon-etag", "smarthome-set", "long-enter", "weather-cache"]}
```
Cheap; used by the watchdog (`90-gemini-controller.sh`) and the Android LAN discovery (looks for
`LG Hub` in the body). `has_key` = `has_gemini_key` (added so both endpoints use one name).
`launcher_version`, `has_key`, `token_required`, `features` are new in 2.2.0.

### `GET /api/status`
```json
{"has_key": false,
 "tv": {"power": "Active", "screen_off": false, "current_app": "netflix",
        "audio": {"soundOutput": "external_optical", "volume": 23, "muted": false,
                  "is_denon_optical": true, "is_tv_speaker": false, "display_title": "Denon AVR-1803 (Optisch)"},
        "network": {"mac_addresses": {"eth0": "02:fc:00:00:00:01"}, "primary_mac": "02:fc:00:00:00:01"},
        "success": true}}
```
`power` is webOS' `getPowerState.state` (`Active`, `Screen Off`, `Active Standby`, …, `Unknown` if
the call failed); `current_app` is `Unknown` if unknown; `soundOutput` defaults to
`external_optical` when the audio service does not answer. The four luna calls run in parallel;
the result is shared for 1 s between pollers and dropped after every POST.

### `GET /api/state` (new in 2.2.0)
One cheap poll for everything a remote shows:
```json
{"version": "2.2.0", "has_key": false, "tv": { …same as /api/status "tv"… },
 "devices": [ …same items as /api/smarthome/devices… ] | null,
 "devices_age": 1.7 | null, "apps_version": 1763600000}
```
`devices` comes from the smart-home cache and never waits for the LAN: `null` when the cache is
missing or older than 30 s (a background refresh is started then; ask again a few seconds later).
`devices_age` = seconds since the devices were read. `apps_version` = mtime of the launcher's
`apps.json`; reload `/api/apps` only when it changes.

### `GET /api/apps`
```json
{"apps": [{"id": "com.webos.app.hdmi1", "title": "PC (HDMI 1)", "type": "input", "alias": "pc"},
          {"id": "netflix", "title": "Netflix", "type": "native", "visible": true, "removable": true,
           "systemApp": false, "iconColor": "#ffffff", "icon": "data/icons/netflix.png", "iconV": 1700000000,
           "usage": 3.81}, …],
 "count": 41, "apps_version": 1763600000}
```
Source: `data/apps.json` written by `launcherd.py` (fallback: luna `listApps`). PC (HDMI 1) first,
then by `usage` (decayed launch count, half-life 45 days, from
`/var/lib/webosbrew/custom-launcher-usage.json`), ties in `apps.json` order. The launcher itself
is not listed; hidden system inputs (HDMI 2–4, Live TV, Settings) are, with `visible: false`.
2.2.0 fixes `usage`: it was always `0.0` (the statistics file format was misread), so the order
was effectively alphabetical by id. `apps_version` is new.

### `GET /api/icon/<appId>`
The icon `launcherd.py` copied (`png/jpg/jpeg/webp/gif`; no SVG). `ETag` + `Cache-Control: public,
max-age=86400`; `If-None-Match` → `304`. Invalid ids / no icon → `404`.

### `GET /api/audio`
The `tv.audio` object of `/api/status`.

### `GET /api/network`
The `tv.network` object (MACs for Wake-on-LAN; remembered once found).

### `GET /api/smarthome/devices` (old alias `GET /api/smartthings/devices`)
JSON array in `devices.json` order:
```json
[{"id": "0a0000000001", "name": "Fernseher Bar", "room": "Wohnzimmer", "raw_name": "Wohnzimmer Fernseher Bar",
  "type": "light", "state": "on", "brightness": 55, "color": "#ff2800", "kelvin": null,
  "capabilities": ["brightness", "color", "white"], "online": true},
 {"id": "0a0000000008", "name": "Licht", "room": "Büro", "raw_name": "Büro Licht", "type": "plug",
  "state": "off", "brightness": null, "color": null, "kelvin": null, "capabilities": [], "online": false,
  "error": "no answer from 192.168.178.50"}]
```
`id` = MAC without separators. `type`: `light` (WiZ) or `plug` (Tapo). Offline devices report
`state: "off"`, `online: false` and a diagnostic `error` (not localised). Cached 5 s; only one refresh
runs at a time and other callers get the previous list meanwhile (a switch empties the cache, the
next read waits for fresh data). A device that does not answer triggers a LAN rescan for moved IPs
at most once per minute, backing off to once per 10 min while scans find nothing (a switched-off
lamp); switching a device always allows a rescan after 1 min.

### `GET /api/weather`
open-meteo forecast (`current` = temperature_2m, apparent_temperature, weather_code; `daily` =
weather_code, temperature_2m_max/min, precipitation_probability_max). Location from
`WEATHER_LAT` / `WEATHER_LON` / `WEATHER_TZ` (defaults 52.5200 / 13.4050 / Europe/Berlin – set your own in the TV's `.env`); `WEATHER_PLACE` adds a `place` field with the name the launcher shows (e.g. `WEATHER_PLACE=Musterstadt`). Cached
10 min; on failure the last good forecast (up to 3 h old), else `200 {"error": "<type>: <msg>"}`.

## POST endpoints

All answer `200` with the action result unless noted. `success` mirrors luna `returnValue`.

| Path | Body | Answer |
|---|---|---|
| `/api/remote/key` | `{"key": "UP", "fast": true}` – key name `[A-Za-z0-9_]{1,32}`; aliases (`ok`, `vol_up`, `ch_down`, …) or webOS names (`ENTER`, `BACK`, `HOME`, `PLAY`, `0`–`9`, `RED` …). `LONG_ENTER`/`OK_LONG`: `INFO` when the hub is in front (opens its options menu), else `ENTER`. `fast` (default true) = fire and forget | `{"action": "send_key", "key": "UP", "success": true}`; invalid key → 400 |
| `/api/remote/text` | `{"text": "…", "replace": false}` (max 2000 chars used) | `{"action": "insert_text", "text": "…", "success": …}`; non-string → 400 |
| `/api/remote/delete` | `{"length": 1}` (1–500, number or numeric string) | `{"action": "delete_text", "length": n, "success": …}` |
| `/api/remote/enter` | – | `{"action": "send_enter", "success": …}` (was a bare `true` before 2.2.0) |
| `/api/launch` | `{"id": "netflix"}` (also `appId` / `app`); app id `[A-Za-z0-9][A-Za-z0-9._-]*` or a friendly name from `tv.APP_MAP` (`"amazon prime"`) | app: `{"app", "app_id", "success", "response"}`; input `com.webos.app.hdmi1..4`: `{"input", "app_id", "success"}`; missing → 400 `App-ID fehlt`, other hdmi → 400 |
| `/api/audio/output` | `{"output": "external_optical"}` or alias `denon/optical/tv/speaker/both/bt/bluetooth` (`[A-Za-z0-9_]{1,40}`) | `{"action": "change_sound_output", "output": "…", "success": …}` |
| `/api/audio/toggle` | – | Denon (optical) ↔ TV speaker, same answer as above |
| `/api/picture/mode` | `{"mode": "game"}` (`game, filmmaker, cinema, isf_dark, isf_bright, standard` or a raw webOS mode) | `{"action": "set_picture_mode", "mode": "filmMaker", "success": …}` |
| `/api/quick` | `{"action": "…"}`: `vol_up, vol_down, mute, screen_off, screen_on, screen_toggle, audio_toggle, power_off, pc, home, pic_<mode>` | result + `action`, `success`; **502** when the TV refused (`success: false`); unknown → `400 {"success": false, "error": "Unbekannte Aktion: <action>"}` |
| `/api/smarthome/toggle` (alias `/api/smartthings/toggle`) | `{"deviceId": "…"}` (or `id`) | `{"status": "ok", "state": "on", "new_state": "on"}` or `{"status": "error", "error": "Schlafzimmer Bett nicht erreichbar (…)"}` (still HTTP 200; clients read `status`) |
| `/api/smarthome/set` | `{"deviceId"\|"id", "state": "on"\|"off" (default "on"), "brightness": 10–100, "color": "#rrggbb", "kelvin": 2200–6500}`; colour wins over kelvin; values clamped; brightness/colour/kelvin only with "on" and only for WiZ | same as toggle; bad id → `400 {"error": "deviceId fehlt oder ist ungültig"}`, bad colour → 400; unknown device → `{"status": "error", "error": "Unbekanntes Gerät"}` |
| `/api/command` | `{"prompt": "…", "history": [{"role": "user"\|"model", "text": "…"}]}` (prompt ≤ 2000 chars used, last 12 turns sent) | `{"text", "actions": [{"tool", "args", "result"}], "suggestions": [≤3], "prompt"}`; no key: `{"text", "actions": [], "needs_key": true}`; HTTP errors from Gemini: German `text` + `details` (Google's error body, key scrubbed); empty prompt → `400 {"text": "Leere Anfrage.", "actions": []}` |
| `/api/key` | `{"api_key": "…"}` (`[A-Za-z0-9_-]{10,200}`) | `{"success": true, "has_key": true}`; empty → `{"success": false, "has_key": …}`; bad format → 400; file error → 500. Stored in `.env` (atomic, mode 0600, other lines kept); never returned |

Every POST drops the status cache, so the next `/api/status` shows the effect.

## Gemini agent (`/api/command`)

One round trip to `generativelanguage.googleapis.com/v1beta/models/<GEMINI_MODEL>:generateContent`
with function declarations (apps, inputs, volume, mute, screen on/off, screensaver, media keys,
status, toast, smart home; the smart-home tool lists rooms/devices from `devices.json`). At most
8 tool calls per answer are executed. If Gemini sends no text, a German summary is built;
failed tools are reported as "Nicht geklappt: …". Timeouts, missing internet, unreadable or
blocked answers all come back as German `text` (never an HTTP error). The key travels only in the
`x-goog-api-key` header and is scrubbed from everything returned.

## Configuration (`gemini-tv-controller/.env`, or the process environment, which wins)

| Name | Meaning |
|---|---|
| `GEMINI_API_KEY` | Gemini key (also settable via `/api/key`) |
| `GEMINI_MODEL` | model id (default `gemini-3.8-flash`) |
| `GEMINI_TIMEOUT` | seconds per Gemini request, 3–55 (default 12) |
| `GEMINI_TOKEN` | optional shared secret for LAN clients (`X-Hub-Token`) |
| `GEMINI_ALLOWED_HOSTS` | extra `Host` names (comma separated), e.g. a DynDNS-free local name |
| `TAPO_EMAIL`, `TAPO_PASSWORD` | TP-Link login for the KLAP handshake (`set-tapo.sh` writes them) |
| `WEATHER_LAT`, `WEATHER_LON`, `WEATHER_TZ` | forecast location |
| `HUB_GATEWAY` | router address used to find the TV's own subnet for the LAN rescan (default: the TV's default route) |

`GEMINI_PORT` / `GEMINI_BIND` are read from the process environment only. The `.env` is re-parsed
only when it changes (cheap per request). Format: `KEY=value`, optional quotes, `export ` prefix
and `#` comments allowed, first definition wins.

`devices.json` (only on the TV, git-ignored; start from `devices.example.json`): `{"devices": [{"mac", "kind": "wiz"|"tapo", "room", "name", "ip"}]}` – identified
by MAC; `ip` is only a start value, learned addresses go to `ip-cache.json`. Order = tile order.
