#!/bin/sh
# Autostart Gemini TV Controller
DIR=/var/lib/webosbrew/gemini-controller
if [ -f "$DIR/server.py" ]; then
    for p in /proc/[0-9]*; do
        c=$(tr "\0" " " < $p/cmdline 2>/dev/null)
        case "$c" in *server.py*) kill ${p#/proc/} 2>/dev/null;; esac
    done
    nohup python3 "$DIR/server.py" > /tmp/gemini-server.log 2>&1 &
fi
