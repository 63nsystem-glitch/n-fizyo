"""NSystem-Übersicht und Box-Anbindung: signierte Meldungen (ohne Patientendaten), signierte Befehle, Runner, Warnungen."""
from __future__ import annotations

import json
import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nfizyo.agent import client as agent, runner, signing
from nfizyo.app import create_app
from nfizyo.central import app as central
from nfizyo.central.__main__ import public_key
from nfizyo.central.config import load as load_central
from nfizyo.core import crypto, totp
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB, SETTINGS


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    pushes = []

    async def fake_push(settings, title, message, priority="high"):
        pushes.append((title, message))
        return True

    monkeypatch.setattr(central, "push", fake_push)
    cs = load_central(None, data_dir=tmp_path / "central", secret_key=b"c" * 32, base_url="http://central.test")
    cs.data_dir.mkdir(parents=True)
    pem, _ = signing.new_signing_key()
    cs.signing_key_path.write_bytes(pem)
    capp = central.create_central_app(cs, start_watch=False)
    box_id, box_key = central.add_box(capp[central.DBK], cs.secret_key, "Physio Test")
    central.add_staff(capp[central.DBK], "s@nsystem.invalid", "S. NSystem", "nsystem-passwort-1")
    server = TestServer(capp)
    cclient = TestClient(server)
    await cclient.start_server()
    settings = load_settings(None, data_dir=tmp_path / "box", secret_key=b"t" * 32, demo=True,
                             central_url=str(server.make_url("")).rstrip("/"), central_box_id=box_id, central_box_key=box_key,
                             central_command_pubkey=public_key(cs))
    bapp = create_app(settings, start_workers=False)
    yield {"central": capp, "cs": cs, "cclient": cclient, "box": bapp, "box_id": box_id, "pushes": pushes}
    await cclient.close()


async def _staff_login(c: TestClient, capp, cs) -> str:
    """Passwort → Einrichtung der Zwei-Schritt-Anmeldung → angemeldet. Gibt das CSRF-Token zurück."""
    html = await (await c.get("/login")).text()
    csrf = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    r = await c.post("/login", data={"_csrf": csrf, "email": "s@nsystem.invalid", "password": "nsystem-passwort-1"}, allow_redirects=False)
    assert r.headers["Location"] == "/login/einrichten"
    html = await (await c.get("/login/einrichten")).text()
    csrf = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    staff = capp[central.DBK].one("SELECT * FROM staff")
    secret = crypto.decrypt(cs.secret_key, central.PURPOSE_TOTP, staff["totp_pending"])
    r = await c.post("/login/einrichten", data={"_csrf": csrf, "code": totp.code_at(secret, totp.current_step())})
    assert "Wiederherstellungscodes" in await r.text()
    html = await (await c.get("/")).text()
    assert "Physio Test" in html
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1), secret


async def test_report_is_signed_and_has_no_patient_data(setup):
    bapp, capp = setup["box"], setup["central"]
    report = agent.build_report(bapp)
    body = json.dumps(report)
    for name in ("Lea", "Muster", "Ayşe", "079 000", "@example.invalid", "Lumboischialgie"):
        assert name not in body, name
    assert report["practices"][0]["staff"] > 0 and "app_version" in report
    assert await agent.send_once(bapp)
    row = capp[central.DBK].one("SELECT * FROM boxes")
    assert row["last_report_at"] and row["last_version"] == report["app_version"]
    # Runner meldet (noch) nicht → Warnung mit Push
    assert any(k == "runner" for (k,) in capp[central.DBK].query("SELECT kind FROM alerts WHERE closed_at IS NULL"))
    assert setup["pushes"] and "Physio Test" in setup["pushes"][0][0]


async def test_report_with_operations_still_has_no_patient_data(setup):
    """0.9.0: Protokoll, Support-Anfragen, Supportzugang und Wartungshinweise in der Meldung – weiterhin keine Patientendaten."""
    from nfizyo.core import nsystem, patientdata

    bapp = setup["box"]
    db = bapp[DB]
    owner = db.one("SELECT * FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    text = "Agenda zeigt Termine doppelt"
    assert patientdata.scan(text, db, 1) == []
    nsystem.create_request(db, practice_id=1, user_id=owner["id"], area="agenda", urgency="normal", body=text, with_access=False)
    nsystem.log_event(db, "support_requested", detail={"hours": 4, "request_id": "sup-x"}, practice_id=1, user_id=owner["id"])
    nsystem.log_event(db, "support_command", detail={"cmd": "logs n-fizyo 200"})
    nsystem.sync_notices(db, [{"id": "n1", "text_de": "Wartung", "text_fr": "Maintenance", "starts_at": "2026-01-01T00:00:00+00:00",
                               "ends_at": "2099-01-01T00:00:00+00:00"}])
    nsystem.dismiss_notice(db, "n1", owner)
    report = agent.build_report(bapp)
    body = json.dumps(report, ensure_ascii=False)
    for name in ("Lea", "Muster", "Ayşe", "079 000", "@example.invalid", "Lumboischialgie", "1992-03-05", "M. Leitung"):
        if name == "M. Leitung":
            assert body.count(name) == 1, name          # nur als Absender der Support-Anfrage (Personal, nicht Patient)
        else:
            assert name not in body, name
    ev = {e["kind"]: e for e in report["events"]}
    assert "detail" not in ev["support_requested"] and ev["support_command"]["detail"] == {"cmd": "logs n-fizyo 200"}
    assert report["support_requests"][0]["body"] == text and set(report["support_requests"][0]) == {
        "uid", "practice", "from_name", "from_role", "area", "urgency", "body", "with_access", "created_at"}
    assert await agent.send_once(bapp)
    stored = setup["central"][central.DBK].value("SELECT payload FROM reports ORDER BY id DESC LIMIT 1")
    assert text not in stored and "Lea" not in stored


async def test_wrong_signature_and_unknown_box_rejected(setup):
    c = setup["cclient"]
    body = b'{"x":1}'
    r = await c.post("/api/v1/report", data=body, headers={"X-Box-Id": setup["box_id"], "X-Timestamp": "1", "X-Signature": "x"})
    assert r.status == 403
    r = await c.post("/api/v1/report", data=body, headers={"X-Box-Id": "bx_unknown", "X-Timestamp": "1", "X-Signature": "x"})
    assert r.status == 403


async def test_signed_command_roundtrip_with_runner(setup, monkeypatch):
    bapp, capp, cs, c = setup["box"], setup["central"], setup["cs"], setup["cclient"]
    csrf, secret = await _staff_login(c, capp, cs)
    # ohne gültigen Code kein Befehl
    await c.post(f"/box/{setup['box_id']}/befehl", data={"_csrf": csrf, "action": "backup_now", "code": "000000"})
    assert capp[central.DBK].value("SELECT COUNT(*) FROM commands") == 0
    step = totp.current_step() + 1  # Einrichtung hat das aktuelle Zeitfenster benutzt
    await c.post(f"/box/{setup['box_id']}/befehl", data={"_csrf": csrf, "action": "backup_now",
                                                         "code": totp.code_at(secret, step)})
    assert capp[central.DBK].value("SELECT status FROM commands") == "pending"
    # nicht erlaubte Aktion wird gar nicht ausgestellt
    await c.post(f"/box/{setup['box_id']}/befehl", data={"_csrf": csrf, "action": "reset_password", "code": "1"})
    assert capp[central.DBK].value("SELECT COUNT(*) FROM commands") == 1

    assert await agent.send_once(bapp)
    db = bapp[DB]
    cmd = db.one("SELECT * FROM box_commands")
    assert cmd["status"] == "queued" and cmd["issued_by"] == "S. NSystem" and cmd["action"] == "backup_now"
    bapp_settings = bapp[SETTINGS]
    spool = list(bapp_settings.data_dir.glob("runner/spool/*.json"))
    assert len(spool) == 1
    # Runner: prüft selbst nochmals und führt aus (hier ohne echtes System)
    monkeypatch.setattr(runner, "STATE_DIR", bapp_settings.data_dir / "runner-state")
    monkeypatch.setattr(runner, "execute", lambda cmd, s: ("done", "Sicherung erstellt"))
    assert runner.process(bapp_settings) == 1
    agent.collect_results(bapp)
    assert db.one("SELECT status, result FROM box_commands")["status"] == "done"
    # gleicher Befehl nochmals in den Spool gelegt → Runner lehnt ab (schon ausgeführt)
    (bapp_settings.data_dir / "runner/spool").mkdir(parents=True, exist_ok=True)
    stored = capp[central.DBK].one("SELECT * FROM commands")
    replay = {"id": stored["id"], "box_id": stored["box_id"], "action": stored["action"], "args": {}, "issued_by": stored["issued_by"],
              "issued_at": stored["issued_at"], "expires_at": stored["expires_at"], "signature": stored["signature"]}
    (bapp_settings.data_dir / "runner/spool" / "r.json").write_text(json.dumps(replay))
    runner.process(bapp_settings)
    res = json.loads((bapp_settings.data_dir / f"runner/results/{stored['id']}.json").read_text())
    assert res["status"] == "rejected" and res["reason"] == "replay"
    db.execute("UPDATE box_commands SET status = 'done' WHERE id = ?", (stored["id"],))
    (bapp_settings.data_dir / f"runner/results/{stored['id']}.json").unlink()
    assert await agent.send_once(bapp)
    assert capp[central.DBK].value("SELECT status FROM commands") == "done"
    # Leitung sieht die Aktion
    from conftest import login

    bclient = TestClient(TestServer(bapp))
    await bclient.start_server()
    try:
        await login(bclient, "leitung@demo.n-fizyo.ch")
        html = await (await bclient.get("/leitung/einstellungen/nsystem")).text()
        assert "S. NSystem" in html and "Sicherung jetzt erstellen" in html and "Erledigt" in html
    finally:
        await bclient.close()


def test_forged_or_foreign_commands_are_rejected(setup):
    bapp = setup["box"]
    pem_other, _ = signing.new_signing_key()
    import time

    base = {"id": "f1", "box_id": setup["box_id"], "action": "restart_app", "args": {}, "issued_by": "Fremd",
            "issued_at": "2026-10-28T10:00:00+00:00", "expires_at": time.time() + 600}
    forged = dict(base, signature=signing.sign_command(pem_other, base))
    good_pem = setup["cs"].signing_key_path.read_bytes()
    other_box = dict(base, id="f2", box_id="bx_other")
    other_box["signature"] = signing.sign_command(good_pem, other_box)
    expired = dict(base, id="f3", expires_at=time.time() - 10)
    expired["signature"] = signing.sign_command(good_pem, expired)
    not_allowed = dict(base, id="f4", action="shell", args={"cmd": "rm -rf /"})
    not_allowed["signature"] = signing.sign_command(good_pem, not_allowed)
    accepted = agent.accept_commands(bapp, [forged, other_box, expired, not_allowed])
    assert accepted == []
    rows = {r["id"]: r["reason"] for r in bapp[DB].query("SELECT id, reason FROM box_commands")}
    assert rows == {"f1": "bad_signature", "f2": "wrong_box", "f3": "expired", "f4": "not_allowed"}


async def test_central_pages_need_login(setup):
    c = setup["cclient"]
    for url in ("/", f"/box/{setup['box_id']}", "/warnungen", "/protokoll"):
        r = await c.get(url, allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/login"


async def test_box_clock_drift_is_reported_not_mistaken_for_silence(setup, monkeypatch):
    """4. Prüfrunde: geht die Uhr der Box falsch (Zeitabgleich im Praxisnetz gesperrt), scheitern Anmeldecodes (30 s Fenster).
    Vorher: bis 5 Minuten merkte NSystem nichts, darüber wurde die Meldung still abgelehnt und die Box galt als «stumm»."""
    import time as _time
    import types

    from nfizyo.central import health
    from nfizyo.core.box import get_state

    bapp, capp = setup["box"], setup["central"]
    db = capp[central.DBK]

    def clock(shift):
        return types.SimpleNamespace(time=lambda: _time.time() + shift, monotonic=_time.monotonic, sleep=_time.sleep)

    monkeypatch.setattr(central, "time", clock(-45))                 # Box geht 45 s vor
    assert await agent.send_once(bapp)
    stored = json.loads(db.value("SELECT payload FROM reports ORDER BY id DESC LIMIT 1"))
    assert 40 <= stored["clock_offset_s"] <= 50
    assert db.one("SELECT 1 FROM alerts WHERE kind = 'clock' AND closed_at IS NULL")
    monkeypatch.setattr(central, "time", _time)
    monkeypatch.setattr(agent, "time", clock(-900))                  # Box 15 min nach: abgelehnt, aber mit Grund
    assert not await agent.send_once(bapp)
    assert "Uhr der Box weicht ab" in get_state(bapp[DB], "last_error")["value"]
    assert "-90" in db.value("SELECT message FROM alerts WHERE kind = 'clock' AND closed_at IS NULL")
    monkeypatch.setattr(agent, "time", _time)
    assert await agent.send_once(bapp)
    assert not db.one("SELECT 1 FROM alerts WHERE kind = 'clock' AND closed_at IS NULL")
    assert [i.kind for i in health.issues({"clock_synced": False})].count("clock_sync") == 1
