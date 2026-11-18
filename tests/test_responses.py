"""Antworten der Versicherer (General Invoice Response 5.0): einlesen, zuordnen, Rückweisung korrigieren; Kostengutsprachen."""
from __future__ import annotations

import io
import zipfile

import pytest
from conftest import login, pid_of

from nfizyo.core import patients as core_patients
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.billing import followup, response
from nfizyo.modules.billing import repo as billing

NS = "http://www.forum-datenaustausch.ch/invoice"


def resp_xml(number: str, *, kind: str = "rejected", tiers: str = "payant", errors=(), explanation: str = "Bitte prüfen",
             balance: dict | None = None, modus: str = "test") -> bytes:
    """Antwort nach generalInvoiceResponse 5.0 (fiktive GLN 2099…)."""
    inner = f"<invoice:explanation>{explanation}</invoice:explanation>" if explanation else ""
    for code, text, rec, bad, good in errors:
        attrs = f' code="{code}" text="{text}"' + (f' record_id="{rec}"' if rec else "") + (f' error_value="{bad}"' if bad else "") \
            + (f' valid_value="{good}"' if good else "")
        inner += f"<invoice:{'error' if kind == 'rejected' else 'message'}{attrs}/>"
    if kind == "accepted":
        b = balance or {"amount": 100.0, "amount_due": 100.0}
        inner += "<invoice:balance " + " ".join(f'{k}="{v}"' for k, v in b.items()) + "/>"
    if tiers == "garant":
        result = f'<invoice:tiers_garant><invoice:notification status_in="received" status_out="processed">{inner}</invoice:notification></invoice:tiers_garant>'
    else:
        result = (f'<invoice:tiers_{tiers}{" allowModification=\"false\"" if tiers == "payant" else ""}>'
                  f'<invoice:{kind} status_in="unknown" status_out="canceled">{inner}</invoice:{kind}></invoice:tiers_{tiers}>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<invoice:response xmlns:invoice="{NS}" language="de" modus="{modus}" guid="0123456789abcdef0123456789abcdef">
  <invoice:processing><invoice:transport from="2099990001009" to="2099990000011"><invoice:via via="2099990009999" sequence_id="1"/></invoice:transport></invoice:processing>
  <invoice:payload response_timestamp="1761600000">
    <invoice:invoice request_timestamp="1761500000" request_date="2026-10-20T10:00:00" request_id="{number}"/>
    <invoice:body>
      <invoice:prolog><invoice:generator name="Versicherer Test"/></invoice:prolog>
      <invoice:insurance gln="2099990001009"><invoice:company><invoice:companyname>Testversicherung</invoice:companyname>
        <invoice:postal><invoice:zip>3000</invoice:zip><invoice:city>Bern</invoice:city></invoice:postal></invoice:company></invoice:insurance>
      <invoice:patient gender="female" sex="female" birthdate="1992-03-05" ssn="7569217076985"><invoice:person>
        <invoice:familyname>Muster</invoice:familyname><invoice:givenname>Lea</invoice:givenname>
        <invoice:postal><invoice:zip>2500</invoice:zip><invoice:city>Biel</invoice:city></invoice:postal></invoice:person></invoice:patient>
      {result}
    </invoice:body>
  </invoice:payload>
</invoice:response>""".encode()


def _final(app, first_name: str = "Lea") -> dict:
    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        patient = core_patients.get(db, pid_of(app, first_name))
        iid = billing.create_from_appointments(db, patient, "de")
        billing.finalize(db, billing.get(db, iid))
        return dict(billing.get(db, iid))


def test_parse_kinds():
    r = response.parse(resp_xml("2026-0007", errors=[("31148", "Falsche Ziffer", 1, "25.111", "25.110"), ("31160", "Nicht versichert", None, None, None)]))
    assert (r.request_id, r.tiers, r.kind, r.status_out, r.modus) == ("2026-0007", "payant", "rejected", "canceled", "test")
    assert r.insurer_name == "Testversicherung" and r.patient_name == "Lea Muster" and r.sender_gln == "2099990001009"
    assert r.notices[0].record_id == 1 and r.notices[0].valid_value == "25.110" and r.notices[1].record_id is None
    r = response.parse(resp_xml("X", tiers="garant", errors=[("1", "Kopie erhalten", None, None, None)]))
    assert r.kind == "notification" and r.tiers == "garant" and r.notices[0].text == "Kopie erhalten"
    r = response.parse(resp_xml("X", kind="accepted", balance={"amount": 120.5, "amount_due": 110.0, "amount_paid": 0}))
    assert r.kind == "accepted" and r.balance["amount_due"] == 110.0
    r = response.parse(resp_xml("X", kind="pending", tiers="soldant"))
    assert r.kind == "pending" and r.tiers == "soldant"
    for bad, code in ((b"kein xml", "not_xml"), (b"<a/>", "not_response")):
        with pytest.raises(response.ResponseError) as exc:
            response.parse(bad)
        assert str(exc.value) == code


def test_entities_are_not_resolved(tmp_path):
    secret = tmp_path / "geheim.txt"
    secret.write_text("GEHEIMNIS")
    xml = resp_xml("2026-0001").replace(b'<?xml version="1.0" encoding="UTF-8"?>',
                                        f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file://{secret}">]>'.encode())
    xml = xml.replace(b"Bitte pr\xc3\xbcfen", b"&x;")
    try:
        r = response.parse(xml)
    except response.ResponseError:
        return
    assert "GEHEIMNIS" not in (r.explanation or "")


def test_import_match_duplicate_unmatched(app):
    inv = _final(app)
    db = app[DB]
    with practice_scope(1, 1):
        res = followup.import_responses(db, "a.xml", resp_xml(inv["number"], errors=[("31148", "Falsche Ziffer", 1, None, None)]))
        assert res["imported"] == 1 and res["unmatched"] == 0
        assert followup.import_responses(db, "a.xml", resp_xml(inv["number"], errors=[("31148", "Falsche Ziffer", 1, None, None)]))["duplicates"] == 1
        res = followup.import_responses(db, "b.xml", resp_xml("1999-0001"))
        assert res["unmatched"] == 1
        rid = db.value("SELECT id FROM invoice_responses WHERE request_id = '1999-0001'")
        assert followup.assign_response(db, rid, inv["number"])
        assert len(followup.responses_for(db, inv["id"])) == 2
        assert "rejected_open" in followup.warnings(db, billing.get(db, inv["id"]))
        # angenommen ohne Hinweise → gleich erledigt
        followup.import_responses(db, "c.xml", resp_xml(inv["number"], kind="accepted", explanation=""))
        assert db.value("SELECT handled_at FROM invoice_responses WHERE kind = 'accepted'") is not None


async def test_zip_upload_and_screens(client, app):
    inv = _final(app)
    db = app[DB]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("antworten/r1.xml", resp_xml(inv["number"], errors=[("31148", "Tarifziffer ungültig", 1, "25.111", "25.110")]))
        zf.writestr("readme.txt", "x")
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    from aiohttp import FormData

    form = FormData()
    form.add_field("_csrf", csrf)
    form.add_field("file", buf.getvalue(), filename="antworten.zip", content_type="application/zip")
    r = await client.post("/leitung/abrechnung/antworten", data=form, allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT COUNT(*) FROM invoice_responses WHERE invoice_id = ?", (inv["id"],)) == 1
    html = await (await client.get("/leitung/abrechnung?ansicht=antworten")).text()
    assert "Rückweisung" in html and "Tarifziffer ungültig" in html and inv["number"] in html
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "zurückgewiesen" in html and "Zeile 1" in html and "falsch: 25.111, richtig: 25.110" in html
    assert "Storno und neuer Entwurf" in html
    html = await (await client.get("/leitung/uebersicht")).text()
    assert "Rückweisungen und Mitteilungen" in html
    # Korrektur: Storno + neuer Entwurf mit denselben Zeilen
    r = await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "correct", "reason": "Rückweisung"},
                          allow_redirects=False)
    assert r.status == 302
    new_id = int(r.headers["Location"].rsplit("/", 1)[1])
    old = db.one("SELECT * FROM invoices WHERE id = ?", (inv["id"],))
    new = db.one("SELECT * FROM invoices WHERE id = ?", (new_id,))
    assert old["status"] == "cancelled" and new["status"] == "draft" and new["total_chf"] == old["total_chf"]
    assert db.value("SELECT COUNT(*) FROM invoice_lines WHERE invoice_id = ?", (new_id,)) == \
        db.value("SELECT COUNT(*) FROM invoice_lines WHERE invoice_id = ?", (inv["id"],))
    assert db.value("SELECT COUNT(*) FROM invoice_responses WHERE invoice_id = ? AND handled_at IS NULL", (inv["id"],)) == 0
    with practice_scope(1):
        assert not billing.unbilled_for(db, inv["patient_id"])
    # falsche Datei
    form = FormData()
    form.add_field("_csrf", csrf)
    form.add_field("file", b"<x/>", filename="x.xml", content_type="text/xml")
    r = await client.post("/leitung/abrechnung/antworten", data=form, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM invoice_responses") == 1


async def test_empfang_can_mark_handled_but_not_correct(client, app):
    inv = _final(app)
    db = app[DB]
    with practice_scope(1, 1):
        followup.import_responses(db, "a.xml", resp_xml(inv["number"]))
    rid = db.value("SELECT id FROM invoice_responses")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/rechnungen/{inv['id']}")).text()
    assert 'value="correct"' not in html and "Rückweisung" in html
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "correct", "reason": "x"})
    assert db.value("SELECT status FROM invoices WHERE id = ?", (inv["id"],)) == "final"
    await client.post("/empfang/abrechnung", data={"_csrf": csrf, "do": "resp_handled", "response_id": str(rid), "note": "telefoniert"})
    assert db.value("SELECT handled_note FROM invoice_responses WHERE id = ?", (rid,)) == "telefoniert"


async def test_cost_approvals(client, app, monkeypatch):
    db = app[DB]
    hans = pid_of(app, "Peter")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post("/empfang/abrechnung/kostengutsprache", data={"_csrf": csrf, "do": "add", "patient_id": str(hans),
                                                                        "requested_on": "2026-10-01", "sessions_requested": "9",
                                                                        "reference": "Brief 1.10."}, allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].endswith("#kostengutsprache")
    cid = db.value("SELECT id FROM cost_approvals WHERE patient_id = ?", (hans,))
    html = await (await client.get(f"/empfang/patienten/{hans}")).text()
    assert "Kostengutsprache" in html and "Beantragt" in html and "Brief 1.10." in html
    # Warnung: Grenze für den Test auf 0 gesetzt
    monkeypatch.setattr(followup, "KVG_FREE_SESSIONS", 0)
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        billing.set_tp_value(db, "kvg312", 1.0, "2026-01-01")
        iid = billing.create_from_appointments(db, core_patients.get(db, hans), "de")
        assert iid and "kogu_36" in followup.warnings(db, billing.get(db, iid))
    await client.post("/empfang/abrechnung/kostengutsprache", data={"_csrf": csrf, "do": "decide", "patient_id": str(hans),
                                                                    "approval_id": str(cid), "status": "approved", "sessions_approved": "9",
                                                                    "valid_until": "2099-12-31"})
    row = db.one("SELECT * FROM cost_approvals WHERE id = ?", (cid,))
    assert row["status"] == "approved" and row["sessions_approved"] == 9 and row["decided_on"]
    with practice_scope(1, owner):
        assert "kogu_36" not in followup.warnings(db, billing.get(db, iid))
    # Entscheid einer fremden Patientin über eine andere Patient-ID → nicht gefunden
    r = await client.post("/empfang/abrechnung/kostengutsprache", data={"_csrf": csrf, "do": "decide", "patient_id": str(pid_of(app, "Lea")),
                                                                        "approval_id": str(cid), "status": "rejected"}, allow_redirects=False)
    assert r.status == 404
