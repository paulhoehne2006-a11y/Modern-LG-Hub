#!/bin/bash
# Build the launcher package, install it on the TV, set up the boot hook and (re)start the root helper.
# Usage: tools/deploy.sh [--no-show]     (--no-show: do not bring the launcher to the front afterwards)
# Aborts before touching the running system if anything on the way fails.
set -e
cd "$(dirname "$0")/.."
TV="${TV_HOST:-root@192.168.178.60}"
APP=/media/developer/apps/usr/palm/applications/com.custom.launcher
HOOK=/var/lib/webosbrew/init.d/99-custom-launcher
SSH="ssh -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR"

# 0. local checks
sh -n build-ipk.sh
sh -n boot/99-custom-launcher
node --check com.custom.launcher/app.js
IPK=$(sh build-ipk.sh)
echo "built $IPK"
scp -q -o BatchMode=yes "$IPK" "$TV:/tmp/custom-launcher.ipk"
scp -q -o BatchMode=yes boot/99-custom-launcher "$TV:/tmp/99-custom-launcher.new"
scp -q -o BatchMode=yes com.custom.launcher/launcherd.py "$TV:/tmp/launcherd.check.py"

# 1. verify on the TV (python syntax, hook syntax) before anything is replaced
$SSH "$TV" 'python3 -c "import ast; ast.parse(open(\"/tmp/launcherd.check.py\").read())" && sh -n /tmp/99-custom-launcher.new && rm -f /tmp/launcherd.check.py' \
  || { echo "ABORT: syntax check failed on the TV, nothing was changed"; exit 1; }

# 2. stop the old helper (by PID, never by pattern match against our own shell)
#    (the deploying flag keeps the boot hook's supervisor from restarting the old helper meanwhile)
$SSH "$TV" '
  touch /tmp/custom-launcher.deploying
  [ -f '"$APP"'/launcherd.py ] && python3 '"$APP"'/launcherd.py stop >/dev/null 2>&1
  for p in /proc/[0-9]*; do
    c=$(tr "\0" " " < $p/cmdline 2>/dev/null)
    case "$c" in python3\ *home-watch.py*|python3\ *launcherd.py\ start*|python3\ *launcherd.py\ run*) kill ${p#/proc/} 2>/dev/null;; esac
  done; true'

# 3. install through the system installer; must report success
OUT=$(tools/tv.sh 'timeout 60 luna-send -i luna://com.webos.appInstallService/dev/install "{\"id\":\"com.custom.launcher\",\"ipkUrl\":\"/tmp/custom-launcher.ipk\",\"subscribe\":true}" | while read l; do case "$l" in *"\"statusValue\":30"*) echo "INSTALLED"; break;; *"returnValue\":false"*|*FAILED*|*errorText*) echo "INSTALL FAILED: $l" | cut -c1-300; break;; esac; done' || true)
echo "$OUT" | grep -v Terminated || true
if ! echo "$OUT" | grep -q INSTALLED; then
  echo "ABORT: install did not report success. Trying to restart the previous helper."
  $SSH "$TV" 'rm -f /tmp/custom-launcher.deploying; [ -f '"$APP"'/launcherd.py ] && python3 '"$APP"'/launcherd.py start; true'
  exit 1
fi

# 4. data, boot hook (copied under a temp name, then moved: never half-written), helper
$SSH "$TV" '
  set -e
  [ -f '"$APP"'/launcherd.py ] || { echo "app files missing after install"; exit 1; }
  n=0; until python3 '"$APP"'/launcherd.py refresh; do n=$((n+1)); [ $n -ge 3 ] && { echo "refresh failed"; exit 1; }; sleep 3; done
  mkdir -p /var/lib/webosbrew/backup-custom-launcher
  if [ -f /var/lib/webosbrew/init.d/99-autostart ]; then
    mv /var/lib/webosbrew/init.d/99-autostart /var/lib/webosbrew/backup-custom-launcher/99-autostart.v1
  fi
  chmod 755 /tmp/99-custom-launcher.new
  mv /tmp/99-custom-launcher.new '"$HOOK"'
  rm -f /tmp/custom-launcher.ipk
  python3 '"$APP"'/launcherd.py start
  rm -f /tmp/custom-launcher.deploying
  sleep 1
  python3 '"$APP"'/launcherd.py status'

# 5. load the new code
if [ "$1" != "--no-show" ]; then
  tools/luna.sh luna://com.webos.applicationManager/closeByAppId '{"id":"com.custom.launcher"}' >/dev/null || true
  $SSH "$TV" 'sleep 1'
  tools/luna.sh luna://com.webos.applicationManager/launch '{"id":"com.custom.launcher"}' >/dev/null
  echo "launcher restarted"
fi
