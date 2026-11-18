"""MediData nach den offiziellen Dokumenten: Client-Zertifikat (gegenseitiges TLS), CSR und Erneuerung, Ablauf-Warnung,
neue X-Client-ID (Box/VA → Webservice), Testsendungen an die Simulatoren (nur Testmodus).

Alles gegen einen lokalen, gefälschten MediData-HTTPS-Server mit einer Wegwerf-CA, die im Test erzeugt wird."""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree

from nfizyo import connections, medidata_cli
from nfizyo.app import create_app
from nfizyo.central import health
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.billing import medidata, medidata_cert as mc, simulator, xml500

from conftest import login
from medidata_fake import CLIENT_ID, NEW_CLIENT_ID, NOW, PKI, FakeMediData

SECRET_PW = "md-geheim-" + "x" * 16


# Wegwerf-PKI und gefälschter MediData-Server: tests/medidata_fake.py
@pytest.fixture
def pki(tmp_path):
    d = tmp_path / "pki"
    d.mkdir()
    return PKI(d)


@pytest.fixture
async def fake(pki, aiohttp_server):
    f = FakeMediData(pki)
    server = await aiohttp_server(f.app(), ssl=f.ssl_context())
    f.url = str(server.make_url("")).rstrip("/")
    return f


def _settings(tmp_path, fake=None, *, cert=None, key=None, ca=True, mode="test", **kw):
    pki = fake.pki if fake else None
    values = dict(data_dir=tmp_path / "data", secret_key=b"t" * 32, demo=kw.pop("demo", False), medidata_mode=mode,
                  medidata_url=fake.url if fake else "", medidata_client_id=CLIENT_ID, medidata_user="praxis",
                  medidata_password=SECRET_PW, medidata_cert=str(cert or ""), medidata_key=str(key or ""),
                  medidata_ca=str(pki.ca_path) if (pki and ca) else "", medidata_service_url=fake.url if fake else "https://x.invalid",
                  medidata_appliance_id="id3")
    values.update(kw)
    return load_settings(None, **values)


# =========================================================================== 1 gegenseitiges TLS
async def test_mtls_accepted_and_refused(tmp_path, fake):
    cert, key = fake.pki.client(days=300)
    s = _settings(tmp_path, fake, cert=cert, key=key)
    checks = await connections.check_medidata(s)
    main = checks[0]
    assert main.status == connections.OK and "mit Client-Zertifikat" in main.detail, main
    assert fake.calls[-1]["cn"] == CLIENT_ID                                  # Server hat das Client-Zertifikat gesehen
    assert any(c.area == "MediData Zertifikat" and c.status == connections.OK and "gültig bis" in c.detail for c in checks)
    # Upload geht mit demselben Zertifikat (Schnittstellendokumentation V5.2.0)
    res = await medidata.MediDataClient(s).upload(b"<?xml version='1.0'?><x/>", "t.xml", {"modus": "test"})
    assert res.status == "sent" and fake.uploads[-1]["cn"] == CLIENT_ID and fake.calls[-1]["client_id"] == CLIENT_ID
    assert medidata.CONFIRMED is True

    # ohne Client-Zertifikat: der Server lehnt ab → Fehler, nichts «gesendet»
    s_none = _settings(tmp_path, fake)
    checks = await connections.check_medidata(s_none)
    assert checks[0].status == connections.FAIL and "Client-Zertifikat prüfen" in checks[0].detail
    res = await medidata.MediDataClient(s_none).upload(b"<x/>", "t.xml", {})
    assert res.status != "sent" and len(fake.uploads) == 1

    # Zertifikat einer fremden CA: abgelehnt
    (tmp_path / "other").mkdir()
    c2, k2 = PKI(tmp_path / "other").client("fremd", days=300)
    checks = await connections.check_medidata(_settings(tmp_path, fake, cert=c2, key=k2))
    assert checks[0].status == connections.FAIL

    # Serverprüfung bleibt immer an: ohne die CA der Testumgebung keine Verbindung (kein «verify=False»)
    checks = await connections.check_medidata(_settings(tmp_path, fake, cert=cert, key=key, ca=False))
    assert checks[0].status == connections.FAIL and "Certificate" in checks[0].detail
    assert SECRET_PW not in connections.render(checks)


def test_ssl_context_rules(tmp_path, pki):
    cert, key = pki.client(days=300)
    assert mc.client_ssl_context(_settings(tmp_path)) is None                  # nichts eingetragen → normale Prüfung
    ctx = mc.client_ssl_context(_settings(tmp_path, cert=cert, key=key))
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    with pytest.raises(mc.CertError, match="gehören zusammen"):
        mc.client_ssl_context(_settings(tmp_path, cert=cert))
    # Schlüssel passt nicht zum Zertifikat
    _, other_key = pki.client("anders", days=300)
    with pytest.raises(mc.CertError, match="passen nicht"):
        mc.client_ssl_context(_settings(tmp_path, cert=cert, key=other_key))


# =========================================================================== 2 Rechte des Schlüssels
async def test_key_permission_refusal(tmp_path, fake):
    cert, key = fake.pki.client(days=300)
    for mode in (0o640, 0o604, 0o644):
        os.chmod(key, mode)
        s = _settings(tmp_path, fake, cert=cert, key=key)
        with pytest.raises(mc.CertError, match="chmod 600"):
            mc.client_ssl_context(s)
        st = mc.status(s)
        assert st.state == "error" and f"{mode:04o}" in st.message
        before = len(fake.calls) + len(fake.uploads)
        res = await medidata.MediDataClient(s).upload(b"<x/>", "t.xml", {})
        assert res.status == "failed" and "chmod 600" in res.error                   # sicher nicht gesendet, nicht «unklar»
        checks = await connections.check_medidata(s)
        assert checks[0].status == connections.FAIL and any(c.area == "MediData Zertifikat" and c.status == connections.FAIL
                                                            for c in checks)
        assert len(fake.calls) + len(fake.uploads) == before                         # keine Verbindung aufgebaut
    os.chmod(key, 0o400)
    assert mc.status(_settings(tmp_path, fake, cert=cert, key=key)).state == "ok"
    # Zertifikat darf nicht für andere beschreibbar sein
    os.chmod(cert, 0o666)
    assert "beschreibbar" in mc.status(_settings(tmp_path, fake, cert=cert, key=key)).message


# =========================================================================== 3 CSR
def test_csr_fields_and_key(tmp_path):
    d = tmp_path / "md"
    d.mkdir()
    s = _settings(tmp_path, key=d / "client.key")
    out: list[str] = []
    assert medidata_cli.csr(s, org="Physio Muster AG", city="Biel", out=out) == 0
    key_path = d / "client.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    key = mc.load_key(str(key_path))
    assert isinstance(key, rsa.RSAPrivateKey) and key.key_size == 2048
    request = x509.load_pem_x509_csr((d / "client.csr").read_bytes())
    attrs = {a.oid: a.value for a in request.subject}
    assert attrs == {NameOID.COUNTRY_NAME: "CH", NameOID.LOCALITY_NAME: "Biel", NameOID.ORGANIZATION_NAME: "Physio Muster AG",
                     NameOID.COMMON_NAME: CLIENT_ID}
    assert request.is_signature_valid and mc.spki(request.public_key()) == mc.spki(key.public_key())
    assert not any("PRIVATE KEY" in line for line in out)
    # zweiter Lauf: vorhandener Schlüssel bleibt (gleicher CSR)
    first = (d / "client.csr").read_bytes()
    out = []
    assert medidata_cli.csr(s, org="Physio Muster AG", city="Biel", out=out) == 0 and "Vorhandener Schlüssel" in out[0]
    assert x509.load_pem_x509_csr((d / "client.csr").read_bytes()).public_key().public_numbers() == \
        x509.load_pem_x509_csr(first).public_key().public_numbers()
    # neuer Schlüssel getrennt, alter bleibt unberührt
    old_key = key_path.read_bytes()
    out = []
    assert medidata_cli.csr(s, org="Physio Muster AG", city="Biel", new_key=True, out=out) == 0
    assert (d / "client.new.key").exists() and (d / "client.new.csr").exists() and key_path.read_bytes() == old_key
    assert stat.S_IMODE((d / "client.new.key").stat().st_mode) == 0o600
    assert medidata_cli.csr(s, org="X", city="Y", new_key=True, out=out) == 1        # nie still überschreiben
    # ungültige Eingaben
    for bad in ({"client_id": "a b/c"}, {"client_id": ""}):
        out = []
        s2 = _settings(tmp_path, key=d / "other.key", medidata_client_id="")
        assert medidata_cli.csr(s2, org="O", city="L", out=out, **bad) == 1 and "Client-ID" in out[-1]
        assert not (d / "other.key").exists()                                     # erst prüfen, dann Schlüssel erzeugen
    out = []
    assert medidata_cli.csr(s, org="  ", city="Biel", out=out) == 1 and "Firma" in out[-1]
    # Schlüssel mit falschen Rechten wird nicht verwendet
    os.chmod(key_path, 0o644)
    out = []
    assert medidata_cli.csr(s, org="O", city="L", out=out) == 1 and "chmod 600" in out[-1]


def test_cli_commands_registered(tmp_path, capsys):
    from nfizyo.__main__ import main

    for cmd in ("medidata-csr", "medidata-renew", "medidata-client-id"):
        with pytest.raises(SystemExit) as e:
            main([cmd, "--help"])
        assert e.value.code == 0
    d = tmp_path / "md"
    d.mkdir()
    env = tmp_path / "test.env"
    env.write_text(f"NFIZYO_SECRET_KEY=x\nNFIZYO_DATA_DIR={tmp_path / 'data'}\nNFIZYO_MEDIDATA_CLIENT_ID={CLIENT_ID}\n"
                   f"NFIZYO_MEDIDATA_KEY={d / 'client.key'}\n")
    with pytest.raises(SystemExit) as e:
        main(["--env", str(env), "medidata-csr", "--org", "Physio Muster AG", "--city", "Biel"])
    assert e.value.code == 0 and "CSR erstellt" in capsys.readouterr().out
    assert stat.S_IMODE((d / "client.key").stat().st_mode) == 0o600


# =========================================================================== 4 Erneuerung
def _prepare_renewal(tmp_path, fake, *, days: int = 30, new_key: bool = False, **kw):
    cert, key = fake.pki.client(days=days)
    s = _settings(tmp_path, fake, cert=cert, key=key, **kw)
    out: list[str] = []
    assert medidata_cli.csr(s, org="Physio Muster AG", city="Biel", new_key=new_key, out=out) == 0, out
    return s, cert, key


@pytest.mark.parametrize("accept", ["pem", "json"])
async def test_renewal_success(tmp_path, fake, accept):
    s, cert, key = _prepare_renewal(tmp_path, fake, days=30)
    old_chain = cert.read_bytes()
    assert mc.status(s).state == "warn"                                       # 30 Tage → Erneuerung möglich
    out: list[str] = []
    assert await medidata_cli.renew(s, accept=accept, out=out) == 0, out
    call = fake.calls[-1]
    assert call["path"] == "/md/srv/id3/client/certificates" and call["cn"] == CLIENT_ID      # mit dem aktuellen Zertifikat
    assert call["accept"] == {"pem": "application/pem-certificate-chain", "json": "application/json"}[accept]
    assert call["ctype"] == "application/octet-stream" and call["auth"] is None
    chain = x509.load_pem_x509_certificates(cert.read_bytes())
    assert mc.spki(chain[0].public_key()) == mc.spki(mc.load_key(str(key)).public_key())       # eigenes Zertifikat zuerst
    assert chain[0].not_valid_after_utc > NOW + timedelta(days=1000) and len(chain) == 2
    backups = list(cert.parent.glob(f"{cert.name}.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == old_chain and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(cert.stat().st_mode) == 0o600
    st = mc.status(s)
    assert st.state == "ok" and st.days_left > 1000
    assert not list(cert.parent.glob(".*tmp*"))                                # keine Reste
    # gleich nochmals: nicht fällig → nichts gesendet
    n = len(fake.calls)
    out = []
    assert await medidata_cli.renew(s, out=out) == 0 and "Noch nicht fällig" in out[0] and len(fake.calls) == n


async def test_renewal_with_new_key(tmp_path, fake):
    s, cert, key = _prepare_renewal(tmp_path, fake, days=20, new_key=True)
    old_key = key.read_bytes()
    new_key = (key.parent / "client.new.key").read_bytes()
    out: list[str] = []
    assert await medidata_cli.renew(s, out=out) == 0, out
    assert key.read_bytes() == new_key and not (key.parent / "client.new.key").exists()
    assert [b.read_bytes() for b in key.parent.glob("client.key.bak-*")] == [old_key]
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert mc.status(s).state == "ok"                                         # neue Kette passt zum neuen Schlüssel
    assert mc.client_ssl_context(s) is not None


@pytest.mark.parametrize("code", sorted(mc.ERRORS))
async def test_renewal_documented_errors(tmp_path, fake, code):
    s, cert, key = _prepare_renewal(tmp_path, fake, days=30)
    before = cert.read_bytes()
    fake.renew_mode = code
    out: list[str] = []
    assert await medidata_cli.renew(s, out=out) == 1
    assert mc.ERRORS[code].split(".")[0] in out[-1] and f"CLIENT-CERT-RENEWAL:{code}" in out[-1]
    assert cert.read_bytes() == before and not list(cert.parent.glob("*.bak-*"))      # nichts überschrieben


async def test_renewal_unknown_error_and_refusals(tmp_path, fake):
    s, cert, key = _prepare_renewal(tmp_path, fake, days=30)
    fake.renew_mode = "SOMETHING-NEW"
    out: list[str] = []
    assert await medidata_cli.renew(s, out=out) == 1 and "CLIENT-CERT-RENEWAL:SOMETHING-NEW" in out[-1]
    fake.renew_mode = "raw400"
    out = []
    assert await medidata_cli.renew(s, out=out) == 1 and "400" in out[-1]
    # ohne applianceid kein Endpunkt
    out = []
    s2 = _settings(tmp_path, fake, cert=cert, key=key, medidata_appliance_id="")
    assert await medidata_cli.renew(s2, out=out) == 1 and "APPLIANCE_ID" in out[-1]
    # nur https
    with pytest.raises(mc.CertError):
        mc.endpoint(_settings(tmp_path, medidata_service_url="http://services.medidata.ch"))
    assert mc.endpoint(_settings(tmp_path, medidata_service_url="https://services.medidata.ch")) == \
        "https://services.medidata.ch/md/srv/id3/client/certificates"
    # abgelaufen: nur noch Kundenportal
    c3, k3 = fake.pki.client("alt", days=-1)
    out = []
    assert await medidata_cli.renew(_settings(tmp_path, fake, cert=c3, key=k3), out=out) == 1 and "abgelaufen" in out[-1]


# =========================================================================== 5 Ablauf überwachen
async def test_expiry_warning_everywhere(tmp_path, fake):
    cert, key = fake.pki.client(days=45)
    s = _settings(tmp_path, fake, cert=cert, key=key)
    st = mc.status(s)
    assert st.state == "warn" and st.days_left in (44, 45) and "läuft in" in st.message
    checks = connections.check_medidata_cert(s)
    assert checks[0].status == connections.WARN and "medidata-renew" in checks[0].detail
    assert mc.summary(s) == {"state": "warn", "days_left": st.days_left}
    found = [i for i in health.issues({"medidata_cert": mc.summary(s)}) if i.kind.startswith("medidata")]
    assert [i.kind for i in found] == ["medidata_cert"] and found[0].push
    assert f"in {st.days_left} Tagen" in found[0].message
    # 61+ Tage: alles gut, keine Warnung
    c2, k2 = fake.pki.client("lang", days=200)
    s2 = _settings(tmp_path, fake, cert=c2, key=k2)
    assert mc.status(s2).state == "ok" and not [i for i in health.issues({"medidata_cert": mc.summary(s2)})
                                                 if i.kind.startswith("medidata")]
    # abgelaufen → Fehler
    c3, k3 = fake.pki.client("alt", days=-2)
    s3 = _settings(tmp_path, fake, cert=c3, key=k3)
    assert mc.status(s3).state == "expired"
    assert connections.check_medidata_cert(s3)[0].status == connections.FAIL
    assert [i.kind for i in health.issues({"medidata_cert": mc.summary(s3)}) if i.kind.startswith("medidata")] == ["medidata_cert_error"]
    # Schlüssel passt nicht → Fehler
    _, other = fake.pki.client("anders", days=200)
    s4 = _settings(tmp_path, fake, cert=c2, key=other)
    assert mc.status(s4).state == "error" and "passt nicht" in mc.status(s4).message
    assert connections.check_medidata_cert(s4)[0].status == connections.FAIL
    # CN ≠ X-Client-ID → Hinweis
    c5, k5 = fake.pki.client("fremdcn", days=200, cn="999_999")
    assert mc.status(_settings(tmp_path, fake, cert=c5, key=k5)).state == "warn"
    # nichts eingetragen: keine Meldung an NSystem; Unsinn von aussen wird ignoriert
    assert mc.summary(_settings(tmp_path)) is None
    for junk in (None, "x", {"state": "warn", "days_left": "30"}, {"state": "warn", "days_left": True}):
        assert not [i for i in health.issues({"medidata_cert": junk}) if i.kind.startswith("medidata")]


async def test_status_report_carries_cert_without_details(tmp_path, fake):
    from nfizyo.agent import status as box_status

    cert, key = fake.pki.client(days=30)
    app = create_app(_settings(tmp_path, fake, cert=cert, key=key, demo=True), start_workers=False)
    report = box_status.collect(app)
    assert report["medidata_cert"]["state"] == "warn" and isinstance(report["medidata_cert"]["days_left"], int)
    body = json.dumps(report)
    assert str(cert) not in body and CLIENT_ID not in body and str(key) not in body


# =========================================================================== 6 neue X-Client-ID (Box/VA → Webservice)
async def test_cloudmigration(tmp_path, fake, caplog):
    cert, key = fake.pki.client(days=300)
    s = _settings(tmp_path, fake, cert=cert, key=key)
    out: list[str] = []
    assert await medidata_cli.client_id(s, instance_id="1000026203", env_file=None, out=out) == 0
    call = fake.calls[-1]
    assert call["path"] == "/md/cloudmigration" and call["query"] == {"cloudclientinstanceid": "1000026203"}
    assert call["headers"]["Accept"] == "application/json" and call["headers"]["X-CLIENT-ID"] == CLIENT_ID
    assert call["headers"]["Authorization"].startswith("Basic ")
    text = "\n".join(out)
    assert f"Neue X-Client-ID: {NEW_CLIENT_ID}" in text and f"NFIZYO_MEDIDATA_CLIENT_ID={NEW_CLIENT_ID}" in text
    assert SECRET_PW not in text and NEW_CLIENT_ID not in caplog.text and SECRET_PW not in caplog.text
    # falsche alte ID → 403 mit Klartext
    out = []
    assert await medidata_cli.client_id(_settings(tmp_path, fake, cert=cert, key=key, medidata_client_id="9_9"),
                                        instance_id="1000026203", env_file=None, out=out) == 1
    assert "Kundenportal" in out[-1] and "Credentials" in out[-1]
    # nur Ziffern; unvollständige Einstellungen
    out = []
    assert await medidata_cli.client_id(s, instance_id="12a", env_file=None, out=out) == 1 and "Ziffern" in out[-1]
    with pytest.raises(medidata.MigrationError, match="NFIZYO_MEDIDATA_URL"):
        await medidata.new_client_id(_settings(tmp_path, medidata_url=""), "1")


def test_migration_response_parsing(monkeypatch, tmp_path):
    """Antwort ohne gültige newXClientId und Statuscodes laut Dokument (400, 403, 500)."""
    import aiohttp

    replies = []

    class Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body
            self.content = self

        async def read(self, n=-1):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **kw):
            return Resp(*replies.pop(0))

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    s = _settings(tmp_path, medidata_url="http://box:8100")
    for status, body, expect in ((200, b'{"newXClientId": "1_2"}', "1_2"), (200, b"{}", "keine gültige"),
                                 (200, b'{"newXClientId": "a b"}', "keine gültige"), (200, b"<html>", "kein JSON"),
                                 (400, b"", "cloudclientinstanceid"), (403, b"", "Credentials"), (500, b"", "Interner Fehler"),
                                 (502, b"", "HTTP 502")):
        replies.append((status, body))
        if status == 200 and expect == "1_2":
            assert asyncio.run(medidata.new_client_id(s, "1000026203")) == "1_2"
        else:
            with pytest.raises(medidata.MigrationError, match=expect):
                asyncio.run(medidata.new_client_id(s, "1000026203"))


# =========================================================================== 7 Simulatoren
def test_simulator_comment_right_after_prolog(app):
    db = app[DB]
    with practice_scope(1, 1):
        for target, flag in [("antwort", f) for f in simulator.FLAGS] + [("versicherung", "")]:
            xml, guid, data = simulator.build(db, target, flag)
            assert xml.startswith(b"<?xml ")                                     # nie etwas vor dem Prolog
            prolog_end = xml.index(b"?>") + 2
            comment = simulator.comment_for(target, flag)
            if comment:
                assert xml[prolog_end:].lstrip().startswith(comment.encode())  # direkt nach dem Prolog
                assert xml.count(b"<!--") == 1
                prev = etree.fromstring(xml).getprevious()                     # Kommentar vor dem Wurzelelement
                assert isinstance(prev, etree._Comment) and prev.text == comment[4:-3]
            else:
                assert b"<!--" not in xml                                        # ohne Steuerung → «Accepted»
            xml500.validate(xml)
            assert xml500.modus_of(xml) == "test"
            root = etree.fromstring(xml)
            ns = {"i": xml500.NS}
            assert root.find("i:processing/i:transport", ns).get("to") == simulator.TARGETS[target].gln
            law = root.find("i:payload/i:body/i:law", ns).get("type")
            assert law in simulator.TARGETS[target].laws
            assert data.patient.first_name == "Test" and data.patient_ssn == simulator.TEST_AHV and data.number.startswith("SIM-")
    assert simulator.comment_for("antwort", "InvalidSchema") == "<!-- invoiceresponsegenerator:InvalidSchema -->"
    assert simulator.comment_for("versicherung", "InvalidSchema") == "<!-- md-dsdt-simulator:success -->"
    with pytest.raises(simulator.SimulatorRefused):
        simulator.comment_for("antwort", "Evil --><x/><!--")
    with pytest.raises(ValueError):
        simulator.insert_after_prolog(b"<?xml version='1.0'?><a/>", "<!-- x --><evil/>")
    with pytest.raises(ValueError):
        simulator.insert_after_prolog(b"<a/>", "<!-- md-dsdt-simulator:success -->")
    assert simulator.insert_after_prolog(b"<?xml version='1.0'?>\n<a/>", "<!-- md-dsdt-simulator:success -->") == \
        b"<?xml version='1.0'?>\n<!-- md-dsdt-simulator:success -->\n<a/>"
    # erfundene AHV-Nummer mit gültiger Prüfziffer, keine echte Patientin
    digits = simulator.TEST_AHV
    assert len(digits) == 13 and digits.startswith("756")


async def test_simulator_refused_in_production(tmp_path, fake, aiohttp_client, monkeypatch):
    cert, key = fake.pki.client(days=300)
    for mode in ("production", "off"):
        s = _settings(tmp_path / mode, fake, cert=cert, key=key, mode=mode, demo=True)
        app = create_app(s, start_workers=False)
        db = app[DB]
        with practice_scope(1, 1):
            with pytest.raises(simulator.SimulatorRefused) as e:
                await simulator.send(s, db, "antwort", "")
            assert e.value.key == "not_test"
        client = await aiohttp_client(app)
        csrf = await login(client, "leitung@demo.n-fizyo.ch")
        html = await (await client.get("/leitung/einstellungen/medidata")).text()
        assert "Nur im Testmodus möglich" in html and 'value="simulator"' not in html
        r = await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "simulator", "target": "antwort", "flag": ""})
        assert "nur im Testmodus erlaubt" in await r.text()
        assert not fake.uploads
        assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = ?", (simulator.AUDIT_ACTION,)) == 0
    # selbst wenn der Modus-Schalter umgangen würde: modus="production" im XML wird nie an den Simulator gesendet
    s = _settings(tmp_path / "t2", fake, cert=cert, key=key, demo=True)
    app = create_app(s, start_workers=False)
    real_build = xml500.build
    monkeypatch.setattr(xml500, "build", lambda d, **kw: real_build(d, **{**kw, "modus": "production"}))
    with practice_scope(1, 1):
        with pytest.raises(simulator.SimulatorRefused):
            await simulator.send(s, app[DB], "antwort", "")
    assert not fake.uploads


async def test_simulator_send_from_leitung(tmp_path, fake, aiohttp_client):
    cert, key = fake.pki.client(days=300)
    s = _settings(tmp_path, fake, cert=cert, key=key, demo=True)
    app = create_app(s, start_workers=False)
    client = await aiohttp_client(app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/zahlung")).text()
    assert "/leitung/einstellungen/medidata" in html
    html = await (await client.get("/leitung/einstellungen/medidata")).text()
    assert "2099988876514" in html and "2099988880030" in html and "XML 4.3 bis 4.5" in html and "XML 5.0" in html
    assert "Gültig bis" in html and str(cert) not in html                           # Zertifikat: Datum, keine Pfade
    for flag in ("ClinicalDatasetMissing", "InvalidSchema", "MultipleDelivery", "PriorNotice",
                 "WrongAmountOfMoneyForNonValuedAdditionalCharges", "UnknownInvoiceId"):
        assert f'value="{flag}"' in html
    r = await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "simulator", "target": "antwort",
                                                                   "flag": "InvalidSchema"})
    html = await r.text()
    assert "an den Simulator gesendet" in html
    up = fake.uploads[-1]
    assert f'to="{simulator.ANSWER_GLN}"'.encode() in up["xml"] and up["info"]["modus"] == "test" and up["cn"] == CLIENT_ID
    assert up["info"]["toPatient"] is False and up["info"]["correlationReference"].startswith("SIM-")
    assert up["xml"].split(b"\n")[1] == b"<!-- invoiceresponsegenerator:InvalidSchema -->"
    assert "SIM-" in html and "InvalidSchema" in html and up["ref"] in html                # Liste der Testsendungen
    # Versicherungssimulator: immer «success», gewählte Steuerung wird ignoriert
    await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "simulator", "target": "versicherung",
                                                               "flag": "InvalidSchema"})
    up = fake.uploads[-1]
    assert f'to="{simulator.INSURANCE_GLN}"'.encode() in up["xml"]
    assert up["xml"].split(b"\n")[1] == b"<!-- md-dsdt-simulator:success -->" and b"invoiceresponsegenerator" not in up["xml"]
    # Unsinn wird abgelehnt
    n = len(fake.uploads)
    await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "simulator", "target": "x", "flag": ""})
    await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "simulator", "target": "antwort", "flag": "Evil"})
    assert len(fake.uploads) == n
    # keine Patientendaten in der Testrechnung
    db = app[DB]
    names = [r["last_name"] for r in db.query("SELECT last_name FROM patients")]
    assert names and not any(n_.encode() in up["xml"] for n_ in names if len(n_) > 3)
    # Empfang kommt nicht auf die Seite
    await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.get("/leitung/einstellungen/medidata", allow_redirects=False)
    assert r.status in (302, 403, 404)


async def test_simulator_missing_master_data_and_no_answer(tmp_path, fake, aiohttp_client):
    """Fehlende Stammdaten → Liste statt Senden. Bleibt die Antwort aus (XML 5.0), wartet N-Fizyo nicht."""
    cert, key = fake.pki.client(days=300)
    s = _settings(tmp_path, fake, cert=cert, key=key, demo=True)
    app = create_app(s, start_workers=False)
    db = app[DB]
    orig = db.value("SELECT gln FROM practices WHERE id = 1")
    db.execute("UPDATE practices SET gln = NULL WHERE id = 1")
    client = await aiohttp_client(app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "simulator", "target": "antwort", "flag": ""})
    html = await r.text()
    assert "Für die Testrechnung fehlt noch" in html and "GLN der Praxis" in html and not fake.uploads
    # Simulator nimmt an, antwortet aber nie: die Sendung gilt als gesendet, nichts hängt
    db.execute("UPDATE practices SET gln = ? WHERE id = 1", (orig,))
    r = await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "simulator", "target": "antwort", "flag": ""})
    assert "an den Simulator gesendet" in await r.text() and len(fake.uploads) == 1
    with practice_scope(1, 1):
        rec = simulator.recent(db)
    assert rec[0]["status"] == "sent" and rec[0]["flag"] == "" and rec[0]["target"] == "antwort"


def test_texts_de_fr_complete():
    de = json.loads(Path("nfizyo/modules/billing/locales/de.json").read_text())["billing"]["md"]
    fr = json.loads(Path("nfizyo/modules/billing/locales/fr.json").read_text())["billing"]["md"]
    assert de.keys() == fr.keys()
    for f in simulator.FLAGS:
        assert f"flag_{f or 'none'}" in de
    for t in simulator.TARGETS:
        assert f"target_{t}" in de and f"target_short_{t}" in de
