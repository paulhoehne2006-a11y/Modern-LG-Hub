#!/bin/bash
# Screenshot of what the TV is displaying right now (UI plane, 1920x1080 PNG).
# Usage: tools/tvshot.sh out.png
TV="${TV_HOST:-root@192.168.178.60}"
out="$1"
[ -n "$out" ] || { echo "usage: tvshot.sh out.png" >&2; exit 2; }
remote="/tmp/tvshot-$$-$RANDOM.png"
"$(dirname "$0")/luna.sh" luna://com.webos.surfacemanager/captureCompositorOutput \
  "{\"output\":\"$remote\",\"format\":\"PNG\"}" >/dev/null || { echo "capture failed" >&2; exit 1; }
scp -q -o BatchMode=yes -o ConnectTimeout=8 "$TV:$remote" "$out"; rc=$?
ssh -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR "$TV" "rm -f '$remote'"
exit $rc
