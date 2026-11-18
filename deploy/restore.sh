#!/usr/bin/env bash
# N-Fizyo – Wiederherstellung aus einer Sicherung (Datenbank + Dokumente + eigene Videos + Diktate).
#
#   sudo bash deploy/restore.sh --list                         # vorhandene Sicherungen anzeigen
#   sudo bash deploy/restore.sh /var/backups/n-fizyo/2026-11-01_0230
#   sudo bash deploy/restore.sh --test /var/backups/n-fizyo/2026-11-01_0230   # nur prüfen, nichts ändern
#
# Aus der verschlüsselten externen Kopie (0.9.0, Einrichtung docs/YEDEK.md; Schlüsseldatei und Ziel aus /etc/n-fizyo/n-fizyo.env):
#   sudo bash deploy/restore.sh --offsite-list                                  # Sätze am externen Ziel
#   sudo bash deploy/restore.sh --test --offsite latest                         # holen, entschlüsseln, prüfen – nichts ändern
#   sudo bash deploy/restore.sh --offsite n-fizyo-2026-11-01_0230.tar.gz.gpg   # holen und zurückspielen
# Der geholte Satz liegt danach entschlüsselt unter /var/backups/n-fizyo/offsite-<Zeit>/ (nur root; im Testmodus gelöscht).
#
# Vorher wird der jetzige Stand selbst gesichert (before-restore-<Zeit>) – eine Wiederherstellung ist also umkehrbar.
# Der Dienst wird angehalten und danach wieder gestartet; /health muss antworten.
set -euo pipefail
APP_USER=${NFIZYO_APP_USER:-nfizyo}
DATA_DIR=${NFIZYO_DATA:-/var/lib/n-fizyo}
BACKUP_DIR=${NFIZYO_BACKUP_DIR:-/var/backups/n-fizyo}
SERVICE=${NFIZYO_SERVICE:-n-fizyo}
HEALTH_URL=${NFIZYO_HEALTH_URL:-http://127.0.0.1:8080/health}
NO_SYSTEMD=${NFIZYO_NO_SYSTEMD:-0}      # nur für den automatischen Test
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }

MODE="restore"
case "${1:-}" in
  --list) ls -1dt "$BACKUP_DIR"/*/ 2>/dev/null | sed 's:/$::' || true; exit 0 ;;
  --offsite-list) exec bash "$SCRIPT_DIR/n-fizyo-backup" --offsite-list ;;
  --test) MODE="test"; shift ;;
  "") die "Bitte den Sicherungsordner angeben (oder --list)." ;;
esac
FETCHED=""
if [[ "${1:-}" == --offsite ]]; then
  [[ -n "${2:-}" ]] || die "Bitte den Namen des Satzes angeben (oder latest) – Liste: --offsite-list"
  say "Externe Kopie holen und entschlüsseln (${2})"
  install -d -m 0700 "$BACKUP_DIR"
  FETCHED="$(mktemp -d "$BACKUP_DIR/offsite-$(date +%Y-%m-%d_%H%M%S)-XXXX")"
  SRC="$(NFIZYO_BACKUP_DIR="$BACKUP_DIR" bash "$SCRIPT_DIR/n-fizyo-backup" --offsite-fetch "$2" "$FETCHED" | tail -n 1)" \
    || { rm -rf "$FETCHED"; die "Externe Kopie nicht verwendbar – nichts geändert."; }
  [[ $MODE == test ]] && trap 'rm -rf "$FETCHED"' EXIT     # Testmodus: keine entschlüsselte Kopie liegen lassen
  echo "  $SRC"
else
  SRC="${1:-}"
fi
[[ -d "$SRC" ]] || die "Ordner nicht gefunden: $SRC"

say "Sicherung prüfen ($SRC)"
found=0
for db in "$SRC"/*.sqlite3 "$SRC"/*.db; do
  [[ -f "$db" ]] || continue
  found=1
  [[ "$(sqlite3 "$db" 'PRAGMA integrity_check;')" == "ok" ]] || die "Datenbank beschädigt: $db"
  echo "  $(basename "$db"): in Ordnung ($(sqlite3 "$db" 'SELECT COUNT(*) FROM patients;' 2>/dev/null || echo '?') Patient/innen)"
done
[[ $found == 1 ]] || die "Keine Datenbank in der Sicherung."
for t in documents videos audio; do
  [[ -f "$SRC/$t.tar.gz" ]] || continue
  tar -tzf "$SRC/$t.tar.gz" >/dev/null || die "Archiv beschädigt: $t.tar.gz"
  echo "  $t.tar.gz: $(tar -tzf "$SRC/$t.tar.gz" | grep -vc '/$') Dateien"
done
[[ $MODE == test ]] && { echo "Prüfung erfolgreich – nichts geändert."; exit 0; }

[[ $EUID -eq 0 || $NO_SYSTEMD == 1 ]] || die "Bitte mit sudo starten."
say "Dienst anhalten"
[[ $NO_SYSTEMD == 1 ]] || systemctl stop "$SERVICE"

say "Jetzigen Stand sichern (umkehrbar)"
NOW="$BACKUP_DIR/before-restore-$(date +%Y-%m-%d_%H%M%S)"
NFIZYO_DATA="$DATA_DIR" NFIZYO_BACKUP_DIR="$NOW.tmp" NFIZYO_BACKUP_KEEP_DAYS=100000 NFIZYO_OFFSITE=0 NFIZYO_BACKUP_STATUS_FILE='' \
  bash "$SCRIPT_DIR/n-fizyo-backup"
mv "$NOW.tmp"/* "$NOW" 2>/dev/null || mv "$NOW.tmp" "$NOW"
rm -rf "$NOW.tmp"
echo "  $NOW"

say "Zurückspielen"
for db in "$SRC"/*.sqlite3 "$SRC"/*.db; do
  [[ -f "$db" ]] || continue
  name="$(basename "$db")"
  rm -f "$DATA_DIR/$name-wal" "$DATA_DIR/$name-shm"
  install -m 0640 "$db" "$DATA_DIR/$name"
done
for t in documents videos audio; do
  [[ -f "$SRC/$t.tar.gz" ]] || continue
  rm -rf "${DATA_DIR:?}/$t.restore-old"
  [[ -d "$DATA_DIR/$t" ]] && mv "$DATA_DIR/$t" "$DATA_DIR/$t.restore-old"
  tar -C "$DATA_DIR" -xzf "$SRC/$t.tar.gz"
  rm -rf "${DATA_DIR:?}/$t.restore-old"
done
if [[ $NO_SYSTEMD != 1 ]]; then
  chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
  say "Starten und prüfen"
  systemctl start "$SERVICE"
  for _ in $(seq 1 30); do curl -fsS "$HEALTH_URL" >/dev/null 2>&1 && { echo "N-Fizyo läuft wieder."; exit 0; }; sleep 2; done
  die "N-Fizyo antwortet nicht. Der vorherige Stand liegt in $NOW."
fi
echo "Wiederhergestellt (Testmodus ohne Dienst)."
