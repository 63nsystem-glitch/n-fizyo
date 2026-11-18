#!/usr/bin/env bash
# Praxis-Box mit der NSystem-Übersicht verbinden.
#
# Aufruf:  sudo bash deploy/connect-central.sh /pfad/box-bx_XXXX.env       (Verbindung)
#          sudo bash deploy/connect-central.sh /pfad/support-bx_XXXX.env   (Supportzugang, optional)
# box-…env erzeugt NSystem mit «python -m nfizyo.central add-box»: Adresse, Box-ID, Gerätegeheimnis, öffentlicher Schlüssel
# für Befehle, ggf. Release-Ablage und öffentlicher Release-Schlüssel. support-…env erzeugt «support-peer»: Hub, öffentlicher
# Hub-Schlüssel, Tunnel-Adresse, öffentliche SSH-Schlüssel von NSystem. Die Werte werden in /etc/n-fizyo/n-fizyo.env
# eingetragen (nicht angezeigt), danach wird die Datei sicher gelöscht. Die Box verbindet sich nur nach aussen (keine offenen
# Ports); der Supportzugang bleibt geschlossen, bis die Leitung ihn öffnet.
set -euo pipefail
ENV_FILE=${NFIZYO_ENV_FILE:-/etc/n-fizyo/n-fizyo.env}
SRC="${1:-}"
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
[[ -f "$SRC" ]] || { echo "Datei fehlt: $SRC" >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Keine Installation gefunden ($ENV_FILE)." >&2; exit 1; }

KEYS=(); VALUES=()
n=0; m=0; r=0
# Windows-Zeilenenden (CRLF) und letzte Zeile ohne Zeilenumbruch sind erlaubt; zuerst alles prüfen, dann erst schreiben
while IFS= read -r line || [[ -n "$line" ]]; do
  line=${line%$'\r'}
  if [[ "$line" =~ ^(NFIZYO_CENTRAL_(URL|BOX_ID|BOX_KEY|COMMAND_PUBKEY))=(.+)$ ]]; then n=$((n + 1))
  elif [[ "$line" =~ ^(NFIZYO_SUPPORT_(HUB|HUB_PUBKEY|ADDRESS|HUB_IP|SSH_KEYS))=(.+)$ ]]; then m=$((m + 1))
  elif [[ "$line" =~ ^(NFIZYO_RELEASE_(BASE|PUBKEY))=(.+)$ ]]; then r=$((r + 1))
  else continue; fi
  KEYS+=("${BASH_REMATCH[1]}"); VALUES+=("${BASH_REMATCH[3]}")
done < "$SRC"
[[ $n -eq 4 || ( $n -eq 0 && $m -eq 5 ) ]] || { echo "Datei unvollständig (Verbindung $n von 4, Supportzugang $m von 5 Werten) – nichts geändert." >&2; exit 1; }
[[ $r -eq 0 || $r -eq 2 ]] || { echo "Datei unvollständig (Release-Ablage $r von 2 Werten) – nichts geändert." >&2; exit 1; }

# Alle Werte in EINEM Schritt eintragen (Werte ersetzen oder anhängen): neue Datei daneben, dann umbenennen –
# bei einem Fehler bleibt die alte .env unverändert. Werte gehen über stdin (nie in der Prozessliste sichtbar).
for i in "${!KEYS[@]}"; do printf '%s=%s\0' "${KEYS[$i]}" "${VALUES[$i]}"; done | python3 -c '
import os, sys
path = sys.argv[1]
pairs = [item.split("=", 1) for item in sys.stdin.buffer.read().decode("utf-8").split("\0") if item]
new = dict(pairs)
lines = open(path, encoding="utf-8").read().splitlines()
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0]
    if "=" in line and key in new:
        if key in seen:
            continue                       # doppelte Zeile: nur einmal behalten
        out.append(f"{key}={new[key]}")
        seen.add(key)
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in pairs if k not in seen and not seen.add(k)]
st = os.stat(path)
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(out) + "\n")
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, 0o640)
os.replace(tmp, path)
' "$ENV_FILE"
if [[ $n -eq 4 ]]; then
  URL="$(grep '^NFIZYO_CENTRAL_URL=' "$ENV_FILE" | cut -d= -f2-)"
  [[ "$URL" == https://* ]] || echo "Warnung: Adresse der Übersicht ist nicht https://" >&2
fi
if [[ $m -eq 5 ]]; then
  # sshd-Regel an die Hub-Adresse anpassen (öffnet nichts)
  bash "$(dirname "${BASH_SOURCE[0]}")/support-setup.sh" >/dev/null || echo "Hinweis: support-setup.sh meldet Probleme." >&2
fi
chown root:nfizyo "$ENV_FILE" 2>/dev/null || true
chmod 0640 "$ENV_FILE"
shred -u "$SRC" 2>/dev/null || rm -f "$SRC"
systemctl restart n-fizyo n-fizyo-runner
[[ $n -eq 4 ]] && echo "Verbunden mit $URL. Die erste Meldung geht in etwa 10 Sekunden raus."
[[ $r -eq 2 ]] && echo "Neue Versionen nur signiert von der Release-Ablage (NFIZYO_RELEASE_BASE / _PUBKEY)."
[[ $m -eq 5 ]] && echo "Supportzugang eingerichtet – bleibt geschlossen, bis die Leitung ihn öffnet (Einstellungen › NSystem)."
echo "Die Leitung der Praxis sieht unter Einstellungen › NSystem, was NSystem auf der Box auslöst."
