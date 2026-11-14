"""
SmartThings OAuth for a personal API_ONLY app (long-lived access to your own devices).

Why: SmartThings personal access tokens (PATs) are only valid for 24 hours. With an
OAuth app the access token is also valid ~24 h, but it comes with a refresh token
(valid 30 days, single use) that we exchange for a fresh pair before it runs out.

One-time setup (see README in this folder or run `python3 st_oauth.py help`):
  1. Create an API_ONLY app (SmartThings CLI `apps:create`) with redirect URI
     https://localhost/callback and scopes r:devices:* x:devices:* r:locations:*
  2. Put SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET into .env
  3. python3 st_oauth.py url      -> open the printed URL, log in, allow
  4. python3 st_oauth.py code '<the https://localhost/callback?code=... URL from the browser>'

Tokens are stored in st_tokens.json (mode 600) next to this file.
"""

import os
import sys
import json
import time
import base64
import secrets
import threading
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
TOKEN_FILE = os.path.join(HERE, "st_tokens.json")
STATE_FILE = os.path.join(HERE, ".st_oauth_state")

AUTHORIZE_URL = "https://api.smartthings.com/v1/oauth/authorize"
TOKEN_URL = "https://api.smartthings.com/v1/oauth/token"
DEFAULT_REDIRECT = "https://localhost/callback"
SCOPES = "r:devices:* x:devices:* r:locations:*"

REFRESH_BEFORE = 2 * 3600          # refresh when the access token has less than 2 h left
KEEPALIVE_EVERY = 12 * 3600        # refresh at least this often, so the 30-day refresh token never ages out

_lock = threading.Lock()


def _env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val and os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(name + "="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    return val


def client() -> tuple:
    return _env("SMARTTHINGS_CLIENT_ID"), _env("SMARTTHINGS_CLIENT_SECRET"), \
        (_env("SMARTTHINGS_REDIRECT_URI") or DEFAULT_REDIRECT)


def configured() -> bool:
    cid, sec, _ = client()
    return bool(cid and sec and os.path.exists(TOKEN_FILE))


def _load() -> Optional[dict]:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("refresh_token") else None
    except (OSError, ValueError):
        return None


def _save(data: dict) -> None:
    tmp = TOKEN_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, TOKEN_FILE)         # atomic: never a half-written token pair


def _token_request(form: dict) -> dict:
    cid, sec, _ = client()
    if not cid or not sec:
        raise RuntimeError("SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET fehlen in .env")
    basic = base64.b64encode(f"{cid}:{sec}".encode("utf-8")).decode("ascii")   # no line wrapping
    req = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Authorization": "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"SmartThings token endpoint HTTP {e.code}: {body}")


def _store_response(res: dict) -> dict:
    if not res.get("access_token") or not res.get("refresh_token"):
        raise RuntimeError(f"Unerwartete Token-Antwort: {list(res.keys())}")
    data = {
        "access_token": res["access_token"],
        "refresh_token": res["refresh_token"],
        "expires_at": time.time() + int(res.get("expires_in", 86000)),
        "refreshed_at": time.time(),
    }
    _save(data)
    return data


def refresh(force: bool = False) -> Optional[dict]:
    """Refreshes if needed (or forced). Single-flight: refresh tokens are single use."""
    with _lock:
        data = _load()
        if not data:
            return None
        now = time.time()
        due = (data.get("expires_at", 0) - now < REFRESH_BEFORE or
               now - data.get("refreshed_at", 0) > KEEPALIVE_EVERY)
        if not (force or due):
            return data
        cid, _, _ = client()
        res = _token_request({"grant_type": "refresh_token",
                              "refresh_token": data["refresh_token"],
                              "client_id": cid})
        return _store_response(res)


def access_token() -> str:
    """Valid OAuth access token, or '' if OAuth is not set up / refresh failed."""
    if not configured():
        return ""
    try:
        data = refresh()
    except Exception as e:
        print(f"[st_oauth] refresh failed: {e}", file=sys.stderr)
        data = _load()
    return (data or {}).get("access_token", "")


def start_keepalive() -> None:
    """Background thread: keeps the refresh-token chain alive even if nobody uses SmartThings."""
    def loop():
        while True:
            if configured():
                try:
                    refresh()
                except Exception as e:
                    print(f"[st_oauth] keepalive refresh failed: {e}", file=sys.stderr)
            time.sleep(30 * 60)
    threading.Thread(target=loop, name="st-oauth-keepalive", daemon=True).start()


# ---- one-time setup helpers ---------------------------------------------------
def authorize_url() -> str:
    cid, _, redirect = client()
    if not cid:
        raise RuntimeError("SMARTTHINGS_CLIENT_ID fehlt in .env")
    state = secrets.token_urlsafe(16)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(state)
    q = urllib.parse.urlencode({"client_id": cid, "response_type": "code", "redirect_uri": redirect,
                                "scope": SCOPES, "state": state}, quote_via=urllib.parse.quote)
    return f"{AUTHORIZE_URL}?{q}"


def exchange(code_or_url: str) -> dict:
    code, state = code_or_url.strip(), None
    if code.startswith("http"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
        if "error" in qs:
            raise RuntimeError(f"SmartThings meldet: {qs['error'][0]}")
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [None])[0]
    if not code:
        raise RuntimeError("Kein code gefunden")
    if state is not None:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                expected = f.read().strip()
        except OSError:
            expected = None
        if expected and state != expected:
            raise RuntimeError("state passt nicht – bitte 'url' neu erzeugen und nochmal anmelden")
    cid, _, redirect = client()
    with _lock:
        res = _token_request({"grant_type": "authorization_code", "code": code,
                              "client_id": cid, "redirect_uri": redirect})
        data = _store_response(res)
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass
    return data


def _main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "url":
        print(authorize_url())
    elif cmd == "code" and len(argv) > 2:
        data = exchange(argv[2])
        print("OK – Tokens gespeichert, gültig bis", time.strftime("%Y-%m-%d %H:%M", time.localtime(data["expires_at"])))
    elif cmd == "refresh":
        data = refresh(force=True)
        print("OK – erneuert" if data else "Keine Tokens gespeichert")
    elif cmd == "status":
        data = _load()
        if not data:
            print("nicht eingerichtet" + ("" if client()[0] else " (SMARTTHINGS_CLIENT_ID fehlt)"))
        else:
            left = (data["expires_at"] - time.time()) / 3600
            print(f"eingerichtet – Access-Token noch {left:.1f} h gültig, zuletzt erneuert "
                  + time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("refreshed_at", 0))))
    else:
        print(__doc__)
        return 1 if cmd != "help" else 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv))
    except Exception as e:
        print("Fehler:", e)
        sys.exit(1)
