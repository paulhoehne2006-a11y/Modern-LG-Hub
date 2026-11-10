#!/bin/bash
# One-shot Luna bus call on the TV; prints the JSON reply.
# Usage:  tools/luna.sh luna://service/method '{"json":"payload"}'
#         tools/luna.sh luna://com.webos.applicationManager/getForegroundAppInfo
# The payload travels base64-encoded, so any JSON quoting is safe.
TV="${TV_HOST:-root@192.168.178.60}"
uri="$1"
payload="$2"
[ -n "$uri" ] || { echo "usage: luna.sh luna://service/method ['{json}']" >&2; exit 2; }
[ -n "$payload" ] || payload='{}'
b64=$(printf '%s' "$payload" | base64 | tr -d '\n')
ssh -tt -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR "$TV" \
  "timeout 15 luna-send -n 1 '$uri' \"\$(echo $b64 | base64 -d)\"" </dev/null | tr -d '\r'
exit "${PIPESTATUS[0]}"
