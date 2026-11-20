"""
One-time Spotify login for the Spotify ambilight (spotify_ambient.py).

OAuth Authorization Code with PKCE: no client secret is needed, only the app's Client ID.
The refresh token lands in spotify-token.json next to this file (gitignored, mode 0600).

    python3 spotify_login.py [--client-id <id>]     log in (opens the browser, waits for the redirect)
    python3 spotify_login.py --check                 test /me/player and /me/player/currently-playing

The Client ID comes from --client-id (then saved to .env), SPOTIFY_CLIENT_ID in the environment
or SPOTIFY_CLIENT_ID in .env. In the Spotify developer dashboard the app needs the redirect URI
    http://127.0.0.1:8888/callback
(Spotify does not accept "localhost").

Logging in for the TV from a PC: run it on the TV through an SSH tunnel and open the printed URL
on the PC:   ssh -L 8888:127.0.0.1:8888 root@<tv> python3 /var/lib/webosbrew/gemini-controller/spotify_login.py
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "spotify-token.json")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-read-currently-playing"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
LOGIN_TIMEOUT = 300        # s to finish the login in the browser


def client_id() -> str:
    sys.path.insert(0, HERE)
    import localhome                     # .env reader shared with the backend
    return (localhome.env_value("SPOTIFY_CLIENT_ID") or "").strip()


def load_token() -> Optional[Dict[str, Any]]:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            tok = json.load(f)
        return tok if isinstance(tok, dict) and tok.get("refresh_token") else None
    except (OSError, ValueError):
        return None


def save_token(tok: Dict[str, Any]) -> None:
    """Atomic write, mode 0600 (it is a credential)."""
    tmp = f"{TOKEN_FILE}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tok, f)
        os.replace(tmp, TOKEN_FILE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def token_request(params: Dict[str, str], timeout: float = 10.0) -> Dict[str, Any]:
    """POST to the token endpoint; returns the stored token dict (refresh token kept if not rotated)."""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read(65536).decode("utf-8"))
    if not isinstance(data, dict) or not data.get("access_token"):
        raise ValueError("unexpected token reply")
    old = load_token() or {}
    tok = {
        "access_token": data["access_token"],
        "expires_at": time.time() + int(data.get("expires_in") or 3600) - 60,
        # PKCE refresh tokens rotate: always keep the newest one
        "refresh_token": data.get("refresh_token") or old.get("refresh_token") or params.get("refresh_token"),
        "scope": data.get("scope") or old.get("scope") or "",
    }
    save_token(tok)
    return tok


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(cid: str) -> int:
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state,
    })
    result: Dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(u.query)
            if q.get("state", [""])[0] != state:
                msg = "Falscher state-Parameter, Login bitte neu starten."
            elif "error" in q:
                msg = "Spotify meldet: " + q["error"][0]
                result["error"] = q["error"][0]
            else:
                result["code"] = q.get("code", [""])[0]
                msg = "Login erfolgreich. Dieses Fenster kann zu."
            page = f"<!doctype html><meta charset=utf-8><title>Spotify-Login</title><p>{msg}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            if result:
                done.set()

    srv = HTTPServer(("127.0.0.1", 8888), Handler)
    srv.timeout = 1
    print("Im Browser bei Spotify anmelden und zustimmen:\n\n  " + url + "\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    end = time.time() + LOGIN_TIMEOUT
    while not done.is_set() and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if not result.get("code"):
        print("Kein Login: " + (result.get("error") or "Zeit abgelaufen"), file=sys.stderr)
        return 1
    try:
        tok = token_request({"grant_type": "authorization_code", "code": result["code"],
                             "redirect_uri": REDIRECT_URI, "client_id": cid, "code_verifier": verifier})
    except urllib.error.HTTPError as e:
        print(f"Token-Tausch fehlgeschlagen: HTTP {e.code} {e.read(500).decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    print(f"Gespeichert in {TOKEN_FILE} (Scopes: {tok['scope']})")
    return 0


def access_token(cid: str) -> str:
    tok = load_token()
    if not tok:
        raise SystemExit("Kein Token: zuerst ohne --check einloggen.")
    if tok.get("access_token") and time.time() < float(tok.get("expires_at") or 0):
        return tok["access_token"]
    return token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                          "client_id": cid})["access_token"]


def check(cid: str) -> int:
    """Calls both player endpoints once and prints what came back (status, device, track)."""
    at = access_token(cid)
    ok = True
    for path in ("/me/player", "/me/player/currently-playing"):
        req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + at})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, raw = r.status, r.read(512 * 1024)
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(2000)
        line = f"GET {path}: HTTP {status}"
        if status == 200 and raw:
            d = json.loads(raw)
            item = d.get("item") or {}
            dev = d.get("device") or {}
            imgs = (item.get("album") or {}).get("images") or []
            line += (f"  playing={d.get('is_playing')}  device={dev.get('name')!r} ({dev.get('type')})"
                     f"  track={item.get('name')!r}  images={[i.get('width') for i in imgs]}")
        elif status == 204:
            line += "  (gerade nichts aktiv: einen Song starten und nochmal prüfen)"
        else:
            ok = False
            line += "  " + raw.decode("utf-8", "replace")[:300]
        print(line)
    return 0 if ok else 2


def main(argv) -> int:
    args = list(argv)
    cid = ""
    if "--client-id" in args:
        i = args.index("--client-id")
        cid = (args[i + 1] if i + 1 < len(args) else "").strip().strip("<>")
        if not (len(cid) == 32 and all(c in "0123456789abcdef" for c in cid.lower())):
            print("Client-ID sieht falsch aus (32 Hex-Zeichen erwartet).", file=sys.stderr)
            return 1
        sys.path.insert(0, HERE)
        import localhome
        localhome.set_env_value("SPOTIFY_CLIENT_ID", cid)
    cid = cid or client_id()
    if not cid:
        print("Keine Client-ID: --client-id <id> angeben oder SPOTIFY_CLIENT_ID in .env setzen.", file=sys.stderr)
        return 1
    if "--check" in args:
        return check(cid)
    return login(cid)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
