#!/bin/bash
# Simulate a remote-control key press on the TV.
# Usage: tools/key.sh HOME | UP | DOWN | LEFT | RIGHT | ENTER | BACK | ...
[ -n "$1" ] || { echo "usage: key.sh <KEYNAME>" >&2; exit 2; }
"$(dirname "$0")/luna.sh" luna://com.webos.service.networkinput/sendSpecialKey "{\"key\":\"$1\"}"
