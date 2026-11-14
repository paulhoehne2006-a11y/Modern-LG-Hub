#!/bin/sh
# Autostart + watchdog for the Gemini TV backend (server.py, port 8080).
#
# Started by the Homebrew Channel at boot (/var/lib/webosbrew/init.d/). Running it again
# (e.g. after a deploy) restarts the server and replaces the previous watchdog.
#
#   Pause   touch /var/lib/webosbrew/gemini-controller.off   (watchdog stops restarting)
#   Resume  rm /var/lib/webosbrew/gemini-controller.off
#   Status  cat /tmp/gemini-supervisor.log
DIR=/var/lib/webosbrew/gemini-controller
OFF=/var/lib/webosbrew/gemini-controller.off
LOG=/tmp/gemini-server.log
SUPLOG=/tmp/gemini-supervisor.log
SUPPID=/tmp/gemini-supervisor.pid
PORT=${GEMINI_PORT:-8080}
INTERVAL=30          # seconds between checks
FAILS_BEFORE_RESTART=2

note() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$SUPLOG"; }

PY=$(command -v python3 2>/dev/null)
if [ -z "$PY" ]; then
    note "python3 not found in PATH ($PATH), backend not started"
    exit 0
fi
[ -f "$DIR/server.py" ] || exit 0

server_pids() {
    for p in /proc/[0-9]*; do
        c=$(tr "\0" " " < $p/cmdline 2>/dev/null)
        case "$c" in *"$DIR/server.py"*) echo "${p#/proc/}";; esac
    done
}

start_server() {
    for pid in $(server_pids); do kill "$pid" 2>/dev/null; done
    sleep 1
    for pid in $(server_pids); do kill -9 "$pid" 2>/dev/null; done
    # keep /tmp (RAM) small: start a fresh log, keep the previous one once
    [ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"
    nohup "$PY" "$DIR/server.py" > "$LOG" 2>&1 &
    note "server started (pid $!)"
}

healthy() {
    [ -n "$(server_pids)" ] || return 1
    wget -q -T 5 -O /dev/null "http://127.0.0.1:$PORT/api" 2>/dev/null
}

# replace an older watchdog (from boot or a previous deploy)
if [ -f "$SUPPID" ]; then
    old=$(cat "$SUPPID" 2>/dev/null)
    [ -n "$old" ] && [ "$old" != "$$" ] && kill "$old" 2>/dev/null
fi

start_server

(
    fails=0
    while sleep "$INTERVAL"; do
        [ -f "$DIR/server.py" ] || { note "server.py removed, watchdog exits"; exit 0; }
        [ -f "$OFF" ] && { fails=0; continue; }
        if healthy; then
            fails=0
        else
            fails=$((fails + 1))
            note "health check failed ($fails/$FAILS_BEFORE_RESTART)"
            if [ "$fails" -ge "$FAILS_BEFORE_RESTART" ]; then
                start_server
                fails=0
                sleep 10                      # give it time to come up before the next check
            fi
        fi
        # trim logs that live in RAM
        for f in "$LOG" "$SUPLOG"; do
            [ -f "$f" ] && [ "$(wc -c < "$f")" -gt 1048576 ] && tail -c 262144 "$f" > "$f.tmp" && mv -f "$f.tmp" "$f"
        done
    done
) </dev/null >/dev/null 2>&1 &
echo $! > "$SUPPID"
note "watchdog started (pid $!)"
exit 0
