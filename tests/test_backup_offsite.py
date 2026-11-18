"""Externe verschlüsselte Sicherung (deploy/n-fizyo-backup, deploy/restore.sh --offsite): Ein lokaler Ordner spielt das
externe Ziel (gleicher rsync-Weg wie für einen SSH-Server). Geprüft: nur verschlüsselt draussen, N Sätze bleiben, fremde Dateien
bleiben, Zurückholen mit Prüfmodus, falscher Schlüssel / veränderte Datei abgelehnt, Fehler gemeldet ohne die lokale Sicherung
zu verlieren, Zustand für die NSystem-Übersicht ohne Pfade oder Inhalte."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "deploy" / "n-fizyo-backup"
RESTORE = ROOT / "deploy" / "restore.sh"
pytestmark = pytest.mark.skipif(any(shutil.which(t) is None for t in ("sqlite3", "gpg", "rsync")),
                                reason="sqlite3, gpg oder rsync fehlt")


def _key(path: Path, text: str = "Testpassphrase-nur-fuer-den-Test-0123456789") -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


def _env(tmp: Path, data: Path, remote: Path | str, key: Path, keep: int = 2) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NFIZYO_")}
    return {**env, "NFIZYO_DATA": str(data), "NFIZYO_BACKUP_DIR": str(tmp / "backups"), "NFIZYO_NO_SYSTEMD": "1",
            "NFIZYO_ENV_FILE": str(tmp / "keine.env"), "NFIZYO_OFFSITE_TARGET": str(remote),
            "NFIZYO_OFFSITE_KEY_FILE": str(key), "NFIZYO_OFFSITE_KEEP": str(keep)}


def _run(args: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", *map(str, args)], env=env, capture_output=True, text=True, timeout=120)


def _small_data(path: Path) -> Path:
    path.mkdir(parents=True)
    con = sqlite3.connect(path / "n-fizyo.sqlite3")
    con.execute("CREATE TABLE patients (id INTEGER PRIMARY KEY, last_name TEXT)")
    con.execute("INSERT INTO patients (last_name) VALUES ('Beispiel')")
    con.commit()
    con.close()
    (path / "documents").mkdir()
    (path / "documents" / "bericht.pdf").write_bytes(b"%PDF-1.4 erfunden")
    return path


def _status(tmp: Path) -> dict:
    return json.loads((tmp / "backups" / "status.json").read_text())


def test_offsite_copy_encrypted_pruned_and_restorable(app, tmp_path):
    from nfizyo.core.keys import DB, SETTINGS

    data = app[SETTINGS].data_dir
    app[DB].execute("PRAGMA wal_checkpoint(TRUNCATE)")
    (data / "videos" / "1").mkdir(parents=True, exist_ok=True)
    (data / "videos" / "1" / "abc.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    remote = tmp_path / "remote"
    remote.mkdir()
    for day in ("01", "02", "03"):                                    # ältere Sätze am Ziel
        (remote / f"n-fizyo-2020-01-{day}_0230.tar.gz.gpg").write_bytes(b"alt")
        (remote / f"n-fizyo-2020-01-{day}_0230.tar.gz.gpg.sha256").write_text("x")
    (remote / "n-fizyo-2019-12-31_0230.tar.gz.gpg").write_bytes(b"abgebrochen")   # Rest ohne Prüfsumme
    (remote / "fremd.txt").write_text("gehört nicht uns")
    key = _key(tmp_path / "offsite.key")
    env = _env(tmp_path, data, remote, key, keep=2)

    r = _run([BACKUP], env)
    assert r.returncode == 0, r.stderr + r.stdout
    sets = sorted(p.name for p in remote.glob("n-fizyo-*.gpg"))
    assert len(sets) == 2 and sets[0] == "n-fizyo-2020-01-03_0230.tar.gz.gpg"           # nur die letzten 2 Sätze
    assert (remote / "fremd.txt").exists() and not (remote / "n-fizyo-2019-12-31_0230.tar.gz.gpg").exists()
    new = remote / sets[1]
    blob = new.read_bytes()
    assert blob[0] in (0x8C, 0xC3) and not blob.startswith(b"\x1f\x8b")               # OpenPGP (symmetrisch), kein tar.gz
    assert (remote / f"{new.name}.sha256").read_text().split()[0] == hashlib.sha256(blob).hexdigest()
    status = _status(tmp_path)
    assert status["local"]["state"] == "ok" and status["offsite"]["state"] == "ok" and status["offsite"]["sets"] == 2
    assert status["offsite"]["error"] is None and status["offsite"]["last_ok_at"]
    secret = key.read_text()
    for text in (r.stdout, r.stderr, json.dumps(status)):
        assert secret not in text and str(remote) not in json.dumps(status)

    # Unglück: Patientendaten kaputt, Video weg
    db_path = data / "n-fizyo.sqlite3"
    patients = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    con = sqlite3.connect(db_path)
    con.execute("UPDATE patients SET last_name = 'KAPUTT'")
    con.commit()
    con.close()
    (data / "videos" / "1" / "abc.mp4").unlink()

    r = _run([RESTORE, "--offsite-list"], env)
    assert r.returncode == 0 and r.stdout.split()[0] == new.name, r.stderr
    r = _run([RESTORE, "--test", "--offsite", "latest"], env)
    assert r.returncode == 0 and "nichts geändert" in r.stdout, r.stderr + r.stdout
    assert not list((tmp_path / "backups").glob("offsite-*"))                         # Testmodus: keine Klartext-Kopie bleibt
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM patients WHERE last_name = 'KAPUTT'").fetchone()[0] == patients

    r = _run([RESTORE, "--offsite", new.name], env)
    assert r.returncode == 0, r.stderr + r.stdout
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM patients WHERE last_name = 'KAPUTT'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == patients
    con.close()
    assert (data / "videos" / "1" / "abc.mp4").exists()
    assert list((tmp_path / "backups").glob("before-restore-*"))                       # umkehrbar …
    assert len(list(remote.glob("n-fizyo-*.gpg"))) == 2                                 # … und dieser Zwischenstand geht nicht nach aussen
    assert _status(tmp_path) == status


def test_offsite_wrong_key_or_changed_file_is_refused(tmp_path):
    data = _small_data(tmp_path / "data")
    remote = tmp_path / "remote"
    remote.mkdir()
    env = _env(tmp_path, data, remote, _key(tmp_path / "offsite.key"))
    r = _run([BACKUP], env)
    assert r.returncode == 0, r.stderr
    name = next(remote.glob("*.gpg")).name

    wrong = _env(tmp_path, data, remote, _key(tmp_path / "anderer.key", "ein-ganz-anderer-schluessel-9876543210"))
    r = _run([RESTORE, "--test", "--offsite", "latest"], wrong)
    assert r.returncode != 0 and "Entschlüsseln fehlgeschlagen" in r.stderr

    blob = bytearray((remote / name).read_bytes())                                      # Datei unterwegs verändert
    blob[len(blob) // 2] ^= 0x01
    (remote / name).write_bytes(bytes(blob))
    r = _run([RESTORE, "--test", "--offsite", name], env)
    assert r.returncode != 0 and "Prüfsumme" in r.stderr
    (remote / f"{name}.sha256").write_text(f"{hashlib.sha256(bytes(blob)).hexdigest()}  {name}\n")   # auch mit passender Prüfsumme
    r = _run([RESTORE, "--test", "--offsite", name], env)
    assert r.returncode != 0 and "Entschlüsseln fehlgeschlagen" in r.stderr
    r = _run([RESTORE, "--test", "--offsite", "../../etc/passwd"], env)
    assert r.returncode != 0
    assert not list((tmp_path / "backups").glob("offsite-*"))
    assert sqlite3.connect(data / "n-fizyo.sqlite3").execute("SELECT last_name FROM patients").fetchone()[0] == "Beispiel"


@pytest.mark.parametrize("case", ["not_mounted", "key_perm", "key_missing"])
def test_offsite_failure_keeps_local_backup_and_is_reported(tmp_path, case):
    data = _small_data(tmp_path / "data")
    key = _key(tmp_path / "offsite.key")
    remote = tmp_path / "remote"
    if case == "not_mounted":                                  # Laufwerk nicht eingehängt: nie auf die eigene Platte schreiben
        remote = tmp_path / "mnt" / "n-fizyo"
    else:
        remote.mkdir()
    if case == "key_perm":
        key.chmod(0o644)
    elif case == "key_missing":
        key = tmp_path / "fehlt.key"
    r = _run([BACKUP], _env(tmp_path, data, remote, key))
    assert r.returncode == 2, r.stderr                         # Dienst meldet Fehler …
    sets = [p for p in (tmp_path / "backups").iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert len(sets) == 1 and (sets[0] / "n-fizyo.sqlite3").exists()     # … die lokale Sicherung ist trotzdem da
    st = _status(tmp_path)
    assert st["local"]["state"] == "ok" and st["offsite"]["state"] == "failed"
    assert st["offsite"]["error"] == {"not_mounted": "upload", "key_perm": "key_perm", "key_missing": "key_missing"}[case]
    assert not (tmp_path / "mnt").exists()
    assert st["offsite"]["last_ok_at"] is None
    assert not list((tmp_path / "backups").glob(".offsite-*"))         # keine verschlüsselten Reste liegen lassen


FAKE_RCLONE = r"""#!/usr/bin/env bash
# Platzhalter für rclone im Test: Remote «fake:<pfad>» = $FAKE_RCLONE_ROOT/<pfad>; nur copyto, lsf, deletefile
echo "$*" >> "$FAKE_RCLONE_ROOT/../rclone.log"
args=(); while [[ $# -gt 0 ]]; do case "$1" in --config|--retries|--low-level-retries) shift 2 ;; --files-only) shift ;;
  *) args+=("$1"); shift ;; esac; done
map() { local p=${1#fake:}; echo "$FAKE_RCLONE_ROOT/${p#/}"; }
case "${args[0]}" in
  copyto) s=${args[1]}; d=${args[2]}; [[ $s == fake:* ]] && s=$(map "$s"); [[ $d == fake:* ]] && d=$(map "$d")
          mkdir -p "$(dirname "$d")" && cp "$s" "$d" ;;
  lsf) d=$(map "${args[1]}"); [[ -d $d ]] || exit 3; find "$d" -maxdepth 1 -type f -printf '%f\n' ;;
  deletefile) rm "$(map "${args[1]}")" ;;
  *) exit 1 ;;
esac
"""


def test_rclone_target(tmp_path):
    """Ziel über rclone (z. B. S3/Swift/WebDAV eines Anbieters): gleiche Abläufe mit copyto, lsf, deletefile."""
    data = _small_data(tmp_path / "data")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rclone").write_text(FAKE_RCLONE)
    (bin_dir / "rclone").chmod(0o755)
    root = tmp_path / "anbieter"
    (root / "praxis" / "box1").mkdir(parents=True)
    (root / "praxis" / "box1" / "n-fizyo-2020-01-01_0230.tar.gz.gpg").write_bytes(b"alt")
    (root / "praxis" / "box1" / "n-fizyo-2020-01-01_0230.tar.gz.gpg.sha256").write_text("x")
    env = _env(tmp_path, data, "rclone:fake:praxis/box1", _key(tmp_path / "offsite.key"), keep=1)
    env.update(PATH=f"{bin_dir}:{env['PATH']}", FAKE_RCLONE_ROOT=str(root), NFIZYO_OFFSITE_RCLONE_CONFIG=str(tmp_path / "rclone.conf"))
    r = _run([BACKUP], env)
    assert r.returncode == 0, r.stderr + r.stdout
    names = sorted(p.name for p in (root / "praxis" / "box1").iterdir())
    assert len(names) == 2 and names[0].startswith("n-fizyo-20") and not names[0].startswith("n-fizyo-2020")
    log = (tmp_path / "rclone.log").read_text()
    assert f"--config {tmp_path / 'rclone.conf'}" in log and "deletefile fake:praxis/box1/n-fizyo-2020-01-01_0230.tar.gz.gpg" in log
    r = _run([RESTORE, "--test", "--offsite", "latest"], env)
    assert r.returncode == 0 and "nichts geändert" in r.stdout, r.stderr + r.stdout


def test_without_offsite_target_nothing_leaves_the_box(tmp_path):
    data = _small_data(tmp_path / "data")
    env = _env(tmp_path, data, "", tmp_path / "fehlt.key")
    r = _run([BACKUP], env)
    assert r.returncode == 0, r.stderr
    assert _status(tmp_path)["offsite"]["state"] == "off"
    assert _run([RESTORE, "--offsite-list"], env).returncode != 0


def test_runner_and_central_report_only_states(tmp_path, monkeypatch):
    from nfizyo.agent import runner
    from nfizyo.central import health

    backups = tmp_path / "backups"
    (backups / "2026-11-07_0230").mkdir(parents=True)
    old = time.time() - 40 * 3600
    os.utime(backups / "2026-11-07_0230", (old, old))
    (backups / "offsite-2026-11-07_120000-abcd").mkdir()                 # geholter Satz zählt nicht als neue Sicherung
    (backups / ".offsite-x1").mkdir()
    (backups / "status.json").write_text(json.dumps({
        "local": {"state": "ok", "at": 1794000000, "size_mb": 12.5, "pfad": "/var/lib/n-fizyo"},
        "offsite": {"state": "failed", "at": 1794000000, "size_mb": 0, "sets": None, "error": "/geheim/host:pfad",
                    "last_ok_at": int(time.time()) - 50 * 3600, "target": "user@host:/x"}}))
    monkeypatch.setattr(runner, "BACKUP_DIR", backups)
    assert runner.backup_age_hours() >= 39
    st = runner.backup_status()
    assert st == {"local": {"state": "ok", "at": 1794000000, "size_mb": 12.5},
                  "offsite": {"state": "failed", "at": 1794000000, "size_mb": 0, "sets": None, "last_ok_at": st["offsite"]["last_ok_at"],
                              "error": None}}
    assert "host" not in json.dumps(st) and "/" not in json.dumps(st)

    report = {"runner": {"updated_at": int(time.time()), "backup_age_h": 5, "backup": st, "services": {}}}
    kinds = {i.kind: i.message for i in health.issues(report)}
    assert "offsite" in kinds and "50 h" in kinds["offsite"] and "backup" not in kinds
    check = {c["key"]: c for c in health.checklist(report)}
    assert check["offsite"]["ok"] is False and check["offsite"]["required"] is False and check["backup"]["ok"] is True

    st["offsite"]["state"] = "ok"
    st["local"]["state"] = "failed"
    kinds = {i.kind: i.message for i in health.issues(report)}
    assert "offsite" not in kinds and kinds["backup"] == "Letzte Sicherung fehlgeschlagen"
    check = {c["key"]: c for c in health.checklist(report)}
    assert check["offsite"]["ok"] is True and check["backup"]["ok"] is False

    (backups / "status.json").write_text("kaputt")
    assert runner.backup_status() is None
