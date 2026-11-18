#!/usr/bin/env bash
# N-Fizyo – eigener Video-Server (Jitsi Meet) für Video-Sitzungen. Ubuntu 24.04, Server in der Schweiz.
#
# Aufruf (aus dem entpackten N-Fizyo-Ordner):
#   sudo bash deploy/jitsi/install-jitsi.sh --domain video.physio.example.ch --email it@physio.example.ch \
#        [--public-ip 203.0.113.10] [--release stable-11146-2] [--nfizyo-env /etc/n-fizyo/n-fizyo.env]
#
# --nfizyo-env: nur wenn N-Fizyo auf DEMSELBEN Server läuft – dann werden Adresse und Schlüssel direkt dort eingetragen.
# Sonst: Schlüssel von hier nach dort kopieren, OHNE ihn anzuzeigen (siehe docs/VIDEO.md). Nie in Chat oder E-Mail.
#
# Einstellungen: Zutritt nur mit Schlüssel (JWT) aus N-Fizyo, keine Gäste ohne Schlüssel, keine Aufzeichnung,
# kein Peer-to-Peer und kein STUN von Google/jitsi.org, Let's Encrypt, Zeitzone Europe/Zurich.
# Das Skript kann mehrmals laufen; ein vorhandener Schlüssel bleibt erhalten.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE="stable-11146-2"     # docker-jitsi-meet, siehe https://github.com/jitsi/docker-jitsi-meet/releases
DIR=/opt/jitsi
DOMAIN=""
EMAIL=""
PUBLIC_IP=""
NFIZYO_ENV=""
APP_ID="nfizyo"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift ;;
    --email) EMAIL="$2"; shift ;;
    --public-ip) PUBLIC_IP="$2"; shift ;;
    --release) RELEASE="$2"; shift ;;
    --nfizyo-env) NFIZYO_ENV="$2"; shift ;;
    *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
  esac
  shift
done
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo starten." >&2; exit 1; }
[[ -n "$DOMAIN" && -n "$EMAIL" ]] || { echo "Bitte --domain und --email angeben." >&2; exit 2; }
[[ "$DOMAIN" =~ ^[a-z0-9.-]+$ ]] || { echo "Ungültiger Domainname." >&2; exit 2; }

echo "== Pakete"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 curl openssl ufw >/dev/null
fi
systemctl enable --now docker >/dev/null

if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  echo "Öffentliche Adresse nicht angegeben – verwende $PUBLIC_IP (hinter NAT bitte --public-ip setzen)."
fi

echo "== docker-jitsi-meet $RELEASE"
mkdir -p "$DIR"
if [[ ! -f "$DIR/.release" || "$(cat "$DIR/.release")" != "$RELEASE" ]]; then
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/jitsi/docker-jitsi-meet/archive/refs/tags/${RELEASE}.tar.gz" -o "$tmp/jitsi.tar.gz"
  tar -xzf "$tmp/jitsi.tar.gz" -C "$tmp"
  src="$(find "$tmp" -maxdepth 1 -type d -name 'docker-jitsi-meet-*' | head -n1)"
  cp -a "$src"/. "$DIR"/
  rm -rf "$tmp"
  echo "$RELEASE" > "$DIR/.release"
fi
cd "$DIR"
if [[ ! -f .env ]]; then
  cp env.example .env
  bash gen-passwords.sh >/dev/null
fi
chmod 600 .env

setenv() {   # Wert in .env setzen (ohne ihn auszugeben)
  local key="$1" value="$2"
  if grep -qE "^#?\s*${key}=" .env; then
    sed -i -E "s|^#?\s*${key}=.*|${key}=${value//|/\\|}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

setenv CONFIG "$DIR/config"
setenv HTTP_PORT 80
setenv HTTPS_PORT 443
setenv TZ Europe/Zurich
setenv PUBLIC_URL "https://$DOMAIN"
setenv ENABLE_LETSENCRYPT 1
setenv LETSENCRYPT_DOMAIN "$DOMAIN"
setenv LETSENCRYPT_EMAIL "$EMAIL"
setenv ENABLE_HTTP_REDIRECT 1
setenv ENABLE_AUTH 1
setenv AUTH_TYPE jwt
setenv ENABLE_GUESTS 0
setenv JWT_APP_ID "$APP_ID"
setenv JWT_ACCEPTED_ISSUERS "$APP_ID"
setenv JWT_ACCEPTED_AUDIENCES jitsi
setenv JWT_ALLOW_EMPTY 0
setenv ENABLE_RECORDING 0
setenv ENABLE_TRANSCRIPTIONS 0
setenv ENABLE_LIVESTREAMING 0
setenv ENABLE_WELCOME_PAGE 0
setenv JVB_ADVERTISE_IPS "$PUBLIC_IP"
setenv JVB_DISABLE_STUN true
if ! grep -qE '^JWT_APP_SECRET=.{32,}' .env; then
  setenv JWT_APP_SECRET "$(openssl rand -hex 32)"
fi

mkdir -p "$DIR/config/web"
cp "$SCRIPT_DIR/custom-config.js" "$DIR/config/web/custom-config.js"

echo "== Firewall"
ufw allow 22/tcp >/dev/null || true
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 10000/udp >/dev/null
ufw --force enable >/dev/null

echo "== Start"
docker compose pull -q
docker compose up -d

if [[ -n "$NFIZYO_ENV" ]]; then
  [[ -f "$NFIZYO_ENV" ]] || { echo "$NFIZYO_ENV nicht gefunden." >&2; exit 1; }
  secret="$(grep -E '^JWT_APP_SECRET=' .env | head -n1 | cut -d= -f2-)"
  for kv in "NFIZYO_VIDEO_URL=https://$DOMAIN" "NFIZYO_VIDEO_APP_ID=$APP_ID" "NFIZYO_VIDEO_SECRET=$secret"; do
    key="${kv%%=*}"
    sed -i "/^${key}=/d" "$NFIZYO_ENV"
    printf '%s\n' "$kv" >> "$NFIZYO_ENV"
  done
  unset secret
  chmod 640 "$NFIZYO_ENV"
  systemctl restart n-fizyo 2>/dev/null || true
  echo "N-Fizyo kennt jetzt den Video-Server (Schlüssel eingetragen, nicht angezeigt)."
else
  echo "Nächster Schritt: Schlüssel auf den N-Fizyo-Server übertragen – siehe docs/VIDEO.md, Schritt 4 (nie anzeigen, nie in Chat/E-Mail)."
fi
echo "Fertig: https://$DOMAIN (ohne Schlüssel aus N-Fizyo kommt niemand in einen Raum)."
