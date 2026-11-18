"""Selbstzahler/VVG (Preisliste, MWST) und Kasse: Verkauf, Quittung, Abos, offene Rechnung an der Theke, Tagesabschluss."""
from __future__ import annotations

import re
import sqlite3

import pytest
from conftest import login, pid_of

from nfizyo.core import patients as core_patients
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import today
from nfizyo.modules.billing import api as billing_api
from nfizyo.modules.billing import payment, private
from nfizyo.modules.billing import repo as billing
from nfizyo.modules.cashdesk import repo as cash


def _owner(app) -> int:
    return app[DB].value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")


def test_uid_check():
    assert private.clean_uid("CHE-123.456.788 MWST") == "CHE-123.456.788"      # Beispiel BFS
    assert private.clean_uid("che123456788") == "CHE-123.456.788"
    assert private.clean_uid("CHE-123.456.789") is None                        # falsche Prüfziffer
    assert private.clean_uid("DE123456788") is None
    assert private.vat_in(108.10, 8.1) == 8.10 and private.vat_in(50, 0) == 0.0


async def test_price_list_page(client, app):
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/preise")).text()
    assert "MTT-10" in html and "Theraband" in html
    r = await client.post("/leitung/einstellungen/preise", data={"_csrf": csrf, "do": "save", "kind": "product", "code": "TB-ROT",
                                                                 "name_de": "Doppelt", "price_chf": "5", "vat_rate": "8.1"})
    assert "schon vergeben" in await r.text()
    r = await client.post("/leitung/einstellungen/preise", data={"_csrf": csrf, "do": "save", "kind": "abo", "code": "ABO-X",
                                                                 "name_de": "Abo ohne Dauer", "price_chf": "100", "vat_rate": "8.1"})
    assert "Gültigkeit in Tagen" in await r.text()
    r = await client.post("/leitung/einstellungen/preise", data={"_csrf": csrf, "do": "save", "kind": "product", "code": "BALL",
                                                                 "name_de": "Igelball", "price_chf": "7.50", "vat_rate": "8.1"},
                          allow_redirects=False)
    assert r.status == 302
    assert app[DB].value("SELECT price_chf FROM price_items WHERE code = 'BALL' AND practice_id = 1") == 7.5
    # Empfang darf die Preisliste nicht ändern
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.get("/empfang/einstellungen/preise", allow_redirects=False)
    assert r.status in (403, 404)


def test_private_invoice_from_appointments_and_vat(app):
    db = app[DB]
    with practice_scope(1, _owner(app)):
        ready = [r for r in billing.ready(db) if r["law"] == "KVG"]
        assert ready
        patient = core_patients.get(db, ready[0]["patient_id"])
        iid = billing.create_from_appointments(db, patient, "de", private_=True)
        inv = billing.get(db, iid)
        assert inv["tariff"] == "private" and inv["law"] == "privat" and inv["tiers"] == "garant" and inv["insurer_id"] is None
        lns = billing.lines(db, iid)
        assert all(ln["unit_price"] and ln["code"] for ln in lns)
        assert inv["total_chf"] == round(sum(ln["amount_chf"] for ln in lns), 2) and inv["vat_chf"] == 0.0
        assert not billing.problems(db, inv, lns)
        # Produkt dazu (8,1 %) – ohne MWST-Nummer keine MWST
        billing.add_line(db, inv, code="TB-ROT", quantity=2, service_date=today().isoformat(), lang="de")
        assert billing.get(db, iid)["vat_chf"] == 0.0
        # Mit MWST-Nummer: Entwurf wird neu berechnet
        payment.save_settings(db, {"vat_number": "CHE-123.456.788"})
        billing.reprice_private_drafts(db)
        inv = billing.get(db, iid)
        assert inv["vat_chf"] == private.vat_in(2 * 12.90, 8.1) == 1.93
        billing.finalize(db, inv)
        inv = billing.get(db, iid)
        assert inv["number"] and inv["vat_number"] == "CHE-123.456.788" and inv["qr_reference"]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE invoices SET vat_chf = 0 WHERE id = ?", (iid,))
        # die Termine gelten als verrechnet
        assert not billing.unbilled_for(db, patient["id"])


async def test_private_invoice_pages(client, app):
    db = app[DB]
    with practice_scope(1, _owner(app)):
        patient = core_patients.get(db, [r for r in billing.ready(db) if r["law"] == "KVG"][0]["patient_id"])
        iid = billing.create_from_appointments(db, patient, "de", private_=True)
        billing.finalize(db, billing.get(db, iid))
    await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get(f"/leitung/rechnungen/{iid}")).text()
    assert "Preisliste der Praxis" in html and "PRIV" in html
    assert "keine elektronische Sendung vorgesehen" in html and 'value="xml"' not in html
    r = await client.get(f"/leitung/rechnungen/{iid}/pdf")
    assert r.status == 200 and (await r.read())[:4] == b"%PDF"


async def test_sale_flow_cash_abo_invoice(client, app):
    db = app[DB]
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    with practice_scope(1):
        items = {it["code"]: it for it in billing_api.price_items(db)}
    # Produkte bar, ohne Patient/in, mit Rückgeld
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "cash", "given": "50",
                                                  f"qty_{items['TB-ROT']['id']}": "2", f"qty_{items['TAPE']['id']}": "1"},
                          allow_redirects=False)
    assert r.status == 302 and "/empfang/kasse/belege/" in r.headers["Location"]
    sid = int(r.headers["Location"].rsplit("/", 1)[1])
    sale = db.one("SELECT * FROM cash_sales WHERE id = ?", (sid,))
    assert sale["total_chf"] == 42.30 and sale["status"] == "paid" and re.fullmatch(r"Q\d{4}-\d{4}", sale["number"])
    html = await (await client.get(r.headers["Location"])).text()
    assert "7.70" in html  # Rückgeld
    r = await client.get(f"/empfang/kasse/belege/{sid}/pdf")
    assert r.status == 200 and (await r.read())[:4] == b"%PDF"
    # Abo ohne Patient/in → Fehler
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "cash", f"qty_{items['MTT-10']['id']}": "1"})
    assert r.status == 400 and "braucht eine Patientin" in await r.text()
    # Abo mit Patient/in (Twint) → Abo mit 10 Besuchen
    lea = pid_of(app, "Lea")
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "twint", "patient_id": str(lea),
                                                  f"qty_{items['MTT-10']['id']}": "1"}, allow_redirects=False)
    assert r.status == 302
    sub = db.one("SELECT * FROM subscriptions WHERE patient_id = ? AND practice_id = 1", (lea,))
    assert sub["sessions_total"] == 10 and sub["valid_from"] == today().isoformat()
    # Besuch abhaken – einmal pro Tag, zurücknehmen möglich
    for expected in (1, 1):
        await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "visit", "sub_id": str(sub["id"])})
        assert db.value("SELECT COUNT(*) FROM subscription_visits WHERE subscription_id = ?", (sub["id"],)) == expected
    html = await (await client.get(f"/empfang/patienten/{lea}")).text()
    assert "1 von 10 Besuchen" in html
    await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "visit_undo", "sub_id": str(sub["id"])})
    assert db.value("SELECT COUNT(*) FROM subscription_visits WHERE subscription_id = ?", (sub["id"],)) == 0
    # Auf Rechnung → Entwurf in der Abrechnung (Selbstzahler-Tarif)
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "invoice", "patient_id": str(lea),
                                                  f"qty_{items['ROLLE']['id']}": "1"}, allow_redirects=False)
    sid = int(r.headers["Location"].rsplit("/", 1)[1])
    sale = db.one("SELECT * FROM cash_sales WHERE id = ?", (sid,))
    inv = db.one("SELECT * FROM invoices WHERE id = ?", (sale["invoice_id"],))
    assert sale["status"] == "invoiced" and inv["status"] == "draft" and inv["tariff"] == "private" and inv["total_chf"] == 34.90
    # Leere Eingabe und ungültige Anzahl
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "cash"})
    assert r.status == 400
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "cash", f"qty_{items['TAPE']['id']}": "1.5"})
    assert r.status == 400


async def test_pay_invoice_at_desk_and_close_day(client, app):
    db = app[DB]
    with practice_scope(1, _owner(app)):
        patient = core_patients.get(db, [r for r in billing.ready(db) if r["law"] == "KVG"][0]["patient_id"])
        iid = billing.create_from_appointments(db, patient, "de", private_=True)
        billing.finalize(db, billing.get(db, iid))
        inv = billing.get(db, iid)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/kasse?rechnung={inv['number']}")).text()
    assert f"{inv['total_chf']:.2f}" in html
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "pay_invoice", "number": inv["number"],
                                                  "amount": f"{inv['total_chf']:.2f}", "method": "cash"}, allow_redirects=False)
    assert r.status == 302
    with practice_scope(1):
        assert billing.balance(db, billing.get(db, iid))["state"] == "paid"
        assert db.value("SELECT method FROM invoice_payments WHERE invoice_id = ?", (iid,)) == "cash"
        items = {it["code"]: it for it in billing_api.price_items(db)}
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "cash", f"qty_{items['TB-GRUEN']['id']}": "1"},
                          allow_redirects=False)
    sid = int(r.headers["Location"].rsplit("/", 1)[1])
    day = today().isoformat()
    with practice_scope(1):
        summary = cash.day_summary(db, day)
    assert summary["cash"] == round(inv["total_chf"] + 14.90, 2)
    # Tagesabschluss mit Differenz
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "close", "day": day, "counted": f"{summary['cash'] - 0.5:.2f}"},
                          allow_redirects=False)
    assert r.status == 302
    closing = db.one("SELECT * FROM cash_closings WHERE practice_id = 1 AND day = ?", (day,))
    assert closing["difference"] == -0.5
    # danach: kein Storno, kein Verkauf, kein zweiter Abschluss
    await client.post(f"/empfang/kasse/belege/{sid}", data={"_csrf": csrf, "do": "cancel", "reason": "Irrtum"})
    assert db.value("SELECT status FROM cash_sales WHERE id = ?", (sid,)) == "paid"
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "cash", f"qty_{items['TB-GRUEN']['id']}": "1"})
    assert r.status == 400 and "schon abgeschlossen" in await r.text()
    await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "close", "day": day, "counted": "0"})
    assert db.value("SELECT COUNT(*) FROM cash_closings WHERE practice_id = 1 AND day = ?", (day,)) == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE cash_closings SET cash_counted = 0 WHERE id = ?", (closing["id"],))


async def test_cancel_before_close_and_receipts_immutable(client, app):
    db = app[DB]
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    with practice_scope(1):
        items = {it["code"]: it for it in billing_api.price_items(db)}
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "card", f"qty_{items['ROLLE']['id']}": "1"},
                          allow_redirects=False)
    sid = int(r.headers["Location"].rsplit("/", 1)[1])
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE cash_sales SET total_chf = 1 WHERE id = ?", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM cash_sales WHERE id = ?", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE cash_sale_lines SET amount_chf = 0 WHERE sale_id = ?", (sid,))
    await client.post(f"/empfang/kasse/belege/{sid}", data={"_csrf": csrf, "do": "cancel", "reason": ""})
    assert db.value("SELECT status FROM cash_sales WHERE id = ?", (sid,)) == "paid"
    await client.post(f"/empfang/kasse/belege/{sid}", data={"_csrf": csrf, "do": "cancel", "reason": "Falsche Rolle"})
    assert db.value("SELECT status FROM cash_sales WHERE id = ?", (sid,)) == "cancelled"
    with practice_scope(1):
        assert cash.day_summary(db, today().isoformat())["by_method"]["card"] == 0.0
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'cancel' AND entity = 'cash_sale' AND entity_id = ?", (sid,)) == 1


async def test_screens_render(client, app):
    await login(client, "empfang@demo.n-fizyo.ch")
    for url in ("/empfang/kasse", "/empfang/kasse?ansicht=heute", "/empfang/kasse?ansicht=abos", "/empfang/kasse?ansicht=abschluss",
                f"/empfang/kasse?patient={pid_of(app, 'Sandra')}", "/empfang/kasse?q=Muster"):
        r = await client.get(url)
        assert r.status == 200, url
    html = await (await client.get("/empfang/kasse?ansicht=abos")).text()
    assert "Sandra" in html and "7 von 10 Besuchen" in html  # 6 am Empfang + 1 Check-in auf der MTT-Fläche (Demo)
    await login(client, "leitung@demo.n-fizyo.ch")
    assert (await client.get("/leitung/kasse?ansicht=abschluss")).status == 200
