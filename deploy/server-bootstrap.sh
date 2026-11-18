#!/usr/bin/env bash
# N-Fizyo – neuen Server EINMALIG für das automatische Einspielen über GitHub vorbereiten (als root).
#
#   sudo bash server-bootstrap.sh --pubkey "ssh-ed25519 AAAA… github-deploy-fizyo" --domain fizyo.nsystem.ch --demo
#
# --pubkey   ÖFFENTLICHER Teil des neuen Deploy-Schlüssels (nicht geheim). Der private Teil kommt nur in GitHub Secrets.
# --genkey   statt --pubkey: Schlüssel hier erzeugen; der private Teil wird einmal angezeigt und gleich gelöscht.
# --domain   öffentlicher Name des Servers (DNS-A-Eintrag muss auf diesen Server zeigen, sonst kein Zertifikat)
# --demo     Demo-Praxis mit fiktiven Daten, ohne KI (--demo --no-ai). Ohne --demo: echte Praxis – nur mit KI-Grafikkarte!
#
# Was eingerichtet wird:
#   - Benutzer «deploy» ohne Passwort, Anmeldung nur mit diesem Schlüssel, eingeschränkt (kein Weiterleiten, kein Terminal)
#   - /usr/local/sbin/n-fizyo-deploy: entpackt das von GitHub hochgeladene Paket und spielt es ein
#       noch nichts installiert → deploy/install.sh   ·   gleiche Version → deploy/update.sh --force   ·   neuere → deploy/update.sh
#     (update.sh: Sicherung vorher, Prüfung, bei Fehler automatisch zurück – der Dienst bleibt nie aus)
#   - sudo NUR für genau dieses eine Programm, ohne Argumente (Domain und Modus stehen fest in /etc/n-fizyo-deploy.conf)
# Mehrmals ausführbar. Geheimnisse werden nicht verwendet und nie angezeigt.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Bitte als root ausführen." >&2; exit 1; }

PUBKEY=""; DOMAIN=""; DEMO=0; GENKEY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pubkey) PUBKEY="${2:-}"; shift ;;
    --genkey) GENKEY=1 ;;
    --domain) DOMAIN="${2:-}"; shift ;;
    --demo) DEMO=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done
KEYDIR=""
if [[ "$GENKEY" == 1 ]]; then
  # Schlüssel hier erzeugen: der private Teil wird EINMAL angezeigt (für GitHub Secrets) und danach gelöscht.
  KEYDIR="$(mktemp -d)"; chmod 700 "$KEYDIR"
  trap 'rm -rf "$KEYDIR"' EXIT
  ssh-keygen -q -t ed25519 -N "" -C "github-deploy-fizyo" -f "$KEYDIR/k"
  PUBKEY="$(cat "$KEYDIR/k.pub")"
fi
[[ "$PUBKEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/]{68}(=){0,2}(\ [A-Za-z0-9._@-]{1,64})?$ ]] \
  || { echo "--pubkey fehlt oder ist kein ed25519-Schlüssel (eine Zeile «ssh-ed25519 AAAA… name»)." >&2; exit 1; }
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]] \
  || { echo "--domain fehlt oder ist ungültig (z. B. fizyo.nsystem.ch)." >&2; exit 1; }
grep -q 'VERSION_ID="24.04"' /etc/os-release || echo "Warnung: getestet ist Ubuntu 24.04." >&2

echo "==> Benutzer deploy"
id -u deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
passwd -l deploy >/dev/null
install -d -o deploy -g deploy -m 0700 /home/deploy/.ssh /home/deploy/incoming
printf 'restrict %s\n' "$PUBKEY" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

echo "==> Einstellungen /etc/n-fizyo-deploy.conf"
{
  echo "# vom Deploy-Programm gelesen (nur diese zwei Zeilen) – von Hand ändern, nicht über GitHub"
  echo "DOMAIN=$DOMAIN"
  echo "DEMO=$DEMO"
} > /etc/n-fizyo-deploy.conf
chmod 644 /etc/n-fizyo-deploy.conf

echo "==> /usr/local/sbin/n-fizyo-deploy"
cat > /usr/local/sbin/n-fizyo-deploy <<'DEPLOY'
#!/usr/bin/env bash
# N-Fizyo – Paket von GitHub einspielen (aufgerufen als: sudo /usr/local/sbin/n-fizyo-deploy, ohne Argumente).
# Eingerichtet von deploy/server-bootstrap.sh. Domain und Modus nur aus /etc/n-fizyo-deploy.conf.
set -euo pipefail
PKG=/home/deploy/incoming/n-fizyo.tar.gz
WORKROOT=/var/lib/n-fizyo-deploy
MAX_BYTES=$((200 * 1024 * 1024))
exec 9>/run/n-fizyo-deploy.lock
flock -n 9 || { echo "Es läuft schon ein Deploy." >&2; exit 1; }

DOMAIN="$(sed -n 's/^DOMAIN=\([a-z0-9.-]*\)$/\1/p' /etc/n-fizyo-deploy.conf)"
DEMO="$(sed -n 's/^DEMO=\([01]\)$/\1/p' /etc/n-fizyo-deploy.conf)"
[[ -n "$DOMAIN" && -n "$DEMO" ]] || { echo "/etc/n-fizyo-deploy.conf unvollständig." >&2; exit 1; }
[[ -f "$PKG" && ! -L "$PKG" ]] || { echo "Kein Paket unter $PKG." >&2; exit 1; }
[[ "$(stat -c %s "$PKG")" -le $MAX_BYTES ]] || { echo "Paket zu gross." >&2; exit 1; }

install -d -m 0700 "$WORKROOT"
WORK="$(mktemp -d "$WORKROOT/src-$(date +%Y%m%d-%H%M%S)-XXXX")"
# Nur normale Dateien und Ordner unter n-fizyo/ – keine absoluten Pfade, kein «..», keine Links nach aussen
if tar -tzf "$PKG" | grep -Eqv '^n-fizyo(/|$)' || tar -tzf "$PKG" | grep -Eq '(^|/)\.\.(/|$)'; then
  echo "Paket enthält unerwartete Pfade – abgelehnt." >&2; exit 1
fi
tar -xzf "$PKG" -C "$WORK" --no-same-owner --no-same-permissions
rm -f "$PKG"
SRC="$WORK/n-fizyo"
[[ -f "$SRC/deploy/install.sh" && -f "$SRC/nfizyo/__init__.py" ]] || { echo "Paket unvollständig." >&2; exit 1; }
version_of() { sed -n 's/^__version__ = "\([0-9][0-9.]*\)"$/\1/p' "$1/nfizyo/__init__.py" 2>/dev/null | head -1; }
NEW="$(version_of "$SRC")"
echo "Neue Version: ${NEW:-?}"

if [[ ! -e /opt/n-fizyo/app ]]; then
  echo "Erstinstallation"
  if [[ "$DEMO" == 1 ]]; then bash "$SRC/deploy/install.sh" --demo --no-ai --domain "$DOMAIN"
  else bash "$SRC/deploy/install.sh" --domain "$DOMAIN"; fi
else
  CUR="$(version_of /opt/n-fizyo/app)"
  echo "Installiert: ${CUR:-?}"
  if [[ -n "$CUR" && "$CUR" == "$NEW" ]]; then
    bash "$SRC/deploy/update.sh" --force       # gleiche Versionsnummer, neuer Stand (Demo-Server: jeder Push auf main)
  else
    bash "$SRC/deploy/update.sh"               # neuer: normal · älter: update.sh lehnt ab
  fi
fi
# alte Arbeitsordner aufräumen (die letzten 3 bleiben)
ls -1dt "$WORKROOT"/src-* 2>/dev/null | tail -n +4 | xargs -r rm -rf
curl -fsS --max-time 10 http://127.0.0.1:8080/health && echo
DEPLOY
chmod 755 /usr/local/sbin/n-fizyo-deploy
chown root:root /usr/local/sbin/n-fizyo-deploy

echo "==> sudo nur für dieses Programm"
echo 'deploy ALL=(root) NOPASSWD: /usr/local/sbin/n-fizyo-deploy ""' > /etc/sudoers.d/n-fizyo-deploy
chmod 440 /etc/sudoers.d/n-fizyo-deploy
visudo -cf /etc/sudoers.d/n-fizyo-deploy >/dev/null

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow OpenSSH >/dev/null; fi

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Fertig. Jetzt in GitHub › Settings › Secrets and variables › Actions › New repository secret:"
echo
echo "== FIZYO_SSH_HOST =="
echo "$IP"
echo
echo "== FIZYO_SSH_KNOWN_HOSTS =="
for f in /etc/ssh/ssh_host_ed25519_key.pub; do
  [[ -f "$f" ]] && echo "$IP $(cut -d' ' -f1,2 "$f")"
done
if [[ -n "$KEYDIR" ]]; then
  echo
  echo "== FIZYO_SSH_KEY == (alles von BEGIN bis END kopieren – nur in GitHub, nie in den Chat; wird jetzt gelöscht)"
  cat "$KEYDIR/k"
fi
echo
echo "Danach in GitHub: Actions › Deploy › Run workflow (oder ein Push auf main)."
