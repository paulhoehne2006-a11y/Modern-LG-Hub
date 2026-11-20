#!/bin/bash
# Run ONE shell command line on the TV *with a TTY*.
# Why: on this TV luna-send prints its replies only when stdout is a terminal
# (without a TTY it exits 0 and prints nothing).
#
# Usage:  tools/tv.sh 'luna-send -n 1 luna://com.webos.applicationManager/getForegroundAppInfo "{}"'
#         tools/tv.sh 'cat /etc/hosts'
# Output: remote stdout+stderr merged (TTY), CRs stripped. Exit code = remote exit code.
TV="${TV_HOST:?TV_HOST setzen, z. B. export TV_HOST=root@<TV-IP>}"
[ $# -eq 1 ] || { echo "usage: tv.sh '<remote command line>'" >&2; exit 2; }
ssh -tt -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR "$TV" "$1" </dev/null | tr -d '\r'
exit "${PIPESTATUS[0]}"
