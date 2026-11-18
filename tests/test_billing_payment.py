"""Abrechnung Stufe 2: Referenzen, XML 5.0 (gegen das offizielle Schema), PDF mit QR-Zahlteil, camt.054, Mahnungen, MediData."""
from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO

import pytest
from conftest import csrf_of, login, pid_of
from lxml import etree

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.billing import camt, payment, xml500
from nfizyo.modules.billing import repo as billing

NS = {"i": "http://www.forum-datenaustausch.ch/invoice"}


def _final_invoice(app, first_name: str) -> dict:
    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        if first_name != "Lea":
            billing.set_tp_value(db, "kvg312", 1.0, "2026-01-01")
        patient = db.one("SELECT * FROM patients WHERE id = ?", (pid_of(app, first_name),))
        iid = billing.create_from_appointments(db, patient, "de")
        inv = billing.get(db, iid)
        assert not billing.problems(db, inv, billing.lines(db, iid)), billing.problems(db, inv, billing.lines(db, iid))
        billing.finalize(db, inv)
        return dict(billing.get(db, iid))


def camt054(entries: list[tuple[str, float, str | None]]) -> bytes:
    tx = "".join(
        f"""<TxDtls><Refs><AcctSvcrRef>{bank}</AcctSvcrRef></Refs><Amt Ccy="CHF">{amount:.2f}</Amt><CdtDbtInd>CRDT</CdtDbtInd>
        <RltdPties><Dbtr><Pty><Nm>Lea Muster</Nm></Pty></Dbtr></RltdPties>
        {f'<RmtInf><Strd><CdtrRefInf><Tp><CdOrPrtry><Prtry>QRR</Prtry></CdOrPrtry></Tp><Ref>{ref}</Ref></CdtrRefInf></Strd></RmtInf>' if ref else ''}
        </TxDtls>""" for bank, amount, ref in entries)
    total = sum(a for _, a, _ in entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.08"><BkToCstmrDbtCdtNtfctn><GrpHdr><MsgId>M1</MsgId></GrpHdr>
<Ntfctn><Id>N1</Id><Ntry><Amt Ccy="CHF">{total:.2f}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
<BookgDt><Dt>2026-10-27</Dt></BookgDt><AcctSvcrRef>E1</AcctSvcrRef><NtryDtls>{tx}</NtryDtls></Ntry></Ntfctn>
</BkToCstmrDbtCdtNtfctn></Document>""".encode()


# --------------------------------------------------------------------------- Referenzen und IBAN
def test_references_and_iban():
    assert payment.scor("539007547034") == "RF18539007547034"          # Beispiel ISO 11649
    assert payment.mod10("21000000000313947143000901") == 7               # Beispiel SIX
    assert payment.is_qr_iban("CH4431999123000889012") and not payment.is_qr_iban("CH9300762011623852957")
    assert not payment.valid_iban("CH9300762011623852958")
    for iid in (1, 42, 987654):
        assert payment.invoice_id_from_reference(payment.qr_reference(iid)) == iid
        assert payment.invoice_id_from_reference(payment.scor_reference(iid)) == iid
    bad = payment.qr_reference(42)[:-1] + "0"
    assert payment.invoice_id_from_reference(bad) in (None, 42) and payment.invoice_id_from_reference("RF00NF42") is None


# --------------------------------------------------------------------------- XML 5.0
def test_xml_uvg_tiers_payant_is_schema_valid(app):
    inv = _final_invoice(app, "Lea")
    db = app[DB]
    with practice_scope(1):
        data = billing.xml_data(db, billing.get(db, inv["id"]))
        assert xml500.missing(data) == []
        xml, guid = xml500.build(data, modus="test")
    doc = etree.fromstring(xml)
    assert doc.get("modus") == "test" and len(guid) == 32
    assert doc.find(".//i:tiers_payant", NS) is not None
    services = doc.findall(".//i:service", NS)
    assert services and {s.get("tariff_type") for s in services} == {"311"}
    for s in services:
        ef = 0.94 if s.get("date_begin")[:10] >= "2026-10-01" else 1.0
        assert float(s.get("external_factor")) == ef
        assert abs(float(s.get("quantity")) * float(s.get("unit")) * float(s.get("unit_factor")) * ef - float(s.get("amount"))) < 0.006
    assert abs(sum(float(s.get("amount")) for s in services) - inv["total_chf"]) < 0.01
    assert doc.find(".//i:esrQR", NS).get("reference_number") == inv["qr_reference"]
    assert doc.find(".//i:law", NS).get("type") == "UVG" and doc.find(".//i:law", NS).get("case_id")
    assert doc.find(".//i:treatment", NS).get("reason") == "accident"


def test_xml_reminder_and_storno_valid(app):
    inv = _final_invoice(app, "Lea")
    db = app[DB]
    with practice_scope(1):
        data = billing.xml_data(db, billing.get(db, inv["id"]))
        xml, _ = xml500.build(data, kind="reminder", reminder={"level": 2, "date": "2026-10-28T09:00:00+00:00", "fee": 10.0,
                                                               "open": inv["total_chf"]})
        doc = etree.fromstring(xml)
        assert doc.find(".//i:payload", NS).get("request_type") == "reminder"
        assert doc.find(".//i:reminder", NS).get("reminder_level") == "2"
        assert float(doc.find(".//i:balance", NS).get("amount_due")) == pytest.approx(inv["total_chf"] + 10.0)
        xml, _ = xml500.build(data, kind="storno")
        assert etree.fromstring(xml).find(".//i:payload", NS).get("request_subtype") == "storno"


def test_xml_lists_missing_master_data(app):
    inv = _final_invoice(app, "Peter")  # KVG, ohne AHV-Nummer
    db = app[DB]
    with practice_scope(1):
        data = billing.xml_data(db, billing.get(db, inv["id"]))
        assert "xml.patient_ahv" in xml500.missing(data)
        with pytest.raises(xml500.XmlInvalid):
            xml500.build(data)
        db.execute("UPDATE patients SET ahv_number = '7569217076985' WHERE id = ?", (inv["patient_id"],))
        data = billing.xml_data(db, billing.get(db, inv["id"]))
        xml, _ = xml500.build(data, kind="copy")
    doc = etree.fromstring(xml)
    assert doc.find(".//i:tiers_garant", NS) is not None
    assert doc.find(".//i:payload", NS).get("request_subtype") == "copy"
    assert {s.get("tariff_type") for s in doc.findall(".//i:service", NS)} == {"312"}


def test_schema_rejects_broken_xml():
    with pytest.raises(xml500.XmlInvalid):
        xml500.validate(b'<?xml version="1.0"?><invoice:request xmlns:invoice="http://www.forum-datenaustausch.ch/invoice" language="de" guid="x"/>')


# --------------------------------------------------------------------------- PDF mit QR-Zahlteil
async def test_invoice_pdf_with_qr_bill(client, app):
    from pypdf import PdfReader

    inv = _final_invoice(app, "Peter")  # Tiers garant → QR-Zahlteil
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.get(f"/empfang/rechnungen/{inv['id']}/pdf")
    assert r.status == 200 and r.content_type == "application/pdf"
    reader = PdfReader(BytesIO(await r.read()))
    last = reader.pages[-1].extract_text()
    assert "Zahlteil" in last and "Empfangsschein" in last and "CH44 3199 9123 0008 8901 2" in last
    assert "Peter Muster" in last
    first = reader.pages[0].extract_text()
    assert "Rechnung Physiotherapie" in first and inv["number"] in first and "312" in first
    assert csrf


async def test_pdf_for_payant_has_no_qr(client, app):
    from pypdf import PdfReader

    inv = _final_invoice(app, "Lea")
    await login(client, "leitung@demo.n-fizyo.ch")
    data = await (await client.get(f"/leitung/rechnungen/{inv['id']}/pdf")).read()
    text = "".join(p.extract_text() for p in PdfReader(BytesIO(data)).pages)
    assert "Tiers payant" in text and "Zahlteil" not in text


# --------------------------------------------------------------------------- Zahlungseingang camt.054
def test_camt_parse_and_match(app):
    inv = _final_invoice(app, "Peter")
    db = app[DB]
    data = camt054([("B1", inv["total_chf"], inv["qr_reference"]), ("B2", 12.5, None)])
    entries = camt.parse(data)
    assert [e.bank_ref for e in entries] == ["B1", "B2"] and entries[0].debtor == "Lea Muster"
    with practice_scope(1, 1):
        result = billing.import_payments(db, "bank.xml", data)
        assert result == {"duplicate": False, "entries": 2, "matched": 1, "unmatched": 1, "skipped": 0}
        assert billing.balance(db, billing.get(db, inv["id"]))["state"] == "paid"
        assert billing.import_payments(db, "bank.xml", data)["duplicate"]
        # gleiche Buchung in anderer Datei → kein Doppel
        again = camt054([("B1", inv["total_chf"], inv["qr_reference"])])
        assert billing.import_payments(db, "other.xml", again)["skipped"] == 1
        assert billing.paid_total(db, inv["id"]) == pytest.approx(inv["total_chf"])
        with pytest.raises(camt.CamtError):
            camt.parse(b"<x/>")


async def test_unmatched_payment_assign(client, app):
    inv = _final_invoice(app, "Peter")
    db = app[DB]
    with practice_scope(1, 1):
        billing.import_payments(db, "b.xml", camt054([("U1", 40.0, None)]))
    pay_id = db.value("SELECT id FROM invoice_payments WHERE bank_ref = 'U1'")
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await client.get("/empfang/abrechnung?ansicht=zahlungen")
    html = await page.text()
    assert "40.00" in html and "Lea Muster" in html
    await client.post("/empfang/abrechnung", data={"_csrf": csrf_of(html), "do": "assign", "payment_id": str(pay_id), "number": inv["number"]})
    assert db.value("SELECT invoice_id FROM invoice_payments WHERE id = ?", (pay_id,)) == inv["id"]


async def test_camt_upload_via_page(client, app):
    inv = _final_invoice(app, "Peter")
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/abrechnung?ansicht=zahlungen")).text()
    import aiohttp

    form = aiohttp.FormData()
    form.add_field("_csrf", csrf_of(html))
    form.add_field("file", camt054([("W1", inv["total_chf"], inv["qr_reference"])]), filename="camt.xml", content_type="application/xml")
    r = await client.post("/empfang/abrechnung/zahlungen", data=form, allow_redirects=False)
    assert r.status == 302
    assert app[DB].value("SELECT invoice_id FROM invoice_payments WHERE bank_ref = 'W1'") == inv["id"]


# --------------------------------------------------------------------------- Mahnungen
async def test_reminders_levels_and_fees(client, app):
    inv = _final_invoice(app, "Peter")
    db = app[DB]
    past = (date.today() - timedelta(days=1)).isoformat()
    db.execute("UPDATE invoices SET due_on = ? WHERE id = ?", (past, inv["id"]))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/rechnungen/{inv['id']}")).text()
    assert "Zahlungserinnerung erstellen" in html and "Überfällig" in html
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "reminder", "level": "2"})
    assert db.value("SELECT COUNT(*) FROM invoice_reminders WHERE invoice_id = ?", (inv["id"],)) == 0  # Stufe überspringen geht nicht
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "reminder", "level": "1"})
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "reminder", "level": "2"})
    assert db.value("SELECT COUNT(*) FROM invoice_reminders WHERE invoice_id = ?", (inv["id"],)) == 1  # neue Frist läuft
    db.execute("UPDATE invoice_reminders SET due_on = ? WHERE invoice_id = ?", (past, inv["id"]))
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "reminder", "level": "2"})
    with practice_scope(1):
        b = billing.balance(db, billing.get(db, inv["id"]))
    assert b["fees"] == 10.0 and b["open"] == pytest.approx(inv["total_chf"] + 10.0)
    r = await client.get(f"/empfang/rechnungen/{inv['id']}/mahnung/2/pdf")
    from pypdf import PdfReader

    text = "".join(p.extract_text() for p in PdfReader(BytesIO(await r.read())).pages)
    assert "2. Mahnung" in text and "10.00" in text and "Zahlteil" in text
    # bezahlt → keine weitere Mahnung
    with practice_scope(1, 1):
        billing.add_payment(db, amount=b["open"], booked_on=date.today().isoformat(), source="manual", invoice_id=inv["id"])
        assert billing.next_reminder(db, billing.get(db, inv["id"])) is None


# --------------------------------------------------------------------------- Seite: XML erzeugen, MediData
async def test_leitung_creates_xml_and_sends_to_medidata(client, app, monkeypatch):
    from nfizyo.modules.billing import medidata

    inv = _final_invoice(app, "Lea")
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "Elektronische Rechnung" in html and "Senden" not in html.split("Elektronische Rechnung")[1].split("</section>")[0]
    r = await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "xml", "kind": "invoice"}, allow_redirects=False)
    assert r.status == 302
    db = app[DB]
    t = db.one("SELECT * FROM invoice_transmissions WHERE invoice_id = ?", (inv["id"],))
    assert t["status"] == "created" and t["channel"] == "file" and t["modus"] == "test"
    r = await client.get(f"/leitung/rechnungen/{inv['id']}/xml/{t['id']}")
    xml = await r.read()
    xml500.validate(xml)
    # Empfang darf kein XML senden
    csrf_e = await login(client, "empfang@demo.n-fizyo.ch")
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf_e, "do": "send", "kind": "invoice"})
    assert db.value("SELECT COUNT(*) FROM invoice_transmissions WHERE invoice_id = ?", (inv["id"],)) == 1

    # MediData eingerichtet (Test) → Senden
    monkeypatch.setattr(medidata.MediDataClient, "mode", property(lambda self: "test"))
    monkeypatch.setattr(medidata.MediDataClient, "configured", property(lambda self: True))
    sent = []

    async def fake_upload(self, xml, filename, info):
        sent.append((xml, filename, info))
        return medidata.UploadResult(True, reference="TR-1")

    monkeypatch.setattr(medidata.MediDataClient, "upload", fake_upload)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    t = db.one("SELECT * FROM invoice_transmissions WHERE invoice_id = ? ORDER BY id DESC LIMIT 1", (inv["id"],))
    assert t["status"] == "sent" and t["reference"] == "TR-1" and t["channel"] == "medidata"
    # Steuerdatei elauploadinfo laut Schnittstellendokumentation V5.2.0, 3.7.2
    assert sent[0][2] == {"modus": "test", "toPatient": False, "documentReference": t["guid"], "correlationReference": inv["number"]}
    assert t["document_reference"] == t["guid"] and t["correlation_reference"] == inv["number"]
    # Kopie für Tiers garant ist hier nicht vorgesehen (Tiers payant) → abgelehnt
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "xml", "kind": "copy"})
    assert db.value("SELECT COUNT(*) FROM invoice_transmissions WHERE invoice_id = ? AND kind = 'copy'", (inv["id"],)) == 0


async def test_finalize_needs_iban(client, app):
    db = app[DB]
    db.execute("UPDATE billing_settings SET iban = NULL")
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/abrechnung?ansicht=bereit")).text()
    assert "IBAN" in html
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        patient = db.one("SELECT * FROM patients WHERE id = ?", (pid_of(app, "Lea"),))
        iid = billing.create_from_appointments(db, patient, "de")
    await client.post(f"/leitung/rechnungen/{iid}", data={"_csrf": csrf, "do": "finalize"})
    assert db.value("SELECT status FROM invoices WHERE id = ?", (iid,)) == "draft"
    r = await client.post("/leitung/einstellungen/zahlung", data={"_csrf": csrf, "iban": "CH93 0076 2011 6238 5295 8"}, allow_redirects=False)
    assert db.value("SELECT iban FROM billing_settings") is None  # Prüfziffer falsch
    await client.post("/leitung/einstellungen/zahlung", data={"_csrf": csrf, "iban": "CH93 0076 2011 6238 5295 7", "payment_days": "30",
                                                              "reminder_days": "10", "reminder_fee_2": "0", "reminder_fee_3": "0"})
    assert db.value("SELECT iban FROM billing_settings") == "CH9300762011623852957"
    await client.post(f"/leitung/rechnungen/{iid}", data={"_csrf": csrf, "do": "finalize"})
    row = db.one("SELECT * FROM invoices WHERE id = ?", (iid,))
    assert row["status"] == "final" and row["qr_reference"].startswith("RF") and row["iban"] == "CH9300762011623852957"
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE invoices SET qr_reference = 'RF00X' WHERE id = ?", (iid,))
    assert r.status == 302


async def test_payant_reminder_goes_to_insurer(client, app):
    """Mahnung zu einer Tiers-payant-Rechnung: adressiert an den Versicherer (er schuldet), QR-Zahlteil mit ihm als Zahler –
    nicht an die Patientin, die nichts schuldet."""
    from pypdf import PdfReader

    inv = _final_invoice(app, "Lea")
    db = app[DB]
    ins = db.one("SELECT * FROM insurers WHERE id = ?", (inv["insurer_id"],))
    db.execute("UPDATE insurers SET street = 'Fluhmattstrasse 1', zip = '6002', city = 'Luzern' WHERE id = ?", (ins["id"],))
    db.execute("UPDATE invoices SET due_on = '2026-10-01' WHERE id = ?", (inv["id"],))
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "reminder", "level": "1"})
    r = await client.get(f"/leitung/rechnungen/{inv['id']}/mahnung/1/pdf")
    assert r.status == 200
    pages = PdfReader(BytesIO(await r.read())).pages
    first, last = pages[0].extract_text(), pages[-1].extract_text()
    assert ins["name"] in first and "6002 Luzern" in first and "Lea Muster" not in first.split(ins["name"])[0]
    assert "Zahlteil" in last and "6002 Luzern" in last


def test_no_reminder_while_rejection_open(app):
    """Versicherer hat zurückgewiesen (noch nicht erledigt) → keine Mahnung; nach «erledigt» wieder möglich."""
    inv = _final_invoice(app, "Lea")
    db = app[DB]
    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    with practice_scope(1, owner):
        db.execute("UPDATE invoices SET due_on = '2026-10-01' WHERE id = ?", (inv["id"],))
        inv = billing.get(db, inv["id"])
        assert billing.next_reminder(db, inv) == 1
        data = {"practice_id": 1, "invoice_id": inv["id"], "kind": "rejected", "created_at": "2026-11-01T08:00:00+00:00"}
        for r in db.query("PRAGMA table_info(invoice_responses)"):
            if r["notnull"] and r["dflt_value"] is None and not r["pk"] and r["name"] not in data:
                data[r["name"]] = "upload" if r["name"] == "source" else "x"
        rid = db.execute(f"INSERT INTO invoice_responses ({','.join(data)}) VALUES ({','.join('?' * len(data))})", tuple(data.values()))
        assert billing.next_reminder(db, inv) is None
        db.execute("UPDATE invoice_responses SET handled_at = '2026-11-02T08:00:00+00:00' WHERE id = ?", (rid,))
        assert billing.next_reminder(db, inv) == 1
