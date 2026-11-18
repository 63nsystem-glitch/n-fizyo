#!/usr/bin/env bash
# N-Fizyo – Supportzugang auf der Box VORBEREITEN (einmalig; install.sh und update.sh rufen es auf, kann mehrmals laufen).
#
# Öffnet NICHTS. Danach ist der Zugang weiterhin geschlossen; nur die Leitung der Praxis öffnet ihn (Einstellungen › NSystem,
# 1–8 Stunden), der Runner baut dann den Tunnel auf und schliesst ihn bei Ablauf selbst (nfizyo/agent/support.py).
#
#   sudo bash deploy/support-setup.sh              # vorbereiten
#   sudo bash deploy/support-setup.sh --show-key   # öffentlicher WireGuard-Schlüssel dieser Box (für «support-peer» bei NSystem)
#
# Was eingerichtet wird:
#   - wireguard-tools, iptables
#   - Benutzer nfizyo-support: Passwort gesperrt, Anmelde-Shell = Support-Werkzeug (keine bash), cron und at gesperrt
#     (/etc/cron.deny, /etc/at.deny), Home /var/lib/n-fizyo-support (gehört root), .ssh/authorized_keys (gehört root, leer –
#     der Runner trägt die Schlüssel von NSystem nur ein, solange der Zugang offen ist: from=Hub, restrict, command=Werkzeug)
#   - /usr/local/sbin/n-fizyo-support-tool (einzige Möglichkeit über SSH) + sudoers nur für dieses Werkzeug («--root …»)
#   - sshd: DenyUsers nfizyo-support von jeder Adresse ausser dem Hub (10.77.0.1 bzw. NFIZYO_SUPPORT_HUB_IP);
#     «Match User nfizyo-support»: ForceCommand Werkzeug, kein Terminal, keine Weiterleitungen
#   - WireGuard-Schlüssel der Box: /etc/n-fizyo/support/wg.key (nur root), wg.pub (öffentlich)
# Geheimnisse werden nie angezeigt (nur der öffentliche Schlüssel mit --show-key).
set -euo pipefail
SUPPORT_USER=nfizyo-support
SUPPORT_HOME=/var/lib/n-fizyo-support
SUPPORT_DIR=/etc/n-fizyo/support
ENV_FILE=${NFIZYO_ENV_FILE:-/etc/n-fizyo/n-fizyo.env}
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
warn() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }

if [[ "${1:-}" == "--show-key" ]]; then
  [[ -f "$SUPPORT_DIR/wg.pub" ]] || { echo "Noch kein Schlüssel – zuerst: sudo bash deploy/support-setup.sh" >&2; exit 1; }
  cat "$SUPPORT_DIR/wg.pub"
  exit 0
fi

HUB_IP="$(sed -n 's/^NFIZYO_SUPPORT_HUB_IP=//p' "$ENV_FILE" 2>/dev/null | tail -n 1)"
HUB_IP="${HUB_IP:-10.77.0.1}"
[[ "$HUB_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { warn "NFIZYO_SUPPORT_HUB_IP ungültig: $HUB_IP"; exit 1; }

if ! command -v wg >/dev/null || ! command -v iptables >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q wireguard-tools iptables >/dev/null || warn "wireguard-tools/iptables nicht installiert (Internet?)."
fi

TOOL=/usr/local/sbin/n-fizyo-support-tool
install -m 0755 "$SRC_DIR/deploy/n-fizyo-support-tool" "$TOOL"
# Anmelde-Shell IST das Werkzeug: auch ohne authorized_keys-Befehl (z. B. su, ältere sshd-Regeln) gibt es keine bash
id -u "$SUPPORT_USER" >/dev/null 2>&1 || useradd --system --home-dir "$SUPPORT_HOME" --no-create-home --shell "$TOOL" "$SUPPORT_USER"
usermod -s "$TOOL" "$SUPPORT_USER" >/dev/null 2>&1 || warn "Anmelde-Shell von $SUPPORT_USER nicht gesetzt."
usermod -L "$SUPPORT_USER" >/dev/null 2>&1 || true            # kein Passwort-Login
# Nichts darf den Zugang überdauern: keine cron-/at-Aufträge für den Support-Benutzer
for deny in /etc/cron.deny /etc/at.deny; do
  [[ -f "$deny" ]] || install -m 0640 -o root -g root /dev/null "$deny"
  grep -qx "$SUPPORT_USER" "$deny" || echo "$SUPPORT_USER" >> "$deny"
done
crontab -r -u "$SUPPORT_USER" >/dev/null 2>&1 || true
install -d -o root -g root -m 0755 "$SUPPORT_HOME" "$SUPPORT_HOME/.ssh"
[[ -f "$SUPPORT_HOME/.ssh/authorized_keys" ]] || install -o root -g root -m 0644 /dev/null "$SUPPORT_HOME/.ssh/authorized_keys"
chown root:root "$SUPPORT_HOME/.ssh/authorized_keys"; chmod 0644 "$SUPPORT_HOME/.ssh/authorized_keys"

install -d -o root -g root -m 0700 "$SUPPORT_DIR"
if [[ ! -f "$SUPPORT_DIR/wg.key" ]] && command -v wg >/dev/null; then
  (umask 077; wg genkey > "$SUPPORT_DIR/wg.key")
fi
if [[ -f "$SUPPORT_DIR/wg.key" ]]; then
  chmod 0600 "$SUPPORT_DIR/wg.key"
  wg pubkey < "$SUPPORT_DIR/wg.key" > "$SUPPORT_DIR/wg.pub"
  chmod 0644 "$SUPPORT_DIR/wg.pub"
fi

TMP="$(mktemp)"
printf '# N-Fizyo Supportzugang: nur das Support-Werkzeug, nur mit --root (prüft selbst nochmals)\n%s ALL=(root) NOPASSWD: /usr/local/sbin/n-fizyo-support-tool --root *\n' "$SUPPORT_USER" > "$TMP"
if visudo -cf "$TMP" >/dev/null; then install -m 0440 "$TMP" /etc/sudoers.d/n-fizyo-support; else warn "sudoers ungültig – nicht installiert."; fi
rm -f "$TMP"

if [[ -d /etc/ssh/sshd_config.d ]]; then
  CONF=/etc/ssh/sshd_config.d/60-n-fizyo-support.conf
  {
    printf '# N-Fizyo Supportzugang: nfizyo-support nur vom NSystem-Hub über den Tunnel (Schlüssel nur solange offen)\nDenyUsers %s@*,!%s\n' \
      "$SUPPORT_USER" "$HUB_IP"
    # Match-Block endet am Ende dieser Datei (OpenSSH ≥ 8.4) – darum zuletzt
    printf 'Match User %s\n' "$SUPPORT_USER"
    printf '\tForceCommand %s\n\tPermitTTY no\n\tAllowTcpForwarding no\n\tAllowStreamLocalForwarding no\n' "$TOOL"
    printf '\tAllowAgentForwarding no\n\tX11Forwarding no\n\tPermitTunnel no\n\tPermitUserRC no\n'
  } > "$CONF.new"
  mv "$CONF.new" "$CONF"
  if sshd -t 2>/dev/null; then
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
  else
    rm -f "$CONF"; warn "sshd-Konfiguration abgelehnt – Ergänzung entfernt (Schlüssel und from= schützen weiterhin)."
  fi
fi

echo "Supportzugang vorbereitet (geschlossen). Öffnen kann nur die Leitung: Einstellungen › NSystem."
[[ -f "$SUPPORT_DIR/wg.pub" ]] && echo "Öffentlicher WireGuard-Schlüssel dieser Box (für NSystem, nicht geheim): $(cat "$SUPPORT_DIR/wg.pub")"
true
