"""Runner-Dienst der Box (läuft als root, systemd «n-fizyo-runner»).

- Nimmt Befehle aus /var/lib/n-fizyo/runner/spool/ (abgelegt von der App).
- Prüft die Signatur von NSystem ein ZWEITES Mal (die App könnte kompromittiert sein), dazu Box-ID, Ablaufzeit und
  ob der Befehl schon ausgeführt wurde (eigene Liste unter /var/lib/n-fizyo-runner/, nur root).
- Führt nur Befehle der festen Liste aus (agent/actions.py), mit Zeitlimit. Ergebnis → runner/results/<id>.json.
- Der Ordner runner/ gehört dem App-Benutzer: lesen, schreiben und löschen dort nur über agent/safefs.py (keine Symlinks,
  keine Pfadteile aus Aufträgen, chown nur über den eigenen Deskriptor) – sonst könnte eine kompromittierte App root werden.
- «Neue Version» nur zu einer NEUEREN Version als installiert (und nie unter die höchste schon eingespielte); zurück nur über
  den eigenen Befehl «rollback» (update.sh --rollback, vorherige Version).
- «Neue Version»: nur von NFIZYO_RELEASE_BASE, nur wenn das Manifest mit dem Release-Schlüssel von NSystem signiert ist
  (NFIZYO_RELEASE_PUBKEY) und Dateiname + SHA-256 zu Befehl und Manifest passen – erst dann läuft update.sh.
- Aufträge der App (runner/local/, HMAC mit NFIZYO_SECRET_KEY): NUR Supportzugang öffnen/schliessen, ausgelöst von der Leitung
  (agent/support.py). NSystem-Befehle können den Zugang nicht öffnen, nur schliessen. Ablauf wird hier jede Runde geprüft.
- Schreibt jede Minute runner/status.json: Alter der letzten Sicherung, Ergebnis der letzten Sicherung und der
  verschlüsselten externen Kopie (ok/fehlgeschlagen, Zeit, Grösse), Dienste, Grafikkarte, Supportzugang (offen bis) –
  keine Patientendaten.

Aufruf: python -m nfizyo.agent.runner --env /etc/n-fizyo/n-fizyo.env [--once | --support-expire]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from ..core import nsystem
from ..core.config import load_settings
from . import actions, release, safefs, signing, support

STATE_DIR = Path(os.environ.get("NFIZYO_RUNNER_STATE", "/var/lib/n-fizyo-runner"))
BACKUP_DIR = Path(os.environ.get("NFIZYO_BACKUP_DIR", "/var/backups/n-fizyo"))
APP_DIR = Path(os.environ.get("NFIZYO_APP_DIR", "/opt/n-fizyo"))
APP_USER = os.environ.get("NFIZYO_APP_USER", "nfizyo")
TIMEOUTS = {"diagnose": 60, "backup_now": 1800, "restart_app": 120, "restart_ai": 180, "update": 3600, "rollback": 1800,
            "support_close": 120}


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        env = dict(os.environ, NFIZYO_FROM_RUNNER="1")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)
        out = (p.stdout + ("\n" + p.stderr if p.stderr else "")).strip()
        return p.returncode, out[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "Zeitlimit überschritten"
    except FileNotFoundError as exc:
        return 127, str(exc)


def _done_ids() -> set[str]:
    f = STATE_DIR / "done.json"
    try:
        return set(json.loads(f.read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def _mark_done(cid: str) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    ids = sorted(_done_ids() | {cid})[-5000:]
    tmp = STATE_DIR / "done.json.tmp"
    tmp.write_text(json.dumps(ids))
    tmp.replace(STATE_DIR / "done.json")


def backup_age_hours() -> float | None:
    """Alter der jüngsten Sicherung. Nicht mitgezählt: Arbeitsordner (.offsite-…) und aus der externen Kopie geholte Sätze."""
    try:
        newest = max((p.stat().st_mtime for p in BACKUP_DIR.iterdir()
                      if p.is_dir() and not p.name.startswith((".", "offsite-"))), default=None)
    except OSError:
        return None
    return round((time.time() - newest) / 3600, 1) if newest else None


BACKUP_STATES = {"local": ("ok", "failed"), "offsite": ("ok", "failed", "off")}
OFFSITE_ERRORS = {"config", "key_missing", "key_perm", "gpg_missing", "rclone_missing", "encrypt", "upload", "list", "verify", "prune"}


def _num(v) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def backup_status() -> dict | None:
    """Ergebnis des letzten Laufs von n-fizyo-backup (<Sicherungsordner>/status.json). Nur feste Felder weitergeben:
    Zustand, Zeit, Grösse, Anzahl externer Sätze, Fehlercode – keine Pfade, kein Ziel, keine Patientendaten."""
    try:
        raw = json.loads((BACKUP_DIR / "status.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for part, states in BACKUP_STATES.items():
        src = raw.get(part)
        if not isinstance(src, dict) or src.get("state") not in states:
            continue
        item = {"state": src["state"], "at": _num(src.get("at")), "size_mb": _num(src.get("size_mb"))}
        if part == "offsite":
            item["sets"] = _num(src.get("sets"))
            item["last_ok_at"] = _num(src.get("last_ok_at"))
            item["error"] = src.get("error") if src.get("error") in OFFSITE_ERRORS else None
        out[part] = item
    return out or None


def services() -> dict:
    out = {}
    for name in ("n-fizyo", "ollama", "caddy", "n-fizyo-backup.timer"):
        code, text = _run(["systemctl", "is-active", name], 10)
        out[name] = text.splitlines()[0] if text else ("active" if code == 0 else "unknown")
    return out


def gpu() -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    code, text = _run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], 15)
    if code != 0 or not text:
        return {"error": "nvidia-smi"}
    name, used, total, temp = [x.strip() for x in text.splitlines()[0].split(",")][:4]
    return {"name": name, "mem_used_mb": int(float(used)), "mem_total_mb": int(float(total)), "temp_c": int(float(temp))}


def write_status(data_dir: Path, settings=None) -> None:
    status = {"updated_at": int(time.time()), "backup_age_h": backup_age_hours(), "backup": backup_status(), "services": services(),
              "gpu": gpu()}
    if settings is not None:
        status["support"] = support.status(settings, STATE_DIR)
    safefs.write_json_atomic(data_dir, ("runner",), "status.json", json.dumps(status), owner=APP_USER)


def _chown(path: Path) -> None:
    """Nur noch für Aufrufer ausserhalb von safefs (keine Datei im Ordner der App) – folgt keinem Symlink."""
    try:
        import pwd

        pw = pwd.getpwnam(APP_USER)
        os.chown(path, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
    except (LookupError, ImportError, PermissionError, OSError):
        pass


def _download(url: str, dest: Path, max_time: int) -> tuple[int, str]:
    """Nur https (auch bei Weiterleitungen)."""
    return _run(["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--max-time", str(max_time), "-o", str(dest), url],
                max_time + 60)


def _update(args: dict, settings) -> tuple[int, str]:
    """Neue Version: signiertes Manifest holen und prüfen, Archiv holen, Prüfsumme vergleichen, erst dann update.sh."""
    base = (getattr(settings, "release_base", "") or os.environ.get("NFIZYO_RELEASE_BASE", "")).rstrip("/")
    pub = getattr(settings, "release_pubkey", "") or os.environ.get("NFIZYO_RELEASE_PUBKEY", "")
    if not base.startswith("https://"):
        return 1, "NFIZYO_RELEASE_BASE fehlt (https://…)"
    if not pub:
        return 1, "NFIZYO_RELEASE_PUBKEY fehlt – nur signierte Versionen werden eingespielt"
    work = Path(tempfile.mkdtemp(prefix="n-fizyo-update-"))
    try:
        mname = release.manifest_for_file(args["file"])
        code, text = _download(f"{base}/{mname}", work / mname, 60)
        if code != 0:
            return code, "Manifest nicht geladen: " + text[-300:]
        try:
            manifest = release.verify_manifest(pub, json.loads((work / mname).read_text(encoding="utf-8")))
        except (release.ReleaseError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            return 1, f"Manifest abgelehnt: {exc} – Update abgebrochen"
        if manifest["file"] != args["file"] or manifest["sha256"] != args["sha256"] or \
                (args.get("version") and args["version"] != manifest["version"]):
            return 1, "Befehl passt nicht zum signierten Manifest – Update abgebrochen"
        refused = downgrade_reason(manifest["version"])
        if refused:
            return 1, refused
        archive = work / args["file"]
        code, text = _download(f"{base}/{args['file']}", archive, 600)
        if code != 0:
            return code, "Download fehlgeschlagen: " + text[-300:]
        return install_archive(work, archive, manifest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install_archive(work: Path, archive: Path, manifest: dict, run=None) -> tuple[int, str]:
    """Geprüftes Manifest + Archiv → Prüfsumme, Version im Paket, dann update.sh des Pakets. Gemeinsam für «Neue Version»
    (Download) und das Servicekabel (agent/service.py). run: eigener Aufrufer (Servicekabel), sonst _run des Runners."""
    run = run or _run
    if release.sha256_file(archive) != manifest["sha256"]:
        return 1, "Prüfsumme stimmt nicht – Update abgebrochen"
    refused = downgrade_reason(manifest["version"])
    if refused:
        return 1, refused
    with tarfile.open(archive) as tar:
        tar.extractall(work / "src", filter="data")
    roots = [p for p in (work / "src").iterdir() if p.is_dir()]
    src = roots[0] if len(roots) == 1 else work / "src"
    script = src / "deploy" / "update.sh"
    if not script.exists():
        return 1, "deploy/update.sh fehlt im Paket"
    try:
        if release.repo_version(src) != manifest["version"]:
            return 1, "Version im Paket passt nicht zum Manifest – Update abgebrochen"
    except (release.ReleaseError, OSError) as exc:
        return 1, f"Paket unlesbar: {exc}"
    code, text = run(["bash", str(script)], TIMEOUTS["update"])
    if code == 0:
        _remember_version(manifest["version"])
    return code, text


def installed_version() -> str | None:
    try:
        return release.repo_version(APP_DIR / "app")
    except (release.ReleaseError, OSError):
        return None


def _highest_version() -> str | None:
    try:
        v = json.loads((STATE_DIR / "release.json").read_text()).get("highest")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and release.VERSION_RX.fullmatch(v) else None


def _remember_version(version: str) -> None:
    """Höchste je eingespielte Version (nur root) – ältere signierte Versionen werden danach nie mehr angenommen."""
    best = _highest_version()
    if best and release.version_key(best) >= release.version_key(version):
        return
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE_DIR / ".release.json.tmp"
    tmp.write_text(json.dumps({"highest": version}))
    tmp.replace(STATE_DIR / "release.json")


def downgrade_reason(version: str) -> str | None:
    """«Neue Version» nur, wenn neuer als die installierte und nicht älter als die höchste je eingespielte.
    Zurück geht es nur über den Befehl «rollback» (vorherige Version mit Datenbank von vor dem Update)."""
    current = installed_version()
    if current is None:
        return "Installierte Version unbekannt – Update abgebrochen"
    if release.version_key(version) <= release.version_key(current):
        return f"Version {version} ist nicht neuer als die installierte ({current}) – Update abgebrochen (zurück nur mit «rollback»)"
    best = _highest_version()
    if best and release.version_key(version) < release.version_key(best):
        return f"Version {version} ist älter als die schon eingespielte {best} – Update abgebrochen"
    return None


def execute(cmd: dict, settings) -> tuple[str, str]:
    action, args = cmd["action"], cmd.get("args") or {}
    if action == "diagnose":
        info = {"services": services(), "gpu": gpu(), "backup_age_h": backup_age_hours(), "backup": backup_status(),
                "disk_free_gb": round(shutil.disk_usage(settings.data_dir).free / 1e9, 1)}
        code, versions = _run([str(APP_DIR / "venv/bin/python"), "-m", "nfizyo", "--env", os.environ.get("NFIZYO_ENV_FILE", ""),
                               "versions", "--json"], 60)
        info["versions"] = versions if code == 0 else "?"
        return "done", json.dumps(info)[:4000]
    if action == "backup_now":
        code, text = _run(["/usr/local/sbin/n-fizyo-backup"], TIMEOUTS[action])
    elif action == "restart_app":
        code, text = _run(["systemctl", "restart", "n-fizyo"], TIMEOUTS[action])
    elif action == "restart_ai":
        code, text = _run(["systemctl", "restart", "ollama"], TIMEOUTS[action])
    elif action == "rollback":
        code, text = _run(["bash", str(APP_DIR / "app/deploy/update.sh"), "--rollback"], TIMEOUTS[action])
    elif action == "update":
        code, text = _update(args, settings)
    elif action == "support_close":
        was_open = support.close_access(STATE_DIR, _events_dir(settings), "nsystem", chown=_chown)
        return "done", "Supportzugang geschlossen" if was_open else "Supportzugang war nicht offen"
    else:
        return "rejected", "not_allowed"
    return ("done" if code == 0 else "failed"), (f"Exit {code}\n" + text)[-4000:]


def _load(data_dir: Path, folder: str, name: str):
    """Auftrag lesen und löschen (sicher, siehe safefs). None = unlesbar."""
    try:
        raw = safefs.read_and_remove(data_dir, ("runner", folder), name)
        return json.loads(raw) if raw is not None else None
    except (OSError, ValueError):
        return None


def process(settings) -> int:
    n = 0
    try:
        names = safefs.list_json(settings.data_dir, "runner", "spool")
    except OSError:                                   # z. B. spool ist ein Symlink → nichts anfassen
        return 0
    for name in names:
        cmd = _load(settings.data_dir, "spool", name)
        if not isinstance(cmd, dict):
            continue
        cid = str(cmd.get("id") or "")
        if not safefs.valid_id(cid):                  # ID wird Dateiname des Ergebnisses: nur [A-Za-z0-9_-]
            continue
        reason = signing.verify_command(settings.central_command_pubkey, cmd, box_id=settings.central_box_id)
        if reason is None and cid in _done_ids():
            reason = "replay"
        if reason is None and not actions.valid_args(str(cmd.get("action")), cmd.get("args") or {}):
            reason = "not_allowed"
        if reason:
            outcome, summary = "rejected", reason
        else:
            _mark_done(cid)
            _write(settings.data_dir, {"id": cid, "status": "running"})
            outcome, summary = execute(cmd, settings)
        _write(settings.data_dir, {"id": cid, "status": outcome, "summary": summary, "reason": summary if outcome == "rejected" else None})
        n += 1
        if outcome == "done" and cmd.get("action") in ("update", "rollback"):
            RESTART_SELF.append(True)  # neuen Programmcode laden (siehe main)
    return n


RESTART_SELF: list[bool] = []


def _events_dir(settings) -> Path:
    return nsystem.runner_paths(settings)["events"]


def process_local(settings) -> int:
    """Aufträge der App: nur Supportzugang öffnen/schliessen (Leitung). Signatur (HMAC), Alter, Dauer und Wiederholung geprüft."""
    n = 0
    try:
        names = safefs.list_json(settings.data_dir, "runner", "local")
    except OSError:
        return 0
    for name in names:
        req = _load(settings.data_dir, "local", name)
        rid = str((req if isinstance(req, dict) else {}).get("id") or name[:-5])[:80]
        reason = nsystem.check_local_request(settings.secret_key, req) if isinstance(req, dict) else "invalid"
        if reason is None and f"local:{rid}" in _done_ids():
            reason = "replay"
        if reason:
            support.emit(_events_dir(settings), "support_request_rejected", {"request_id": rid, "reason": reason}, chown=_chown)
            n += 1
            continue
        _mark_done(f"local:{rid}")
        if req["action"] == "support_open":
            support.open_access(settings, STATE_DIR, _events_dir(settings), rid, int(req["hours"]), chown=_chown)
        else:
            support.close_access(STATE_DIR, _events_dir(settings), "early", rid, chown=_chown)
        n += 1
    return n


def support_tick(settings) -> None:
    try:
        support.tick(settings, STATE_DIR, _events_dir(settings), chown=_chown)
    except Exception:  # noqa: BLE001 - Prüfung darf den Runner nie anhalten; nächste Runde
        pass


def _reexec(env_file: str) -> None:
    """Nach Update/Rückkehr: Runner mit dem Code der jetzt aktiven Version neu starten."""
    import sys

    os.chdir(APP_DIR / "app")
    os.execv(sys.executable, [sys.executable, "-m", "nfizyo.agent.runner", "--env", env_file])


def _write(data_dir: Path, data: dict) -> None:
    """Ergebnis → runner/results/<id>.json (ID vorher geprüft; nie über Symlinks)."""
    if not safefs.valid_id(str(data.get("id") or "")):
        return
    try:
        safefs.write_json_atomic(data_dir, ("runner", "results"), f"{data['id']}.json", json.dumps(data), owner=APP_USER)
    except OSError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="N-Fizyo Runner (root)")
    ap.add_argument("--env", default="/etc/n-fizyo/n-fizyo.env")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--support-expire", action="store_true", help="nur Ablauf des Supportzugangs prüfen (systemd-Zeitgeber)")
    a = ap.parse_args()
    os.environ.setdefault("NFIZYO_ENV_FILE", a.env)
    settings = load_settings(a.env)
    if a.support_expire:
        support_tick(settings)
        return
    while True:
        process(settings)
        process_local(settings)
        support_tick(settings)
        try:
            write_status(settings.data_dir, settings)
        except Exception:  # noqa: BLE001
            pass
        if a.once:
            break
        if RESTART_SELF:
            _reexec(a.env)
        time.sleep(20)


if __name__ == "__main__":
    main()
