#!/bin/sh
# Prepares the TV for the Spotify-Licht (spotify_ambient.py) at every boot (Homebrew init.d).
#
# The backend reads the Spotify app on the TV through WebAppMgr's web inspector (port 9998)
# instead of the Spotify Web API. For that:
#   1. port 9998 answers only the TV itself (WebAppMgr listens on all interfaces; without this
#      anyone in the home network could read and control every inspectable app),
#   2. the Spotify app is marked "inspectable" in its appinfo.json (a Spotify update resets it;
#      this script sets it again, the app picks it up at its next start).
#
#   Install  cp 70-ambient-inspector.sh /var/lib/webosbrew/init.d/ && sh /var/lib/webosbrew/init.d/70-ambient-inspector.sh
#   Remove   delete it from init.d and reboot (the appinfo.orig next to appinfo.json restores Spotify)
#   Dev PC   custom-launcher/tools/cdp.sh opens an ssh tunnel to the inspector by itself
PATH="/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
export PATH
PORT=9998
SPOTIFY=/media/cryptofs/apps/usr/palm/applications/spotify-beehive
LOG=/tmp/gemini-supervisor.log

note() { echo "$(date '+%Y-%m-%d %H:%M:%S') ambient-inspector: $*" >> "$LOG"; }

# 1. inspector only for the TV itself (idempotent)
if command -v iptables >/dev/null 2>&1; then
    iptables -C INPUT -p tcp --dport "$PORT" ! -i lo -j DROP 2>/dev/null ||
        iptables -I INPUT -p tcp --dport "$PORT" ! -i lo -j DROP ||
        note "iptables rule for port $PORT failed"
else
    note "iptables missing: port $PORT stays open in the home network"
fi

# 2. Spotify inspectable (keeps a copy of the original once)
if [ -f "$SPOTIFY/appinfo.json" ]; then
    [ -f "$SPOTIFY/appinfo.json.orig" ] || cp "$SPOTIFY/appinfo.json" "$SPOTIFY/appinfo.json.orig"
    python3 - "$SPOTIFY/appinfo.json" <<'EOF' || note "Spotify appinfo not changed"
import json, os, sys
p = sys.argv[1]
with open(p) as f:
    d = json.load(f)
if d.get("inspectable") is not True:
    d["inspectable"] = True
    with open(p + ".tmp", "w") as f:
        f.write(json.dumps(d, separators=(",", ":")))
    os.replace(p + ".tmp", p)
    print("Spotify marked inspectable")
EOF
fi
exit 0
