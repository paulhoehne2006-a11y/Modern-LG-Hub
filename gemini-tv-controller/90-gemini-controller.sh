#!/bin/sh
# Autostart Gemini TV Controller
DIR=/var/lib/webosbrew/gemini-controller
if ! command -v python3 >/dev/null 2>&1; then
    echo "$(date): python3 not found in PATH ($PATH)" > /tmp/gemini-server.log
    exit 0
fi
if [ -f "$DIR/server.py" ]; then
    for p in /proc/[0-9]*; do
        c=$(tr "\0" " " < $p/cmdline 2>/dev/null)
        case "$c" in *"$DIR/server.py"*) kill ${p#/proc/} 2>/dev/null;; esac
    done
    nohup python3 "$DIR/server.py" > /tmp/gemini-server.log 2>&1 &
fi
