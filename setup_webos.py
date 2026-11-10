import subprocess
import sys

TV_IP = "192.168.178.60"
SSH_PORT = "22"  # Standard-Port des webosbrew SSH-Servers; falls inaktiv, auf 22 ändern
SSH_USER = "root"

# Hier die gewünschte App-ID eintragen (z. B. youtube.leanback.v4)
DEFAULT_AUTOSTART_APP = "youtube.leanback.v4"


def run_ssh_command(cmd: str) -> subprocess.CompletedProcess:
    ssh_cmd = [
        "ssh",
        "-p",
        SSH_PORT,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        f"{SSH_USER}@{TV_IP}",
        cmd,
    ]
    return subprocess.run(ssh_cmd, capture_output=True, text=True)


def main():
    print(f"[*] Verbinde mit LG TV unter {TV_IP}:{SSH_PORT}...")

    # 1. Verbindungstest
    test = run_ssh_command("uname -a")
    if test.returncode != 0:
        print("[-] Verbindung fehlgeschlagen.")
        print(f"Fehler: {test.stderr.strip()}")
        print("Hinweis: Prüfe, ob der SSH-Server im Homebrew Channel aktiv ist.")
        sys.exit(1)

    print(f"[+] Verbindung erfolgreich: {test.stdout.strip()}")

    # 2. Telemetrie in /etc/hosts sperren
    print("[*] Konfiguriere Telemetrie-Sperre in /etc/hosts...")
    hosts_block = (
        "grep -q 'lgsmartad.com' /etc/hosts || cat << 'EOF' >> /etc/hosts\n"
        "127.0.0.1 lgsmartad.com\n"
        "127.0.0.1 lgtvsdp.com\n"
        "127.0.0.1 adsmeasurement.com\n"
        "127.0.0.1 ibs.lgappstv.com\n"
        "127.0.0.1 lgad.cjpowercast.com\n"
        "EOF"
    )
    res_hosts = run_ssh_command(hosts_block)
    if res_hosts.returncode == 0:
        print("[+] /etc/hosts erfolgreich aktualisiert.")
    else:
        print(f"[-] Fehler beim Aktualisieren der hosts-Datei: {res_hosts.stderr.strip()}")

    # 3. Autostart-Skript anlegen (in init.d, um das webosbrew startup.sh nicht zu überschreiben)
    print(f"[*] Richte Autostart für '{DEFAULT_AUTOSTART_APP}' ein...")
    startup_script = (
        "mkdir -p /var/lib/webosbrew/init.d && "
        "cat << 'EOF' > /var/lib/webosbrew/init.d/99-autostart\n"
        "#!/bin/sh\n"
        "sleep 5\n"
        f"luna-send -n 1 luna://com.webos.applicationManager/launch '{{\"id\":\"{DEFAULT_AUTOSTART_APP}\"}}'\n"
        "EOF\n"
        "chmod +x /var/lib/webosbrew/init.d/99-autostart"
    )
    res_startup = run_ssh_command(startup_script)
    if res_startup.returncode == 0:
        print("[+] Autostart-Skript unter /var/lib/webosbrew/init.d/99-autostart angelegt.")
    else:
        print(f"[-] Fehler beim Anlegen des Autostart-Skripts: {res_startup.stderr.strip()}")

    # 4. Liste der installierten Apps abrufen
    print("[*] Installierte Apps auf dem Fernseher:")
    apps_cmd = "luna-send -n 1 luna://com.webos.applicationManager/listApps '{}' | grep -o '\"id\":\"[^\"]*\"'"
    res_apps = run_ssh_command(apps_cmd)
    if res_apps.returncode == 0:
        print(res_apps.stdout.strip())
    else:
        print(f"[-] Konnte App-Liste nicht abfragen: {res_apps.stderr.strip()}")

    print("[+] Fertig. Starte den TV neu, um das Verhalten zu testen.")


if __name__ == "__main__":
    main()