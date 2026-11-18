"""NSystem-Betrieb 0.9.0: signierte Releases, Supportzugang nur mit Einverständnis der Leitung (Ablauf, Protokoll),
Support-Anfragen ohne Patientendaten (Blockade, Antwort zurück in die App), Wartungshinweise (Banner, ausblenden)."""
from __future__ import annotations

import base64
import json
import os
import re
import tarfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from conftest import login
from nfizyo.agent import client as agent, release, runner, signing, support
from nfizyo.app import create_app
from nfizyo.central import app as central, ops, releases
from nfizyo.central.__main__ import public_key
from nfizyo.central.config import load as load_central
from nfizyo.core import crypto, nsystem, patientdata, totp
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB
from nfizyo.core.timeutil import TZ

ROOT = Path(__file__).resolve().parent.parent
HUB_PUB = base64.b64encode(b"h" * 32).decode()
SSH_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl nsystem-support"
SUPPORT = {"support_hub": "hub.nsystem.test:51820", "support_hub_pubkey": HUB_PUB, "support_address": "10.77.0.12",
           "support_hub_ip": "10.77.0.1", "support_ssh_keys": SSH_KEY}


# --------------------------------------------------------------------------- Hilfen
class FakeSystem:
    """Spielt wg / wg-quick / journalctl / pkill für den Runner (es wird nichts am echten System geändert)."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.up = False
        self.journal: list[str] = []
        self.handshake = 0

    def __call__(self, cmd, timeout=60):
        self.calls.append(list(cmd))
        if cmd[:2] == ["wg", "show"]:
            if len(cmd) == 3:
                return (0, "interface: nfz-support") if self.up else (1, "No such device")
            return (0, f"{HUB_PUB}\t{self.handshake}") if self.up else (1, "")
        if cmd[0] == "wg-quick":
            self.up = cmd[1] == "up"
            return 0, ""
        if cmd[0] == "journalctl":
            return 0, "\n".join(self.journal)
        if cmd[:3] == ["ip", "link", "delete"]:
            self.up = False
        return 0, ""

    def ran(self, *prefix) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


@pytest.fixture
def fake_system(tmp_path, monkeypatch):
    fs = FakeSystem()
    monkeypatch.setattr(support, "_run", fs)
    monkeypatch.setattr(support, "WG_DIR", tmp_path / "wireguard")
    monkeypatch.setattr(support, "SUPPORT_DIR", tmp_path / "support")
    monkeypatch.setattr(support, "SUPPORT_HOME", tmp_path / "support-home")
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path / "runner-state")
    (tmp_path / "support").mkdir()
    (tmp_path / "support" / "wg.key").write_text("geheimer-test-schluessel\n")
    return fs


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """Übersicht (mit Release-Ablage) + verbundene Box (mit Supportzugang eingerichtet)."""
    pushes = []

    async def fake_push(settings, title, message, priority="high"):
        pushes.append((title, message, priority))
        return True

    monkeypatch.setattr(central, "push", fake_push)
    releases.clear_cache()
    rel_pem, rel_pub = signing.new_signing_key()
    rel_dir = tmp_path / "releases"
    rel_dir.mkdir()
    cs = load_central(None, data_dir=tmp_path / "central", secret_key=b"c" * 32, base_url="http://central.test",
                      release_dir=str(rel_dir), release_pubkey=rel_pub)
    cs.data_dir.mkdir(parents=True)
    pem, _ = signing.new_signing_key()
    cs.signing_key_path.write_bytes(pem)
    capp = central.create_central_app(cs, start_watch=False)
    box_id, box_key = central.add_box(capp[central.DBK], cs.secret_key, "Physio Test")
    other_id, _ = central.add_box(capp[central.DBK], cs.secret_key, "Andere Praxis")
    central.add_staff(capp[central.DBK], "s@nsystem.invalid", "S. NSystem", "nsystem-passwort-1")
    server = TestServer(capp)
    cclient = TestClient(server)
    await cclient.start_server()
    settings = load_settings(None, data_dir=tmp_path / "box", secret_key=b"t" * 32, demo=True,
                             central_url=str(server.make_url("")).rstrip("/"), central_box_id=box_id, central_box_key=box_key,
                             central_command_pubkey=public_key(cs), release_base="https://releases.test", release_pubkey=rel_pub,
                             **SUPPORT)
    bapp = create_app(settings, start_workers=False)
    bclient = TestClient(TestServer(bapp))
    await bclient.start_server()
    yield {"central": capp, "cs": cs, "cclient": cclient, "box": bapp, "bclient": bclient, "box_id": box_id, "other_id": other_id,
           "pushes": pushes, "rel_pem": rel_pem, "rel_pub": rel_pub, "rel_dir": rel_dir, "settings": settings, "tmp": tmp_path}
    await bclient.close()
    await cclient.close()
    releases.clear_cache()


async def staff_login(c: TestClient, capp, cs):
    html = await (await c.get("/login")).text()
    csrf = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    await c.post("/login", data={"_csrf": csrf, "email": "s@nsystem.invalid", "password": "nsystem-passwort-1"})
    html = await (await c.get("/login/einrichten")).text()
    csrf = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    staff = capp[central.DBK].one("SELECT * FROM staff")
    secret = crypto.decrypt(cs.secret_key, central.PURPOSE_TOTP, staff["totp_pending"])
    await c.post("/login/einrichten", data={"_csrf": csrf, "code": totp.code_at(secret, totp.current_step())})
    html = await (await c.get("/")).text()
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1), secret


class Codes:
    """Jeder Befehl braucht einen neuen, noch nicht benutzten Code."""

    def __init__(self, secret):
        self.secret, self.step = secret, totp.current_step()

    def next(self) -> str:
        self.step += 1
        return totp.code_at(self.secret, self.step)


def fake_repo(root: Path, version: str = "9.9.9") -> Path:
    files = {
        "nfizyo/__init__.py": f'__version__ = "{version}"\n',
        "nfizyo/core/x.py": "X = 1\n",
        "nfizyo/core/migrations/001_a.sql": "CREATE TABLE a (id INTEGER);\n",
        "nfizyo/core/migrations/002_b.sql": "CREATE TABLE b (id INTEGER);\n",
        "nfizyo/modules/agenda/__init__.py": 'MODULE = dict(version="2.3.0")\n',
        "nfizyo/core/__pycache__/x.cpython-313.pyc": "binär",
        "nfizyo/core/geheim.key": "privat",
        "nfizyo/knowledge/data/regions.json": "{}",
        "deploy/update.sh": "#!/usr/bin/env bash\necho update\n",
        "docs/MERKEZ.md": "# doc\n",
        "tests/test_x.py": "def test(): pass\n",
        "tools/release.py": "print()\n",
        "data/runtime/n-fizyo.sqlite3": "db",
        ".env": "NFIZYO_SECRET_KEY=geheim\n",
        "deploy/box-bx_1.env": "NFIZYO_CENTRAL_BOX_KEY=geheim\n",
        ".env.example": "NFIZYO_SECRET_KEY=\n",
        "requirements.txt": "aiohttp\n",
        "README.md": "# X\n\nYeni (sürüm 9.9.9 – Test):\n\n- **Erstes:** etwas Neues `mit Code`\n  und Fortsetzung.\n- **Zweites:** noch was.\n\n"
                     "Yeni (sürüm 9.9.8):\n\n- **Altes:** nicht dabei.\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "deploy/update.sh").chmod(0o755)
    return root


def publish(repo: Path, out: Path, pem: bytes, version: str = "9.9.9") -> dict:
    archive, sha = release.build_archive(repo, out, version, mtime=1_700_000_000)
    manifest = release.make_manifest(repo, archive, sha, version)
    (out / release.manifest_name(version)).write_text(json.dumps(release.signed(pem, release.KIND_MANIFEST, manifest)))
    (out / "index.json").write_text(json.dumps(release.build_index(out, pem)))
    return manifest


# --------------------------------------------------------------------------- 1) Releases
def test_release_archive_manifest_and_index(tmp_path):
    repo = fake_repo(tmp_path / "repo")
    pem, pub = signing.new_signing_key()
    m = publish(repo, tmp_path / "out", pem)
    with tarfile.open(tmp_path / "out" / m["file"]) as tar:
        names = set(tar.getnames())
        mode = tar.getmember("n-fizyo-9.9.9/deploy/update.sh").mode
    assert "n-fizyo-9.9.9/nfizyo/__init__.py" in names and "n-fizyo-9.9.9/nfizyo/knowledge/data/regions.json" in names
    assert "n-fizyo-9.9.9/.env.example" in names and mode == 0o755
    for top in ("tests", "tools", "data"):
        assert not any(n.startswith(f"n-fizyo-9.9.9/{top}") for n in names), top
    for bad in ("__pycache__", ".env", "geheim.key", "box-bx_1.env"):
        assert not any(bad in n and not n.endswith(".env.example") for n in names), bad
    assert m["changelog"] == ["Erstes: etwas Neues mit Code und Fortsetzung.", "Zweites: noch was."]
    assert m["migrations"]["core"] == "002_b.sql" and m["modules"] == {"agenda": "2.3.0"}
    assert (tmp_path / "out" / f"{m['file']}.sha256").read_text().split()[0] == m["sha256"]
    # reproduzierbar: gleiche Eingabe → gleiche Prüfsumme
    _, sha2 = release.build_archive(repo, tmp_path / "out2", "9.9.9", mtime=1_700_000_000)
    assert sha2 == m["sha256"]
    # Signaturen: Manifest und Index gültig, verändert oder fremder Schlüssel → abgelehnt
    doc = json.loads((tmp_path / "out" / release.manifest_name("9.9.9")).read_text())
    assert release.verify_manifest(pub, doc)["sha256"] == m["sha256"]
    assert [x["version"] for x in release.verify_index(pub, json.loads((tmp_path / "out" / "index.json").read_text()))] == ["9.9.9"]
    tampered = json.loads(json.dumps(doc))
    tampered["manifest"]["sha256"] = "0" * 64
    _, other_pub = signing.new_signing_key()
    for bad_doc, key in ((tampered, pub), (doc, other_pub)):
        with pytest.raises(release.ReleaseError):
            release.verify_manifest(key, bad_doc)
    # eine Manifest-Signatur gilt nie als Befehl (andere Art)
    assert signing.verify_command(pub, dict(doc["manifest"], signature=doc["signature"]), box_id="x") is not None


def test_release_tool_cli_keeps_key_outside_repo(tmp_path):
    import subprocess
    import sys

    env = {**os.environ, "NFIZYO_RELEASE_KEY": str(ROOT / "nfizyo" / "release.pem")}
    r = subprocess.run([sys.executable, str(ROOT / "tools/release.py"), "keygen"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "Projektordner" in r.stderr and not (ROOT / "nfizyo" / "release.pem").exists()
    env["NFIZYO_RELEASE_KEY"] = str(tmp_path / "keys" / "release.pem")
    r = subprocess.run([sys.executable, str(ROOT / "tools/release.py"), "keygen"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert len(r.stdout.strip().splitlines()[-1]) == 44                  # öffentlicher Schlüssel (Base64)
    assert (tmp_path / "keys" / "release.pem").stat().st_mode & 0o077 == 0
    assert "PRIVATE" not in r.stdout
    r = subprocess.run([sys.executable, str(ROOT / "tools/release.py"), "keygen"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "nie überschrieben" in r.stderr


async def test_panel_lists_signed_releases_and_fills_update_command(env):
    capp, cs, c = env["central"], env["cs"], env["cclient"]
    m = publish(fake_repo(env["tmp"] / "repo"), env["rel_dir"], env["rel_pem"])
    csrf, secret = await staff_login(c, capp, cs)
    codes = Codes(secret)
    html = await (await c.get(f"/box/{env['box_id']}")).text()
    assert "9.9.9" in html and "Erstes: etwas Neues" in html and m["sha256"][:16] in html
    # Version wählen → Datei, SHA-256 und Version kommen aus dem signierten Index
    await c.post(f"/box/{env['box_id']}/befehl", data={"_csrf": csrf, "action": "update", "release": "9.9.9", "code": codes.next()})
    row = capp[central.DBK].one("SELECT * FROM commands")
    assert json.loads(row["args"]) == {"file": m["file"], "sha256": m["sha256"], "version": "9.9.9"} and row["deliver_after"] is None
    # unbekannte Version → kein Befehl
    r = await c.post(f"/box/{env['box_id']}/befehl", data={"_csrf": csrf, "action": "update", "release": "1.0.0", "code": codes.next()},
                     allow_redirects=False)
    assert r.headers["Location"].endswith("ok=release") and capp[central.DBK].value("SELECT COUNT(*) FROM commands") == 1
    # Index mit falscher Signatur → keine Version wird angeboten
    idx = json.loads((env["rel_dir"] / "index.json").read_text())
    idx["index"]["releases"][0]["manifest"]["sha256"] = "1" * 64
    idx["signature"] = signing.sign_payload(signing.new_signing_key()[0], release.KIND_INDEX, idx["index"])
    (env["rel_dir"] / "index.json").write_text(json.dumps(idx))
    releases.clear_cache()
    html = await (await c.get(f"/box/{env['box_id']}")).text()
    assert "Signatur des Index ungültig" in html and 'value="9.9.9"' not in html
    # Box nimmt den Befehl an (Version-Feld erlaubt), Runner prüft danach selbst das Manifest
    assert await agent.send_once(env["box"])
    assert env["box"][DB].one("SELECT action, status FROM box_commands")["status"] == "queued"


def test_runner_update_checks_signed_manifest_and_hash(tmp_path, monkeypatch):
    repo = fake_repo(tmp_path / "repo")
    pem, pub = signing.new_signing_key()
    store = tmp_path / "store"
    m = publish(repo, store, pem)
    ran = []

    def fake_download(url, dest, max_time):
        assert url.startswith("https://releases.test/")
        src = store / url.rsplit("/", 1)[1]
        if not src.exists():
            return 22, "404"
        Path(dest).write_bytes(src.read_bytes())
        return 0, ""

    def fake_run(cmd, timeout):
        ran.append(cmd)
        return 0, "Update fertig"

    monkeypatch.setattr(runner, "_download", fake_download)
    monkeypatch.setattr(runner, "_run", fake_run)
    installed = tmp_path / "opt" / "app" / "nfizyo"                       # installierte (ältere) Version auf der Box
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text('__version__ = "1.0.0"\n')
    monkeypatch.setattr(runner, "APP_DIR", tmp_path / "opt")
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path / "runner-state")
    s = load_settings(None, data_dir=tmp_path / "box", secret_key=b"t" * 32, demo=True, release_base="https://releases.test",
                      release_pubkey=pub)
    args = {"file": m["file"], "sha256": m["sha256"], "version": "9.9.9"}
    code, text = runner._update(args, s)
    assert code == 0 and ran and ran[-1][0] == "bash" and ran[-1][1].endswith("deploy/update.sh")
    ran.clear()
    # anderer Release-Schlüssel auf der Box → Manifest abgelehnt, update.sh läuft nicht
    code, text = runner._update(args, s.with_overrides(release_pubkey=signing.new_signing_key()[1]))
    assert code == 1 and "Manifest abgelehnt" in text and not ran
    # Befehl mit anderer Prüfsumme als im Manifest
    code, text = runner._update(dict(args, sha256="a" * 64), s)
    assert code == 1 and "passt nicht" in text and not ran
    # Archiv in der Ablage ausgetauscht → Prüfsumme stimmt nicht
    (store / m["file"]).write_bytes(b"manipuliert")
    code, text = runner._update(args, s)
    assert code == 1 and "Prüfsumme" in text and not ran
    # ohne öffentlichen Release-Schlüssel kein Update
    monkeypatch.delenv("NFIZYO_RELEASE_PUBKEY", raising=False)
    code, text = runner._update(args, s.with_overrides(release_pubkey=""))
    assert code == 1 and "NFIZYO_RELEASE_PUBKEY" in text and not ran


async def test_scheduled_update_with_automatic_notice(env):
    capp, cs, c, bapp = env["central"], env["cs"], env["cclient"], env["box"]
    m = publish(fake_repo(env["tmp"] / "repo"), env["rel_dir"], env["rel_pem"])
    csrf, secret = await staff_login(c, capp, cs)
    when = (datetime.now(TZ) + timedelta(hours=3)).replace(second=0, microsecond=0)
    r = await c.post(f"/box/{env['box_id']}/befehl", data={"_csrf": csrf, "action": "update", "release": "9.9.9", "when": when.strftime("%Y-%m-%dT%H:%M"),
                                                           "notice": "1", "code": Codes(secret).next()}, allow_redirects=False)
    assert r.headers["Location"].endswith("ok=scheduled")
    db = capp[central.DBK]
    cmd = db.one("SELECT * FROM commands")
    assert abs(cmd["deliver_after"] - when.timestamp()) < 1 and cmd["expires_at"] > when.timestamp() and cmd["notice_id"]
    # Meldung: Hinweis kommt sofort, der Befehl noch nicht
    assert await agent.send_once(bapp)
    assert bapp[DB].value("SELECT COUNT(*) FROM box_commands") == 0
    notice = bapp[DB].one("SELECT * FROM box_notices")
    assert "9.9.9" in notice["text_de"] and when.strftime("%H:%M") in notice["text_de"] and "mis à jour" in notice["text_fr"]
    # Zeitpunkt erreicht → Box holt den Befehl ab
    db.execute("UPDATE commands SET deliver_after = ? WHERE id = ?", (time.time() - 1, cmd["id"]))
    assert await agent.send_once(bapp)
    assert bapp[DB].one("SELECT action FROM box_commands")["action"] == "update"
    assert m["file"] in bapp[DB].value("SELECT args FROM box_commands")


# --------------------------------------------------------------------------- 2) Supportzugang
async def test_support_access_consent_flow_and_log(env, fake_system):
    bapp, bc, s = env["box"], env["bclient"], env["settings"]
    db = bapp[DB]
    runner.write_status(s.data_dir, s)                               # Runner läuft, Zugang zu
    csrf = await login(bc, "leitung@demo.n-fizyo.ch")
    html = await (await bc.get("/leitung/einstellungen/nsystem")).text()
    assert "Supportzugang öffnen" in html and "technisch auf diesen Praxis-Server zugreifen" in html and "(AVV)" in html
    assert not fake_system.ran("wg-quick", "up")
    r = await bc.post("/leitung/einstellungen/nsystem", data={"_csrf": csrf, "do": "support_open", "hours": "3"}, allow_redirects=False)
    assert r.status == 302
    sess = db.one("SELECT * FROM box_support_sessions")
    assert sess["status"] == "requested" and sess["hours"] == 3
    assert list((s.data_dir / "runner/local").glob("*.json"))
    # Runner (root) führt aus
    assert runner.process_local(s) == 1
    assert fake_system.ran("wg-quick", "up")
    conf = (support.WG_DIR / "nfz-support.conf").read_text()
    assert "AllowedIPs = 10.77.0.1/32" in conf and "--dport 22" in conf and "PrivateKey" not in conf and "geheimer" not in conf
    keys = support.keys_file().read_text()
    assert keys.startswith('from="10.77.0.1",restrict,command="/usr/local/sbin/n-fizyo-support-tool" ssh-ed25519 ')
    state = support.load_state(runner.STATE_DIR)
    assert state["open"] and abs(state["until"] - (time.time() + 3 * 3600)) < 60
    runner.write_status(s.data_dir, s)
    # Leitung sieht: offen bis, Restzeit, Schliessen; Banner auf anderen Leitungsseiten
    html = await (await bc.get("/leitung/einstellungen/nsystem")).text()
    assert "Supportzugang offen bis" in html and "data-countdown" in html and "Jetzt schliessen" in html and "countdown.js" in html
    assert db.one("SELECT status FROM box_support_sessions")["status"] == "open"
    assert "Der Supportzugang für NSystem ist offen bis" in await (await bc.get("/leitung/uebersicht")).text()
    # Verbindung von NSystem: Tunnel, SSH-Anmeldung, Befehl → Protokoll
    now_us = int(time.time() * 1e6)
    fake_system.handshake = int(time.time())
    fake_system.journal = [
        json.dumps({"__CURSOR": "c1", "__REALTIME_TIMESTAMP": str(now_us), "SYSLOG_IDENTIFIER": "sshd",
                    "MESSAGE": "Accepted publickey for nfizyo-support from 10.77.0.1 port 50022 ssh2: ED25519 SHA256:x"}),
        json.dumps({"__CURSOR": "c2", "__REALTIME_TIMESTAMP": str(now_us + 1), "SYSLOG_IDENTIFIER": "n-fizyo-support",
                    "MESSAGE": "cmd=logs n-fizyo 200"}),
        json.dumps({"__CURSOR": "c3", "__REALTIME_TIMESTAMP": str(now_us + 2), "SYSLOG_IDENTIFIER": "sshd",
                    "MESSAGE": "Accepted publickey for admin from 192.168.1.5 port 1 ssh2"}),
        json.dumps({"__CURSOR": "c4", "__REALTIME_TIMESTAMP": str(now_us + 3), "SYSLOG_IDENTIFIER": "sshd",
                    "MESSAGE": "Disconnected from user nfizyo-support 10.77.0.1 port 50022"}),
    ]
    runner.support_tick(s)
    assert support.load_state(runner.STATE_DIR)["cursor"] == "c4"
    fake_system.journal = []
    runner.support_tick(s)                                           # zweite Runde: nichts doppelt
    html = await (await bc.get("/leitung/einstellungen/nsystem")).text()
    for text in ("NSystem verbunden (Tunnel)", "NSystem angemeldet", "Befehl von NSystem", "logs n-fizyo 200", "NSystem abgemeldet", "M. Leitung"):
        assert text in html, text
    kinds = [r["kind"] for r in db.query("SELECT kind FROM box_events ORDER BY id")]
    assert kinds.count("support_ssh_login") == 1 and "admin" not in json.dumps([dict(r) for r in db.query("SELECT * FROM box_events")])
    # Meldung an NSystem: Übersicht zeigt «Support offen bis», Protokoll ohne Namen, Push
    assert await agent.send_once(bapp)
    cdb = env["central"][central.DBK]
    assert {"support_opened", "support_ssh_login", "support_command"} <= {r["kind"] for r in cdb.query("SELECT kind FROM box_events")}
    assert "M. Leitung" not in json.dumps([dict(r) for r in cdb.query("SELECT * FROM box_events")])
    assert any("Supportzugang von der Praxis geöffnet" in p[1] for p in env["pushes"])
    cs_csrf, _ = await staff_login(env["cclient"], env["central"], env["cs"])
    assert "Support offen bis" in await (await env["cclient"].get("/")).text()
    html = await (await env["cclient"].get(f"/box/{env['box_id']}")).text()
    assert "Support offen bis" in html and "logs n-fizyo 200" in html
    # früher schliessen
    r = await bc.post("/leitung/einstellungen/nsystem", data={"_csrf": csrf, "do": "support_close"}, allow_redirects=False)
    assert runner.process_local(s) == 1
    assert support.keys_file().read_text() == "" and not (support.WG_DIR / "nfz-support.conf").exists()
    assert fake_system.ran("pkill", "-KILL", "-u", "nfizyo-support") and not fake_system.up
    runner.write_status(s.data_dir, s)
    html = await (await bc.get("/leitung/einstellungen/nsystem")).text()
    assert "Supportzugang öffnen" in html and "früher geschlossen" in html
    assert db.one("SELECT status, close_reason, closed_by FROM box_support_sessions")["close_reason"] == "early"
    # Praxis-Audit
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE action IN ('support_access_open', 'support_access_close')") == 2


def test_support_closes_at_expiry_without_app_and_restores_after_reboot(tmp_path, fake_system):
    s = load_settings(None, data_dir=tmp_path / "box", secret_key=b"t" * 32, demo=True, **SUPPORT)
    events = nsystem.runner_paths(s)["events"]
    assert support.open_access(s, runner.STATE_DIR, events, "sup-1", 1)
    # Neustart der Box innerhalb der Zeit: Tunnel ist weg → wird wieder aufgebaut (Einverständnis gilt noch)
    fake_system.up = False
    fake_system.calls.clear()
    runner.support_tick(s)
    assert fake_system.ran("wg-quick", "up") and support.load_state(runner.STATE_DIR)["open"]
    # Zeit abgelaufen: der Runner (oder der systemd-Zeitgeber mit --support-expire) schliesst – ohne App
    st = support.load_state(runner.STATE_DIR)
    st["until"] = time.time() - 5
    support.save_state(runner.STATE_DIR, st)
    runner.support_tick(s)
    assert not support.load_state(runner.STATE_DIR)["open"] and support.keys_file().read_text() == ""
    assert not (support.WG_DIR / "nfz-support.conf").exists() and fake_system.ran("systemctl", "stop", "n-fizyo-support-expire.timer")
    kinds = [json.loads(f.read_text())["kind"] for f in sorted(events.glob("*.json"))]
    assert kinds[0] == "support_opened" and kinds[-1] == "support_closed"
    assert json.loads(sorted(events.glob("*.json"))[-1].read_text())["detail"]["reason"] == "expired"
    assert any(c[0] == "systemd-run" and "--support-expire" in c for c in fake_system.calls)
    # Rest einer Konfiguration ohne offenen Zugang wird aufgeräumt
    (support.WG_DIR / "nfz-support.conf").write_text("[Interface]\n")
    runner.support_tick(s)
    assert not (support.WG_DIR / "nfz-support.conf").exists()


async def test_nothing_opens_without_leitung(env, fake_system):
    bapp, bc, s = env["box"], env["bclient"], env["settings"]
    db = bapp[DB]
    # NSystem kann «öffnen» nicht befehlen (nicht auf der Liste) – weder App noch Runner nehmen es an
    good_pem = env["cs"].signing_key_path.read_bytes()
    cmd = {"id": "x1", "box_id": env["box_id"], "action": "support_open", "args": {"hours": 8}, "issued_by": "NSystem",
           "issued_at": "2026-11-11T10:00:00+00:00", "expires_at": time.time() + 600}
    cmd["signature"] = signing.sign_command(good_pem, cmd)
    assert agent.accept_commands(bapp, [cmd]) == []
    assert db.one("SELECT reason FROM box_commands WHERE id = 'x1'")["reason"] == "not_allowed"
    spool = s.data_dir / "runner" / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "x1.json").write_text(json.dumps(cmd))
    runner.process(s)
    assert json.loads((s.data_dir / "runner/results/x1.json").read_text())["status"] == "rejected"
    # Aufträge an den Runner ohne gültige Signatur, zu alt, falsche Dauer, doppelt → abgelehnt
    local = nsystem.runner_paths(s)["local"]
    local.mkdir(parents=True, exist_ok=True)
    forged = {"id": "sup-f", "action": "support_open", "hours": 4, "at": int(time.time()), "mac": "0" * 64}
    (local / "a.json").write_text(json.dumps(forged))
    old = {"id": "sup-o", "action": "support_open", "hours": 4, "at": int(time.time()) - 3600}
    old["mac"] = nsystem.local_mac(s.secret_key, old)
    (local / "b.json").write_text(json.dumps(old))
    long = {"id": "sup-l", "action": "support_open", "hours": 12, "at": int(time.time())}
    long["mac"] = nsystem.local_mac(s.secret_key, long)
    (local / "c.json").write_text(json.dumps(long))
    (local / "d.json").write_text("kein json")
    assert runner.process_local(s) == 4
    assert not fake_system.ran("wg-quick", "up") and not support.load_state(runner.STATE_DIR).get("open")
    reasons = sorted(json.loads(f.read_text())["detail"]["reason"] for f in nsystem.runner_paths(s)["events"].glob("*.json"))
    assert reasons == ["bad_hours", "bad_signature", "expired", "invalid"]
    ok = {"id": "sup-r", "action": "support_open", "hours": 2, "at": int(time.time())}
    ok["mac"] = nsystem.local_mac(s.secret_key, ok)
    (local / "e.json").write_text(json.dumps(ok))
    runner.process_local(s)
    support.close_access(runner.STATE_DIR, nsystem.runner_paths(s)["events"], "early")
    fake_system.calls.clear()
    (local / "f.json").write_text(json.dumps(ok))                     # gleicher Auftrag nochmals
    runner.process_local(s)
    assert not fake_system.ran("wg-quick", "up")
    # Runner-Runde ohne Auftrag öffnet nichts
    runner.support_tick(s)
    assert not fake_system.ran("wg-quick", "up")
    # Therapie und Empfang können den Zugang nicht öffnen (keine Seite; «gleich öffnen» bei der Anfrage wird ignoriert)
    for email, screen in (("therapie@demo.n-fizyo.ch", "therapie"), ("empfang@demo.n-fizyo.ch", "empfang")):
        csrf = await login(bc, email)
        r = await bc.post("/leitung/einstellungen/nsystem", data={"_csrf": csrf, "do": "support_open", "hours": "4"}, allow_redirects=False)
        assert r.status in (403, 404)
        r = await bc.post(f"/{screen}/einstellungen/nsystem", data={"_csrf": csrf, "do": "support_open", "hours": "4"}, allow_redirects=False)
        assert r.status == 404
        before = len(list(local.glob("*.json")))
        r = await bc.post(f"/{screen}/hilfe", data={"_csrf": csrf, "area": "technical", "urgency": "normal", "body": "Drucker geht nicht",
                                                    "with_access": "1", "hours": "8"}, allow_redirects=False)
        assert r.status == 302 and len(list(local.glob("*.json"))) == before
    assert db.value("SELECT COUNT(*) FROM box_support_requests WHERE with_access = 1") == 0
    assert db.value("SELECT COUNT(*) FROM box_support_sessions") == 0


def test_journal_parser_only_support_user():
    lines = [json.dumps({"__CURSOR": "a", "SYSLOG_IDENTIFIER": "sshd", "MESSAGE": "Accepted publickey for nfizyo-support from 10.77.0.1 port 1"}),
             json.dumps({"__CURSOR": "b", "SYSLOG_IDENTIFIER": "sshd", "MESSAGE": "Failed publickey for nfizyo-support from 192.168.1.9 port 2"}),
             json.dumps({"__CURSOR": "c", "SYSLOG_IDENTIFIER": "sshd", "MESSAGE": "Accepted password for root from 192.168.1.9 port 3"}),
             json.dumps({"__CURSOR": "d", "SYSLOG_IDENTIFIER": "n-fizyo-support", "MESSAGE": "cmd=rm -rf / ; cat /etc/shadow"}),
             json.dumps({"__CURSOR": "e", "SYSLOG_IDENTIFIER": "n-fizyo-support", "MESSAGE": "cmd=restart n-fizyo"}), "kaputt"]
    found, cursor = support.parse_journal(lines)
    assert [(k, d) for k, d, _ in found] == [("support_ssh_login", {"from": "10.77.0.1"}), ("support_ssh_denied", {}),
                                             ("support_command", {"cmd": "restart n-fizyo"})]
    assert cursor == "e"


def test_support_config_is_validated(tmp_path):
    s = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, **SUPPORT)
    cfg, err = support.config(s)
    assert err is None and cfg["ssh_keys"] == [SSH_KEY]
    for bad in ({"support_hub": "hub.test:51820\nPostUp = rm -rf /"}, {"support_address": "10.77.0.1"},
                {"support_hub_pubkey": "kein-schluessel"}, {"support_ssh_keys": 'ssh-ed25519 AAAA" command="sh'}):
        assert support.config(s.with_overrides(**bad))[0] is None, bad
    assert support.config(load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True))[1] == "not_configured"


# --------------------------------------------------------------------------- 3) Support-Anfragen
BLOCKED = (
    ("Rechnung von Lea Muster geht nicht", "name"),
    ("Bei Muster Peter fehlt der Termin", "name"),
    ("ayse ornek kann sich nicht anmelden", "name"),
    ("Geburtsdatum 05.03.1992 wird falsch angezeigt", "birthdate"),
    ("geb. 1948-11-02", "birthdate"),
    ("AHV 756.1234.5678.97 wird abgelehnt", "ahv"),
    ("Karte 80756 01234 56789 01234 ungültig", "insured"),
    ("Versichertennummer 12345678901 falsch", "insured"),
    ("Bitte zurückrufen: 079 123 45 67", "phone"),
    ("Tel +41 32 123 45 67", "phone"),
    ("Mail an lea@example.ch kam nicht an", "email"),
    ("Frau Muster ruft an (nur Nachname)", "name"),               # Anrede + Nachname einer Patientin (QA F14)
)


def test_patient_data_scanner(app):
    db = app[DB]
    db.execute("UPDATE patients SET insured_number = '123.4567.8901' WHERE first_name = 'Hans' AND practice_id = 1")
    for text, cat in BLOCKED:
        assert cat in patientdata.scan(text, db, 1), text
    for ok in ("Rechnung R2026-0012 lässt sich seit 03.11.2026 nicht stornieren", "Version 0.9.0, Fehler 500 beim Speichern",
               "Frau Keller vom Empfang ruft an (kein Patientenname)", "Agenda ab 07:00 nicht sichtbar",
               "IBAN CH93 0076 2011 6238 5295 7"):
        assert patientdata.scan(ok, db, 1) == [], ok
    assert patientdata.scan("Lea Muster", db, 2) == []                 # Namen nur der eigenen Praxis
    assert patientdata.scan("AHV 7561234567897") == ["ahv"]             # Panel: nur Muster


async def test_support_request_blocks_patient_data_and_never_stores_it(client, app):
    db = app[DB]
    db.execute("UPDATE patients SET insured_number = '12345678901' WHERE first_name = 'Hans' AND practice_id = 1")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get("/therapie/hilfe")).text()
    assert "Bitte keine Patientendaten schreiben." in html and "Supportzugang gleich öffnen" not in html
    for text, cat in BLOCKED:
        r = await client.post("/therapie/hilfe", data={"_csrf": csrf, "area": "billing", "urgency": "normal", "body": text})
        page = await r.text()
        assert r.status == 422 and "Nicht gesendet" in page, text
        assert {"name": "Name einer Patientin", "birthdate": "Geburtsdatum", "ahv": "AHV-Nummer", "insured": "Versicherten",
                "phone": "Telefonnummer", "email": "E-Mail-Adresse"}[cat] in page
    assert db.value("SELECT COUNT(*) FROM box_support_requests") == 0 and db.value("SELECT COUNT(*) FROM box_events") == 0
    dump = json.dumps([dict(r) for r in db.query("SELECT * FROM audit_log")], ensure_ascii=False)
    assert all(text not in dump for text, _ in BLOCKED)
    # sauberer Text geht
    r = await client.post("/therapie/hilfe", data={"_csrf": csrf, "area": "billing", "urgency": "urgent",
                                                    "body": "Storno einer Rechnung meldet «gesperrt»"}, allow_redirects=False)
    assert r.status == 302
    assert db.one("SELECT area, urgency, sent_at FROM box_support_requests")["urgency"] == "urgent"


async def test_support_request_roundtrip_reply_and_panel_check(env):
    bapp, bc, capp, c = env["box"], env["bclient"], env["central"], env["cclient"]
    csrf = await login(bc, "therapie@demo.n-fizyo.ch")
    await bc.post("/therapie/hilfe", data={"_csrf": csrf, "area": "agenda", "urgency": "normal", "body": "Wochenansicht lädt sehr langsam"})
    report = agent.build_report(bapp)
    assert report["support_requests"][0]["body"] == "Wochenansicht lädt sehr langsam"
    assert report["support_requests"][0]["from_name"] == "T. Demo" and "Lea" not in json.dumps(report)
    assert await agent.send_once(bapp)
    cdb = capp[central.DBK]
    row = cdb.one("SELECT * FROM support_requests")
    assert row["body"] == "Wochenansicht lädt sehr langsam" and row["status"] == "new" and row["blocked"] is None
    assert "Wochenansicht" not in cdb.value("SELECT payload FROM reports ORDER BY id DESC LIMIT 1")   # Text nicht in der Meldung
    assert any("Neue Support-Anfrage: Agenda" in p[1] and "Wochenansicht" not in p[1] for p in env["pushes"])
    assert bapp[DB].one("SELECT sent_at FROM box_support_requests")["sent_at"]
    # NSystem antwortet (Antwort mit Patientendaten wird abgewiesen)
    staff_csrf, _ = await staff_login(c, capp, env["cs"])
    assert "Wochenansicht" in await (await c.get(f"/support/{row['id']}")).text()
    r = await c.post(f"/support/{row['id']}", data={"_csrf": staff_csrf, "status": "in_progress", "reply": "Rufen Sie 079 123 45 67 an"})
    assert "persönliche Daten" in await r.text() and cdb.value("SELECT reply FROM support_requests") is None
    await c.post(f"/support/{row['id']}", data={"_csrf": staff_csrf, "status": "in_progress", "reply": "Wir prüfen es heute Nachmittag."})
    assert cdb.one("SELECT rev, status FROM support_requests")["rev"] == 1
    assert await agent.send_once(bapp)                              # Box meldet ihren Stand (rev 0) → bekommt die Antwort
    local = bapp[DB].one("SELECT * FROM box_support_requests")
    assert local["status"] == "in_progress" and local["reply"] == "Wir prüfen es heute Nachmittag." and local["reply_by"] == "S. NSystem"
    html = await (await bc.get("/therapie/heute")).text()
    assert re.search(r'href="/therapie/hilfe".*?nav-badge">1<', html, re.S)
    html = await (await bc.get("/therapie/hilfe")).text()
    assert "Wir prüfen es heute Nachmittag." in html and "In Bearbeitung" in html
    assert bapp[DB].one("SELECT reply_seen_at FROM box_support_requests")["reply_seen_at"]
    # Antwort mit falscher Signatur wird nicht übernommen
    forged = {"uid": local["uid"], "rev": 5, "status": "done", "reply": "gefälscht", "reply_by": "X", "signature": "AAAA"}
    assert agent.accept_replies(bapp, [forged]) == 0 and bapp[DB].value("SELECT reply FROM box_support_requests") != "gefälscht"
    # Eine veränderte Box schickt doch Patientendaten → Übersicht speichert den Text nicht
    body = json.dumps({"app_version": "0.9.0", "support_requests": [{"uid": "req-x", "area": "billing", "urgency": "urgent",
                                                                     "body": "AHV 756.1234.5678.97 von Lea Muster"}]}).encode()
    ts = str(int(time.time()))
    box_key = crypto.decrypt(env["cs"].secret_key, central.PURPOSE_BOX, cdb.value("SELECT key_enc FROM boxes WHERE id = ?", (env["box_id"],)))
    r = await c.post("/api/v1/report", data=body, headers={"X-Box-Id": env["box_id"], "X-Timestamp": ts,
                                                            "X-Signature": signing.report_signature(box_key, ts, body)})
    assert r.status == 200
    blocked = cdb.one("SELECT * FROM support_requests WHERE uid = 'req-x'")
    assert blocked["body"] is None and blocked["blocked"] == "ahv"
    everything = json.dumps({t: [dict(x) for x in cdb.query(f"SELECT * FROM {t}")] for t in ("support_requests", "reports", "audit", "alerts")},
                            ensure_ascii=False)
    assert "756.1234" not in everything and "Lea Muster" not in everything
    assert "Text nicht gespeichert" in await (await c.get(f"/support/{blocked['id']}")).text()


async def test_leitung_can_open_access_with_request(env, fake_system):
    bapp, bc, s = env["box"], env["bclient"], env["settings"]
    runner.write_status(s.data_dir, s)
    csrf = await login(bc, "leitung@demo.n-fizyo.ch")
    html = await (await bc.get("/leitung/hilfe")).text()
    assert "Supportzugang gleich öffnen" in html
    r = await bc.post("/leitung/hilfe", data={"_csrf": csrf, "area": "technical", "urgency": "urgent", "body": "Server sehr langsam",
                                               "with_access": "1", "hours": "2"}, allow_redirects=False)
    assert r.status == 302
    assert bapp[DB].one("SELECT with_access FROM box_support_requests")["with_access"] == 1
    assert bapp[DB].one("SELECT hours, status FROM box_support_sessions")["hours"] == 2
    runner.process_local(s)
    assert support.load_state(runner.STATE_DIR)["open"]
    assert await agent.send_once(bapp)
    assert env["central"][central.DBK].one("SELECT with_access FROM support_requests")["with_access"] == 1


# --------------------------------------------------------------------------- 4) Wartungshinweise
async def test_maintenance_notice_banner_window_dismiss_and_withdraw(env):
    bapp, bc, capp, c = env["box"], env["bclient"], env["central"], env["cclient"]
    staff_csrf, _ = await staff_login(c, capp, env["cs"])
    now = datetime.now(TZ)
    fmt = "%Y-%m-%dT%H:%M"
    await c.post("/hinweise", data={"_csrf": staff_csrf, "text_de": "Heute 18 Uhr kurze Wartung.", "text_fr": "Maintenance brève à 18 h.",
                                    "starts_at": (now - timedelta(minutes=5)).strftime(fmt), "ends_at": (now + timedelta(hours=2)).strftime(fmt),
                                    "target": "selected", "box": env["box_id"]})
    await c.post("/hinweise", data={"_csrf": staff_csrf, "text_de": "Später: Umzug des Servers.", "text_fr": "Plus tard : déménagement.",
                                    "starts_at": (now + timedelta(hours=5)).strftime(fmt), "ends_at": (now + timedelta(hours=6)).strftime(fmt),
                                    "target": "all"})
    await c.post("/hinweise", data={"_csrf": staff_csrf, "text_de": "Nur für die andere Praxis.", "text_fr": "Seulement l'autre.",
                                    "ends_at": (now + timedelta(hours=1)).strftime(fmt), "target": "selected", "box": env["other_id"]})
    assert capp[central.DBK].value("SELECT COUNT(*) FROM notices") == 3
    assert await agent.send_once(bapp)
    assert bapp[DB].value("SELECT COUNT(*) FROM box_notices") == 2                       # nicht der für die andere Box
    csrf = await login(bc, "therapie@demo.n-fizyo.ch")
    html = await (await bc.get("/therapie/heute")).text()
    assert "Heute 18 Uhr kurze Wartung." in html and "Später: Umzug" not in html and "Nur für die andere" not in html
    nid = bapp[DB].value("SELECT id FROM box_notices WHERE text_de LIKE 'Heute%'")
    r = await bc.post(f"/therapie/hinweise/{nid}/ausblenden", data={"_csrf": csrf, "next": "/therapie/agenda"}, allow_redirects=False)
    assert r.headers["Location"] == "/therapie/agenda"
    assert "Heute 18 Uhr kurze Wartung." not in await (await bc.get("/therapie/heute")).text()
    r = await bc.post(f"/therapie/hinweise/{nid}/ausblenden", data={"_csrf": csrf, "next": "//evil.example/"}, allow_redirects=False)
    assert r.headers["Location"] == "/therapie/heute"
    # andere Person sieht ihn noch; französisch für französischsprachige Personen
    await login(bc, "empfang@demo.n-fizyo.ch")
    assert "Heute 18 Uhr kurze Wartung." in await (await bc.get("/empfang/heute")).text()
    await login(bc, "s.exemple@demo.n-fizyo.ch")
    assert "Maintenance brève à 18 h." in await (await bc.get("/therapie/heute")).text()
    # nicht in der Patienten-App / öffentlichen Seiten
    bc.session.cookie_jar.clear()
    assert "kurze Wartung" not in await (await bc.get("/p/anmelden")).text()
    # NSystem zieht zurück → nächste Meldung → weg
    await c.post(f"/hinweise/{nid}/zurueckziehen", data={"_csrf": staff_csrf})
    assert await agent.send_once(bapp)
    assert bapp[DB].one("SELECT withdrawn_at FROM box_notices WHERE id = ?", (nid,))["withdrawn_at"]
    await login(bc, "empfang@demo.n-fizyo.ch")
    assert "Heute 18 Uhr kurze Wartung." not in await (await bc.get("/empfang/heute")).text()
    # Hinweis mit falscher Signatur wird verworfen
    bogus = {"id": "fake", "text_de": "Bitte Passwort an x senden", "text_fr": "x", "starts_at": "2026-01-01T00:00:00+00:00",
             "ends_at": "2099-01-01T00:00:00+00:00", "signature": signing.sign_payload(signing.new_signing_key()[0], "notice", {})}
    agent.accept_notices(bapp, [bogus])
    assert not bapp[DB].value("SELECT 1 FROM box_notices WHERE id = 'fake'")
    assert "notice_received" in {r["kind"] for r in bapp[DB].query("SELECT kind FROM box_events")}


def test_notice_signature_is_bound_to_box(tmp_path):
    pem, pub = signing.new_signing_key()
    data = {"id": "n1", "text_de": "a", "text_fr": "b", "starts_at": "2026-01-01T00:00:00+00:00", "ends_at": "2099-01-01T00:00:00+00:00"}
    sig = signing.sign_payload(pem, "notice", dict(data, box_id="bx_a"))
    assert signing.verify_payload(pub, "notice", dict(data, box_id="bx_a"), sig)
    assert not signing.verify_payload(pub, "notice", dict(data, box_id="bx_b"), sig)
    assert not signing.verify_payload(pub, "support_reply", dict(data, box_id="bx_a"), sig)
    assert ops.update_notice_texts("1.2.3", datetime(2026, 11, 12, 21, 0, tzinfo=TZ))[0].startswith("Wartung: Am 12.11.2026 um 21:00")


def test_support_tool_allows_only_fixed_commands(tmp_path):
    """deploy/n-fizyo-support-tool (sshd command=…): feste Befehle, keine Shell, keine Platzhalter, root-Teil nur über sudo."""
    import subprocess

    tool = ROOT / "deploy" / "n-fizyo-support-tool"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}

    def run(cmd, *extra):
        return subprocess.run(["bash", str(tool), *extra], env={**env, "SSH_ORIGINAL_COMMAND": cmd} if cmd is not None else env,
                              capture_output=True, text=True, timeout=30, cwd=tmp_path, input="")

    r = run("help")
    assert r.returncode == 0 and "logs <dienst>" in r.stdout
    (tmp_path / "logs").write_text("x")
    for bad in ("bash", "sh -c id", "logs n-fizyo; id", "logs /etc/shadow", "logs n-fizyo 99999", "restart sshd", "cat /etc/n-fizyo/n-fizyo.env",
                "backup-test ../../etc", "*", "status $(id)", "versions --env /tmp/x", "logs n-fizyo 10 20"):
        r = run(bad)
        assert r.returncode != 0 and "Nicht erlaubt" in r.stderr, bad
    r = run(None, "--root", "logs n-fizyo 10")                         # nicht über sudo vom Support-Benutzer → abgelehnt
    assert r.returncode != 0 and "Nicht erlaubt" in r.stderr
    sudoers = (ROOT / "deploy" / "support-setup.sh").read_text()
    assert "NOPASSWD: /usr/local/sbin/n-fizyo-support-tool --root *" in sudoers and "DenyUsers %s@*,!%s" in sudoers
