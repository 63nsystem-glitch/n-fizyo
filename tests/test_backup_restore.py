"""Sicherung und Wiederherstellung (deploy/n-fizyo-backup, deploy/restore.sh): Datenbank, Dokumente, eigene Videos, Diktate.
Probe aufs Exempel: Daten löschen → aus der Sicherung zurückholen → alles wieder da, vorheriger Stand ebenfalls gesichert."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3-Programm fehlt")


def _env(data: Path, backups: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NFIZYO_OFFSITE")}
    return {**env, "NFIZYO_DATA": str(data), "NFIZYO_BACKUP_DIR": str(backups), "NFIZYO_NO_SYSTEMD": "1",
            "NFIZYO_ENV_FILE": str(backups.parent / "keine.env")}


def test_backup_and_restore_round_trip(app, tmp_path):
    from nfizyo.core.keys import DB, SETTINGS

    settings = app[SETTINGS]
    data = settings.data_dir
    app[DB].execute("PRAGMA wal_checkpoint(TRUNCATE)")
    (data / "videos" / "1").mkdir(parents=True, exist_ok=True)
    (data / "videos" / "1" / "abc.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    (data / "audio").mkdir(exist_ok=True)
    (data / "audio" / "diktat.webm").write_bytes(b"\x1a\x45\xdf\xa3audio")
    backups = tmp_path / "backups"
    env = _env(data, backups)
    r = subprocess.run(["bash", str(ROOT / "deploy" / "n-fizyo-backup")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    snap = next(p for p in backups.iterdir() if p.is_dir())
    assert {p.name for p in snap.iterdir()} >= {"n-fizyo.sqlite3", "videos.tar.gz", "audio.tar.gz"}

    # Unglück: Patientin gelöscht, Video weg
    db_path = data / "n-fizyo.sqlite3"
    patients = sqlite3.connect(snap / "n-fizyo.sqlite3").execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("UPDATE patients SET last_name = 'KAPUTT'")
    con.commit()
    con.close()
    (data / "videos" / "1" / "abc.mp4").unlink()

    r = subprocess.run(["bash", str(ROOT / "deploy" / "restore.sh"), "--test", str(snap)], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "nichts geändert" in r.stdout, r.stderr
    r = subprocess.run(["bash", str(ROOT / "deploy" / "restore.sh"), str(snap)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM patients WHERE last_name = 'KAPUTT'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == patients
    con.close()
    assert (data / "videos" / "1" / "abc.mp4").exists() and (data / "audio" / "diktat.webm").exists()
    before = [p for p in backups.iterdir() if p.name.startswith("before-restore-")]
    assert before and (before[0] / "n-fizyo.sqlite3").exists()                     # Wiederherstellung ist umkehrbar
    kaputt = sqlite3.connect(before[0] / "n-fizyo.sqlite3").execute("SELECT COUNT(*) FROM patients WHERE last_name = 'KAPUTT'")
    assert kaputt.fetchone()[0] == patients


def test_restore_refuses_a_broken_backup(tmp_path):
    snap = tmp_path / "b" / "2026-01-01_0230"
    snap.mkdir(parents=True)
    (snap / "n-fizyo.sqlite3").write_bytes(b"kein SQLite")
    r = subprocess.run(["bash", str(ROOT / "deploy" / "restore.sh"), "--test", str(snap)], env=_env(tmp_path / "data", tmp_path / "b"),
                       capture_output=True, text=True)
    assert r.returncode != 0


def test_full_export_contains_own_videos(app):
    import zipfile

    from nfizyo.core import export
    from nfizyo.core.keys import DB, SETTINGS

    settings = app[SETTINGS]
    (settings.data_dir / "videos" / "1").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "videos" / "1" / "uebung.mp4").write_bytes(b"video")
    (settings.data_dir / "videos" / "2").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "videos" / "2" / "fremd.mp4").write_bytes(b"andere Praxis")
    path = export.build(app[DB], settings, 1)
    names = zipfile.ZipFile(path).namelist()
    assert "videos/uebung.mp4" in names and not any("fremd" in n for n in names)
