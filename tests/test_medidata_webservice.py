"""MediData-Webservice nach «Leistungsabrechnung – Schnittstellendokumentation» V5.2.0 (+ Client Schnittstellen V1.5,
Best Practice Webservice): alles gegen den gefälschten MediData-Server aus tests/medidata_fake.py (HTTPS mit gegenseitigem
TLS, Antworten wie in den Beispielen der Dokumentation).

Versand (Anfrage wie 3.7.4, Erfolg, jede Fehlerart, unklar, 409-Duplikat), Teilnehmerverzeichnis (Seiten, Filter, Annahme-
Prüfung vor dem Senden, Anzeige), Status (bis zum Endstand, unklare Sendungen klären), Dokumente abholen (erst speichern,
dann bestätigen, nichts doppelt), Benachrichtigungen (speichern, zuordnen, bestätigen), Abfrage-Takt, nie automatisch
nochmals senden, Verbindungstest nur lesend."""
from __future__ import annotations

import base64
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import login, pid_of
from medidata_fake import CLIENT_ID, PKI, FakeMediData, participant
from test_responses import resp_xml

from nfizyo import connections
from nfizyo.app import create_app
from nfizyo.core import patients as core_patients
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB, SETTINGS
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.billing import followup, medidata, medidata_sync
from nfizyo.modules.billing import repo as billing

SUVA = "2099990001102"         # Versicherer der Demo-Patientin Lea (UVG, Tiers payant)
HELSANA = "2099990001058"      # KVG, Tiers garant
RECEIVER = "2099990009991"     # abweichende Empfänger-GLN (glnReceiver, 3.6.2)
PW = "md-geheim-" + "y" * 16
XML = b'<?xml version="1.0" encoding="UTF-8"?>\n<invoice:request xmlns:invoice="http://www.forum-datenaustausch.ch/invoice"/>\n'


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


def settings_for(tmp_path: Path, fake, *, mode: str = "test", path: str = "/md/srv/id0", **kw):
    cert, key = fake.pki.client(days=300)
    values = dict(data_dir=tmp_path / "data", secret_key=b"t" * 32, demo=True, medidata_mode=mode, medidata_url=fake.url + path,
                  medidata_client_id=CLIENT_ID, medidata_user="praxis", medidata_password=PW, medidata_cert=str(cert),
                  medidata_key=str(key), medidata_ca=str(fake.pki.ca_path))
    values.update(kw)
    return load_settings(None, **values)


@pytest.fixture
def md_app(tmp_path, fake):
    return create_app(settings_for(tmp_path, fake), start_workers=False)


def _final(app, first_name: str = "Lea") -> dict:
    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        billing.set_tp_value(db, "kvg312", 1.0, "2026-01-01")
        patient = core_patients.get(db, pid_of(app, first_name))
        iid = billing.create_from_appointments(db, patient, "de")
        billing.finalize(db, billing.get(db, iid))
        return dict(billing.get(db, iid))


def _uploads(fake) -> list[dict]:
    return [c for c in fake.calls if c["method"] == "POST" and c["path"].endswith("/ela/uploads")]


def _gets(fake, suffix: str) -> list[dict]:
    return [c for c in fake.calls if c["method"] == "GET" and c["path"].endswith(suffix)]


async def _sync(app, *kinds):
    return await medidata_sync.run(app, kinds=list(kinds))


def _last_tr(db, iid):
    return db.one("SELECT * FROM invoice_transmissions WHERE invoice_id = ? ORDER BY id DESC LIMIT 1", (iid,))


# =========================================================================== 1 Versand (3.7)
async def test_upload_request_matches_spec(tmp_path, fake):
    s = settings_for(tmp_path, fake)
    md = medidata.MediDataClient(s)
    info = md.info(modus="test", document_reference="doc-1", correlation_reference="2026-0001")
    r = await md.upload(XML, "2026-0001_invoice.xml", info)
    up = fake.uploads[-1]
    assert r.ok and r.status == "sent" and r.http_status == 201 and r.reference == up["ref"] and not r.duplicate
    call = _uploads(fake)[-1]
    assert call["path"] == "/md/srv/id0/ela/uploads"                                  # Webservice …/md/srv/id<N>/ela/uploads
    assert call["client_id"] == CLIENT_ID and call["cn"] == CLIENT_ID                  # X-CLIENT-ID + Client-Zertifikat (CN)
    assert call["auth"] == "Basic " + base64.b64encode(f"praxis:{PW}".encode()).decode()
    assert call["expect"] == "100-continue"                                           # Anhang BestPractice «Common Errors»
    assert up["order"] == ["elauploadinfo", "elauploadstream"]                        # Beispiel 3.7.4
    assert up["parts"]["elauploadinfo"] == ("application/json", "info.json")
    assert up["parts"]["elauploadstream"] == ("application/octet-stream", "2026-0001_invoice.xml")
    assert up["info"] == {"modus": "test", "toPatient": False, "documentReference": "doc-1", "correlationReference": "2026-0001"}
    assert up["xml"] == XML
    assert medidata.client_id_ok(s) and medidata.MIME_REQUEST == "application/x-fd-geninv-req-v50+xml"
    # Box/VA (Client Schnittstellen 3.1.1): gleiche eLA-Pfade unter …/md
    box = settings_for(tmp_path / "box", fake, path="")
    r = await medidata.MediDataClient(box).upload(XML + b"<!-- box -->", "b.xml", info)
    assert r.ok and _uploads(fake)[-1]["path"] == "/md/ela/uploads"
    assert medidata.client_id_ok(box)                                              # Box-IDs werden nicht geprüft


@pytest.mark.parametrize("plan, status, code, http", [
    ("err:UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION:400", "failed", "UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION", 400),
    ("err:UPLOAD:MIMETYPE-NOT-SENDABLE-TO-RECEIVER:400", "failed", "UPLOAD:MIMETYPE-NOT-SENDABLE-TO-RECEIVER", 400),
    ("err:UPLOAD:SUBSCRIPTION-LOCKED:403", "failed", "UPLOAD:SUBSCRIPTION-LOCKED", 403),
    ("err::401", "failed", None, 401),
    ("err::413", "failed", None, 413),
    ("err::429", "failed", None, 429),
    ("408", "failed", None, 408),
    ("500", "unknown", "UPLOAD:UNKNOWN-ERROR", 500),
    ("err:UPLOAD:COMMUNICATION-ERROR:503", "unknown", "UPLOAD:COMMUNICATION-ERROR", 503),
    ("noref", "unknown", None, 201),
    ("timeout", "unknown", None, None),
    ("drop", "unknown", None, None),
])
async def test_upload_result_classes(tmp_path, fake, monkeypatch, plan, status, code, http):
    """Nur eindeutige Ablehnungen sind «failed»; Zeitüberschreitung, Abbruch, 5xx und 2xx ohne Referenz sind «unknown».
    In jedem Fall genau EIN Versuch – nie automatisch nochmals."""
    monkeypatch.setattr(medidata, "UPLOAD_TIMEOUT", 0.5)
    md = medidata.MediDataClient(settings_for(tmp_path, fake))
    fake.upload_plan = [plan]
    r = await md.upload(XML, "x.xml", md.info(modus="test", document_reference="d-" + plan))
    assert r.status == status and r.error_code == code and r.http_status == http and not r.ok
    assert len(_uploads(fake)) == 1
    if plan in ("timeout", "drop", "noref"):
        assert len(fake.uploads) == 1                    # tatsächlich angekommen – darum «unklar», nicht «Fehler»
    assert PW not in (r.error or "") and "praxis" not in (r.error or "")


async def test_upload_duplicate_409_and_no_connection(tmp_path, fake):
    md = medidata.MediDataClient(settings_for(tmp_path, fake))
    info = md.info(modus="test", document_reference="dup", correlation_reference="2026-0002")
    r1 = await md.upload(XML, "a.xml", info)
    r2 = await md.upload(XML, "a.xml", info)                          # gleiche Sendung innerhalb einer Minute → 409
    assert r2.ok and r2.duplicate and r2.http_status == 409 and r2.reference == r1.reference and r2.resolution == "duplicate"
    assert len(fake.uploads) == 1
    # keine Verbindung aufgebaut → sicher nicht gesendet → «failed»
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    closed = settings_for(tmp_path / "closed", fake, medidata_url=f"https://127.0.0.1:{port}/md/srv/id0")
    r = await medidata.MediDataClient(closed).upload(XML, "a.xml", info)
    assert r.status == "failed" and not r.unclear
    # Einordnung der Antworten (3.7.5, Client Schnittstellen 2.5)
    assert medidata.classify(302, b"").status == "failed"
    assert medidata.classify(409, b"").status == "unknown"
    assert medidata.classify(201, b'{"transmissionReference": "9b364f5c-59f9-4d27-bc6a-0ae4f92568dc"}').reference == \
        "9b364f5c-59f9-4d27-bc6a-0ae4f92568dc"


def test_error_codes_have_texts_de_fr():
    def flat(d, p=""):
        out = {}
        for k, v in d.items():
            out.update(flat(v, f"{p}{k}.") if isinstance(v, dict) else {f"{p}{k}": v})
        return out

    for lang in ("de", "fr"):
        keys = flat(json.loads(Path(f"nfizyo/modules/billing/locales/{lang}.json").read_text()))
        for code in medidata.KNOWN_CODES + ("UPLOAD:EBILL-BC_INVALID_DATE",):
            assert medidata.code_key(code) in keys, (lang, code)
    assert medidata.code_key("UPLOAD:SOMETHING-NEW") is None                 # unbekannte Codes werden roh angezeigt


# =========================================================================== 2 Teilnehmerverzeichnis (3.6)
@pytest.mark.parametrize("paging", ["link", "offset", "foreign"])
async def test_participants_paging_and_filters(tmp_path, fake, paging):
    fake.participants = [participant(f"20999900{i:05d}", f"Versicherer {i}") for i in range(120)]
    fake.paging = paging
    md = medidata.MediDataClient(settings_for(tmp_path, fake))
    ctx = md._ctx()
    async with md.session() as session:
        items = await md.participants(session, ctx=ctx, page_size=50)
        assert len({p["gln_participant"] for p in items}) == 120
        calls = _gets(fake, "/ela/participants")
        assert [c["query"].get("offset") for c in calls] == [None, "50", "100"] and {c["query"]["limit"] for c in calls} == {"50"}
        await md.participants(session, ctx=ctx, lawtype=1)                 # Filter «lawtype» (3.6.1)
        assert _gets(fake, "/ela/participants")[-1]["query"] == {"limit": "1000", "lawtype": "1"}
        for bad in ({"lawtype": 9}, {"page_size": 0}, {"page_size": 1001}, {"gln": "123"}):
            with pytest.raises(medidata.MdError):
                await md.participants(session, ctx=ctx, **bad)


async def test_directory_sync_check_and_display(md_app, fake, aiohttp_client):
    app, db = md_app, md_app[DB]
    fake.participants = [participant(SUVA, "Suva", receiver=RECEIVER, laws=(2, 3, 4)),
                         participant(HELSANA, "Helsana", not_supported=["application/x-fd-geninv-req-v45+xml"])]
    with practice_scope(1):
        assert medidata_sync.recipient_problems(db, SUVA, tiers="payant", size=10) == ([], None)   # Verzeichnis leer
    assert (await _sync(app, "participants")) == {"participants": 2}
    rows = db.query("SELECT * FROM medidata_participants")
    assert {r["practice_id"] for r in rows} == {1} and {r["gln_receiver"] for r in rows} == {RECEIVER, HELSANA}
    with practice_scope(1):
        probs, r = medidata_sync.recipient_problems(db, SUVA, tiers="payant", size=1000)
        assert probs == [] and r["receiver"] == RECEIVER and r["v50"]
        assert medidata_sync.recipient_problems(db, HELSANA, tiers="payant", size=10)[0] == []          # nur 4.5 gesperrt
        assert medidata_sync.recipient_problems(db, HELSANA, tiers="garant", size=10)[0] == ["chk_tg"]   # tgAllowed false
        assert medidata_sync.recipient_problems(db, "2099990001119", tiers="payant", size=10)[0] == ["chk_not_listed"]
    # Fehler beim Abruf: altes Verzeichnis bleibt
    fake.force["participants"] = 500
    assert (await _sync(app, "participants"))["participants"] == "error"
    assert db.value("SELECT COUNT(*) FROM medidata_participants") == 2
    fake.force.clear()

    inv = _final(app)
    client = await aiohttp_client(app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert f"Empfänger-GLN {RECEIVER}" in html and 'value="send"' in html
    # Senden: transport@to = glnReceiver aus dem Verzeichnis
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    assert f'to="{RECEIVER}"'.encode() in fake.uploads[-1]["xml"] and _last_tr(db, inv["id"])["status"] == "sent"
    # Versicherer nimmt XML 5.0 nicht an → klare Warnung, kein Senden-Knopf, Senden wird abgewiesen (nichts geht raus)
    fake.participants[0]["notSupported"] = ["application/x-fd-geninv-req-v50+xml"]
    await _sync(app, "participants")
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "Senden über MediData nicht möglich" in html and "XML 5.0" in html and 'value="send"' not in html
    n = len(_uploads(fake))
    r = await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice", "again": "1"})
    assert "nimmt laut MediData keine Rechnungen im Format XML 5.0 an" in await r.text() and len(_uploads(fake)) == n
    # nicht im Verzeichnis → ebenso
    fake.participants = fake.participants[1:]
    await _sync(app, "participants")
    r = await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice", "again": "1"})
    assert "steht nicht im MediData-Teilnehmerverzeichnis" in await r.text() and len(_uploads(fake)) == n
    # Einstellungen › Versicherer: Erreichbarkeit je Versicherer, Suche im Verzeichnis
    html = await (await client.get("/leitung/einstellungen/versicherer?md=Helsana")).text()
    assert "nicht bei MediData" in html and "MediData-Teilnehmerverzeichnis" in html and "Als Versicherer übernehmen" in html
    assert "XML 5.0: Ja · XML 4.5: Nein" in html


# =========================================================================== 3 Status (3.8)
async def test_status_tracked_until_final(md_app, fake, aiohttp_client):
    app, db = md_app, md_app[DB]
    inv = _final(app)
    client = await aiohttp_client(app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    t, rec = _last_tr(db, inv["id"]), fake.uploads[-1]
    assert t["reference"] == rec["ref"] and t["document_reference"] == rec["info"]["documentReference"] == t["guid"]
    await _sync(app, "status")
    t = _last_tr(db, inv["id"])
    assert t["md_status"] == "PROCESSING" and "MAIN_RECEIVER" in t["md_detail"]                # Teilauslieferungen (3.8.4)
    assert _gets(fake, "/status/detail")
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "MediData: in Bearbeitung" in html and "Versicherung (in Bearbeitung)" in html
    rec["status"] = "DONE"
    await _sync(app, "status")
    assert _last_tr(db, inv["id"])["md_status"] == "DONE"
    n = len(_gets(fake, "/status"))
    await _sync(app, "status")                                     # Endstand erreicht → nicht mehr abfragen
    assert len(_gets(fake, "/status")) == n
    html = await (await client.get("/leitung/abrechnung?ansicht=abgeschlossen")).text()
    assert "MediData: zugestellt" in html
    # ERROR mit Grund und unbekannte Referenz (404)
    with practice_scope(1, 1):
        inv_row = billing.get(db, inv["id"])
        err = billing.add_transmission(db, inv_row, kind="invoice", guid="g-err", modus="test", channel="medidata", status="sent",
                                       xml=XML, reference=rec["ref"])
        db.execute("UPDATE invoice_transmissions SET md_status = NULL WHERE id = ?", (err,))
        gone = billing.add_transmission(db, inv_row, kind="invoice", guid="g-404", modus="test", channel="medidata", status="sent",
                                        xml=XML, reference="00000000-0000-0000-0000-000000000000")
    rec["status"], rec["errorReason"] = "ERROR", "UPLOAD:DOCUMENT-NOT-ACCEPTED-BY-RECEIVER"
    await _sync(app, "status")
    assert db.value("SELECT md_status FROM invoice_transmissions WHERE id = ?", (err,)) == "ERROR"
    assert db.value("SELECT md_status FROM invoice_transmissions WHERE id = ?", (gone,)) == "NOT_FOUND"
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "MediData: Fehler" in html and "Der Versicherer nimmt dieses Dokument nicht an." in html


async def test_unclear_upload_never_resent_and_resolved_automatically(md_app, fake, aiohttp_client, monkeypatch):
    app, db = md_app, md_app[DB]
    monkeypatch.setattr(medidata, "UPLOAD_TIMEOUT", 0.5)
    inv = _final(app)
    client = await aiohttp_client(app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    fake.upload_plan = ["timeout"]
    r = await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    assert "Stand unklar" in await r.text()
    t = _last_tr(db, inv["id"])
    assert t["status"] == "unknown" and t["reference"] is None and len(_uploads(fake)) == 1 and len(fake.uploads) == 1
    # alle Abfragen laufen – nichts beweist etwas: bleibt unklar, und es wird NIE nochmals gesendet
    await medidata_sync.run(app)
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice", "again": "1"})
    assert _last_tr(db, inv["id"])["status"] == "unknown" and len(_uploads(fake)) == 1
    # Benachrichtigung mit documentReference/transmissionReference → automatisch geklärt (angekommen), Referenz übernommen
    rec = fake.uploads[-1]
    fake.add_note(501, severity="WARNING", code="UPLOAD:POSTAL-ADDRESS-INCOMPLETE", tref=rec["ref"],
                  dref=rec["info"]["documentReference"])
    await _sync(app, "notifications")
    t = _last_tr(db, inv["id"])
    assert t["status"] == "sent" and t["resolution"] == "notification" and t["reference"] == rec["ref"]
    assert len(_uploads(fake)) == 1
    # Statusabfrage beweist es ebenso (Sendung mit Referenz, z. B. aus einer Benachrichtigung)
    db.execute("UPDATE invoice_transmissions SET status = 'unknown', resolution = NULL WHERE id = ?", (t["id"],))
    await _sync(app, "status")
    t = _last_tr(db, inv["id"])
    assert t["status"] == "sent" and t["resolution"] == "status" and t["md_status"] == "PROCESSING"


async def test_unclear_upload_resolved_by_insurer_response(md_app, fake, aiohttp_client):
    app, db = md_app, md_app[DB]
    inv = _final(app)
    client = await aiohttp_client(app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    fake.upload_plan = ["500"]
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    assert _last_tr(db, inv["id"])["status"] == "unknown"
    fake.add_doc(resp_xml(inv["number"], kind="accepted"), correlation=inv["number"])
    await _sync(app, "downloads")
    t = _last_tr(db, inv["id"])
    assert t["status"] == "sent" and t["resolution"] == "response" and len(_uploads(fake)) == 1


# =========================================================================== 4 Dokumente abholen (3.9)
async def test_downloads_stored_before_confirm_and_never_twice(md_app, fake, aiohttp_client, monkeypatch):
    app, db = md_app, md_app[DB]
    inv = _final(app)
    answer = resp_xml(inv["number"], errors=[("31148", "Falsche Ziffer", 1, "25.111", "25.110")])
    ref1 = fake.add_doc(answer, correlation=inv["number"])
    ref2 = fake.add_doc(b'<?xml version="1.0"?><x:other xmlns:x="urn:x"/>', doc_type="application/x-fd-gennot-v50+xml")
    # Speichern schlägt fehl → nichts bestätigt, nichts gespeichert (bleibt bei MediData)
    real = medidata_sync.store_download

    def boom(*a, **kw):
        raise RuntimeError("Platte voll")

    monkeypatch.setattr(medidata_sync, "store_download", boom)
    assert (await _sync(app, "downloads"))["downloads"] == "error"
    assert fake.confirm_calls == [] and db.value("SELECT COUNT(*) FROM medidata_downloads") == 0
    assert db.value("SELECT COUNT(*) FROM invoice_responses") == 0
    monkeypatch.setattr(medidata_sync, "store_download", real)
    # Bestätigung schlägt fehl (5xx) → gespeichert, aber unbestätigt; MediData liefert es wieder
    fake.force["confirm"] = 500
    assert (await _sync(app, "downloads"))["downloads"] == "error"
    assert db.value("SELECT COUNT(*) FROM invoice_responses") == 1 and fake.docs[ref1]["confirmed"] == 0
    fetched = fake.docs[ref1]["fetched"]
    fake.force.clear()
    assert (await _sync(app, "downloads"))["downloads"] == 1                     # nur das zweite neu gespeichert
    assert fake.docs[ref1]["fetched"] == fetched                                  # gespeichertes nicht nochmals geholt
    assert fake.docs[ref1]["confirmed"] == 1 and fake.docs[ref2]["confirmed"] == 1
    assert db.value("SELECT COUNT(*) FROM invoice_responses") == 1               # kein doppelter Import
    rows = {r["transmission_reference"]: r for r in db.query("SELECT * FROM medidata_downloads WHERE practice_id = 1")}
    assert rows[ref1]["state"] == "imported" and rows[ref1]["invoice_id"] == inv["id"] and rows[ref1]["confirmed_at"]
    assert rows[ref2]["state"] == "stored" and bytes(rows[ref2]["content"]).startswith(b"<?xml") and rows[ref2]["confirmed_at"]
    # alle Downloads mit Accept: application/octet-stream (3.9.3); Bestätigung genau {"status": "CONFIRMED"} (Fake prüft)
    assert {c["accept"] for c in fake.calls if c["method"] == "GET" and "/ela/downloads/" in c["path"]} == {"application/octet-stream"}
    assert (await _sync(app, "downloads"))["downloads"] == 0                     # Liste leer
    # gleicher Inhalt unter neuer Referenz (z. B. schon von Hand hochgeladen) → kein zweiter Import
    ref3 = fake.add_doc(answer, correlation=inv["number"])
    await _sync(app, "downloads")
    assert db.value("SELECT COUNT(*) FROM invoice_responses") == 1 and fake.docs[ref3]["confirmed"] == 1
    assert db.value("SELECT state FROM medidata_downloads WHERE transmission_reference = ?", (ref3,)) == "imported"
    # erscheint wie eine von Hand hochgeladene Antwort
    with practice_scope(1):
        resp = followup.responses_for(db, inv["id"])
        assert [r["source"] for r in resp] == ["medidata"] and resp[0]["kind"] == "rejected"
        assert followup.count_open_rejections(db) == 1
    client = await aiohttp_client(app)
    await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "Falsche Ziffer" in html
    html = await (await client.get("/leitung/abrechnung?ansicht=antworten")).text()
    assert inv["number"] in html and "Falsche Ziffer" in html
    # nicht übernommenes Dokument: Leitung sieht es und kann das Original laden
    html = await (await client.get("/leitung/einstellungen/medidata")).text()
    assert "Nicht übernommene Dokumente" in html and "application/x-fd-gennot-v50+xml" in html
    r = await client.get(f"/leitung/einstellungen/medidata/dokument/{rows[ref2]['id']}")
    assert r.status == 200 and (await r.read()).startswith(b"<?xml")


# =========================================================================== 5 Benachrichtigungen (3.10)
async def test_notifications_stored_linked_then_confirmed(md_app, fake, aiohttp_client, monkeypatch):
    app, db = md_app, md_app[DB]
    inv = _final(app)
    client = await aiohttp_client(app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    t, rec = _last_tr(db, inv["id"]), fake.uploads[-1]
    fake.add_note(2001038377, tref=rec["ref"], dref=t["document_reference"], cref=inv["number"],
                  code="UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION")
    fake.add_note(2001038378, severity="INFO", code="")
    # Speichern schlägt fehl → keine Bestätigung
    real = medidata_sync.store_notification

    def boom(*a, **kw):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(medidata_sync, "store_notification", boom)
    await _sync(app, "notifications")
    assert all(n["confirmed"] == 0 for n in fake.notes.values()) and db.value("SELECT COUNT(*) FROM medidata_notifications") == 0
    monkeypatch.setattr(medidata_sync, "store_notification", real)
    assert (await _sync(app, "notifications"))["notifications"] == 2
    assert all(n["confirmed"] == 1 for n in fake.notes.values())                 # PUT {"notificationFetched": true}
    assert "from" not in _gets(fake, "/ela/notifications")[-1]["query"]           # erste erfolgreiche Abfrage ohne «from»
    note = db.one("SELECT * FROM medidata_notifications WHERE notification_id = 2001038377")
    assert note["practice_id"] == 1 and note["invoice_id"] == inv["id"] and note["transmission_id"] == t["id"]
    assert json.loads(note["subject"])["fr"] == "La transmission a échoué"
    t = _last_tr(db, inv["id"])
    assert t["md_status"] == "ERROR" and t["md_error_reason"] == "UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION"
    # nächste Abfrage mit «from» (2 Tage Überlappung), nichts doppelt
    fake.add_note(2001038377, tref=rec["ref"], code="UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION")    # nochmals geliefert
    assert (await _sync(app, "notifications"))["notifications"] == 0
    since = _gets(fake, "/ela/notifications")[-1]["query"]["from"]
    assert since == (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    assert db.value("SELECT COUNT(*) FROM medidata_notifications") == 2
    # Anzeige: Rechnung (Abrechnung), Leitung › MediData, Arbeitsliste; «Gesehen»
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "Übermittlung fehlgeschlagen" in html and "Empfänger angeben" in html
    with practice_scope(1):
        assert medidata_sync.open_count(db) == 1                                    # INFO zählt nicht
    html = await (await client.get("/leitung/einstellungen/medidata")).text()
    assert "Benachrichtigungen von MediData" in html and "UPLOAD:UNKNOWN-RECEIVER-ORGANIZATION" in html
    html = await (await client.get("/leitung/uebersicht")).text()
    assert "MediData-Meldungen" in html
    await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "seen_note", "id": str(note["id"])})
    with practice_scope(1):
        assert medidata_sync.open_count(db) == 0


# =========================================================================== 6 Abfrage-Takt (Anhang BestPractice «Polling»)
async def test_polling_schedule_and_round_rules(md_app, fake, aiohttp_client, app, tmp_path):
    assert medidata_sync.INTERVALS == {"participants": 24 * 3600, "status": 1800, "downloads": 1800, "notifications": 1800}
    db = md_app[DB]
    now = datetime.now(timezone.utc)
    with practice_scope(1):
        assert medidata_sync.due(db, now) == list(medidata_sync.KINDS)
    # 401/403/429/5xx oder keine Verbindung: Runde sofort beenden (kein Hämmern), Rest erst beim nächsten Intervall
    fake.force["participants"] = 403
    result = await medidata_sync.run(md_app)
    assert result == {"participants": "error", "notifications": "skipped", "status": "skipped", "downloads": "skipped"}
    assert not [c for c in fake.calls if "/ela/" in c["path"] and "participants" not in c["path"]]
    assert (await medidata_sync.run(md_app)) == {"skipped": "not_due"}
    fake.force.clear()
    with practice_scope(1):
        st = medidata_sync.state(db)
        assert "403" in st["participants"]["last_error"] and PW not in st["participants"]["last_error"]
        assert medidata_sync.due(db, now + timedelta(minutes=10)) == []
        assert medidata_sync.due(db, now + timedelta(minutes=31)) == ["notifications", "status", "downloads"]
        assert medidata_sync.due(db, now + timedelta(hours=24, minutes=1)) == list(medidata_sync.KINDS)
        assert medidata_sync.due(db, now + timedelta(minutes=3), manual=True) == ["notifications", "status", "downloads"]
        assert medidata_sync.due(db, now + timedelta(minutes=61), manual=True) == list(medidata_sync.KINDS)
    # «Jetzt abrufen» in der Leitung
    client = await aiohttp_client(md_app)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    db.execute("DELETE FROM medidata_sync")
    r = await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "sync"})
    assert "Bei MediData abgefragt." in await r.text()
    r = await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": "sync"})
    assert "Gerade erst abgefragt" in await r.text()
    assert not _uploads(fake)                                                       # Abfragen senden nie etwas
    # Hintergrund: nur wenn eingerichtet; Verschiebung je Box 60–360 s
    assert 60 <= medidata_sync.first_delay(md_app[SETTINGS]) <= 360
    assert (await medidata_sync.run(app)) == {"skipped": "not_configured"}           # Demo-App ohne MediData
    await medidata_sync.start(app)
    assert app.get(medidata_sync.TASK) is None
    fresh = create_app(settings_for(tmp_path / "fresh", fake), start_workers=False)        # wie beim Start der Box
    await medidata_sync.start(fresh)
    task = fresh.get(medidata_sync.TASK)
    assert task is not None and not task.done()
    await medidata_sync.stop(fresh)


# =========================================================================== 7 Verbindungstest (nur lesen)
async def test_check_connections_reads_only_and_hides_secrets(tmp_path, fake):
    s = settings_for(tmp_path, fake)
    app = create_app(s, start_workers=False)
    checks = await connections.check_medidata(s, app[DB])
    by = {c.area: c for c in checks}
    assert by["MediData"].status == connections.OK and "Teilnehmerverzeichnis lesbar" in by["MediData"].detail
    assert by["MediData Zertifikat"].status == connections.OK and "MediData Abfragen" in by
    ela = [c for c in fake.calls if "/ela/" in c["path"]]
    assert [(c["method"], c["path"], c["query"]) for c in ela] == [("GET", "/md/srv/id0/ela/participants", {"limit": "1"})]
    text = " ".join(f"{c.area} {c.detail}" for c in checks)
    for secret in (PW, s.medidata_key, s.medidata_cert, base64.b64encode(f"praxis:{PW}".encode()).decode()):
        assert secret not in text
    # falsches X-CLIENT-ID-Format (Webservice: 10_10 Ziffern) → Hinweis; MediData lehnt ab → Fehler mit Hinweis
    s2 = settings_for(tmp_path / "id", fake, medidata_client_id="2000")
    checks = await connections.check_medidata(s2)
    by = {c.area: c for c in checks}
    assert by["MediData X-CLIENT-ID"].status == connections.WARN
    assert by["MediData"].status == connections.FAIL and "403" in by["MediData"].detail and "X-CLIENT-ID" in by["MediData"].detail
    assert not _uploads(fake)
