#!/usr/bin/env bash
# N-Fizyo – Update mit Sicherung und automatischer Rückkehr (Rollback).
#
# Aufruf (aus dem entpackten Ordner der NEUEN Version):
#   sudo bash deploy/update.sh               # Update einspielen
#   sudo bash deploy/update.sh --check       # nur anzeigen, was sich ändert (Programm und Module)
#   sudo bash /opt/n-fizyo/app/deploy/update.sh --rollback   # zurück zur vorherigen Version (inkl. Datenbank von vor dem Update)
#
# Ablauf beim Update (der Dienst läuft so lange wie möglich weiter):
#   1) Vergleich: Programmversion und Version jedes Moduls (alt → neu). Gleiche Version: nichts zu tun.
#      Ältere Version: abgelehnt – zurück nur mit --rollback (von Hand mit --force möglich, nie über den Runner).
#   2) Neue Version als eigener Ordner unter /opt/n-fizyo/releases/, Pakete genau nach requirements.lock (Prüfsummen),
#      Sicherung der Dokumente und der Datenbank (pre-update-<Zeit>) – alles, während der Dienst noch läuft.
#      Geht hier etwas schief: neuer Ordner wird gelöscht, Pakete der alten Version wiederhergestellt, der Dienst lief durch.
#   3) Dienst anhalten, Datenbank nochmals sichern (genauer Stand), umschalten (/opt/n-fizyo/app), Datenbank migrieren.
#   4) Starten und prüfen (/health meldet die neue Version).
#   5) Geht ab dem Anhalten etwas schief (auch Abbruch): Datenbank zurückspielen (falls schon migriert), alte Version wieder
#      einschalten, starten – automatisch. Der Dienst bleibt nie aus.
# Es bleiben die letzten 3 Versionen liegen (nach Zeitstempel im Ordnernamen; aktuelle und vorherige nie gelöscht).
# Daten, .env und Sicherungen werden nie gelöscht; die Sicherung für --rollback bleibt auch nach 30 Tagen (n-fizyo-backup).
# Geheimnisse (Schlüssel, Passwörter) werden NIE angezeigt oder geloggt.
set -euo pipefail

# Pfade wie in install.sh (die NFIZYO_*-Variablen nur für den automatischen Test des Skripts)
APP_USER=${NFIZYO_APP_USER:-nfizyo}
APP_DIR=${NFIZYO_APP_DIR:-/opt/n-fizyo}
DATA_DIR=${NFIZYO_DATA:-/var/lib/n-fizyo}
ENV_FILE=${NFIZYO_ENV_FILE:-/etc/n-fizyo/n-fizyo.env}
BACKUP_DIR=${NFIZYO_BACKUP_DIR:-/var/backups/n-fizyo}
HEALTH_URL=${NFIZYO_HEALTH_URL:-http://127.0.0.1:8080/health}
HEALTH_TRIES=${NFIZYO_HEALTH_TRIES:-30}
SBIN_DIR=${NFIZYO_SBIN_DIR:-/usr/local/sbin}
SUPPORT_SETUP=${NFIZYO_SUPPORT_SETUP-}            # leer = deploy/support-setup.sh der neuen Version
KEEP_RELEASES=3
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE=update
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE=check ;;
    --rollback) MODE=rollback ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
  shift
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }
[[ $EUID -eq 0 ]] || { echo "Bitte mit sudo ausführen." >&2; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "Keine Installation gefunden ($ENV_FILE fehlt). Zuerst deploy/install.sh ausführen." >&2; exit 1; }
if [[ $FORCE -eq 1 && "${NFIZYO_FROM_RUNNER:-}" == "1" ]]; then
  echo "--force ist nur von Hand erlaubt (nicht über NSystem-Befehle)." >&2; exit 1
fi

PY="$APP_DIR/venv/bin/python"
CUDA_LIBS="$(systemctl show n-fizyo -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^LD_LIBRARY_PATH=//p' || true)"
as_app() { sudo -u "$APP_USER" env HOME="$DATA_DIR" LD_LIBRARY_PATH="$CUDA_LIBS" "$@"; }
versions_of() { (cd "$1" && as_app "$PY" -m nfizyo versions --json 2>/dev/null) || echo '{}'; }
health_version() { curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true; }

version_of_dir() {  # version_of_dir <Programmordner> – liest nfizyo/__init__.py, ohne etwas zu importieren
  [[ -n "$1" && -f "$1/nfizyo/__init__.py" ]] || { echo ""; return 0; }
  sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$1/nfizyo/__init__.py" | head -n 1
}

vcmp() {  # vcmp <a> <b> → -1 | 0 | 1 (0.9 == 0.9.0 < 0.9.1-rc1 < 0.9.1); gleiche Regel wie agent/release.py version_key
  python3 - "$1" "$2" <<'PY'
import re, sys
def key(v):
    main, _, pre = v.partition("-")
    nums = [int(p) if p.isdigit() else -1 for p in main.split(".")][:6]
    nums += [0] * (6 - len(nums))
    if not pre:
        return (*nums, 1, ())
    return (*nums, 0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.\-]", pre)))
a, b = key(sys.argv[1]), key(sys.argv[2])
print((a > b) - (a < b))
PY
}

wait_healthy() {  # wait_healthy <erwartete Version> – bis 60 s
  for _ in $(seq 1 "$HEALTH_TRIES"); do
    [[ "$(health_version)" == "$1" ]] && return 0
    sleep 2
  done
  return 1
}

compare() {  # compare <alt json> <neu json>
  "$PY" - "$1" "$2" <<'PY'
import json, sys
old, new = json.loads(sys.argv[1] or "{}"), json.loads(sys.argv[2] or "{}")
print(f"Programm: {old.get('app', '–')} → {new.get('app', '–')}")
om, nm = old.get("modules", {}), new.get("modules", {})
for name in sorted(set(om) | set(nm)):
    a, b = om.get(name), nm.get(name)
    if a != b:
        print(f"  {name:14} {a or 'neu':>8} → {b or 'entfernt'}")
if om == nm:
    print("  (Module unverändert)")
PY
}

backup_files() {  # backup_files <Ordner> – Dokumente, eigene Videos, Diktate (geht im Betrieb)
  install -d -m 0700 "$1"
  for dir in documents videos audio; do
    if [[ -d "$DATA_DIR/$dir" ]]; then tar -C "$DATA_DIR" -czf "$1/$dir.tar.gz" "$dir"; fi
  done
  chmod -R go-rwx "$1"
}

backup_dbs() {  # backup_dbs <Ordner> – konsistente Kopie jeder Datenbank (auch im Betrieb) + Prüfung der Kopie
  install -d -m 0700 "$1"
  local db copy
  for db in "$DATA_DIR"/*.db "$DATA_DIR"/*.sqlite3; do
    [[ -f "$db" ]] || continue
    copy="$1/$(basename "$db")"
    rm -f "$copy.new"
    sqlite3 "$db" ".backup '$copy.new'"
    [[ "$(sqlite3 "$copy.new" 'PRAGMA quick_check;')" == "ok" ]] || { warn "Sicherung beschädigt: $copy"; return 1; }
    mv -f "$copy.new" "$copy"
  done
  chmod -R go-rwx "$1"
}

backup_now() { backup_files "$1"; backup_dbs "$1"; }

restore_db() {  # restore_db <Ordner> – nur Datenbank (Dokumente werden nie gelöscht)
  local f
  for f in "$1"/*.db "$1"/*.sqlite3; do
    [[ -f "$f" ]] || continue
    rm -f "$DATA_DIR/$(basename "$f")-wal" "$DATA_DIR/$(basename "$f")-shm"
    install -o "$APP_USER" -g "$APP_USER" -m 0640 "$f" "$DATA_DIR/$(basename "$f")"
  done
}

pip_install() {  # pip_install <Release-Ordner>
  # Genau die Versionen aus requirements.lock (mit Prüfsummen): neuere Versionen werden dabei auch wirklich eingespielt
  # (früher blieb mit «>=» einfach die alte, evtl. unsichere Version liegen). Ältere Versionen ohne Lock-Datei (Rückkehr): wie bisher.
  if [[ -f "$1/requirements.lock" ]]; then
    "$APP_DIR/venv/bin/pip" install -q --require-hashes -r "$1/requirements.lock"
  else
    "$APP_DIR/venv/bin/pip" install -q -r "$1/requirements.txt"
  fi
  if grep -q '^NFIZYO_STT=faster_whisper' "$ENV_FILE" && [[ -f "$1/requirements-ai.txt" ]]; then
    "$APP_DIR/venv/bin/pip" install -q -r "$1/requirements-ai.txt"
  fi
}

current_release() { if [[ -e "$APP_DIR/app" ]]; then readlink -f "$APP_DIR/app"; fi; }
link_target() { if [[ -L "$1" && -e "$1" ]]; then readlink -f "$1"; fi; }

# ------------------------------------------------------------------ Rollback (von Hand oder Befehl «rollback» von NSystem)
if [[ $MODE == rollback ]]; then
  PREV="$(link_target "$APP_DIR/previous")"
  [[ -n "$PREV" && -d "$PREV" ]] || { echo "Keine vorherige Version vorhanden." >&2; exit 1; }
  CUR="$(current_release)"
  LAST_BACKUP="$(head -n 1 "$APP_DIR/.last-update-backup" 2>/dev/null || true)"
  if [[ -z "$LAST_BACKUP" || ! -d "$LAST_BACKUP" ]]; then
    warn "ACHTUNG: Die Sicherung von vor dem Update fehlt (${LAST_BACKUP:-keine Angabe in $APP_DIR/.last-update-backup})."
    warn "Ohne sie bliebe die Datenbank auf dem Stand der neueren Version – die ältere Version startet dann evtl. nicht."
    if [[ $FORCE -ne 1 ]]; then
      warn "Abgebrochen, nichts geändert. Nur das Programm zurücksetzen (Datenbank bleibt): sudo bash $0 --rollback --force"
      exit 1
    fi
    warn "--force: nur das Programm wird zurückgesetzt, die Datenbank bleibt wie sie ist."
    LAST_BACKUP=""
  fi
  EXPECTED="$(version_of_dir "$PREV")"
  CUR_VERSION="$(version_of_dir "$CUR")"
  STAMP="$(date +%Y-%m-%d_%H%M%S)"
  BEFORE="$BACKUP_DIR/before-rollback-$STAMP"
  [[ -e "$BEFORE" || "$BEFORE" == "$LAST_BACKUP" ]] && BEFORE="$BEFORE-$$"   # nie die Sicherung überschreiben, die gleich zurückkommt
  say "Zurück zu $(basename "$PREV") – zuerst Sicherung des jetzigen Stands ($BEFORE)"
  backup_now "$BEFORE"                       # im Betrieb; nach dem Anhalten nochmals die Datenbank (genauer Stand)
  PHASE=running; RESTORED=0
  rollback_failed() {
    local rc=$?
    trap - EXIT
    [[ $PHASE == finished || $rc -eq 0 ]] && exit "$rc"
    set +e
    warn "Rückkehr fehlgeschlagen – die bisherige Version $CUR_VERSION wird wieder gestartet."
    if [[ $PHASE == stopped ]]; then
      systemctl stop n-fizyo
      [[ $RESTORED -eq 1 ]] && restore_db "$BEFORE"
      if [[ -n "$CUR" ]]; then ln -sfn "$CUR" "$APP_DIR/app"; ln -sfn "$PREV" "$APP_DIR/previous"; pip_install "$CUR"; fi
      systemctl start n-fizyo
    fi
    if wait_healthy "$CUR_VERSION"; then warn "Version $CUR_VERSION läuft wieder (Sicherung: $BEFORE)."
    else warn "Antwortet nicht – journalctl -u n-fizyo -n 80. Sicherung: $BEFORE"; fi
    exit 1
  }
  trap rollback_failed EXIT
  systemctl stop n-fizyo
  PHASE=stopped
  backup_dbs "$BEFORE"
  if [[ -n "$LAST_BACKUP" ]]; then
    echo "Datenbank von vor dem Update wird zurückgespielt ($LAST_BACKUP)."
    echo "Achtung: Einträge seit dem Update stehen nur noch in der Sicherung $BEFORE"
    RESTORED=1
    restore_db "$LAST_BACKUP"
  fi
  ln -sfn "$PREV" "$APP_DIR/app"
  if [[ -n "$CUR" ]]; then ln -sfn "$CUR" "$APP_DIR/previous"; fi
  pip_install "$PREV"
  systemctl start n-fizyo
  wait_healthy "$EXPECTED" || { warn "Version $EXPECTED antwortet nicht."; false; }
  PHASE=finished
  trap - EXIT
  # Ein weiteres --rollback (zurück zur eben verlassenen Version) spielt den Stand von eben zurück – nicht die alte Sicherung
  echo "$BEFORE" > "$APP_DIR/.last-update-backup"
  echo "Läuft wieder mit Version $EXPECTED. Stand vor der Rückkehr: $BEFORE"
  exit 0
fi

# ------------------------------------------------------------------ Vergleich
NEW_VERSION="$(version_of_dir "$SRC_DIR")"
[[ -n "$NEW_VERSION" ]] || { echo "Version im Paket nicht gefunden ($SRC_DIR/nfizyo/__init__.py)." >&2; exit 1; }
OLD_REL="$(current_release)"
CUR_VERSION="$(version_of_dir "$OLD_REL")"
OLD_JSON="$(versions_of "$APP_DIR/app")"
say "Änderungen"
NEW_JSON="$(cd "$SRC_DIR" && "$PY" -m nfizyo versions --json 2>/dev/null || echo '{}')"
compare "$OLD_JSON" "$NEW_JSON"
if [[ -n "$CUR_VERSION" ]]; then
  CMP="$(vcmp "$NEW_VERSION" "$CUR_VERSION")"
  if [[ "$CMP" == 0 && $FORCE -ne 1 ]]; then
    echo "Version $NEW_VERSION ist schon installiert – nichts zu tun (erneut einspielen: --force)."
    exit 0
  fi
  if [[ "$CMP" == -1 && $FORCE -ne 1 ]]; then
    echo "Version $NEW_VERSION ist älter als die installierte ($CUR_VERSION) – abgelehnt." >&2
    echo "Zurück zur vorherigen Version: sudo bash $APP_DIR/app/deploy/update.sh --rollback" >&2
    exit 1
  fi
fi
[[ $MODE == check ]] && exit 0

# ------------------------------------------------------------------ Update
STAMP="$(date +%Y-%m-%d_%H%M%S)"
REL="$APP_DIR/releases/${NEW_VERSION}_${STAMP}"
PRE="$BACKUP_DIR/pre-update-$STAMP"
OLD_PREV="$(link_target "$APP_DIR/previous")"
OLD_MARK="$(head -n 1 "$APP_DIR/.last-update-backup" 2>/dev/null || true)"
LEGACY=0
[[ -d "$APP_DIR/app" && ! -L "$APP_DIR/app" ]] && LEGACY=1    # alte Installation ohne Versionsordner
[[ ! -e "$REL" ]] || { echo "$REL gibt es schon – bitte in einer Sekunde nochmals." >&2; exit 1; }
PHASE=prepare      # prepare: Dienst läuft noch · stopped: Dienst aus · finished: fertig
MIGRATED=0
PIP_TOUCHED=0

on_exit() {
  local rc=$?
  trap - EXIT
  if [[ $PHASE == finished || $rc -eq 0 ]]; then exit "$rc"; fi
  set +e
  if [[ $PHASE == prepare ]]; then
    warn "Update abgebrochen, bevor der Dienst angehalten wurde – Version ${CUR_VERSION:-?} läuft weiter."
    rm -rf "$REL"
    if [[ $LEGACY -eq 1 ]]; then rm -rf "$APP_DIR/releases/legacy_$STAMP"; fi
    if [[ $PIP_TOUCHED -eq 1 && -n "$OLD_REL" && -d "$OLD_REL" ]]; then
      pip_install "$OLD_REL" >/dev/null 2>&1 || warn "Hinweis: Pakete der alten Version nicht wiederhergestellt (Internet?)."
    fi
    exit 1
  fi
  warn "Update fehlgeschlagen (Code $rc) – automatische Rückkehr zur vorherigen Version …"
  systemctl stop n-fizyo
  [[ $MIGRATED -eq 1 ]] && restore_db "$PRE"
  if [[ -n "$OLD_REL" && -d "$OLD_REL" ]]; then
    ln -sfn "$OLD_REL" "$APP_DIR/app"
    pip_install "$OLD_REL" >/dev/null 2>&1 || warn "Hinweis: Pakete der alten Version nicht vollständig wiederhergestellt."
  fi
  # Zustand wie vor dem Versuch: «previous» und die Marke für --rollback unverändert lassen; neuer Ordner weg
  if [[ -n "$OLD_PREV" ]]; then ln -sfn "$OLD_PREV" "$APP_DIR/previous"; else rm -f "$APP_DIR/previous"; fi
  if [[ -n "$OLD_MARK" ]]; then echo "$OLD_MARK" > "$APP_DIR/.last-update-backup"; else rm -f "$APP_DIR/.last-update-backup"; fi
  [[ "$(readlink -f "$APP_DIR/app")" != "$REL" ]] && rm -rf "$REL"
  systemctl start n-fizyo
  if wait_healthy "${CUR_VERSION:-?}"; then
    warn "Vorherige Version ${CUR_VERSION:-?} läuft wieder. Nichts verloren (Sicherung: $PRE)."
  else
    warn "Auch die vorherige Version antwortet nicht. Bitte NSystem anrufen. Sicherung: $PRE"
  fi
  exit 1
}
trap on_exit EXIT

say "Neue Version nach $REL (Dienst läuft weiter)"
install -d -m 0755 "$APP_DIR/releases"
if [[ $LEGACY -eq 1 ]]; then
  rsync -a "$APP_DIR/app/" "$APP_DIR/releases/legacy_$STAMP/"
  OLD_REL="$APP_DIR/releases/legacy_$STAMP"
fi
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '/data/' --exclude '.env' --exclude 'tests/' "$SRC_DIR/" "$REL/"
PIP_TOUCHED=1
pip_install "$REL"

say "Sicherung ($PRE)"
backup_now "$PRE"

say "Dienst anhalten, Datenbank nochmals sichern (genauer Stand), umschalten und migrieren"
PHASE=stopped
systemctl stop n-fizyo
backup_dbs "$PRE"
if [[ $LEGACY -eq 1 ]]; then rm -rf "$APP_DIR/app"; fi
ln -sfn "$REL" "$APP_DIR/app"
MIGRATED=1
(cd "$APP_DIR/app" && as_app "$PY" -m nfizyo --env "$ENV_FILE" migrate)

say "Starten und prüfen"
systemctl start n-fizyo
wait_healthy "$NEW_VERSION" || { warn "/health meldet nicht Version $NEW_VERSION"; false; }
PHASE=finished
trap - EXIT

if [[ -n "$OLD_REL" ]]; then ln -sfn "$OLD_REL" "$APP_DIR/previous"; fi
echo "$PRE" > "$APP_DIR/.last-update-backup"   # für «update.sh --rollback»; n-fizyo-backup löscht diese Sicherung nicht
if [[ -f "$REL/deploy/n-fizyo-backup" && -d "$SBIN_DIR" ]]; then install -m 0700 "$REL/deploy/n-fizyo-backup" "$SBIN_DIR/n-fizyo-backup" || true; fi
# Verschlüsselung der externen Sicherung (0.9.0) braucht gpg – auf älteren Boxen nachinstallieren
command -v gpg >/dev/null || { DEBIAN_FRONTEND=noninteractive apt-get install -y -q gnupg >/dev/null 2>&1 || warn "Hinweis: gnupg fehlt (externe Sicherung)."; }
# Supportzugang (0.9.0) vorbereiten bzw. Werkzeug erneuern – öffnet nichts
SUPPORT_SETUP="${SUPPORT_SETUP:-$REL/deploy/support-setup.sh}"
if [[ -f "$SUPPORT_SETUP" ]]; then
  bash "$SUPPORT_SETUP" >/dev/null 2>&1 || warn "Hinweis: Supportzugang nicht vorbereitet (deploy/support-setup.sh)."
fi
(cd "$APP_DIR/app" && as_app "$PY" -m nfizyo --env "$ENV_FILE" check-ai) || warn "Hinweis: KI-Prüfung meldet Probleme (Update bleibt aktiv)."

say "Aufräumen (letzte $KEEP_RELEASES Versionen bleiben)"
KEEP_A="$(readlink -f "$APP_DIR/app")"; KEEP_B="$(link_target "$APP_DIR/previous")"
# Reihenfolge nach dem Zeitstempel im Ordnernamen (<version>_JJJJ-MM-TT_hhmmss) – nicht nach Änderungszeit der Dateien
n=0
while IFS= read -r old; do
  [[ -n "$old" ]] || continue
  n=$((n + 1))
  [[ $n -le $KEEP_RELEASES || "$old" == "$KEEP_A" || "$old" == "$KEEP_B" ]] && continue
  rm -rf "$old"
done < <(for d in "$APP_DIR"/releases/*/; do
           [[ -d "$d" ]] || continue
           d="${d%/}"
           stamp="$(basename "$d" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}$' || true)"
           printf '%s\t%s\n' "${stamp:-0000-00-00_000000}" "$d"
         done | sort -r | cut -f2-)

# Runner (NSystem-Befehle) mit neuem Code – ausser das Update kommt vom Runner selbst (der lädt sich danach neu)
if [[ "${NFIZYO_FROM_RUNNER:-}" != "1" ]]; then systemctl try-restart --no-block n-fizyo-runner 2>/dev/null || true; fi
# Servicekabel (falls eingerichtet) ebenso – ausser das Update kam über das Kabel (der Dienst lädt sich danach selbst neu)
if [[ "${NFIZYO_FROM_SERVICE:-}" != "1" ]]; then systemctl try-restart --no-block n-fizyo-service 2>/dev/null || true; fi

echo
echo "Update fertig: Version $NEW_VERSION läuft. Sicherung von vorher: $PRE"
echo "Zurück zur vorherigen Version: sudo bash $REL/deploy/update.sh --rollback"
