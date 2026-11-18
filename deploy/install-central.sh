#!/usr/bin/env bash
# NSystem-Übersicht – Installation auf einem Server von NSystem in der Schweiz (Ubuntu 24.04).
# Kann auf demselben Server wie der KI-Server laufen (eigener Dienst, eigener Name, eigener Port).
#
# Aufruf (aus dem entpackten Projektordner):
#   sudo bash deploy/install-central.sh --domain panel.nsystem.ch [--ntfy https://ntfy.sh/<geheimes-thema>]
#        [--releases-domain releases.nsystem.ch]     # Release-Ablage /srv/n-fizyo-releases statisch über HTTPS ausliefern
#
# Richtet auch die nächtliche Sicherung des Panels ein (Datenbank + Schlüssel, 03:15; verschlüsselte externe Kopie wie bei
# den Boxen, sobald NFIZYO_OFFSITE_* in central.env stehen – docs/MERKEZ.md › «Panel yedeği»).
# Support-Hub (WireGuard) separat: sudo bash deploy/install-support-hub.sh
#
# Danach:
#   cd /opt/n-fizyo-central/app && sudo -u nfizyo-central /opt/n-fizyo-central/venv/bin/python -m nfizyo.central --env /etc/n-fizyo-central/central.env add-staff --email … --name …
#   cd /opt/n-fizyo-central/app && sudo -u nfizyo-central /opt/n-fizyo-central/venv/bin/python -m nfizyo.central --env /etc/n-fizyo-central/central.env add-box --name "Physio …"
# Geheimnisse werden nie angezeigt: Schlüssel liegen nur in /etc/n-fizyo-central/ und /var/lib/n-fizyo-central/ (0600).
set -euo pipefail

APP_USER=nfizyo-central
APP_DIR=/opt/n-fizyo-central
DATA_DIR=/var/lib/n-fizyo-central
ENV_DIR=/etc/n-fizyo-central
ENV_FILE=$ENV_DIR/central.env
PORT=8095
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN=""
NTFY=""
RELEASES_DOMAIN=""
RELEASE_DIR=/srv/n-fizyo-releases
BACKUP_DIR=/var/backups/n-fizyo-central
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift ;;
    --ntfy) NTFY="${2:-}"; shift ;;
    --releases-domain) RELEASES_DOMAIN="${2:-}"; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done
[[ -n "$DOMAIN" ]] || { echo "Bitte --domain angeben (z. B. panel.nsystem.ch)." >&2; exit 1; }
[[ -z "$NTFY" || "$NTFY" == https://* ]] || { echo "--ntfy muss mit https:// beginnen." >&2; exit 1; }
say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }

say "Systempakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip rsync sqlite3 caddy ufw curl gnupg

say "Benutzer, Ordner, Programm"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$DATA_DIR"
install -d -o root -g "$APP_USER" -m 0750 "$ENV_DIR"
install -d -m 0755 "$APP_DIR"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '/data/' --exclude '.env' --exclude 'tests/' "$SRC_DIR/" "$APP_DIR/app/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q --require-hashes -r "$APP_DIR/app/requirements.lock"   # feste Versionen mit Prüfsummen

say "Konfiguration ($ENV_FILE)"
if [[ ! -f "$ENV_FILE" ]]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  {
    echo "# NSystem-Übersicht – erzeugt am $(date -I). Nur root und $APP_USER dürfen lesen."
    echo "NFIZYO_CENTRAL_SECRET_KEY=$SECRET"
    echo "NFIZYO_CENTRAL_DATA_DIR=$DATA_DIR"
    echo "NFIZYO_CENTRAL_HOST=127.0.0.1"
    echo "NFIZYO_CENTRAL_PORT=$PORT"
    echo "NFIZYO_CENTRAL_BASE_URL=https://$DOMAIN"
    echo "NFIZYO_CENTRAL_NTFY_URL=$NTFY"
    echo "NFIZYO_CENTRAL_SILENCE_MINUTES=30"
    echo "# Releases (tools/release.py, docs/MERKEZ.md): Ablage und öffentlicher Release-Schlüssel (nicht geheim)"
    echo "NFIZYO_CENTRAL_RELEASE_DIR=${RELEASES_DOMAIN:+$RELEASE_DIR}"
    echo "NFIZYO_CENTRAL_RELEASE_BASE=${RELEASES_DOMAIN:+https://$RELEASES_DOMAIN}"
    echo "NFIZYO_CENTRAL_RELEASE_PUBKEY="
    echo "# Supportzugang (deploy/install-support-hub.sh): Hub-Adresse, öffentlicher Hub-Schlüssel, SSH-Schlüssel von NSystem (öffentlich)"
    echo "NFIZYO_CENTRAL_SUPPORT_ENDPOINT="
    echo "NFIZYO_CENTRAL_SUPPORT_HUB_PUBKEY="
    echo "NFIZYO_CENTRAL_SUPPORT_NET=10.77.0.0/24"
    echo "NFIZYO_CENTRAL_SUPPORT_SSH_KEYS="
    echo "NFIZYO_CENTRAL_SUPPORT_SSH_HOST="
    echo "# Externe verschlüsselte Sicherung des Panels (wie docs/YEDEK.md, eigener Ordner am Ziel!)"
    echo "NFIZYO_OFFSITE_TARGET="
    echo "NFIZYO_OFFSITE_KEY_FILE=/etc/n-fizyo-central/offsite.key"
    echo "NFIZYO_OFFSITE_KEEP=14"
  } > "$ENV_FILE"
  unset SECRET
else
  echo "Bestehende Konfiguration bleibt erhalten."
  [[ -n "$NTFY" ]] && sed -i "s|^NFIZYO_CENTRAL_NTFY_URL=.*|NFIZYO_CENTRAL_NTFY_URL=$NTFY|" "$ENV_FILE"
fi
chown root:"$APP_USER" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
echo "Optional: NFIZYO_CENTRAL_NTFY_TOKEN=… von Hand in $ENV_FILE eintragen (eigener ntfy-Server mit Zugang)."

say "Signaturschlüssel für Befehle"
cd "$APP_DIR/app"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -m nfizyo.central --env "$ENV_FILE" init

say "Dienst (systemd)"
cat > /etc/systemd/system/n-fizyo-central.service <<UNIT
[Unit]
Description=NSystem-Übersicht (N-Fizyo)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/app
ExecStart=$APP_DIR/venv/bin/python -m nfizyo.central --env $ENV_FILE run
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

say "Nächtliche Sicherung des Panels ($BACKUP_DIR; Datenbank + Schlüssel; extern verschlüsselt, wenn eingerichtet)"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
install -m 0700 "$SRC_DIR/deploy/n-fizyo-backup" /usr/local/sbin/n-fizyo-backup
cat > /etc/systemd/system/n-fizyo-central-backup.service <<UNIT
[Unit]
Description=NSystem-Übersicht: Sicherung (Datenbank + Schlüssel)
[Service]
Type=oneshot
Environment=NFIZYO_DATA=$DATA_DIR
Environment=NFIZYO_BACKUP_DIR=$BACKUP_DIR
Environment=NFIZYO_ENV_FILE=$ENV_FILE
Environment=NFIZYO_BACKUP_STATUS_FILE=$DATA_DIR/backup-status.json
Environment=NFIZYO_BACKUP_STATUS_OWNER=$APP_USER
Environment="NFIZYO_BACKUP_FILES=$ENV_FILE $DATA_DIR/command-signing.pem /etc/wireguard/nfz-hub.key /etc/wireguard/nfz-hub.conf"
ExecStart=/usr/local/sbin/n-fizyo-backup
UNIT
cat > /etc/systemd/system/n-fizyo-central-backup.timer <<'UNIT'
[Unit]
Description=NSystem-Übersicht: Sicherung täglich
[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT

if [[ -n "$RELEASES_DOMAIN" ]]; then
  install -d -o root -g root -m 0755 "$RELEASE_DIR"
fi

say "HTTPS (Caddy) – eigener Abschnitt, andere Seiten (z. B. KI-Server) bleiben"
touch /etc/caddy/Caddyfile
python3 - "$DOMAIN" "$PORT" "$RELEASES_DOMAIN" "$RELEASE_DIR" <<'PY'
import re, sys
domain, port, rel_domain, rel_dir = sys.argv[1:]
path = "/etc/caddy/Caddyfile"
text = open(path, encoding="utf-8").read()
block = f"""# n-fizyo-central BEGIN
{domain} {{
	request_body {{
		max_size 1MB
	}}
	header {{
		Strict-Transport-Security "max-age=31536000"
		X-Frame-Options DENY
		X-Content-Type-Options nosniff
		-Server
	}}
	reverse_proxy 127.0.0.1:{port}
}}
# n-fizyo-central END
"""
if rel_domain:
    # Release-Ablage: nur Dateien ausliefern (keine Verzeichnisliste), Signatur prüfen Panel und Box selbst
    block += f"""# n-fizyo-releases BEGIN
{rel_domain} {{
	root * {rel_dir}
	file_server
	header {{
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		Cache-Control "no-cache"
		-Server
	}}
}}
# n-fizyo-releases END
"""
text = re.sub(r"# n-fizyo-central BEGIN.*?# n-fizyo-central END\n?", "", text, flags=re.S)
text = re.sub(r"# n-fizyo-releases BEGIN.*?# n-fizyo-releases END\n?", "", text, flags=re.S)
open(path, "w", encoding="utf-8").write(text.rstrip() + "\n\n" + block)
PY

ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null
systemctl daemon-reload
systemctl enable --now n-fizyo-central.service n-fizyo-central-backup.timer
systemctl restart n-fizyo-central.service
systemctl reload caddy || systemctl restart caddy
sleep 2
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null && echo "Übersicht läuft: https://$DOMAIN" || echo "Antwortet nicht – journalctl -u n-fizyo-central -n 50" >&2

cat <<DONE

Nächste Schritte:
  1) Konto für NSystem-Personal (Passwort wird abgefragt; beim ersten Anmelden Zwei-Schritt-Anmeldung einrichten):
     cd $APP_DIR/app && sudo -u $APP_USER $APP_DIR/venv/bin/python -m nfizyo.central --env $ENV_FILE add-staff --email name@nsystem.ch --name "Vorname Name"
  2) Pro Praxis-Box:
     cd $APP_DIR/app && sudo -u $APP_USER $APP_DIR/venv/bin/python -m nfizyo.central --env $ENV_FILE add-box --name "Physio Muster"
     → Datei box-bx_….env (nur root/Dienst lesbar) auf die Box kopieren und dort: sudo bash deploy/connect-central.sh <Datei>
  3) Push aufs Telefon: App «ntfy» installieren und das Thema aus NFIZYO_CENTRAL_NTFY_URL abonnieren.
  4) Sicherung des Panels: sudo NFIZYO_ENV_FILE=$ENV_FILE n-fizyo-backup --offsite-keygen (Schlüssel auf Papier!),
     NFIZYO_OFFSITE_TARGET in $ENV_FILE, dann sudo systemctl start n-fizyo-central-backup (docs/MERKEZ.md).
  5) Releases: tools/release.py keygen auf dem Build-Rechner, öffentlichen Schlüssel als NFIZYO_CENTRAL_RELEASE_PUBKEY eintragen.
  6) Supportzugang: sudo bash deploy/install-support-hub.sh
DONE
