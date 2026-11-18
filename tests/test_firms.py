"""Firmenkunden: Firma erfassen, Rechnung (Preisliste und frei), Abschluss nur durch die Leitung, unveränderbar, QR-Referenz
getrennt von der Abrechnung, Zahlung aus der Bankdatei, Mahnungen, E-Mail, Storno, Buchhaltungsexport."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest
from conftest import login

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.billing import api as billing_api
from nfizyo.modules.billing import payment
from nfizyo.modules.billing import repo as billing
from nfizyo.modules.firms import repo


def _firm_form(csrf: str, **kw) -> dict:
    data = {"_csrf": csrf, "do": "create", "name": "Test Uhren AG", "department": "HR", "contact": "Frau Test", "street": "Teststrasse",
            "house_no": "3", "zip": "2502", "city": "Biel", "email": "hr@test-uhren.example", "lang": "de", "payment_days": "20"}
    data.update(kw)
    return data


async def _new_invoice(client, csrf: str) -> tuple[int, int]:
    r = await client.post("/leitung/firmen", data=_firm_form(csrf), allow_redirects=False)
    assert r.status == 302
    fid = int(r.headers["Location"].rsplit("/", 1)[1])
    r = await client.post(f"/leitung/firmen/{fid}", data={"_csrf": csrf, "do": "invoice", "subject": "Rückenschule"}, allow_redirects=False)
    iid = int(r.headers["Location"].rsplit("/", 1)[1])
    return fid, iid


async def test_firm_validation_and_listing(client, app):
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post("/empfang/firmen", data=_firm_form(csrf, name=""), allow_redirects=False)
    assert r.status == 302 and "neu=1" in r.headers["Location"]
    r = await client.post("/empfang/firmen", data=_firm_form(csrf, uid="CHE-123.456.789"), allow_redirects=False)   # Prüfziffer falsch
    assert "neu=1" in r.headers["Location"]
    r = await client.post("/empfang/firmen", data=_firm_form(csrf, uid="che 123 456 788 mwst"), allow_redirects=False)
    assert r.status == 302 and "/empfang/firmen/" in r.headers["Location"]
    fid = int(r.headers["Location"].rsplit("/", 1)[1])
    assert app[DB].value("SELECT uid FROM firms WHERE id = ?", (fid,)) == "CHE-123.456.788"
    text = await (await client.get("/empfang/firmen")).text()
    assert "Test Uhren AG" in text and "Beispiel Präzision AG" in text          # eigene und Demo-Firma
    assert "Test Uhren AG" in await (await client.get("/empfang/firmen?q=Uhren")).text()


async def test_invoice_flow_finalize_lock_pdf(client, app):
    db = app[DB]
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post("/empfang/firmen", data=_firm_form(csrf), allow_redirects=False)
    fid = int(r.headers["Location"].rsplit("/", 1)[1])
    r = await client.post(f"/empfang/firmen/{fid}", data={"_csrf": csrf, "do": "invoice", "subject": "Rückenschule"}, allow_redirects=False)
    iid = int(r.headers["Location"].rsplit("/", 1)[1])
    base = f"/empfang/firmen/rechnungen/{iid}"
    item = db.value("SELECT id FROM price_items WHERE practice_id = 1 AND code = 'TB-ROT'")
    await client.post(base, data={"_csrf": csrf, "do": "item", "item_id": str(item), "quantity": "10", "service_date": date.today().isoformat()})
    await client.post(base, data={"_csrf": csrf, "do": "free", "label": "Rückenschule im Betrieb", "quantity": "2", "unit_price": "360",
                                  "vat_rate": "8.1", "kind": "service"})
    r = await client.post(base, data={"_csrf": csrf, "do": "free", "label": "x", "quantity": "1", "unit_price": "10", "vat_rate": "7.7"},
                          allow_redirects=False)
    assert "firms.invalid" in r.headers.get("Location", "") or r.status == 302             # MWST-Satz nicht erlaubt
    lines = db.query("SELECT * FROM firm_invoice_lines WHERE invoice_id = ? ORDER BY id", (iid,))
    assert len(lines) == 2
    assert lines[0]["amount_chf"] == 129.0 and lines[0]["kind"] == "product"
    # Demo-Praxis ohne MWST-Nummer → keine MWST, auch wenn ein Satz gewählt wurde
    assert lines[1]["vat_rate"] == 0 and lines[0]["vat_rate"] == 0
    assert db.value("SELECT total_chf FROM firm_invoices WHERE id = ?", (iid,)) == 849.0
    # Menge ändern
    await client.post(base, data={"_csrf": csrf, "do": "line", "line_id": str(lines[1]["id"]), "quantity": "3", "unit_price": "350"})
    assert db.value("SELECT total_chf FROM firm_invoices WHERE id = ?", (iid,)) == 129.0 + 1050.0
    # Der Empfang kann nicht abschliessen
    await client.post(base, data={"_csrf": csrf, "do": "finalize"})
    assert db.value("SELECT status FROM firm_invoices WHERE id = ?", (iid,)) == "draft"
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post(f"/leitung/firmen/rechnungen/{iid}", data={"_csrf": csrf, "do": "finalize"}, allow_redirects=False)
    inv = db.one("SELECT * FROM firm_invoices WHERE id = ?", (iid,))
    assert inv["status"] == "final" and inv["number"] == f"F{date.today().year}-0002"          # 0001 = Demo-Rechnung
    assert inv["due_on"] == (date.today() + timedelta(days=20)).isoformat()                    # Frist der Firma
    # QR-Referenz: Kennziffer 9, nicht mit Rechnungen der Abrechnung verwechselbar
    assert inv["qr_reference"].startswith("9") and len(inv["qr_reference"]) == 27
    assert payment.invoice_id_from_reference(inv["qr_reference"]) != iid or billing.get(db, iid) is None
    assert billing_api.parse_reference(inv["qr_reference"], 9, "NFF") == iid
    # Unveränderbar
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE firm_invoices SET total_chf = 1 WHERE id = ?", (iid,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM firm_invoice_lines WHERE invoice_id = ?", (iid,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM firm_invoices WHERE id = ?", (iid,))
    page = await (await client.get(f"/leitung/firmen/rechnungen/{iid}")).text()
    assert inv["number"] in page and "Abschliessen" not in page
    r = await client.get(f"/leitung/firmen/rechnungen/{iid}/pdf")
    body = await r.read()
    assert r.status == 200 and body[:4] == b"%PDF" and len(body) > 5000


async def test_scor_reference_without_qr_iban(app):
    db = app[DB]
    with practice_scope(1, 1):
        payment.save_settings(db, {"iban": "CH9300762011623852957"})                    # normale IBAN → RF-Referenz
        firm = repo.get_firm(db, repo.save_firm(db, {"name": "RF Test AG", "zip": "3000", "city": "Bern", "lang": "fr"}))
        inv = repo.get(db, repo.create(db, firm))
        repo.add_free_line(db, inv, label="Atelier", quantity=1, unit_price=100.0, vat_rate=0.0, kind="service")
        assert repo.finalize(db, repo.get(db, inv["id"]))
        inv = repo.get(db, inv["id"])
        assert inv["qr_reference"].startswith("RF") and "NFF" in inv["qr_reference"]
        assert repo.by_reference(db, inv["qr_reference"])["id"] == inv["id"]
        assert billing.payment.invoice_id_from_reference(inv["qr_reference"]) is None      # nie eine Rechnung der Abrechnung


async def test_camt_payment_goes_to_firm_invoice(client, app):
    from test_billing_payment import camt054

    db = app[DB]
    with practice_scope(1, 1):
        inv = repo.get(db, db.value("SELECT id FROM firm_invoices WHERE practice_id = 1 AND status = 'final' LIMIT 1"))
        before = db.value("SELECT COUNT(*) FROM invoice_payments", ())
        res = billing.import_payments(db, "bank.xml", camt054([("FB1", 500.0, inv["qr_reference"])]))
        assert res["matched"] == 1 and res["unmatched"] == 0
        assert db.value("SELECT COUNT(*) FROM invoice_payments", ()) == before                 # nicht in der Abrechnung
        assert db.value("SELECT amount_chf FROM firm_payments WHERE invoice_id = ? AND source = 'camt'", (inv["id"],)) == 500.0
        b = repo.balance(db, inv)
        assert b["state"] == "partial" and b["open"] == round(inv["total_chf"] - 500, 2)
        # dieselbe Buchung in einer anderen Datei: kein Doppel
        res = billing.import_payments(db, "bank2.xml", camt054([("FB1", 500.0, inv["qr_reference"])]).replace(b"M1", b"M2"))
        assert res["skipped"] == 1 and db.value("SELECT COUNT(*) FROM firm_payments WHERE invoice_id = ?", (inv["id"],)) == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE firm_payments SET amount_chf = 1 WHERE invoice_id = ?", (inv["id"],))


async def test_reminders_email_cancel(client, app, monkeypatch):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    iid = db.value("SELECT id FROM firm_invoices WHERE practice_id = 1 AND status = 'final' LIMIT 1")
    base = f"/leitung/firmen/rechnungen/{iid}"
    # noch nicht fällig
    r = await client.post(base, data={"_csrf": csrf, "do": "reminder", "level": "1"}, allow_redirects=False)
    assert db.value("SELECT COUNT(*) FROM firm_reminders WHERE invoice_id = ?", (iid,)) == 0

    monkeypatch.setattr(repo, "today", lambda: date.today() + timedelta(days=45))
    await client.post(base, data={"_csrf": csrf, "do": "reminder", "level": "1"})
    assert db.value("SELECT level FROM firm_reminders WHERE invoice_id = ?", (iid,)) == 1
    r = await client.get(f"{base}/mahnung/1/pdf")
    assert r.status == 200 and (await r.read())[:4] == b"%PDF"
    monkeypatch.undo()
    # E-Mail an die Firma mit PDF
    await client.post(base, data={"_csrf": csrf, "do": "email"})
    row = db.one("SELECT recipient, attachment_name, subject FROM outbox WHERE channel = 'email' ORDER BY id DESC LIMIT 1")
    assert row["recipient"] == "hr@beispiel-praezision.example" and row["attachment_name"].endswith(".pdf") and "F" in row["subject"]
    # Storno nur mit Grund, nur Leitung
    csrf_e = await login(client, "empfang@demo.n-fizyo.ch")
    await client.post(f"/empfang/firmen/rechnungen/{iid}", data={"_csrf": csrf_e, "do": "cancel", "reason": "Fehler"})
    assert db.value("SELECT status FROM firm_invoices WHERE id = ?", (iid,)) == "final"
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post(base, data={"_csrf": csrf, "do": "cancel", "reason": "Doppelt verrechnet"})
    assert db.value("SELECT status FROM firm_invoices WHERE id = ?", (iid,)) == "cancelled"


async def test_journal_contains_firm_invoices(client, app):
    db = app[DB]
    await login(client, "leitung@demo.n-fizyo.ch")
    t = date.today()
    r = await client.get(f"/leitung/export/journal.csv?von={(t - timedelta(days=5)).isoformat()}&bis={t.isoformat()}")
    text = (await r.read()).decode("utf-8-sig")
    number = db.value("SELECT number FROM firm_invoices WHERE practice_id = 1 AND status = 'final' LIMIT 1")
    assert number in text and "(Firma)" in text
    assert "1290.00" in text


async def test_draft_delete_and_problems(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/firmen", data=_firm_form(csrf, zip="", city=""), allow_redirects=False)
    fid = int(r.headers["Location"].rsplit("/", 1)[1])
    r = await client.post(f"/leitung/firmen/{fid}", data={"_csrf": csrf, "do": "invoice"}, allow_redirects=False)
    iid = int(r.headers["Location"].rsplit("/", 1)[1])
    page = await (await client.get(f"/leitung/firmen/rechnungen/{iid}")).text()
    assert "noch keine Positionen" in page and "Adresse der Firma fehlt" in page
    await client.post(f"/leitung/firmen/rechnungen/{iid}", data={"_csrf": csrf, "do": "finalize"})
    assert db.value("SELECT status FROM firm_invoices WHERE id = ?", (iid,)) == "draft"
    r = await client.post(f"/leitung/firmen/rechnungen/{iid}", data={"_csrf": csrf, "do": "delete"}, allow_redirects=False)
    assert r.headers["Location"].startswith(f"/leitung/firmen/{fid}")
    assert db.value("SELECT COUNT(*) FROM firm_invoices WHERE id = ?", (iid,)) == 0


async def test_module_can_be_switched_off(client, app):
    db = app[DB]
    db.execute("INSERT INTO practice_modules (practice_id, module, enabled) VALUES (1, 'firms', 0)"
               " ON CONFLICT(practice_id, module) DO UPDATE SET enabled = 0")
    await login(client, "leitung@demo.n-fizyo.ch")
    assert (await client.get("/leitung/firmen")).status == 404
    assert "/leitung/firmen" not in await (await client.get("/leitung/uebersicht")).text()
