#!/usr/bin/env bash
# N-Fizyo – Servicekabel einrichten (einmalig pro Box; kann mehrmals laufen). Ablauf und Regeln: docs/SERVIS_KABLOSU.md
#
#   sudo bash deploy/service-port-setup.sh --list                                  # Netzwerkkarten anzeigen
#   sudo bash deploy/service-port-setup.sh --interface enx00e04c680001 --pubkey <Base64>
#   sudo bash deploy/service-port-setup.sh --off                                   # Servicekabel ausschalten
#
# --interface   die ZWEITE Netzwerkkarte (oder ein USB-Netzwerkadapter), NICHT die Karte im Praxisnetz
# --pubkey      öffentlicher Service-Schlüssel von NSystem (Service-Programm: «Anahtar» › «Açık anahtarı göster») – nicht geheim
#
# Was eingerichtet wird:
#   - Die Karte bekommt fest 169.254.77.1/16 (link-local, kein DHCP, kein Gateway, kein DNS) – netplan
#   - Firewall (ufw): auf DIESER Karte nur TCP 8780 zu 169.254.77.1, alles andere zu (auch SSH, Web, KI); nichts wird weitergeleitet
#   - Dienst n-fizyo-service (root, python -m nfizyo.agent.service) – hört nur auf 169.254.77.1:8780
#   - NFIZYO_SERVICE_PUBKEY und NFIZYO_SERVICE_IFACE in der .env
# Geheimnisse werden nicht verwendet und nie angezeigt (der öffentliche Schlüssel ist keins).
set -euo pipefail
APP_DIR=${NFIZYO_APP_DIR:-/opt/n-fizyo}
ENV_FILE=${NFIZYO_ENV_FILE:-/etc/n-fizyo/n-fizyo.env}
NETPLAN=/etc/netplan/60-n-fizyo-service.yaml
UNIT=/etc/systemd/system/n-fizyo-service.service
ADDR=169.254.77.1
PORT=8780
say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Keine Installation gefunden ($ENV_FILE fehlt). Zuerst deploy/install.sh." >&2; exit 1; }

IFACE=""; PUBKEY=""; MODE=setup
while [[ $# -gt 0 ]]; do
  case "$1" in
    --interface) IFACE="${2:-}"; shift ;;
    --pubkey) PUBKEY="${2:-}"; shift ;;
    --list) MODE=list ;;
    --off) MODE=off ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done

set_env() {  # set_env KEY VALUE – Zeile ersetzen oder anhängen (Werte hier nur [A-Za-z0-9+/=._-])
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
default_iface() { ip -o route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'; }

if [[ $MODE == list ]]; then
  DEF="$(default_iface)"
  echo "Netzwerkkarten (Praxisnetz = Karte mit Standard-Route, NICHT als Servicekabel verwenden):"
  for d in /sys/class/net/*; do
    n="$(basename "$d")"; [[ "$n" == lo || "$n" == wg* || "$n" == docker* ]] && continue
    state="$(cat "$d/operstate" 2>/dev/null || echo ?)"
    mark=""; [[ "$n" == "$DEF" ]] && mark="  ← Praxisnetz"
    printf '  %-20s %s%s\n' "$n" "$state" "$mark"
  done
  exit 0
fi

if [[ $MODE == off ]]; then
  say "Servicekabel ausschalten"
  OLD="$(grep '^NFIZYO_SERVICE_IFACE=' "$ENV_FILE" | cut -d= -f2 || true)"
  systemctl disable --now n-fizyo-service 2>/dev/null || true
  rm -f "$UNIT" "$NETPLAN"; systemctl daemon-reload
  if [[ -n "$OLD" ]]; then
    ufw delete allow in on "$OLD" to "$ADDR" port "$PORT" proto tcp >/dev/null 2>&1 || true
    ufw delete deny in on "$OLD" >/dev/null 2>&1 || true
  fi
  sed -i '/^NFIZYO_SERVICE_PUBKEY=/d;/^NFIZYO_SERVICE_IFACE=/d' "$ENV_FILE"
  netplan apply 2>/dev/null || true
  echo "Aus. Die Karte hat keine Adresse mehr, der Dienst läuft nicht."
  exit 0
fi

[[ -n "$IFACE" ]] || { echo "--interface fehlt (Liste: --list)" >&2; exit 1; }
[[ "$IFACE" =~ ^[A-Za-z0-9._-]{1,15}$ && -d "/sys/class/net/$IFACE" ]] || { echo "Netzwerkkarte «$IFACE» nicht gefunden (--list)." >&2; exit 1; }
[[ "$IFACE" != "$(default_iface)" ]] || { warn "«$IFACE» ist die Karte im Praxisnetz – dafür eine ZWEITE Karte oder einen USB-Adapter nehmen."; exit 1; }
[[ "$PUBKEY" =~ ^[A-Za-z0-9+/]{43}=$ ]] || { echo "--pubkey fehlt oder ist kein Ed25519-Schlüssel (Base64, 44 Zeichen)." >&2; exit 1; }

say "Karte $IFACE: feste Adresse $ADDR (link-local, kein Gateway, kein DNS)"
cat > "$NETPLAN" <<YAML
# N-Fizyo Servicekabel (deploy/service-port-setup.sh) – nur Laptop von NSystem, nicht das Praxisnetz
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: false
      dhcp6: false
      accept-ra: false
      link-local: []
      addresses: [$ADDR/16]
      optional: true
YAML
chmod 600 "$NETPLAN"
netplan apply

say "Firewall: auf $IFACE nur Port $PORT, alles andere zu"
OLD="$(grep '^NFIZYO_SERVICE_IFACE=' "$ENV_FILE" | cut -d= -f2 || true)"
for i in $OLD $IFACE; do
  ufw delete allow in on "$i" to "$ADDR" port "$PORT" proto tcp >/dev/null 2>&1 || true
  ufw delete deny in on "$i" >/dev/null 2>&1 || true
done
# Reihenfolge zählt: zuerst erlauben (Regel 1), dann alles andere auf der Karte sperren (Regel 2) – vor «allow OpenSSH/80/443»
ufw insert 1 deny in on "$IFACE" >/dev/null
ufw insert 1 allow in on "$IFACE" to "$ADDR" port "$PORT" proto tcp >/dev/null
ufw route deny in on "$IFACE" >/dev/null 2>&1 || true
ufw route deny out on "$IFACE" >/dev/null 2>&1 || true

say "Schlüssel und Dienst"
set_env NFIZYO_SERVICE_PUBKEY "$PUBKEY"
set_env NFIZYO_SERVICE_IFACE "$IFACE"
install -d -o root -g root -m 0700 /var/lib/n-fizyo-service
cat > "$UNIT" <<UNIT
[Unit]
Description=N-Fizyo Servicekabel (nur signierte Anfragen von NSystem, nur 169.254.77.1)
After=network-online.target n-fizyo-runner.service

[Service]
User=root
WorkingDirectory=$APP_DIR/app
EnvironmentFile=$ENV_FILE
Environment=NFIZYO_ENV_FILE=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python -m nfizyo.agent.service --env $ENV_FILE --bind $ADDR --port $PORT
Restart=always
RestartSec=10
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable n-fizyo-service >/dev/null
systemctl restart n-fizyo-service

echo
echo "Fertig. Laptop per Kabel an $IFACE anschliessen, Service-Programm öffnen («Bağlan»)."
echo "Windows gibt sich nach 20–60 Sekunden selbst eine 169.254-Adresse – am Laptop nichts einstellen."
