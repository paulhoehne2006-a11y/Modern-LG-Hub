#!/bin/sh
# Asks for the TP-Link (Tapo) login and stores it in .env for the local KLAP control.
# The password is not echoed. Run from the PC (works in cmd.exe and PowerShell too):
#   ssh -t root@192.168.178.60 sh /var/lib/webosbrew/gemini-controller/set-tapo.sh
DIR=/var/lib/webosbrew/gemini-controller
F=$DIR/.env
printf "Tapo E-Mail: "
read -r e
printf "Tapo Passwort: "
stty -echo
read -r p
stty echo
echo
if [ -z "$e" ] || [ -z "$p" ]; then
    echo "Abgebrochen: E-Mail oder Passwort leer, nichts geaendert."
    exit 1
fi
touch "$F"
chmod 600 "$F"
sed -i '/^TAPO_EMAIL=/d;/^TAPO_PASSWORD=/d' "$F"
printf 'TAPO_EMAIL=%s\nTAPO_PASSWORD=%s\n' "$e" "$p" >> "$F"
echo "Gespeichert. Teste die Steckdosen ..."
cd "$DIR" && python3 -B localhome.py status
