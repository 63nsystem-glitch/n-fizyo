"""QA-Fixes 0.9.0 (Bereich A): Runner (root) ohne Symlink-/Pfad-Tricks, Support-Werkzeug ohne Shell, keine älteren signierten
Versionen, update.sh lässt den Dienst nie aus, Aufräumen alter Einträge, Migration 008 mit wartenden SMS, saubere
Fehlermeldungen der Kommandozeile, Schutz-Kopfzeilen der NSystem-Übersicht, Caddy-Abschnitt des KI-Servers, connect-central."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nfizyo.agent import release, runner, safefs, signing, support
from nfizyo.core import housekeeping, nsystem
from nfizyo.core.config import load_settings
from nfizyo.core.db import Database, utcnow
from nfizyo.core.keys import DB

ROOT = Path(__file__).resolve().parent.parent
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# --------------------------------------------------------------------------- SEC-01: Runner und Ordner der App
@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path / "runner-state")
    monkeypatch.setattr(runner, "services", lambda: {})
    monkeypatch.setattr(runner, "gpu", lambda: None)
    s = load_settings(None, data_dir=tmp_path / "data", secret_key=b"t" * 32, demo=True)
    (s.data_dir / "runner").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "passwd").write_text("root:x:0:0\n")
    return s, victim


def test_status_never_follows_symlinks(box):
    s, victim = box
    base = s.data_dir / "runner"
    (base / ".status.tmp").symlink_to(victim / "passwd")             # alter Angriff: feste Zwischendatei
    (base / "status.json").symlink_to(victim / "passwd")             # Ziel als Symlink
    runner.write_status(s.data_dir)
    assert (victim / "passwd").read_text() == "root:x:0:0\n"
    assert not (base / "status.json").is_symlink() and "updated_at" in json.loads((base / "status.json").read_text())
    assert [p.name for p in base.glob(".*.tmp")] == [".status.tmp"]   # nur der gelegte Link, keine eigenen Reste
    # ganzer Ordner runner → Symlink auf einen fremden Ordner: nichts wird dort geschrieben
    shutil.rmtree(base)
    base.symlink_to(victim)
    with pytest.raises(OSError):
        runner.write_status(s.data_dir)
    assert sorted(p.name for p in victim.iterdir()) == ["passwd"]


def test_spool_and_local_requests_cannot_escape(box):
    s, victim = box
    spool = s.data_dir / "runner" / "spool"
    spool.mkdir()
    (spool / "a.json").symlink_to(victim / "passwd")                  # Symlink auf fremde Datei: nicht gelesen, Link weg
    (spool / "b.json").write_text(json.dumps({"id": "../../../victim/x", "action": "diagnose"}))   # Pfad in der ID
    os.mkfifo(spool / "c.json")                                        # FIFO: darf den Runner nicht blockieren
    (spool / "d.json").write_text(json.dumps(["keine", "zuordnung"]))
    t0 = time.monotonic()
    assert runner.process(s) == 0
    assert time.monotonic() - t0 < 5
    assert (victim / "passwd").read_text() == "root:x:0:0\n" and sorted(p.name for p in victim.iterdir()) == ["passwd"]
    assert not list(spool.iterdir())                                   # alles weggeräumt (Link, nicht das Ziel)
    results = s.data_dir / "runner" / "results"
    assert not results.exists() or not list(results.iterdir())
    # gültige ID, aber unsigniert → abgelehnt, Ergebnis unter results/<id>.json
    (spool / "e.json").write_text(json.dumps({"id": "abc-123", "action": "diagnose"}))
    assert runner.process(s) == 1
    assert json.loads((results / "abc-123.json").read_text())["status"] == "rejected"
    # spool selbst als Symlink auf einen Ordner mit *.json (z. B. Zustand des Runners): wird nicht angefasst
    shutil.rmtree(spool)
    (victim / "done.json").write_text("[]")
    spool.symlink_to(victim)
    assert runner.process(s) == 0 and (victim / "done.json").exists()
    local = s.data_dir / "runner" / "local"
    local.symlink_to(victim)
    assert runner.process_local(s) == 0 and (victim / "done.json").exists()


def test_events_folder_symlink_is_refused(box):
    s, victim = box
    events = nsystem.runner_paths(s)["events"]
    support.emit(events, "support_closed", {"reason": "early"})
    assert len(list(events.glob("*.json"))) == 1
    shutil.rmtree(events)
    events.symlink_to(victim)
    with pytest.raises(OSError):
        support.emit(events, "support_closed", {"reason": "early"})
    assert sorted(p.name for p in victim.iterdir()) == ["passwd"]


def test_safe_ids():
    for good in ("0f8fad5b-d9cb-469f-a165-70867728950e", "abc", "A_1-b"):
        assert safefs.valid_id(good)
    for bad in ("", "../x", "a/b", ".hidden", "a" * 65, "x.json", "a b", "-x"):
        assert not safefs.valid_id(bad)


# --------------------------------------------------------------------------- SEC-05: keine älteren Versionen
def test_version_order():
    k = release.version_key
    assert k("0.9") == k("0.9.0") < k("0.9.1-rc1") < k("0.9.1-rc2") < k("0.9.1") < k("0.10.0") < k("1.0.0")


def test_update_refuses_same_or_older_signed_release(tmp_path, monkeypatch):
    app_dir = tmp_path / "opt"
    (app_dir / "app" / "nfizyo").mkdir(parents=True)
    (app_dir / "app" / "nfizyo" / "__init__.py").write_text('__version__ = "0.9.0"\n')
    monkeypatch.setattr(runner, "APP_DIR", app_dir)
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path / "state")
    assert "nicht neuer" in runner.downgrade_reason("0.8.0")
    assert "nicht neuer" in runner.downgrade_reason("0.9.0")
    assert runner.downgrade_reason("0.9.1") is None
    runner._remember_version("0.9.3")                                   # schon einmal 0.9.3 eingespielt, dann Rückkehr
    assert "älter als die schon eingespielte" in runner.downgrade_reason("0.9.2")
    assert runner.downgrade_reason("0.9.3") is None                    # gleiche Version nach Rückkehr: erlaubt
    monkeypatch.setattr(runner, "APP_DIR", tmp_path / "fehlt")
    assert "unbekannt" in runner.downgrade_reason("9.9.9")


def test_runner_update_rejects_downgrade_before_download(tmp_path, monkeypatch):
    from test_nsystem_ops import fake_repo, publish

    repo = fake_repo(tmp_path / "repo")                                 # Version 9.9.9
    pem, pub = signing.new_signing_key()
    store = tmp_path / "store"
    m = publish(repo, store, pem)
    app_dir = tmp_path / "opt"
    (app_dir / "app" / "nfizyo").mkdir(parents=True)
    (app_dir / "app" / "nfizyo" / "__init__.py").write_text('__version__ = "10.0.0"\n')
    monkeypatch.setattr(runner, "APP_DIR", app_dir)
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path / "state")
    downloads, ran = [], []

    def fake_download(url, dest, max_time):
        downloads.append(url)
        Path(dest).write_bytes((store / url.rsplit("/", 1)[1]).read_bytes())
        return 0, ""

    monkeypatch.setattr(runner, "_download", fake_download)
    monkeypatch.setattr(runner, "_run", lambda cmd, timeout: ran.append(cmd) or (0, "ok"))
    s = load_settings(None, data_dir=tmp_path / "box", secret_key=b"t" * 32, demo=True, release_base="https://releases.test",
                      release_pubkey=pub)
    code, text = runner._update({"file": m["file"], "sha256": m["sha256"], "version": "9.9.9"}, s)
    assert code == 1 and "nicht neuer" in text and not ran
    assert all(u.endswith(".manifest.json") for u in downloads)        # Archiv gar nicht erst geladen


# --------------------------------------------------------------------------- SEC-02: Support-Werkzeug
def _tool_copy(tmp_path) -> Path:
    """Werkzeug mit eigenem PATH (Test-Editor und Test-logger) – sonst unverändert."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for ed in ("editor", "vi", "emacs", "nano"):
        (bin_dir / ed).write_text(f'#!/bin/sh\necho "id > {tmp_path}/PWNED_{ed}" > "$1"\n')
        (bin_dir / ed).chmod(0o755)
    (bin_dir / "logger").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/journal.log\n')
    (bin_dir / "logger").chmod(0o755)
    src = (ROOT / "deploy" / "n-fizyo-support-tool").read_text()
    tool = tmp_path / "tool.sh"
    tool.write_text(re.sub(r"^export PATH=.*$", f"export PATH={bin_dir}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                           src, count=1, flags=re.M))
    return tool


def test_support_tool_has_no_line_editor_escape(tmp_path):
    """Ctrl-X Ctrl-E (Readline: Zeile im Editor bearbeiten und als Shell-Befehl ausführen) darf nichts ausführen."""
    import pty
    import select

    tool = _tool_copy(tmp_path)
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - Kindprozess
        os.execve("/bin/bash", ["bash", str(tool)], {"TERM": "xterm", "HOME": str(tmp_path), "VISUAL": "editor",
                                                      "EDITOR": "editor", "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"})
    out = b""

    def read(seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    return

    read(1.0)
    os.write(fd, b"status\x18\x05\n")
    read(1.5)
    os.write(fd, b"bash\n")
    read(0.5)
    os.write(fd, b"exit\n")
    read(1.0)
    os.waitpid(pid, 0)
    assert not list(tmp_path.glob("PWNED_*")), out.decode(errors="replace")
    assert b"Nicht erlaubt" in out
    log = (tmp_path / "journal.log").read_text()
    assert "cmd=denied status" in log and "cmd=denied bash" in log and "cmd=exit" in log


def test_support_tool_as_login_shell(tmp_path):
    """Anmelde-Shell = Werkzeug: «-c <Befehl>» geht nur durch die feste Liste; ohne erlaubten Befehl nichts."""
    tool = _tool_copy(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}

    def run(*args, original=None):
        e = dict(env, SSH_ORIGINAL_COMMAND=original) if original is not None else env
        return subprocess.run(["bash", str(tool), *args], env=e, capture_output=True, text=True, timeout=20, input="")

    r = run("-c", "id > " + str(tmp_path / "PWNED_c"))
    assert r.returncode != 0 and "Nicht erlaubt" in r.stderr and not (tmp_path / "PWNED_c").exists()
    r = run("-c", "/usr/local/sbin/n-fizyo-support-tool", original="bash -i")
    assert r.returncode != 0 and "Nicht erlaubt" in r.stderr
    r = run("-c", "/usr/local/sbin/n-fizyo-support-tool", original="help")
    assert r.returncode == 0 and "logs <dienst>" in r.stdout
    r = run("-i")
    assert r.returncode != 0
    assert "cmd=help" in (tmp_path / "journal.log").read_text()


def test_support_setup_locks_down_the_account():
    keys = support.authorized_keys({"hub_ip": "10.77.0.1", "ssh_keys": ["ssh-ed25519 AAAA k"]})
    assert "pty" not in keys and "restrict" in keys and 'command="/usr/local/sbin/n-fizyo-support-tool"' in keys
    setup = (ROOT / "deploy" / "support-setup.sh").read_text()
    assert '--shell "$TOOL"' in setup and 'usermod -s "$TOOL"' in setup
    assert "ForceCommand" in setup and "PermitTTY no" in setup and "/etc/cron.deny /etc/at.deny" in setup
    assert "read -r -e" not in (ROOT / "deploy" / "n-fizyo-support-tool").read_text()


def test_closing_support_removes_cron_and_at_jobs(tmp_path, monkeypatch):
    calls = []

    def fake(cmd, timeout=60):
        calls.append(list(cmd))
        if cmd == ["atq"]:
            return 0, "12\tWed Nov 11 10:00:00 2026 a nfizyo-support\n13\tWed Nov 11 11:00:00 2026 a root"
        return 1, ""

    monkeypatch.setattr(support, "_run", fake)
    monkeypatch.setattr(support, "SUPPORT_HOME", tmp_path / "home")
    monkeypatch.setattr(support, "WG_DIR", tmp_path / "wg")
    support._teardown()
    assert ["crontab", "-r", "-u", support.SUPPORT_USER] in calls and ["atrm", "12"] in calls and ["atrm", "13"] not in calls
    assert ["loginctl", "disable-linger", support.SUPPORT_USER] in calls


# --------------------------------------------------------------------------- CODE-14: Aufräumen
def test_housekeeping_purges_only_finished_old_rows(app):
    db = app[DB]
    now = datetime.now(timezone.utc)
    old, recent = housekeeping.iso(now - timedelta(days=120)), housekeeping.iso(now - timedelta(days=2))
    pat = db.value("SELECT id FROM patients WHERE practice_id = 1 LIMIT 1")
    db.execute("DELETE FROM jobs")
    for status, at in (("done", old), ("done", recent), ("failed", old), ("queued", old), ("running", old)):
        db.execute("INSERT INTO jobs (practice_id, kind, ref_id, status, created_at, finished_at) VALUES (1, 'x', 1, ?, ?, ?)",
                   (status, at, at if status in ("done", "failed") else None))
    db.execute("DELETE FROM outbox")
    for status, at in (("sent", old), ("logged", old), ("failed", old), ("queued", old), ("sent", recent)):
        db.execute("INSERT INTO outbox (practice_id, channel, recipient, body, status, created_at) VALUES (1, 'sms', '+41790000000', 'x', ?, ?)",
                   (status, at))
    db.execute("DELETE FROM notify_queue")
    for status, at in (("sent", old), ("skipped", old), ("pending", old), ("sent", recent)):
        db.execute("INSERT INTO notify_queue (practice_id, patient_id, kind, status, due_at, created_at) VALUES (1, ?, 'message', ?, ?, ?)",
                   (pat, status, at, at))
    db.execute("DELETE FROM sessions")
    user = db.value("SELECT id FROM users WHERE practice_id = 1 LIMIT 1")
    for th, exp, seen in (("expired", housekeeping.iso(now - timedelta(hours=1)), recent),
                          ("idle", housekeeping.iso(now + timedelta(days=5)), housekeeping.iso(now - timedelta(days=1))),
                          ("valid", housekeeping.iso(now + timedelta(days=5)), housekeeping.iso(now - timedelta(minutes=5)))):
        db.execute("INSERT INTO sessions (token_hash, kind, user_id, practice_id, csrf, created_at, expires_at, last_seen_at)"
                   " VALUES (?, 'staff', ?, 1, 'c', ?, ?, ?)", (th, user, recent, exp, seen))
    out = housekeeping.run_once(app, now)
    assert sorted(r[0] for r in db.query("SELECT status FROM jobs")) == ["done", "queued", "running"]
    assert sorted(r[0] for r in db.query("SELECT status FROM outbox")) == ["queued", "sent"]
    assert sorted(r[0] for r in db.query("SELECT status FROM notify_queue")) == ["pending", "sent"]
    assert [r[0] for r in db.query("SELECT token_hash FROM sessions")] == ["valid"]
    assert out["jobs"] == 2 and out["outbox"] == 3 and out["notify"] == 2 and out["sessions"] == 2
    assert not any(housekeeping.run_once(app, now).values())


def test_housekeeping_hook_errors_do_not_stop_others(app, caplog):
    def broken(db, now):
        raise RuntimeError("kaputt")

    housekeeping.register(app, "kaputt", broken)
    out = housekeeping.run_once(app)
    assert "notify" in out and "kaputt" not in out and "kaputt" in caplog.text


# --------------------------------------------------------------------------- UPG-02: wartende SMS bei der Migration 008
def test_migration_008_keeps_queued_sms_deliverable(tmp_path):
    from nfizyo.app import create_app
    from nfizyo.core import sms as smsmod
    from nfizyo.core.keys import SMS

    core_old = tmp_path / "core_old"
    core_old.mkdir()
    for f in sorted((ROOT / "nfizyo" / "core" / "migrations").glob("00[1-7]_*.sql")):
        shutil.copy(f, core_old / f.name)
    db = Database(tmp_path / "data" / "n-fizyo.sqlite3")
    db.migrate([("core", core_old)])                                   # Stand 0.8.0 (Kern)
    db.execute("INSERT INTO practices (id, name, slug, default_lang, created_at) VALUES (1, 'P', 'p', 'de', ?)", (utcnow(),))
    link = "https://box.test/p/einladung/eyJTOKEN.SIG"
    db.execute("INSERT INTO outbox (practice_id, channel, recipient, body, status, created_at, provider, attempts, next_try_at)"
               " VALUES (1, 'sms', '+41791234567', ?, 'queued', ?, 'aspsms', 1, '2020-01-01T00:00:00+00:00')",
               (f"Guten Tag, Ihre App: {link} – Code: 123456", utcnow()))
    db.execute("INSERT INTO outbox (practice_id, channel, recipient, body, status, created_at) VALUES (1, 'sms', '+41791234567', ?,"
               " 'sent', ?)", ("Video: https://box.test/p/video/abcdef", utcnow()))
    db.close()
    app = create_app(load_settings(None, data_dir=tmp_path / "data", secret_key=b"t" * 32, sms_engine="live",
                                   aspsms_userkey="u", aspsms_password="p"), start_workers=False, seed_demo=False)
    rows = {r["status"]: r for r in app[DB].query("SELECT * FROM outbox")}
    assert "eyJTOKEN" not in rows["queued"]["body"] and rows["queued"]["body_secret"].endswith("Code: 123456")
    assert rows["sent"]["body"].endswith("/p/video/••••••") and rows["sent"]["body_secret"] is None
    sent = []

    class Fake:
        name, configured = "aspsms", True

        async def send(self, sender, to, text):
            sent.append(text)
            return smsmod.Result(True, ref="r1")

    svc = app[SMS]
    svc.providers["aspsms"] = Fake()
    import asyncio

    for mid in svc.due_retries():
        asyncio.run(svc.deliver(mid))
    assert sent == [f"Guten Tag, Ihre App: {link} – Code: 123456"]
    row = app[DB].one("SELECT * FROM outbox WHERE id = ?", (rows["queued"]["id"],))
    assert row["status"] == "sent" and row["body_secret"] is None and "eyJTOKEN" not in row["body"]


# --------------------------------------------------------------------------- UPG-06 / UPG-11: Kommandozeile
def _env(tmp_path, demo=1) -> Path:
    env = tmp_path / "n-fizyo.env"
    env.write_text(f"NFIZYO_SECRET_KEY=x\nNFIZYO_DATA_DIR={tmp_path / 'data'}\nNFIZYO_DEMO={demo}\n", encoding="utf-8")
    return env


def test_reset_demo_refuses_with_real_practice(tmp_path):
    from nfizyo.__main__ import main

    env = _env(tmp_path)
    main(["--env", str(env), "seed-demo"])
    main(["--env", str(env), "create-practice", "--name", "Physio Seeland", "--slug", "seeland"])
    db_file = tmp_path / "data" / "n-fizyo.sqlite3"
    with pytest.raises(SystemExit) as exc:
        main(["--env", str(env), "reset-demo"])
    assert "echte Praxis" in str(exc.value.code) and "nichts gelöscht" in str(exc.value.code)
    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT COUNT(*) FROM practices WHERE slug = 'seeland'").fetchone()[0] == 1
    conn.execute("DELETE FROM practices WHERE slug = 'seeland'")
    conn.commit()
    conn.close()
    main(["--env", str(env), "reset-demo"])                             # nur Demo-Praxen → erlaubt


def test_create_practice_errors_are_one_clean_line(tmp_path):
    env = _env(tmp_path, demo=0)
    base = [sys.executable, "-m", "nfizyo", "--env", str(env), "create-practice", "--name", "Frisch"]
    r = subprocess.run([*base, "--slug", "frisch"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode == 0 and "Praxis angelegt" in r.stdout
    r = subprocess.run([*base, "--slug", "frisch"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode != 0 and "gibt es schon" in r.stderr and "Traceback" not in r.stderr
    r = subprocess.run([*base, "--slug", "Böse/../x"], capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert r.returncode != 0 and "ungültig" in r.stderr and "Traceback" not in r.stderr


def test_release_verify_wrong_key_is_one_clean_line(tmp_path):
    from test_nsystem_ops import fake_repo

    repo = fake_repo(tmp_path / "repo")
    pem, _ = signing.new_signing_key()
    out = tmp_path / "dist"
    archive, sha = release.build_archive(repo, out, "9.9.9", mtime=1)
    doc = release.signed(pem, release.KIND_MANIFEST, release.make_manifest(repo, archive, sha, "9.9.9"))
    manifest = out / release.manifest_name("9.9.9")
    manifest.write_text(json.dumps(doc))
    other = signing.new_signing_key()[1]
    tool = [sys.executable, str(ROOT / "tools" / "release.py"), "verify", str(manifest)]
    env = {k: v for k, v in os.environ.items() if k != "NFIZYO_RELEASE_KEY"}
    r = subprocess.run([*tool, "--pubkey", other], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "Signatur des Manifests ungültig" in r.stderr and "Traceback" not in r.stderr
    r = subprocess.run([*tool, "--pubkey", "kein-schluessel"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "Traceback" not in r.stderr
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release.py"), "verify", str(tmp_path / "fehlt.json"), "--pubkey", other],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "nicht lesbar" in r.stderr and "Traceback" not in r.stderr
    r = subprocess.run([*tool, "--pubkey", signing.public_of(pem)], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and r.stdout.startswith("ok:")


# --------------------------------------------------------------------------- SEC-09: Kopfzeilen der NSystem-Übersicht
async def test_panel_sends_csp_and_hsts(tmp_path, aiohttp_client):
    from nfizyo.central import app as central
    from nfizyo.central.config import load as load_central

    cs = load_central(None, data_dir=tmp_path / "central", secret_key=b"c" * 32, base_url="https://panel.test")
    c = await aiohttp_client(central.create_central_app(cs, start_watch=False))
    for path, method in (("/login", "get"), ("/", "get"), ("/static/css/app.css", "get"), ("/login", "post")):
        r = await getattr(c, method)(path, allow_redirects=False)
        csp = r.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "form-action 'self'" in csp, (path, method, r.status)
        assert r.headers.get("Strict-Transport-Security", "").startswith("max-age=") and r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff" and "camera=()" in r.headers["Permissions-Policy"]
    cs2 = load_central(None, data_dir=tmp_path / "central2", secret_key=b"c" * 32, base_url="http://panel.test")
    c2 = await aiohttp_client(central.create_central_app(cs2, start_watch=False))
    r = await c2.get("/login")
    assert "Strict-Transport-Security" not in r.headers and "Content-Security-Policy" in r.headers


# --------------------------------------------------------------------------- UPG-04 / UPG-10 / UPG-08: Skripte
def test_ai_server_caddy_block_keeps_other_sites(tmp_path):
    script = (ROOT / "deploy" / "install-ai-server.sh").read_text()
    assert "cat > /etc/caddy/Caddyfile" not in script
    snippet = re.search(r'python3 - "\$CADDYFILE" "\$DOMAIN" <<\'PY\'\n(.*?)\nPY\n', script, re.S).group(1)
    caddy = tmp_path / "Caddyfile"
    caddy.write_text("ai.n-fizyo.ch {\n\trequest_body {\n\t\tmax_size 45MB\n\t}\n\treverse_proxy 127.0.0.1:8090\n}\n\n"
                     "# n-fizyo-central BEGIN\npanel.nsystem.ch {\n\treverse_proxy 127.0.0.1:8095\n}\n# n-fizyo-central END\n"
                     "# n-fizyo-releases BEGIN\nreleases.nsystem.ch {\n\tfile_server\n}\n# n-fizyo-releases END\n")
    (tmp_path / "snip.py").write_text(snippet)
    for _ in range(2):
        subprocess.run([sys.executable, str(tmp_path / "snip.py"), str(caddy), "ai.n-fizyo.ch"], check=True)
    text = caddy.read_text()
    assert text.count("ai.n-fizyo.ch {") == 1 and text.count("# n-fizyo-ai BEGIN") == 1
    assert "panel.nsystem.ch {" in text and "releases.nsystem.ch {" in text


@pytest.mark.skipif(not IS_ROOT, reason="connect-central.sh läuft nur als root")
def test_connect_central_crlf_and_atomic(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "systemctl").chmod(0o755)
    env_file = tmp_path / "n-fizyo.env"
    env_file.write_text("NFIZYO_SECRET_KEY=abc\nNFIZYO_CENTRAL_URL=alt\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", NFIZYO_ENV_FILE=str(env_file))
    script = ROOT / "deploy" / "connect-central.sh"
    bad = tmp_path / "bad.env"
    bad.write_bytes(b"NFIZYO_CENTRAL_URL=https://panel.test\r\nNFIZYO_CENTRAL_BOX_ID=bx_1\r\nNFIZYO_CENTRAL_BOX_KEY=k\r\n")
    r = subprocess.run(["bash", str(script), str(bad)], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "nichts geändert" in r.stderr
    assert env_file.read_text() == "NFIZYO_SECRET_KEY=abc\nNFIZYO_CENTRAL_URL=alt\n"
    good = tmp_path / "box.env"
    good.write_bytes(b"NFIZYO_CENTRAL_URL=https://panel.test\r\nNFIZYO_CENTRAL_BOX_ID=bx_1\r\nNFIZYO_CENTRAL_BOX_KEY=k\r\n"
                     b"NFIZYO_CENTRAL_COMMAND_PUBKEY=pk")                # letzte Zeile ohne Zeilenumbruch
    r = subprocess.run(["bash", str(script), str(good)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = env_file.read_text()
    assert "\r" not in text and "NFIZYO_CENTRAL_URL=https://panel.test\n" in text and text.endswith("NFIZYO_CENTRAL_COMMAND_PUBKEY=pk\n")
    assert text.count("NFIZYO_CENTRAL_URL=") == 1 and not good.exists() and oct(env_file.stat().st_mode & 0o777) == "0o640"


def test_daily_backup_keeps_the_rollback_backup(tmp_path):
    data, dest = tmp_path / "data", tmp_path / "backups"
    data.mkdir()
    sqlite3.connect(data / "n-fizyo.sqlite3").executescript("CREATE TABLE t (x); INSERT INTO t VALUES (1);")
    for name in ("pre-update-2026-01-01_000000", "pre-update-2025-12-01_000000", "2025-12-01_0230"):
        (dest / name).mkdir(parents=True)
        os.utime(dest / name, (time.time() - 40 * 86400,) * 2)
    mark = tmp_path / ".last-update-backup"
    mark.write_text(f"{dest / 'pre-update-2026-01-01_000000'}\n")
    env = dict(os.environ, NFIZYO_DATA=str(data), NFIZYO_BACKUP_DIR=str(dest), NFIZYO_ENV_FILE=str(tmp_path / "keine.env"),
               NFIZYO_BACKUP_KEEP_MARK=str(mark), NFIZYO_OFFSITE="0")
    r = subprocess.run(["bash", str(ROOT / "deploy" / "n-fizyo-backup")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    names = sorted(p.name for p in dest.iterdir() if p.is_dir())
    assert "pre-update-2026-01-01_000000" in names and "pre-update-2025-12-01_000000" not in names and "2025-12-01_0230" not in names


# --------------------------------------------------------------------------- UPG-01 / 07 / 08 / SEC-05: update.sh
STUBS = {
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$SBX/calls.log"\n'
                 'ver() { sed -n \'s/^__version__ *= *"\\([^"]*\\)".*/\\1/p\' "$SBX/opt/app/nfizyo/__init__.py"; }\n'
                 'case "$1" in\n  stop) [[ "$2" == n-fizyo ]] && rm -f "$SBX/running" ;;\n'
                 '  start|restart) [[ "$2" == n-fizyo ]] && ver > "$SBX/running" ;;\nesac\nexit 0\n',
    "curl": '#!/bin/bash\nfor a in "$@"; do if [[ "$a" == *"/health" ]]; then [[ -f "$SBX/running" ]] || exit 7;'
            ' echo "{\\"version\\": \\"$(cat "$SBX/running")\\"}"; exit 0; fi; done\nexit 1\n',
    "sudo": '#!/bin/bash\n[[ "$1" == "-u" ]] && shift 2\nexec "$@"\n',
    "apt-get": "#!/bin/bash\nexit 0\n",
}


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    if not IS_ROOT or not shutil.which("rsync") or not shutil.which("sqlite3"):
        pytest.skip("update.sh braucht root, rsync und sqlite3")
    sbx = tmp_path_factory.mktemp("update")
    for d in ("bin", "opt/releases", "opt/venv/bin", "data", "backups", "sbin"):
        (sbx / d).mkdir(parents=True)
    for name, text in STUBS.items():
        (sbx / "bin" / name).write_text(text)
        (sbx / "bin" / name).chmod(0o755)
    (sbx / "opt/venv/bin/pip").write_text('#!/bin/bash\necho "pip $*" >> "$SBX/calls.log"\n[[ -f "$SBX/fail_pip" ]] && exit 1\nexit 0\n')
    (sbx / "opt/venv/bin/python").write_text('#!/bin/bash\nif [[ -f "$SBX/fail_migrate" && " $* " == *" migrate "* ]]; then exit 1; fi\n'
                                             f'exec {sys.executable} "$@"\n')
    for f in ("pip", "python"):
        (sbx / "opt/venv/bin" / f).chmod(0o755)
    old = sbx / "opt/releases/0.8.9_2026-01-05_120000"
    subprocess.run(["rsync", "-a", "--exclude", ".git", "--exclude", "__pycache__", "--exclude", "tests/", "--exclude", "/data/",
                    f"{ROOT}/", f"{old}/"], check=True)
    init = old / "nfizyo" / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', '__version__ = "0.8.9"', init.read_text()))
    (sbx / "opt/app").symlink_to(old)
    (sbx / "n-fizyo.env").write_text(f"NFIZYO_SECRET_KEY={'k' * 64}\nNFIZYO_DATA_DIR={sbx / 'data'}\nNFIZYO_DEMO=1\n")
    subprocess.run([sys.executable, "-m", "nfizyo", "--env", str(sbx / "n-fizyo.env"), "init-db"], cwd=old, check=True,
                   capture_output=True)
    (sbx / "running").write_text("0.8.9\n")
    return sbx


def _update(sbx, *args, src=ROOT):
    (sbx / "calls.log").write_text("")
    env = dict(os.environ, SBX=str(sbx), PATH=f"{sbx / 'bin'}:{os.environ['PATH']}", NFIZYO_APP_USER="root",
               NFIZYO_APP_DIR=str(sbx / "opt"), NFIZYO_DATA=str(sbx / "data"), NFIZYO_ENV_FILE=str(sbx / "n-fizyo.env"),
               NFIZYO_BACKUP_DIR=str(sbx / "backups"), NFIZYO_HEALTH_TRIES="2", NFIZYO_SBIN_DIR=str(sbx / "sbin"),
               NFIZYO_SUPPORT_SETUP="/nonexistent")
    r = subprocess.run(["bash", str(src / "deploy" / "update.sh"), *args], env=env, capture_output=True, text=True, timeout=600)
    calls = (sbx / "calls.log").read_text()
    running = (sbx / "running").read_text().strip() if (sbx / "running").exists() else None
    return r, calls, running


def _releases(sbx):
    return sorted(p.name for p in (sbx / "opt/releases").iterdir())


def test_update_failure_before_stop_keeps_service_running(sandbox):
    (sandbox / "fail_pip").write_text("1")
    try:
        r, calls, running = _update(sandbox)
    finally:
        (sandbox / "fail_pip").unlink()
    assert r.returncode == 1 and "systemctl stop n-fizyo" not in calls and running == "0.8.9"
    assert _releases(sandbox) == ["0.8.9_2026-01-05_120000"]          # kein halber Versionsordner bleibt liegen


def test_update_failure_after_stop_rolls_back(sandbox):
    (sandbox / "fail_migrate").write_text("1")
    try:
        r, calls, running = _update(sandbox)
    finally:
        (sandbox / "fail_migrate").unlink()
    assert r.returncode == 1 and "systemctl stop n-fizyo" in calls and calls.rstrip().endswith("systemctl start n-fizyo")
    assert running == "0.8.9" and os.path.realpath(sandbox / "opt/app").endswith("0.8.9_2026-01-05_120000")
    assert _releases(sandbox) == ["0.8.9_2026-01-05_120000"] and not (sandbox / "opt/previous").exists()


def test_update_then_same_version_then_older_then_rollback(sandbox, tmp_path):
    r, calls, running = _update(sandbox)
    assert r.returncode == 0, r.stdout + r.stderr
    assert running == release.repo_version(ROOT)
    assert calls.count("systemctl stop n-fizyo") == 1
    rels, mark = _releases(sandbox), (sandbox / "opt/.last-update-backup").read_text().strip()
    assert Path(mark).name.startswith("pre-update-") and (Path(mark) / "n-fizyo.sqlite3").exists()
    assert os.path.realpath(sandbox / "opt/previous").endswith("0.8.9_2026-01-05_120000")
    # gleiche Version nochmals: nichts zu tun, «previous» und Sicherung bleiben
    r, calls, _ = _update(sandbox)
    assert r.returncode == 0 and "nichts zu tun" in r.stdout and "systemctl stop" not in calls
    assert _releases(sandbox) == rels and (sandbox / "opt/.last-update-backup").read_text().strip() == mark
    # ältere Version: abgelehnt (zurück nur mit --rollback)
    older = tmp_path / "older"
    shutil.copytree(sandbox / "opt/releases/0.8.9_2026-01-05_120000", older, symlinks=True)
    shutil.copy(ROOT / "deploy" / "update.sh", older / "deploy" / "update.sh")
    r, calls, _ = _update(sandbox, src=older)
    assert r.returncode == 1 and "älter" in r.stderr and "systemctl stop" not in calls
    # --force nie über den Runner
    env_runner = dict(os.environ, NFIZYO_FROM_RUNNER="1", NFIZYO_ENV_FILE=str(sandbox / "n-fizyo.env"))
    r = subprocess.run(["bash", str(ROOT / "deploy" / "update.sh"), "--force"], env=env_runner, capture_output=True, text=True)
    assert r.returncode == 1 and "nur von Hand" in r.stderr
    # Rückkehr ohne die Sicherung von vor dem Update: abgelehnt, nichts geändert
    shutil.move(mark, mark + ".weg")
    r, calls, running = _update(sandbox, "--rollback")
    assert r.returncode == 1 and "fehlt" in r.stderr and "systemctl stop" not in calls and running != "0.8.9"
    shutil.move(mark + ".weg", mark)
    r, calls, running = _update(sandbox, "--rollback")
    assert r.returncode == 0 and running == "0.8.9", r.stdout + r.stderr
