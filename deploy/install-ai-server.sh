#!/usr/bin/env bash
# N-Fizyo KI-Server – Installation auf einem GPU-Server in einem Schweizer Rechenzentrum (Ubuntu 24.04).
#
# Für Praxen mit kleiner Box ohne Grafikkarte (Betriebsart 2): Patientendaten bleiben in der Praxis,
# nur die einzelne Aufgabe (Audio, Text) kommt per HTTPS hierher. Der Server speichert keine Inhalte.
#
# Aufruf (aus dem entpackten Projektordner):
#   sudo bash deploy/install-ai-server.sh --domain ai.n-fizyo.ch
#
# Danach pro Praxis einen Zugang anlegen (Token wird einmal angezeigt, direkt in die Box eintragen):
#   cd /opt/n-fizyo-ai/app && sudo -u nfizyo-ai /opt/n-fizyo-ai/venv/bin/python -m nfizyo.aiserver --env /etc/n-fizyo-ai/ai.env add-client --name "Physio Muster"
# Caddy: nur der Abschnitt «# n-fizyo-ai BEGIN/END» wird geschrieben – NSystem-Übersicht und Release-Ablage auf demselben
# Server (install-central.sh) bleiben erhalten. Erneut ausführen = Update des KI-Servers.
set -euo pipefail

APP_USER=nfizyo-ai
APP_DIR=/opt/n-fizyo-ai
DATA_DIR=/var/lib/n-fizyo-ai
ENV_DIR=/etc/n-fizyo-ai
ENV_FILE=$ENV_DIR/ai.env
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIN_VRAM_MB=22000   # Server für mehrere Praxen: mindestens 24 GB

DOMAIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done
[[ -n "$DOMAIN" ]] || { echo "Bitte --domain angeben (z. B. ai.n-fizyo.ch)." >&2; exit 1; }

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }

say "Systempakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip rsync sqlite3 caddy ufw ffmpeg pciutils ubuntu-drivers-common curl

say "Grafikkarte prüfen"
lspci | grep -qi nvidia || { echo "Keine NVIDIA-Grafikkarte gefunden." >&2; exit 1; }
if ! nvidia-smi >/dev/null 2>&1; then
  ubuntu-drivers install
  echo "Treiber installiert. Bitte neu starten (sudo reboot) und dieses Skript nochmals ausführen."
  exit 0
fi
VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | sort -n | tail -1 | tr -d ' ')"
echo "Gefunden: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1), ${VRAM_MB} MiB"
[[ "${VRAM_MB:-0}" -ge $MIN_VRAM_MB ]] || { echo "Für mehrere Praxen mindestens 24 GB Grafikspeicher nötig." >&2; exit 1; }

say "Benutzer, Ordner, Programm"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR" "$DATA_DIR/tmp"
install -d -o root -g "$APP_USER" -m 0750 "$ENV_DIR"
install -d -m 0755 "$APP_DIR"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '/data/' --exclude '.env' --exclude 'tests/' "$SRC_DIR/" "$APP_DIR/app/"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q --require-hashes -r "$APP_DIR/app/requirements.lock"   # feste Versionen mit Prüfsummen
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/app/requirements-ai.txt"
CUDA_LIBS="$("$APP_DIR/venv/bin/python" -c 'import os, nvidia.cublas.lib, nvidia.cudnn.lib; print(os.path.dirname(nvidia.cublas.lib.__file__) + ":" + os.path.dirname(nvidia.cudnn.lib.__file__))')"

if [[ ! -f "$ENV_FILE" ]]; then
  cat > "$ENV_FILE" <<ENV
# N-Fizyo KI-Server – erzeugt von install-ai-server.sh am $(date -I)
NAI_HOST=127.0.0.1
NAI_PORT=8090
NAI_DATA_DIR=$DATA_DIR
NFIZYO_STT_MODEL=large-v3-turbo
NFIZYO_STT_DEVICE=cuda
NFIZYO_OLLAMA_URL=http://127.0.0.1:11434
NFIZYO_LLM_MODEL=qwen2.5:7b-instruct
NFIZYO_TRANSLATE_MODEL=translategemma:12b
ENV
fi
chown root:"$APP_USER" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
get_env() { grep "^$1=" "$ENV_FILE" | cut -d= -f2; }

say "Whisper-Modell laden"
sudo -u "$APP_USER" env HOME="$DATA_DIR" LD_LIBRARY_PATH="$CUDA_LIBS" "$APP_DIR/venv/bin/python" -c \
  "from faster_whisper import WhisperModel; WhisperModel('$(get_env NFIZYO_STT_MODEL)', device='cuda', compute_type='float16')"

say "Ollama (nur 127.0.0.1) und Modelle"
command -v ollama >/dev/null 2>&1 || curl -fsSL https://ollama.com/install.sh | sh
install -d /etc/systemd/system/ollama.service.d
# Modelle bleiben geladen; mehrere Anfragen gleichzeitig (mehrere Praxen)
printf '[Service]\nEnvironment="OLLAMA_HOST=127.0.0.1:11434"\nEnvironment="OLLAMA_KEEP_ALIVE=24h"\nEnvironment="OLLAMA_NUM_PARALLEL=4"\n' \
  > /etc/systemd/system/ollama.service.d/local-only.conf
systemctl daemon-reload
systemctl enable --now ollama
systemctl restart ollama
sleep 3
ollama pull "$(get_env NFIZYO_LLM_MODEL)"
ollama pull "$(get_env NFIZYO_TRANSLATE_MODEL)"

say "Dienst"
cat > /etc/systemd/system/n-fizyo-ai.service <<UNIT
[Unit]
Description=N-Fizyo KI-Server
After=network-online.target ollama.service
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/app
ExecStart=$APP_DIR/venv/bin/python -m nfizyo.aiserver --env $ENV_FILE run
Environment=HOME=$DATA_DIR
Environment=LD_LIBRARY_PATH=$CUDA_LIBS
Environment=HF_HUB_OFFLINE=1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT

say "HTTPS (Caddy, Let's Encrypt) – eigener Abschnitt, andere Seiten (z. B. NSystem-Übersicht, Release-Ablage) bleiben"
CADDYFILE=${NFIZYO_CADDYFILE:-/etc/caddy/Caddyfile}
touch "$CADDYFILE"
python3 - "$CADDYFILE" "$DOMAIN" <<'PY'
import os, re, sys
path, domain = sys.argv[1:]
original = text = open(path, encoding="utf-8").read()
block = f"""# n-fizyo-ai BEGIN
{domain} {{
	request_body {{
		max_size 45MB
	}}
	header {{
		Strict-Transport-Security "max-age=31536000"
		-Server
	}}
	log {{
		output discard
	}}
	reverse_proxy 127.0.0.1:8090
}}
# n-fizyo-ai END
"""
text = re.sub(r"# n-fizyo-ai BEGIN.*?# n-fizyo-ai END\n?", "", text, flags=re.S)
# Ältere Installationen (bis 0.9.0) schrieben die ganze Datei ohne Marken: genau diesen Abschnitt ersetzen
text = re.sub(r"(?ms)^" + re.escape(domain) + r" \{\n.*?reverse_proxy 127\.0\.0\.1:8090\n\}\n?", "", text)
if original.strip():
    with open(path + ".bak", "w", encoding="utf-8") as f:     # vorherige Fassung, falls etwas zu prüfen ist
        f.write(original)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write((text.strip() + "\n\n" if text.strip() else "") + block)
os.replace(tmp, path)
PY

ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw --force enable >/dev/null
systemctl daemon-reload
systemctl enable --now n-fizyo-ai.service
systemctl restart n-fizyo-ai.service
systemctl reload caddy || systemctl restart caddy
sleep 2
curl -fsS http://127.0.0.1:8090/health >/dev/null && echo "KI-Server läuft: https://$DOMAIN" || echo "KI-Server antwortet nicht – journalctl -u n-fizyo-ai -n 50" >&2

cat <<DONE

Nächste Schritte:
  1) Pro Praxis einen Zugang anlegen (Token wird einmal angezeigt):
     cd $APP_DIR/app && sudo -u $APP_USER $APP_DIR/venv/bin/python -m nfizyo.aiserver --env $ENV_FILE add-client --name "Physio Muster"
  2) In der Praxis die kleine Box installieren und das Token dort eingeben:
     sudo bash deploy/install.sh --ai-server https://$DOMAIN
  3) Nutzung pro Praxis (für Kapazität und Abrechnung, ohne Inhalte):
     cd $APP_DIR/app && sudo -u $APP_USER $APP_DIR/venv/bin/python -m nfizyo.aiserver --env $ENV_FILE usage --days 30
DONE
