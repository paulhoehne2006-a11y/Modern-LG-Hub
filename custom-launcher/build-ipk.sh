#!/bin/sh
# Packages ./com.custom.launcher into an .ipk that webOS's appInstallService accepts.
# Usage: sh build-ipk.sh   -> writes com.custom.launcher_<version>_all.ipk
set -e
cd "$(dirname "$0")"
APP=com.custom.launcher
command -v node >/dev/null 2>&1 || { echo "build-ipk.sh: node is required" >&2; exit 1; }
VER=$(node -p "require('./$APP/appinfo.json').version")
case "$VER" in
  ''|*[!0-9A-Za-z.+~-]*) echo "build-ipk.sh: bad version '$VER' in appinfo.json" >&2; exit 1 ;;
esac
OUT="${APP}_${VER}_all.ipk"
B=$(mktemp -d)
trap 'rm -rf "$B"' EXIT          # no temp dirs left behind when a step fails

mkdir -p "$B/data/usr/palm/applications/$APP" "$B/control"
cp -r "$APP"/. "$B/data/usr/palm/applications/$APP/"
# generated on the TV by launcherd.py / v1 leftovers: never ship them
rm -rf "$B/data/usr/palm/applications/$APP/data" "$B/data/usr/palm/applications/$APP/apps.json" "$B/data/usr/palm/applications/$APP/refresh-apps.py" "$B/data/usr/palm/applications/$APP/home-watch.py" "$B/data/usr/palm/applications/$APP/__pycache__"
find "$B/data" \( -name '*.pyc' -o -name '*.tmp' \) -exec rm -f {} +
cat > "$B/control/control" <<EOF
Package: $APP
Version: $VER
Section: misc
Priority: optional
Architecture: all
Installed-Size: $(du -sk "$B/data" | cut -f1)
Maintainer: local
Description: OLED custom launcher
webOS-Package-Format-Version: 2
webOS-Packager-Version: x.y.x
EOF
printf '2.0\n' > "$B/debian-binary"

(cd "$B/control" && tar --owner=0 --group=0 -czf ../control.tar.gz ./control)
(cd "$B/data" && tar --owner=0 --group=0 -czf ../data.tar.gz ./usr)

# Minimal "ar" archive writer (no binutils needed).
node -e '
const fs=require("fs"),path=require("path");
const [out,...files]=process.argv.slice(1);
const parts=[Buffer.from("!<arch>\n")];
for(const f of files){
  const d=fs.readFileSync(f);
  const h=(path.basename(f)+"/").padEnd(16)+"0".padEnd(12)+"0".padEnd(6)+"0".padEnd(6)+"100644".padEnd(8)+String(d.length).padEnd(10)+"`\n";
  parts.push(Buffer.from(h),d); if(d.length%2) parts.push(Buffer.from("\n"));
}
fs.writeFileSync(out,Buffer.concat(parts));
' "$OUT" "$B/debian-binary" "$B/control.tar.gz" "$B/data.tar.gz"

echo "$OUT"
