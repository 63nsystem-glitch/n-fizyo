"""Servicekabel (agent/service.py + tools/servis/servis_client.py): Laptop von NSystem → Box per Netzwerkkabel.

Geprüft: nur signierte Anfragen, Einmal-Code, Prüfsumme des Inhalts, nur signierte und neuere Pakete, nur eine Aufgabe zugleich,
Rückkehr, nur link-local-Adresse, Protokoll der Leitung. Der echte Ablauf (update.sh) ist hier ersetzt (run=…).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from nfizyo.agent import runner, service, servicelink, signing
from nfizyo.core.config import load_settings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "servis"))
import servis_client  # noqa: E402
from servis_client import BoxClient, ServiceError  # noqa: E402
from test_nsystem_ops import fake_repo, publish  # noqa: E402


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Box mit installierter Version 1.0.0, Servicekabel auf 127.0.0.1 (im Betrieb nur 169.254.77.1)."""
    installed = tmp_path / "opt" / "app" / "nfizyo"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text('__version__ = "1.0.0"\n')
    monkeypatch.setattr(runner, "APP_DIR", tmp_path / "opt")
    monkeypatch.setattr(runner, "STATE_DIR", tmp_path / "runner-state")
    monkeypatch.setattr(runner, "services", lambda: {"n-fizyo": "active"})
    monkeypatch.setattr(runner, "gpu", lambda: None)
    monkeypatch.setattr(service, "_health", lambda: {"ok": True, "version": "1.0.0"})
    service_pem, service_pub = signing.new_signing_key()
    release_pem, release_pub = signing.new_signing_key()
    s = load_settings(None, data_dir=tmp_path / "data", secret_key=b"t" * 32, demo=True, service_pubkey=service_pub,
                      release_pubkey=release_pub, central_box_id="box-7")
    ran = []

    def fake_run(cmd, timeout):
        ran.append(cmd)
        return 0, "Update fertig"

    svc = service.Service(s, state_dir=tmp_path / "service-state", run=fake_run)
    srv = service.make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = BoxClient(service_pem, host="127.0.0.1", port=srv.server_address[1])
    yield {"svc": svc, "client": client, "ran": ran, "release_pem": release_pem, "settings": s, "tmp": tmp_path,
           "port": srv.server_address[1]}
    srv.shutdown()


def _events(settings) -> list[dict]:
    folder = settings.data_dir / "runner" / "events"
    return [json.loads(f.read_text()) for f in sorted(folder.glob("*.json"))] if folder.exists() else []


def _wait(client, state="running"):
    for _ in range(100):
        job = client.progress()["job"]
        if job.get("state") != state:
            return job
        time.sleep(0.05)
    raise AssertionError("Aufgabe endet nicht")


def _package(box, version="9.9.9"):
    repo = fake_repo(box["tmp"] / f"repo-{version}")
    if version != "9.9.9":
        init = repo / "nfizyo" / "__init__.py"
        init.write_text(init.read_text().replace("9.9.9", version))
    store = box["tmp"] / f"store-{version}"
    m = publish(repo, store, box["release_pem"])
    return store / f"n-fizyo-{m['version']}.manifest.json", m


def test_hello_gives_version_and_one_time_code(box):
    h = box["client"].hello()
    assert h["version"] == "1.0.0" and h["box_id"] == "box-7" and h["ready"] is True and len(h["nonce"]) >= 30
    assert box["client"].hello()["nonce"] != h["nonce"]


def test_signed_status(box):
    st = box["client"].status()
    assert st["version"] == "1.0.0" and st["health"]["ok"] and st["services"] == {"n-fizyo": "active"}
    assert st["pending"] is None and st["job"]["state"] == "idle"


def test_wrong_key_and_replay_are_refused(box):
    other = BoxClient(signing.new_signing_key()[0], host="127.0.0.1", port=box["port"])
    with pytest.raises(ServiceError, match="bad_signature"):
        other.status()
    # gleicher Einmal-Code zweimal → zweites Mal abgewiesen
    c = box["client"]
    nonce = c.hello()["nonce"]
    data = servicelink.payload("status", nonce, servicelink.sha256_bytes(b""))
    import http.client

    for expected in (200, 403):
        conn = http.client.HTTPConnection("127.0.0.1", box["port"], timeout=5)
        conn.request("POST", "/status", body=b"", headers={**servicelink.headers(c.pem, data), "Content-Length": "0"})
        assert conn.getresponse().status == expected
    kinds = [(e["kind"], e["detail"].get("why")) for e in _events(box["settings"])]
    assert ("service_denied", "bad_signature") in kinds and ("service_denied", "nonce") in kinds


def test_signature_covers_action_and_body(box):
    import http.client

    c = box["client"]
    # Signatur für «status» → als «rollback» geschickt: abgewiesen
    data = servicelink.payload("status", c.hello()["nonce"], servicelink.sha256_bytes(b""))
    conn = http.client.HTTPConnection("127.0.0.1", box["port"], timeout=5)
    conn.request("POST", "/rollback", body=b"", headers={**servicelink.headers(c.pem, data), "Content-Length": "0"})
    assert conn.getresponse().status == 400
    # anderer Inhalt als signiert
    data = servicelink.payload("status", c.hello()["nonce"], servicelink.sha256_bytes(b""))
    conn = http.client.HTTPConnection("127.0.0.1", box["port"], timeout=5)
    conn.request("POST", "/status", body=b"x", headers={**servicelink.headers(c.pem, data), "Content-Length": "1"})
    assert conn.getresponse().status == 400
    assert not box["ran"]


def test_unknown_paths_and_no_key(box):
    import http.client

    for method, path in (("GET", "/status"), ("POST", "/shell"), ("GET", "/../etc/passwd")):
        conn = http.client.HTTPConnection("127.0.0.1", box["port"], timeout=5)
        conn.request(method, path, body=b"" if method == "POST" else None)
        assert conn.getresponse().status == 404
    box["svc"].settings = box["svc"].settings.with_overrides(service_pubkey="")
    assert box["client"].hello()["ready"] is False
    with pytest.raises(ServiceError, match="no_service_key"):
        box["client"].status()


def test_upload_update_and_protocol(box):
    manifest_path, m = _package(box)
    out = box["client"].upload(manifest_path)
    assert out["manifest"]["version"] == "9.9.9" and out["diff"]["from"] == "1.0.0" and out["diff"]["to"] == "9.9.9"
    assert box["client"].status()["pending"]["version"] == "9.9.9"
    assert not box["ran"]                                                  # Hochladen allein ändert nichts
    started = box["client"].update()
    assert started["job"]["action"] == "update"
    job = _wait(box["client"])
    assert job["state"] == "done" and "Update fertig" in job["log"]
    assert box["ran"][-1][0] == "bash" and box["ran"][-1][1].endswith("deploy/update.sh")
    assert box["svc"].restart_self and box["client"].status()["pending"] is None
    acts = [(e["detail"].get("action"), e["detail"].get("result")) for e in _events(box["settings"]) if e["kind"] == "service_action"]
    assert ("upload", "ok") in acts and ("update", "started") in acts and ("update", "ok") in acts
    # Ergebnis bleibt nach Neustart des Dienstes sichtbar (der Dienst lädt sich nach dem Update neu)
    again = service.Service(box["settings"], state_dir=box["tmp"] / "service-state", events=False)
    assert again.job_view()["state"] == "done"


def test_upload_refuses_unsigned_manipulated_or_older(box):
    manifest_path, m = _package(box)
    doc = json.loads(manifest_path.read_text())
    # Manifest mit fremdem Release-Schlüssel signiert
    foreign = signing.new_signing_key()[0]
    bad = dict(doc, signature=signing.sign_payload(foreign, "release-manifest", doc["manifest"]))
    p = manifest_path.parent / "fremd" / manifest_path.name
    p.parent.mkdir()
    p.write_text(json.dumps(bad))
    (p.parent / m["file"]).write_bytes((manifest_path.parent / m["file"]).read_bytes())
    with pytest.raises(ServiceError, match="Manifest abgelehnt"):
        box["client"].upload(p)
    # Archiv ausgetauscht (Manifest echt) → Prüfsumme passt nicht
    (manifest_path.parent / m["file"]).write_bytes(b"manipuliert")
    with pytest.raises(ServiceError, match="Prüfsumme"):
        box["client"].upload(manifest_path)
    # ältere Version als installiert
    (runner.APP_DIR / "app" / "nfizyo" / "__init__.py").write_text('__version__ = "10.0.0"\n')
    older, _ = _package(box, "9.9.10")
    with pytest.raises(ServiceError, match="nicht neuer"):
        box["client"].upload(older)
    assert box["client"].status()["pending"] is None and not box["ran"]
    with pytest.raises(ServiceError, match="Kein geprüftes Paket"):
        box["client"].update()


def test_one_job_at_a_time_and_rollback(box):
    gate = threading.Event()

    def slow_run(cmd, timeout):
        box["ran"].append(cmd)
        gate.wait(5)
        return 0, "ok"

    box["svc"].run = slow_run
    box["client"].backup()
    with pytest.raises(ServiceError, match="läuft gerade"):
        box["client"].rollback()
    gate.set()
    assert _wait(box["client"])["state"] == "done"
    box["client"].rollback()
    assert _wait(box["client"])["state"] == "done"
    assert box["ran"][0] == ["/usr/local/sbin/n-fizyo-backup"]
    assert box["ran"][1][-1] == "--rollback"


def test_large_bodies_refused(box, monkeypatch):
    monkeypatch.setattr(servicelink, "MAX_BODY", 10)
    import http.client

    c = box["client"]
    body = b"x" * 20
    data = servicelink.payload("status", c.hello()["nonce"], servicelink.sha256_bytes(body))
    conn = http.client.HTTPConnection("127.0.0.1", box["port"], timeout=5)
    conn.request("POST", "/status", body=body, headers={**servicelink.headers(c.pem, data), "Content-Length": str(len(body))})
    assert conn.getresponse().status == 413


def test_only_link_local_address():
    service.check_bind("169.254.77.1")
    for bad in ("0.0.0.0", "192.168.1.10", "127.0.0.1", "10.0.0.5"):
        with pytest.raises(SystemExit):
            service.check_bind(bad)


def test_service_key_lives_outside_project(tmp_path):
    key = tmp_path / "NSystem" / "key.pem"
    pub = servis_client.create_key(key)
    assert servis_client.public_key(key) == pub and len(pub) == 44
    with pytest.raises(ServiceError):
        servis_client.create_key(key)                                   # nie überschreiben
    with pytest.raises(ServiceError, match="proje"):
        servis_client.create_key(servis_client.ROOT / "tools" / "servis" / "key.pem")


def test_setup_script_guards():
    text = (Path(__file__).resolve().parent.parent / "deploy" / "service-port-setup.sh").read_text()
    assert "169.254.77.1" in text and "ufw insert 1 deny in on" in text and "ufw insert 1 allow in on" in text
    assert "Karte im Praxisnetz" in text                                 # Praxisnetz-Karte wird abgelehnt
    upd = (Path(__file__).resolve().parent.parent / "deploy" / "update.sh").read_text()
    assert "NFIZYO_FROM_SERVICE" in upd
