#!/usr/bin/env bash
# N-Fizyo – Installation auf Ubuntu 24.04 (Praxis-Box im lokalen Netz oder Server in der Schweiz).
#
# Lokale KI gehört zum Produkt: Spracherkennung (Whisper), Notizen/Berichte (Ollama), Übersetzung (TranslateGemma).
# Voraussetzung: NVIDIA-Grafikkarte mit mindestens 16 GB Speicher (empfohlen 24 GB).
#
# Aufruf (aus dem entpackten Projektordner):
#   sudo bash deploy/install.sh                       # Praxis-Box mit KI, Adresse https://<hostname>.local
#   sudo bash deploy/install.sh --domain physio.example.ch   # Server in der Schweiz mit öffentlichem Namen (Let's Encrypt)
#   sudo bash deploy/install.sh --demo                # Demo-Praxis mit fiktiven Daten (nie mit echten Daten!)
#   sudo bash deploy/install.sh --ai-server https://ai.n-fizyo.ch   # kleine Box ohne Grafikkarte: Daten bleiben
#                                                     # in der Praxis, KI rechnet auf dem KI-Server in der Schweiz
#   sudo bash deploy/install.sh --demo --no-ai        # nur zum Testen ohne Grafikkarte (KI wird simuliert)
#
# Drei Betriebsarten (siehe docs/DONANIM.md):
#   1) Box mit Grafikkarte in der Praxis            → install.sh
#   2) Kleine Box in der Praxis + KI-Server Schweiz  → install.sh --ai-server <URL>   (KI-Server: install-ai-server.sh)
#   3) Alles auf einem Server in der Schweiz         → install.sh --domain <name> auf dem GPU-Server
#
# Das Skript kann mit DERSELBEN Version mehrmals laufen (z. B. nach dem Treiber-Neustart): Daten und .env bleiben.
# Ist schon eine andere Version installiert, bricht es ab: Updates nur mit deploy/update.sh (Sicherung vorher, Prüfung,
# automatische Rückkehr). Eine echte Praxis (NFIZYO_DEMO=0) wird nie auf --demo umgestellt.
# Geheimnisse (Schlüssel, Passwörter) werden NIE angezeigt oder geloggt.
set -euo pipefail

APP_USER=nfizyo
APP_DIR=/opt/n-fizyo
DATA_DIR=/var/lib/n-fizyo
ENV_DIR=/etc/n-fizyo
ENV_FILE=$ENV_DIR/n-fizyo.env
BACKUP_DIR=/var/backups/n-fizyo
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WITH_AI=1
DEMO=0
DOMAIN=""
AI_SERVER=""
MIN_VRAM_MB=15500   # 16-GB-Karten melden etwas weniger als 16384 MiB
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ai) WITH_AI=1 ;;            # Standard, nur noch für ältere Aufrufe
    --no-ai) WITH_AI=0 ;;
    --ai-server) AI_SERVER="${2:-}"; shift ;;
    --demo) DEMO=1 ;;
    --domain) DOMAIN="${2:-}"; shift ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || echo "Hinweis: getestet für Ubuntu 24.04 (gefunden: ${PRETTY_NAME:-?})."
if [[ $WITH_AI -eq 0 && $DEMO -eq 0 ]]; then
  echo "Ohne KI ist N-Fizyo nur zum Testen gedacht. Bitte mit --demo --no-ai aufrufen." >&2; exit 1
fi

# Bestehende Installation: nur dieselbe Version erneut einrichten – alles andere über update.sh (mit Sicherung)
NEW_VERSION="$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$SRC_DIR/nfizyo/__init__.py" | head -n 1)"
if [[ -f "$APP_DIR/app/nfizyo/__init__.py" ]]; then
  OLD_VERSION="$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$APP_DIR/app/nfizyo/__init__.py" | head -n 1)"
  if [[ "$OLD_VERSION" != "$NEW_VERSION" ]]; then
    echo "N-Fizyo ${OLD_VERSION:-?} ist auf dieser Box schon installiert (dieses Paket: $NEW_VERSION)." >&2
    echo "Updates bitte mit Sicherung und automatischer Rückkehr:  sudo bash deploy/update.sh" >&2
    exit 1
  fi
fi
if [[ $DEMO -eq 1 && -f "$ENV_FILE" ]] && grep -q '^NFIZYO_DEMO=0' "$ENV_FILE"; then
  echo "Diese Box ist eine echte Praxis (NFIZYO_DEMO=0) – sie wird nicht auf Demo umgestellt." >&2
  echo "Für eine Vorführung bitte eine eigene Box oder einen eigenen Datenordner verwenden." >&2
  exit 1
fi

if [[ -n "$AI_SERVER" ]]; then
  [[ "$AI_SERVER" == https://* ]] || { echo "--ai-server muss mit https:// beginnen." >&2; exit 1; }
  WITH_AI=2   # KI über den Server in der Schweiz, keine Grafikkarte nötig
fi

if [[ $WITH_AI -eq 1 ]]; then
  say "Grafikkarte prüfen"
  apt-get update -q && apt-get install -y -q pciutils ubuntu-drivers-common >/dev/null
  if ! lspci | grep -qi 'nvidia'; then
    echo "Keine NVIDIA-Grafikkarte gefunden. Die lokale KI braucht eine NVIDIA-Karte mit mindestens 16 GB." >&2
    echo "(Nur zum Testen: --demo --no-ai)" >&2
    exit 1
  fi
  if ! nvidia-smi >/dev/null 2>&1; then
    echo "NVIDIA-Treiber wird installiert …"
    ubuntu-drivers install
    echo
    echo "Treiber installiert. Bitte jetzt neu starten (sudo reboot) und danach dieses Skript nochmals ausführen."
    exit 0
  fi
  VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | sort -n | tail -1 | tr -d ' ')"
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  echo "Gefunden: $GPU_NAME, ${VRAM_MB} MiB"
  if [[ "${VRAM_MB:-0}" -lt $MIN_VRAM_MB ]]; then
    echo "Zu wenig Grafikspeicher (${VRAM_MB} MiB). Mindestens 16 GB nötig (Whisper + Sprachmodell + Übersetzung)." >&2
    exit 1
  fi
fi

say "Systempakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip rsync sqlite3 gnupg caddy ufw wireguard-tools iptables \
  tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-ita tesseract-ocr-eng poppler-utils ffmpeg

say "Uhrzeit (Zeitabgleich): Anmeldecodes, Rechnungen und Termine brauchen die genaue Zeit"
timedatectl set-ntp true >/dev/null 2>&1 || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]] && break
  sleep 3
done
if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == "yes" ]]; then
  echo "Uhr abgeglichen."
else
  echo "Warnung: Uhr noch nicht abgeglichen. Im Praxisnetz muss NTP (UDP 123) nach aussen erlaubt sein" >&2
  echo "         oder in /etc/systemd/timesyncd.conf ein interner Zeitserver eingetragen werden (NTP=…)." >&2
fi

say "Benutzer und Ordner"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR" "$DATA_DIR/scan-eingang"
install -d -o root -g "$APP_USER" -m 0750 "$ENV_DIR"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
install -d -m 0755 "$APP_DIR"

say "Programm kopieren (Versionsordner unter $APP_DIR/releases, $APP_DIR/app zeigt auf die aktuelle)"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
VERSION="$NEW_VERSION"
REL="$APP_DIR/releases/${VERSION}_${STAMP}"
install -d -m 0755 "$APP_DIR/releases"
if [[ -d "$APP_DIR/app" && ! -L "$APP_DIR/app" ]]; then mv "$APP_DIR/app" "$APP_DIR/releases/legacy_$STAMP"; fi
CUR_REL="$(readlink -f "$APP_DIR/app" 2>/dev/null || true)"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '/data/' --exclude '.env' --exclude 'tests/' "$SRC_DIR/" "$REL/"
[[ -n "$CUR_REL" && -d "$CUR_REL" ]] && ln -sfn "$CUR_REL" "$APP_DIR/previous"
ln -sfn "$REL" "$APP_DIR/app"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
# Feste Versionen mit Prüfsummen (requirements.lock, erzeugt mit tools/deps.sh lock) – auch bei erneutem Aufruf genau diese
"$APP_DIR/venv/bin/pip" install -q --require-hashes -r "$APP_DIR/app/requirements.lock"

say "Konfiguration ($ENV_FILE)"
if [[ ! -f "$ENV_FILE" ]]; then
  SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  HOST_NAME="${DOMAIN:-$(hostname -s).local}"
  {
    echo "# N-Fizyo – erzeugt von install.sh am $(date -I). Nur root und $APP_USER dürfen lesen."
    echo "NFIZYO_SECRET_KEY=$SECRET"
    echo "NFIZYO_DATA_DIR=$DATA_DIR"
    echo "NFIZYO_HOST=127.0.0.1"
    echo "NFIZYO_PORT=8080"
    echo "NFIZYO_BASE_URL=https://$HOST_NAME"
    echo "NFIZYO_DEMO=$DEMO"
    echo "NFIZYO_DEFAULT_LANG=de"
    echo "NFIZYO_STT=none"
    echo "NFIZYO_LLM=rules"
    echo "NFIZYO_TRANSLATE=none"
    echo "NFIZYO_OLLAMA_URL=http://127.0.0.1:11434"
    echo "NFIZYO_ALLOW_REMOTE_AI=0"
    echo "NFIZYO_DELETE_AUDIO_AFTER_APPROVAL=1"
    echo "NFIZYO_SMS=log"
    echo "NFIZYO_REQUIRE_2FA=$((1 - DEMO))"
    echo "NFIZYO_MEDIDATA=off"
    echo "NFIZYO_MAIL=log"
    echo "# SMS (ASPSMS/eCall), HIN, MediData: siehe docs/BAGLANTILAR.md · NSystem-Übersicht: deploy/connect-central.sh"
    echo "# Video (eigener Jitsi-Server): deploy/jitsi/install-jitsi.sh trägt NFIZYO_VIDEO_URL/_APP_ID/_SECRET ein – docs/VIDEO.md"
    echo "# Externe verschlüsselte Sicherung (optional, docs/YEDEK.md): NFIZYO_OFFSITE_TARGET, NFIZYO_OFFSITE_KEY_FILE, NFIZYO_OFFSITE_KEEP"
    echo "# Neue Versionen (signiert) und Supportzugang: deploy/connect-central.sh trägt NFIZYO_RELEASE_* / NFIZYO_SUPPORT_* ein – docs/MERKEZ.md"
  } > "$ENV_FILE"
  unset SECRET
else
  echo "Bestehende Konfiguration bleibt erhalten."
  sed -i "s/^NFIZYO_DEMO=.*/NFIZYO_DEMO=$DEMO/" "$ENV_FILE"
fi
chown root:"$APP_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

set_env() {  # set_env KEY VALUE – Zeile ersetzen oder anhängen
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}

CUDA_LIBS=""
if [[ $WITH_AI -eq 1 ]]; then
  say "Lokale KI: Spracherkennung (faster-whisper, GPU)"
  "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/app/requirements-ai.txt"
  # cuBLAS und cuDNN für CUDA 12 kommen als Python-Pakete mit; der Dienst bekommt ihren Pfad
  CUDA_LIBS="$("$APP_DIR/venv/bin/python" -c 'import os, nvidia.cublas.lib, nvidia.cudnn.lib; print(os.path.dirname(nvidia.cublas.lib.__file__) + ":" + os.path.dirname(nvidia.cudnn.lib.__file__))')"
  STT_MODEL="$(grep '^NFIZYO_STT_MODEL=' "$ENV_FILE" | cut -d= -f2 || true)"; STT_MODEL="${STT_MODEL:-large-v3-turbo}"
  set_env NFIZYO_STT faster_whisper; set_env NFIZYO_STT_DEVICE cuda; set_env NFIZYO_STT_MODEL "$STT_MODEL"
  echo "Whisper-Modell $STT_MODEL wird geladen (einmalig, danach offline) …"
  sudo -u "$APP_USER" env HOME="$DATA_DIR" LD_LIBRARY_PATH="$CUDA_LIBS" "$APP_DIR/venv/bin/python" -c \
    "from faster_whisper import WhisperModel; WhisperModel('$STT_MODEL', device='cuda', compute_type='float16')"

  say "Lokale KI: Ollama (nur auf 127.0.0.1)"
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  install -d /etc/systemd/system/ollama.service.d
  # Nur lokal erreichbar; Modelle bleiben geladen (keine Wartezeit am Morgen)
  printf '[Service]\nEnvironment="OLLAMA_HOST=127.0.0.1:11434"\nEnvironment="OLLAMA_KEEP_ALIVE=24h"\n' \
    > /etc/systemd/system/ollama.service.d/local-only.conf
  systemctl daemon-reload
  systemctl enable --now ollama
  systemctl restart ollama
  sleep 3
  LLM_MODEL="$(grep '^NFIZYO_LLM_MODEL=' "$ENV_FILE" | cut -d= -f2 || true)"; LLM_MODEL="${LLM_MODEL:-qwen2.5:7b-instruct}"
  TR_MODEL="$(grep '^NFIZYO_TRANSLATE_MODEL=' "$ENV_FILE" | cut -d= -f2 || true)"; TR_MODEL="${TR_MODEL:-translategemma:12b}"
  ollama pull "$LLM_MODEL"
  ollama pull "$TR_MODEL"
  set_env NFIZYO_LLM ollama; set_env NFIZYO_LLM_MODEL "$LLM_MODEL"
  set_env NFIZYO_TRANSLATE ollama; set_env NFIZYO_TRANSLATE_MODEL "$TR_MODEL"
fi

if [[ $WITH_AI -eq 2 ]]; then
  say "KI-Server: $AI_SERVER"
  set_env NFIZYO_AI_MODE remote
  set_env NFIZYO_AI_SERVER_URL "$AI_SERVER"
  if ! grep -q '^NFIZYO_AI_TOKEN=.' "$ENV_FILE"; then
    echo "Zugangs-Token der Praxis eingeben (von NSystem, wird nicht angezeigt):"
    read -rs AI_TOKEN
    [[ -n "$AI_TOKEN" ]] || { echo "Kein Token eingegeben." >&2; exit 1; }
    set_env NFIZYO_AI_TOKEN "$AI_TOKEN"
    unset AI_TOKEN
  fi
  if curl -fsS --max-time 10 "$AI_SERVER/health" >/dev/null; then echo "KI-Server erreichbar."; else echo "Warnung: KI-Server nicht erreichbar." >&2; fi
fi

say "Dienst (systemd)"
cat > /etc/systemd/system/n-fizyo.service <<UNIT
[Unit]
Description=N-Fizyo Praxissoftware
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/app
ExecStart=$APP_DIR/venv/bin/python -m nfizyo --env $ENV_FILE run
Environment=HOME=$DATA_DIR
Environment=TZ=Europe/Zurich
Environment=LD_LIBRARY_PATH=$CUDA_LIBS
Environment=HF_HUB_OFFLINE=1
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
UMask=0027

[Install]
WantedBy=multi-user.target
UNIT

say "Runner für NSystem-Befehle (root, nur signierte Befehle der festen Liste)"
# Ordner gehören der App (sie legt Aufträge ab und löscht gelesene Ergebnisse); der Runner (root) schreibt dort nur über
# nfizyo/agent/safefs.py – ohne Symlinks zu folgen.
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR/runner" "$DATA_DIR/runner/spool" "$DATA_DIR/runner/results" \
  "$DATA_DIR/runner/local" "$DATA_DIR/runner/events"
install -d -o root -g root -m 0700 /var/lib/n-fizyo-runner
cat > /etc/systemd/system/n-fizyo-runner.service <<UNIT
[Unit]
Description=N-Fizyo Runner (signierte NSystem-Befehle, Zustand von Sicherung und Geräten)
After=network-online.target

[Service]
User=root
WorkingDirectory=$APP_DIR/app
EnvironmentFile=$ENV_FILE
Environment=NFIZYO_ENV_FILE=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python -m nfizyo.agent.runner --env $ENV_FILE
Restart=always
RestartSec=10
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

say "Supportzugang vorbereiten (bleibt geschlossen – nur die Leitung öffnet ihn, docs/MERKEZ.md)"
bash "$SRC_DIR/deploy/support-setup.sh" || echo "Hinweis: Supportzugang nicht vorbereitet (später: sudo bash deploy/support-setup.sh)." >&2

say "Tägliche Sicherung ($BACKUP_DIR, 30 Tage; externe verschlüsselte Kopie optional – docs/YEDEK.md)"
install -m 0700 "$SRC_DIR/deploy/n-fizyo-backup" /usr/local/sbin/n-fizyo-backup
cat > /etc/systemd/system/n-fizyo-backup.service <<'UNIT'
[Unit]
Description=N-Fizyo Sicherung
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/n-fizyo-backup
UNIT
cat > /etc/systemd/system/n-fizyo-backup.timer <<'UNIT'
[Unit]
Description=N-Fizyo Sicherung täglich
[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT

say "Webserver (Caddy, HTTPS)"
if [[ -n "$DOMAIN" ]]; then
  SITE="$DOMAIN"; TLS=""
else
  SITE="$(hostname -s).local, localhost"; TLS="tls internal"
fi
cat > /etc/caddy/Caddyfile <<CADDY
$SITE {
	$TLS
	encode zstd gzip
	request_body {
		max_size 60MB
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		Referrer-Policy same-origin
		Permissions-Policy "camera=(), geolocation=(), microphone=(self)"
		-Server
	}
	reverse_proxy 127.0.0.1:8080
}
CADDY

say "Firewall"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

say "Starten"
systemctl daemon-reload
systemctl enable --now n-fizyo.service n-fizyo-backup.timer n-fizyo-runner.service
systemctl restart n-fizyo.service n-fizyo-runner.service
systemctl reload caddy || systemctl restart caddy
sleep 2
if curl -fsS http://127.0.0.1:8080/health >/dev/null; then echo "N-Fizyo läuft."; else echo "N-Fizyo antwortet nicht – siehe: journalctl -u n-fizyo -n 50" >&2; fi
(cd "$APP_DIR/app" && sudo -u "$APP_USER" env HOME="$DATA_DIR" LD_LIBRARY_PATH="$CUDA_LIBS" "$APP_DIR/venv/bin/python" -m nfizyo --env "$ENV_FILE" check-ai) \
  || echo "Hinweis: KI-Prüfung meldet Probleme (siehe oben)." >&2

URL="$(grep '^NFIZYO_BASE_URL=' "$ENV_FILE" | cut -d= -f2)"
cat <<DONE

Fertig. Adresse: $URL

Nächste Schritte (echte Praxis) – die Befehle laufen im Programmordner $APP_DIR/app:
  1) Praxis anlegen:
     cd $APP_DIR/app && sudo -u $APP_USER $APP_DIR/venv/bin/python -m nfizyo --env $ENV_FILE create-practice --name "Physio Muster" --slug muster
  2) Leitung anlegen (Passwort wird abgefragt, nicht angezeigt):
     cd $APP_DIR/app && sudo -u $APP_USER $APP_DIR/venv/bin/python -m nfizyo --env $ENV_FILE create-user --email leitung@praxis.ch --name "Vorname Name" --roles owner,therapist --practice muster
  3) Scanner/Netzwerkordner auf $DATA_DIR/scan-eingang/muster/ zeigen lassen.
  4) Updates später: neue Version entpacken, im entpackten Ordner  sudo bash deploy/update.sh  (mit Sicherung und Rückkehr)
     oder aus der NSystem-Übersicht (signierter Befehl «Neue Version»). Zurück: sudo bash $APP_DIR/app/deploy/update.sh --rollback
  4b) Mit der NSystem-Übersicht verbinden (Datei von NSystem, enthält das Gerätegeheimnis):
     sudo bash $APP_DIR/app/deploy/connect-central.sh /pfad/box-bx_….env
     Supportzugang (optional): öffentlichen Schlüssel an NSystem geben (sudo bash $APP_DIR/app/deploy/support-setup.sh --show-key),
     Datei support-bx_….env von NSystem ebenso mit connect-central.sh einspielen. Öffnen kann ihn nur die Leitung.
  5) Bei «tls internal»: das lokale Zertifikat von Caddy auf den Praxisgeräten als vertrauenswürdig installieren
     (Datei: /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt).
DONE
