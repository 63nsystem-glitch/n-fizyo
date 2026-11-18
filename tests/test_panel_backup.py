"""Sicherung der NSystem-Übersicht (0.9.0): gleicher Weg wie die Box (deploy/n-fizyo-backup) – Datenbank + Schlüsselmaterial
(keys.tar.gz) lokal, verschlüsselt nach aussen; Zustand für den Dienst lesbar; Übersicht zeigt/meldet Probleme."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import time

import pytest

from nfizyo.central import app as central, ops
from nfizyo.central.config import load as load_central

from test_backup_offsite import BACKUP, _key

pytestmark = pytest.mark.skipif(any(shutil.which(t) is None for t in ("sqlite3", "gpg", "rsync")), reason="sqlite3, gpg oder rsync fehlt")


def _central(tmp_path):
    cs = load_central(None, data_dir=tmp_path / "central", secret_key=b"c" * 32, base_url="http://central.test")
    capp = central.create_central_app(cs, start_watch=False)        # legt central.sqlite3 an
    central.add_box(capp[central.DBK], cs.secret_key, "Physio Test")
    capp[central.DBK].execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cs.signing_key_path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n")
    env_file = tmp_path / "etc" / "central.env"
    env_file.parent.mkdir()
    env_file.write_text("NFIZYO_CENTRAL_SECRET_KEY=nur-ein-test\n")
    (cs.data_dir / "box-bx_neu.env").write_text("NFIZYO_CENTRAL_BOX_KEY=geheim-noch-nicht-kopiert\n")
    return cs, capp, env_file


def _run(env):
    return subprocess.run(["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=120)


def test_panel_backup_db_and_keys_encrypted_offsite(tmp_path):
    cs, capp, env_file = _central(tmp_path)
    remote = tmp_path / "remote"
    remote.mkdir()
    key = _key(tmp_path / "offsite.key")
    env = {k: v for k, v in os.environ.items() if not k.startswith("NFIZYO_")}
    env.update(NFIZYO_DATA=str(cs.data_dir), NFIZYO_BACKUP_DIR=str(tmp_path / "backups"), NFIZYO_ENV_FILE=str(env_file),
               NFIZYO_BACKUP_STATUS_FILE=str(cs.backup_status_path), NFIZYO_OFFSITE_TARGET=str(remote), NFIZYO_OFFSITE_KEY_FILE=str(key),
               NFIZYO_BACKUP_FILES=f"{env_file} {cs.signing_key_path} {tmp_path / 'etc' / 'nfz-hub.key'}")
    r = _run(env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "nfz-hub.key" in r.stderr                                         # fehlende Datei gemeldet, nicht fatal
    sets = [p for p in (tmp_path / "backups").iterdir() if p.is_dir()]
    assert len(sets) == 1
    s = sets[0]
    con = sqlite3.connect(s / "central.sqlite3")
    assert con.execute("SELECT name FROM boxes").fetchone()[0] == "Physio Test"
    con.close()
    with tarfile.open(s / "keys.tar.gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("central.env") for n in names) and any(n.endswith("command-signing.pem") for n in names)
        assert not any(n.startswith("/") for n in names)
    assert not (s / "box-bx_neu.env").exists()                               # Verbindungsdateien für Boxen gehören nicht dazu
    # extern nur verschlüsselt
    blobs = list(remote.glob("n-fizyo-*.tar.gz.gpg"))
    assert len(blobs) == 1 and blobs[0].read_bytes()[0] in (0x8C, 0xC3) and b"nur-ein-test" not in blobs[0].read_bytes()
    st = ops.backup_state(cs)
    assert st["known"] and st["local"] == "ok" and st["offsite"] == "ok" and st["problem"] is None
    # geholt und entschlüsselt: Schlüssel und Datenbank sind wieder da (Ablauf wie docs/MERKEZ.md › Panel yedeği)
    out = tmp_path / "restore"
    r = subprocess.run(["bash", str(BACKUP), "--offsite-fetch", "latest", str(out)], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    restored = r.stdout.strip().splitlines()[-1]
    assert os.path.exists(os.path.join(restored, "central.sqlite3")) and os.path.exists(os.path.join(restored, "keys.tar.gz"))


async def test_panel_shows_and_pushes_own_backup_problem(tmp_path, monkeypatch, aiohttp_client):
    pushes = []

    async def fake_push(settings, title, message, priority="high"):
        pushes.append(message)
        return True

    monkeypatch.setattr(central, "push", fake_push)
    cs = load_central(None, data_dir=tmp_path / "central", secret_key=b"c" * 32, base_url="http://central.test")
    capp = central.create_central_app(cs, start_watch=False)
    await central.check_own_backup(capp)
    assert pushes == []                                                      # noch kein Ergebnis: kein Alarm
    now = int(time.time())
    cs.backup_status_path.write_text(json.dumps({"local": {"state": "failed", "at": now, "size_mb": 0},
                                                 "offsite": {"state": "off", "at": now}}))
    await central.check_own_backup(capp)
    await central.check_own_backup(capp)
    assert pushes == ["Sicherung des Panels fehlgeschlagen"]                 # nur einmal
    cs.backup_status_path.write_text(json.dumps({"local": {"state": "ok", "at": now, "size_mb": 1.2},
                                                 "offsite": {"state": "ok", "at": now, "sets": 3}}))
    await central.check_own_backup(capp)
    assert ops.get_state(capp[central.DBK], "backup_alert") is None
    cs.backup_status_path.write_text(json.dumps({"local": {"state": "ok", "at": now - 40 * 3600}, "offsite": {"state": "ok", "at": now}}))
    assert ops.backup_state(cs)["problem"] == "Sicherung des Panels zu alt"
