#!/usr/bin/env bash
# NSystem Support-Hub (WireGuard) auf dem Server der NSystem-Übersicht – einmalig (Ubuntu 24.04). Anleitung: docs/MERKEZ.md.
#
#   sudo bash deploy/install-support-hub.sh [--port 51820] [--net 10.77.0.0/24]
#
# - Schnittstelle nfz-hub, Adresse .1 im Tunnel-Netz, UDP-Port 51820 (Firewall wird geöffnet).
# - Privater Schlüssel nur in /etc/wireguard/nfz-hub.key (root, 0600), nie in einer Konfigurationsdatei.
# - Peers (Boxen) kommen aus /var/lib/n-fizyo-central/wg-peers.conf (nur öffentliche Schlüssel + Adressen, geschrieben von
#   «python -m nfizyo.central support-peer»). Ein systemd-Pfad übernimmt Änderungen sofort (nfz-hub-sync).
# - Keine Weiterleitung zwischen Boxen (ip_forward bleibt aus): jede Box sieht nur den Hub.
# - Eine Box ist nur erreichbar, solange ihre Leitung den Supportzugang geöffnet hat (sonst baut sie keinen Tunnel auf).
# Danach: öffentlichen Schlüssel des Hubs (wird angezeigt) als NFIZYO_CENTRAL_SUPPORT_HUB_PUBKEY in central.env eintragen.
set -euo pipefail
PORT=51820
NET=10.77.0.0/24
PEERS=/var/lib/n-fizyo-central/wg-peers.conf
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:-}"; shift ;;
    --net) NET="${2:-}"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || { echo "--port ungültig" >&2; exit 1; }
HUB_ADDR="$(python3 -c 'import ipaddress,sys; n=ipaddress.ip_network(sys.argv[1], strict=False); print(f"{next(n.hosts())}/{n.prefixlen}")' "$NET")"

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -q wireguard-tools >/dev/null
install -d -m 0700 /etc/wireguard
if [[ ! -f /etc/wireguard/nfz-hub.key ]]; then (umask 077; wg genkey > /etc/wireguard/nfz-hub.key); fi
chmod 0600 /etc/wireguard/nfz-hub.key
cat > /etc/wireguard/nfz-hub.conf <<CONF
# NSystem Support-Hub – Schlüssel separat (nfz-hub.key), Peers aus $PEERS (nfz-hub-sync)
[Interface]
Address = $HUB_ADDR
ListenPort = $PORT
PostUp = wg set %i private-key /etc/wireguard/nfz-hub.key
PostUp = test -f $PEERS && wg addconf %i $PEERS || true
CONF
chmod 0600 /etc/wireguard/nfz-hub.conf

cat > /usr/local/sbin/nfz-hub-sync <<SYNC
#!/usr/bin/env bash
# Peers des Support-Hubs aus $PEERS übernehmen (nur [Peer]-Abschnitte mit PublicKey/AllowedIPs werden akzeptiert).
set -euo pipefail
[[ -f $PEERS ]] || exit 0
if grep -vE '^(#.*|\[Peer\]|PublicKey = [A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=|AllowedIPs = [0-9.]+/32|)$' $PEERS >/dev/null; then
  echo "wg-peers.conf enthält unerwartete Zeilen – nicht übernommen." >&2; exit 1
fi
wg syncconf nfz-hub <(printf '[Interface]\nPrivateKey = %s\nListenPort = $PORT\n' "\$(cat /etc/wireguard/nfz-hub.key)"; cat $PEERS)
SYNC
chmod 0700 /usr/local/sbin/nfz-hub-sync
cat > /etc/systemd/system/nfz-hub-sync.service <<'UNIT'
[Unit]
Description=NSystem Support-Hub: Peers übernehmen
After=wg-quick@nfz-hub.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nfz-hub-sync
UNIT
cat > /etc/systemd/system/nfz-hub-sync.path <<UNIT
[Unit]
Description=NSystem Support-Hub: Peers beobachten
[Path]
PathChanged=$PEERS
[Install]
WantedBy=multi-user.target
UNIT

ufw allow "$PORT/udp" >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable --now wg-quick@nfz-hub.service nfz-hub-sync.path
/usr/local/sbin/nfz-hub-sync || true
echo
echo "Support-Hub läuft (nfz-hub, $HUB_ADDR, UDP $PORT)."
echo "In /etc/n-fizyo-central/central.env eintragen (nicht geheim):"
echo "  NFIZYO_CENTRAL_SUPPORT_HUB_PUBKEY=$(wg pubkey < /etc/wireguard/nfz-hub.key)"
echo "  NFIZYO_CENTRAL_SUPPORT_ENDPOINT=<name-dieses-servers>:$PORT"
echo "  NFIZYO_CENTRAL_SUPPORT_NET=$NET"
