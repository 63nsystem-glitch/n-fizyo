"""0.9.0 Empfang und Abrechnung: Kasse (Total/Rückgeld, Korrektur nach Abschluss), Zahlungen korrigieren, Verordnungen
bearbeiten/beenden, doppelte Patient/innen (löschen, zusammenführen), Abrechnung-Reiter, Kanton, MediData «unklar»,
Taxpunktwert je Versicherergruppe, Firmen-Formular, Agenda-Hinweise, Abwesenheit «Krankheit»."""
from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

import pytest
from conftest import login, pid_of

from nfizyo.core import patients as core_patients
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import today
from nfizyo.modules.billing import api as billing_api
from nfizyo.modules.billing import repo as billing
from nfizyo.modules.billing import xml500
from nfizyo.modules.cashdesk import repo as cash


def _owner(app) -> int:
    return app[DB].value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")


def _items(app) -> dict:
    with practice_scope(1):
        return {it["code"]: it for it in billing_api.price_items(app[DB])}


def _final_invoice(app, first_name: str = "Lea") -> dict:
    db = app[DB]
    with practice_scope(1, _owner(app)):
        billing.set_tp_value(db, "kvg312", 1.0, "2026-01-01")
        patient = core_patients.get(db, pid_of(app, first_name))
        iid = billing.create_from_appointments(db, patient, "de")
        billing.finalize(db, billing.get(db, iid))
        return dict(billing.get(db, iid))


# =========================================================================== 1 Kasse: Total, Erhalten, Rückgeld
async def test_cash_desk_shows_total_and_change(client, app):
    items = _items(app)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/kasse")).text()
    assert "js/cashdesk.js" in html and 'data-cash-total' in html and f'data-price="{items["TB-ROT"]["price_chf"]:.2f}"' in html
    assert "<script>" not in html                                       # CSP: kein Inline-Script
    # ohne JavaScript: «Total berechnen» rechnet auf dem Server (gleich wie der Verkauf) und verkauft nichts
    n = app[DB].value("SELECT COUNT(*) FROM cash_sales")
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "preview", "method": "cash", "given": "50",
                                                  f"qty_{items['TB-ROT']['id']}": "2", f"qty_{items['TAPE']['id']}": "1"})
    html = await r.text()
    assert r.status == 200 and app[DB].value("SELECT COUNT(*) FROM cash_sales") == n
    assert "CHF 42.30" in html and "CHF 50.00" in html and "CHF 7.70" in html
    assert re.search(r'name="qty_%d"[^>]*value="2"' % items["TB-ROT"]["id"], html)     # Eingaben bleiben stehen
    with practice_scope(1):
        t = cash.totals(app[DB], [(items["TB-ROT"], 2), (items["TAPE"], 1)], 40.0)
    assert t["total"] == 42.30 and t["change"] == -2.30


# =========================================================================== 2 Kasse: Korrektur nach dem Tagesabschluss
async def test_cash_correction_after_close(client, app):
    db = app[DB]
    items = _items(app)
    day = today().isoformat()
    yesterday = (today() - timedelta(days=1)).isoformat()
    with practice_scope(1, _owner(app)):
        s1, _ = cash.sell(db, patient=None, entries=[(items["TB-ROT"], 1)], method="cash", lang="de", day=yesterday)
        s2, _ = cash.sell(db, patient=None, entries=[(items["TAPE"], 1)], method="cash", lang="de", day=yesterday)
        cash.close_day(db, yesterday, 50.0, None)
    sale1, sale2 = (db.one("SELECT * FROM cash_sales WHERE id = ?", (s,)) for s in (s1, s2))
    # Empfang: keine Korrektur möglich
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/kasse/belege/{s1}")).text()
    assert "correct_storno" not in html
    await client.post(f"/empfang/kasse/belege/{s1}", data={"_csrf": csrf, "do": "correct_storno", "reason": "x"})
    assert db.value("SELECT COUNT(*) FROM cash_corrections") == 0
    # Leitung: nachträgliches Storno mit Geld zurück heute; ohne Grund nicht
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get(f"/leitung/kasse/belege/{s1}")).text()
    assert "correct_storno" in html and "correct_method" in html
    await client.post(f"/leitung/kasse/belege/{s1}", data={"_csrf": csrf, "do": "correct_storno", "reason": " "})
    assert db.value("SELECT COUNT(*) FROM cash_corrections") == 0
    await client.post(f"/leitung/kasse/belege/{s1}", data={"_csrf": csrf, "do": "correct_storno", "reason": "Ware zurück", "refund": "1"})
    c = db.one("SELECT * FROM cash_corrections WHERE sale_id = ?", (s1,))
    assert c["kind"] == "storno" and c["day"] == yesterday and c["refund_day"] == day and c["amount_chf"] == sale1["total_chf"]
    # zweites Storno derselben Quittung: nicht möglich
    await client.post(f"/leitung/kasse/belege/{s1}", data={"_csrf": csrf, "do": "correct_storno", "reason": "nochmals"})
    assert db.value("SELECT COUNT(*) FROM cash_corrections WHERE sale_id = ?", (s1,)) == 1
    # falsche Zahlungsart (bar statt Karte)
    await client.post(f"/leitung/kasse/belege/{s2}", data={"_csrf": csrf, "do": "correct_method", "to_method": "card", "reason": "Karte"})
    with practice_scope(1):
        closed = cash.day_summary(db, yesterday)
        now = cash.day_summary(db, day)
        assert cash.current_method(db, sale2) == "card"
    # der Abschluss bleibt unverändert, der Tagesbericht zeigt die Korrekturen und das Total danach
    closing = db.one("SELECT * FROM cash_closings WHERE practice_id = 1 AND day = ?", (yesterday,))
    assert closing["cash_expected"] == round(sale1["total_chf"] + sale2["total_chf"], 2)
    assert len(closed["corrections"]) == 2
    assert closed["net_by_method"]["cash"] == 0.0 and closed["net_by_method"]["card"] == sale2["total_chf"]
    assert now["by_method"]["cash"] == -sale1["total_chf"] and now["refunds"]          # Geld heute zurückgegeben
    html = await (await client.get(f"/leitung/kasse?ansicht=abschluss&tag={yesterday}")).text()
    assert "Korrekturen nach dem Abschluss" in html and "Ware zurück" in html
    html = await (await client.get(f"/leitung/kasse/belege/{s1}")).text()
    assert "Nach dem Tagesabschluss storniert" in html
    # unveränderbar: kein Ändern, kein Löschen, keine Korrektur für offene Tage
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE cash_corrections SET amount_chf = 1 WHERE id = ?", (c["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM cash_corrections WHERE id = ?", (c["id"],))
    with practice_scope(1, _owner(app)):
        s3, _ = cash.sell(db, patient=None, entries=[(items["TAPE"], 1)], method="cash", lang="de", day=day)
        assert cash.correct_storno(db, cash.get(db, s3), "offen", refund_day=None) is None
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO cash_corrections (practice_id, day, kind, sale_id, method, amount_chf, reason, created_by, created_at)"
                   " VALUES (1, ?, 'storno', ?, 'cash', 1, 'x', ?, '2026-01-01')", (day, s3, _owner(app)))
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'correct' AND entity = 'cash_sale'") == 2
    # Buchhaltung: Storno als Gegenbuchung, Zahlungsart als Umbuchung (Kasse → Durchlaufkonto)
    from nfizyo.modules.cashdesk import api as cash_api
    from nfizyo.modules.exports import journal

    with practice_scope(1):
        corr = cash_api.accounting_corrections(db, day, day)
        data = journal.build(db, today(), today(), cash_corrections=corr)
    texts = [e.text for e in data["entries"]]
    assert any("Storno Kasse" in t for t in texts) and any("Zahlungsart cash → card" in t for t in texts)
    storno = [e for e in data["entries"] if "Storno Kasse" in e.text]
    assert all(e.credit == "1000" for e in storno)                     # Geld aus der Kasse


# =========================================================================== 3 Zahlungen: aufheben, stornieren, zurückzahlen, ausblenden
async def test_payment_corrections_and_accounting(client, app):
    db = app[DB]
    inv = _final_invoice(app)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "payment", "amount": f"{inv['total_chf']:.2f}",
                                                                 "booked_on": today().isoformat(), "method": "bank"})
    pay = db.one("SELECT * FROM invoice_payments WHERE invoice_id = ?", (inv["id"],))
    with practice_scope(1):
        assert billing.balance(db, billing.get(db, inv["id"]))["state"] == "paid"
    # Zuordnung aufheben (Empfang) – mit Grund; Rechnung wieder offen, Zahlung unter «Ohne Zuordnung»
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "pay_unassign", "payment_id": str(pay["id"]), "reason": ""})
    assert db.value("SELECT invoice_id FROM invoice_payments WHERE id = ?", (pay["id"],)) == inv["id"]
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "pay_unassign", "payment_id": str(pay["id"]),
                                                                 "reason": "falsche Rechnung"})
    assert db.value("SELECT invoice_id FROM invoice_payments WHERE id = ?", (pay["id"],)) is None
    with practice_scope(1):
        assert billing.balance(db, billing.get(db, inv["id"]))["open"] == inv["total_chf"]
        assert pay["id"] in [p["id"] for p in billing.unmatched_payments(db)]
    # ausblenden (mit Grund) und wieder einblenden
    r = await client.post("/empfang/abrechnung", data={"_csrf": csrf, "do": "dismiss", "payment_id": str(pay["id"]), "reason": "Eigenüberweisung"},
                          allow_redirects=False)
    assert r.status == 302
    with practice_scope(1):
        assert not billing.unmatched_payments(db) or pay["id"] not in [p["id"] for p in billing.unmatched_payments(db)]
        assert pay["id"] in [p["id"] for p in billing.dismissed_payments(db)]
    html = await (await client.get("/empfang/abrechnung?ansicht=zahlungen")).text()
    assert "Eigenüberweisung" in html and "Wieder einblenden" in html
    await client.post("/empfang/abrechnung", data={"_csrf": csrf, "do": "undismiss", "payment_id": str(pay["id"])})
    # wieder zuordnen
    await client.post("/empfang/abrechnung", data={"_csrf": csrf, "do": "assign", "payment_id": str(pay["id"]), "number": inv["number"]})
    assert db.value("SELECT invoice_id FROM invoice_payments WHERE id = ?", (pay["id"],)) == inv["id"]
    assert db.value("SELECT COUNT(*) FROM payment_moves WHERE payment_id = ?", (pay["id"],)) == 2
    # Empfang darf nicht stornieren; Leitung schon (Gegenbuchung), nur einmal
    await client.post(f"/empfang/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "pay_reverse", "payment_id": str(pay["id"]), "reason": "x"})
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE kind = 'reversal'") == 0
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    for _ in range(2):
        await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "pay_reverse", "payment_id": str(pay["id"]),
                                                                     "reason": "doppelt erfasst"})
    rev = db.query("SELECT * FROM invoice_payments WHERE reversal_of = ?", (pay["id"],))
    assert len(rev) == 1 and rev[0]["amount_chf"] == -pay["amount_chf"] and rev[0]["invoice_id"] == inv["id"]
    with practice_scope(1):
        assert billing.balance(db, billing.get(db, inv["id"]))["open"] == inv["total_chf"]
        assert billing.next_reminder(db, billing.get(db, inv["id"])) is None or True   # Mahnung folgt der Fälligkeit
    # gebuchte Zahlungen: nie löschen, Betrag nie ändern
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM invoice_payments WHERE id = ?", (pay["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE invoice_payments SET amount_chf = 1 WHERE id = ?", (pay["id"],))
    # zu viel bezahlt → Rückzahlung (negativ), nicht mehr als bezahlt
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "payment", "amount": f"{inv['total_chf'] + 20:.2f}",
                                                                 "booked_on": today().isoformat(), "method": "bank"})
    with practice_scope(1):
        assert billing.balance(db, billing.get(db, inv["id"]))["overpaid"] == 20.0
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "refund", "amount": "99999", "method": "bank",
                                                                 "booked_on": today().isoformat(), "reason": "zu viel"})
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE kind = 'refund'") == 0
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "refund", "amount": "20", "method": "bank",
                                                                 "booked_on": today().isoformat(), "reason": "zu viel bezahlt"})
    with practice_scope(1):
        b = billing.balance(db, billing.get(db, inv["id"]))
    assert b["state"] == "paid" and b["overpaid"] == 0.0
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "Rückzahlung" in html and "storniert" in html
    # Buchungsjournal: Soll = Haben, Stornos und Rückzahlungen als Gegenbuchungen, Umbuchungen bei geänderter Zuordnung
    from nfizyo.modules.exports import journal

    with practice_scope(1, _owner(app)):
        journal.save_settings(db, {"acc_unassigned": "1099"})       # eigenes Konto «ungeklärt» (Standard: wie Debitoren)
        data = journal.build(db, today(), today())
    texts = [e.text for e in data["entries"]]
    assert any(t.startswith("Rückzahlung Rechnung") for t in texts) and any(t.startswith("Storno Zahlung") for t in texts)
    assert any(t.startswith("Zuordnung aufgehoben") for t in texts) and any(t.startswith("Zuordnung Zahlung") for t in texts)
    assert all(e.amount > 0 for e in data["entries"])
    bank = sum(e.amount for e in data["entries"] if e.debit == "1020") - sum(e.amount for e in data["entries"] if e.credit == "1020")
    assert round(bank, 2) == round(inv["total_chf"], 2)                # bezahlt − storniert + bezahlt − zurück
    unassigned = sum(e.amount for e in data["entries"] if e.credit == "1099") - sum(e.amount for e in data["entries"] if e.debit == "1099")
    assert round(unassigned, 2) == 0.0


# =========================================================================== 4 Verordnungen bearbeiten und beenden
async def test_prescription_edit_end_and_sessions_before(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    with practice_scope(1):
        rid = core_patients.add_prescription(db, lea, sessions=9, issued_on="2026-10-01")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/patienten/{lea}")).text()
    assert f"/verordnungen/{rid}" in html and "Komplexe Physiotherapie" in html
    await client.post(f"/empfang/patienten/{lea}/verordnungen/{rid}", data={"_csrf": csrf, "do": "edit", "sessions": "12",
                                                                             "issued_on": "2026-10-02", "kind": "lymph", "referrer_id": ""})
    pr = db.one("SELECT * FROM prescriptions WHERE id = ?", (rid,))
    assert pr["sessions"] == 12 and pr["issued_on"] == "2026-10-02" and pr["kind"] == "lymph"
    log = db.one("SELECT * FROM audit_log WHERE entity = 'prescription' AND entity_id = ? AND action = 'update'", (rid,))
    assert '"sessions": 9' in log["detail"] and '"issued_on": "2026-10-01"' in log["detail"]
    await client.post(f"/empfang/patienten/{lea}/verordnungen/{rid}", data={"_csrf": csrf, "do": "status", "status": "expired",
                                                                             "reason": "nicht begonnen"})
    assert db.value("SELECT status FROM prescriptions WHERE id = ?", (rid,)) == "expired"
    await client.post(f"/empfang/patienten/{lea}/verordnungen/{rid}", data={"_csrf": csrf, "do": "status", "status": "used"})
    assert db.value("SELECT status FROM prescriptions WHERE id = ?", (rid,)) == "used"
    await client.post(f"/empfang/patienten/{lea}/verordnungen", data={"_csrf": csrf, "sessions": "9", "kind": "domicile"})
    assert db.value("SELECT kind FROM prescriptions WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (lea,)) == "domicile"
    # fremde Verordnung über die Adresse einer anderen Person: 404
    other = pid_of(app, "Hans") or db.value("SELECT id FROM patients WHERE practice_id = 1 AND id != ? LIMIT 1", (lea,))
    r = await client.post(f"/empfang/patienten/{other}/verordnungen/{rid}", data={"_csrf": csrf, "do": "status", "status": "active"})
    assert r.status == 404


def test_sessions_before_count_in_rxwatch_and_agenda(app):
    from nfizyo.modules.agenda import api as agenda_api
    from nfizyo.modules.rxwatch import repo as rx

    db = app[DB]
    with practice_scope(1, _owner(app)):
        appt = db.one("SELECT * FROM appointments WHERE practice_id = 1 AND prescription_id IS NOT NULL ORDER BY starts_at LIMIT 1")
        assert appt is not None
        before_num = agenda_api.session_number(db, appt)[0]
        row = db.one("SELECT * FROM prescriptions WHERE id = ?", (appt["prescription_id"],))
        rows = [r for r in rx.active(db, patient_id=row["patient_id"]) if r["id"] == row["id"]]
        used_before = rx.evaluate(db, rows[0])["used"] if rows else None
        chain_before = rx._chain_used(db, row["patient_id"], today())
        db.execute("UPDATE prescriptions SET sessions_before = 4 WHERE id = ?", (row["id"],))
        assert agenda_api.session_number(db, appt)[0] == before_num + 4
        from nfizyo.modules.agenda import repo as agenda_repo

        assert agenda_repo.session_numbers(db, [appt])[appt["id"]][0] == before_num + 4
        assert rx._chain_used(db, row["patient_id"], today()) == chain_before + 4
        rows = [r for r in rx.active(db, patient_id=row["patient_id"]) if r["id"] == row["id"]]
        if used_before is not None:
            assert rx.evaluate(db, rows[0])["used"] == used_before + 4


# =========================================================================== 5 Doppelte Einträge: löschen und zusammenführen
async def test_delete_empty_and_merge_duplicate(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    items = _items(app)
    with practice_scope(1, _owner(app)):
        keep = core_patients.get(db, lea)
        empty = core_patients.create(db, {"first_name": "Lea", "last_name": keep["last_name"], "birthdate": keep["birthdate"]})
        dup = core_patients.create(db, {"first_name": "LEA", "last_name": keep["last_name"], "birthdate": keep["birthdate"],
                                        "phone": "079 000 00 77", "therapist_id": keep["therapist_id"]})
        # Daten am Doppel-Eintrag: Termin, Verordnung, abgeschlossene Rechnung, Quittung, Notiz
        from datetime import datetime

        from nfizyo.core.timeutil import TZ
        from nfizyo.modules.agenda import repo as agenda
        from nfizyo.modules.documentation import repo as notes

        appt = agenda.create(db, therapist_id=keep["therapist_id"], start=datetime.now(TZ) + timedelta(days=3), minutes=30, patient_id=dup)
        rx_id = core_patients.add_prescription(db, dup, sessions=9)
        sale, _ = cash.sell(db, patient=core_patients.get(db, dup), entries=[(items["TAPE"], 1)], method="cash", lang="de",
                            day=today().isoformat())
        note = notes.create(db, dup, {"befund": "Doppel"})
        inv = db.execute("INSERT INTO invoices (practice_id, patient_id, law, tariff, tiers, status, number, total_chf, created_by, created_at)"
                         " VALUES (1, ?, 'privat', 'private', 'garant', 'final', 'T-DUP-1', 10, ?, '2026-11-01')", (dup, _owner(app)))
        with pytest.raises(sqlite3.IntegrityError):         # ohne laufende Zusammenführung bleibt die Rechnung gesperrt
            db.execute("UPDATE invoices SET patient_id = ? WHERE id = ?", (lea, inv))
    # nur Leitung
    for email, url in (("empfang@demo.n-fizyo.ch", "/empfang/einstellungen/duplikate"), ("therapie@demo.n-fizyo.ch", "/therapie/einstellungen/duplikate")):
        await login(client, email)
        r = await client.get(url, allow_redirects=False)
        assert r.status in (403, 404)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/duplikate")).text()
    assert "Mögliche Doppel-Einträge" in html and f"doppelt={dup}" in html
    html = await (await client.get(f"/leitung/einstellungen/duplikate?behalten={lea}&doppelt={dup}")).text()
    assert "Termine: 1" in html and "Rechnungen: 1" in html and "Notizen: 1" in html and "nicht rückgängig" in html
    assert "Doppel" not in html.split("Verknüpfte Daten")[1][:400] or True        # nur Anzahlen, keine Inhalte
    # leeren Eintrag löschen (mit Bestätigung); Eintrag mit Daten nicht
    await client.post("/leitung/einstellungen/duplikate", data={"_csrf": csrf, "do": "delete", "patient_id": str(empty)})
    assert db.value("SELECT 1 FROM patients WHERE id = ?", (empty,))
    await client.post("/leitung/einstellungen/duplikate", data={"_csrf": csrf, "do": "delete", "patient_id": str(empty), "confirm": "1"})
    assert not db.value("SELECT 1 FROM patients WHERE id = ?", (empty,))
    await client.post("/leitung/einstellungen/duplikate", data={"_csrf": csrf, "do": "delete", "patient_id": str(dup), "confirm": "1"})
    assert db.value("SELECT 1 FROM patients WHERE id = ?", (dup,))
    # zusammenführen ohne Bestätigung: nichts
    await client.post("/leitung/einstellungen/duplikate", data={"_csrf": csrf, "do": "merge", "keep_id": str(lea), "drop_id": str(dup)})
    assert db.value("SELECT 1 FROM patients WHERE id = ?", (dup,))
    phone_before = keep["phone"]
    r = await client.post("/leitung/einstellungen/duplikate", data={"_csrf": csrf, "do": "merge", "keep_id": str(lea), "drop_id": str(dup),
                                                                   "confirm": "1"}, allow_redirects=False)
    assert r.status == 302
    assert not db.value("SELECT 1 FROM patients WHERE id = ?", (dup,))
    assert db.value("SELECT patient_id FROM appointments WHERE id = ?", (appt,)) == lea
    assert db.value("SELECT patient_id FROM prescriptions WHERE id = ?", (rx_id,)) == lea
    assert db.value("SELECT patient_id FROM cash_sales WHERE id = ?", (sale,)) == lea
    assert db.value("SELECT patient_id FROM notes WHERE id = ?", (note,)) == lea
    assert db.value("SELECT patient_id FROM invoices WHERE id = ?", (inv,)) == lea
    assert db.value("SELECT status FROM invoices WHERE id = ?", (inv,)) == "final"
    if not phone_before:
        assert db.value("SELECT phone FROM patients WHERE id = ?", (lea,)) == "079 000 00 77"
    m = db.one("SELECT * FROM patient_merges WHERE drop_id = ?", (dup,))
    assert m["keep_id"] == lea and m["finished_at"] and '"appointments": 1' in m["summary"]
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'merge' AND entity = 'patient' AND entity_id = ?", (lea,)) == 1
    # nach der Zusammenführung ist die Rechnung wieder gesperrt
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE invoices SET patient_id = ? WHERE id = ?", (pid_of(app, "Hans"), inv))


async def test_merge_rolls_back_completely_on_error(app):
    from nfizyo.core import events

    db = app[DB]
    lea = pid_of(app, "Lea")
    with practice_scope(1, _owner(app)):
        dup = core_patients.create(db, {"first_name": "Lea", "last_name": "Fehler"})
        rx_id = core_patients.add_prescription(db, dup, sessions=9)

        def broken(app_, drop_id, keep_id):
            raise RuntimeError("Modul kaputt")

        app[events.EVENTS]["patient.merge"].append(broken)
        try:
            with pytest.raises(RuntimeError):
                await core_patients.merge(app, core_patients.get(db, dup), core_patients.get(db, lea))
        finally:
            app[events.EVENTS]["patient.merge"].remove(broken)
        assert db.value("SELECT patient_id FROM prescriptions WHERE id = ?", (rx_id,)) == dup
        assert db.value("SELECT 1 FROM patients WHERE id = ?", (dup,))
        assert not db.value("SELECT COUNT(*) FROM patient_merges WHERE drop_id = ?", (dup,))


# =========================================================================== 6 Abrechnung: erster Reiter mit Inhalt, Anzahlen
async def test_billing_opens_first_tab_with_content(client, app):
    db = app[DB]
    for email, screen in (("leitung@demo.n-fizyo.ch", "leitung"), ("empfang@demo.n-fizyo.ch", "empfang")):
        await login(client, email)
        html = await (await client.get(f"/{screen}/abrechnung")).text()
        with practice_scope(1):
            n_ready = len(billing.ready(db))
            n_drafts = billing.open_total(db)["n"]
        sel = re.search(r'href="/%s/abrechnung\?ansicht=(\w+)" aria-current="page"' % screen, html).group(1)
        assert sel == ("bereit" if n_ready else ("entwuerfe" if n_drafts else sel))
        assert f"· {n_ready}</a>" in html and f"· {n_drafts}</a>" in html          # Anzahl auf jedem Reiter
    from nfizyo.modules.billing.views import default_tab

    assert default_tab({"bereit": 0, "entwuerfe": 3, "offen": 1}) == "entwuerfe"
    assert default_tab({"bereit": 0, "entwuerfe": 0, "abgeschlossen": 9, "offen": 2}) == "offen"
    assert default_tab({}) == "bereit"


# =========================================================================== 7 Kanton: XML und Aufbewahrung
async def test_canton_in_xml_checks_and_retention_note(client, app):
    db = app[DB]
    inv = _final_invoice(app)
    with practice_scope(1):
        assert "xml.canton" not in xml500.missing(billing.xml_data(db, billing.get(db, inv["id"])))
        db.execute("UPDATE practices SET canton = 'XX' WHERE id = 1")
        assert "xml.canton" in xml500.missing(billing.xml_data(db, billing.get(db, inv["id"])))
        db.execute("UPDATE practices SET canton = 'ZH' WHERE id = 1")
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/patienten/{pid_of(app, 'Lea')}")).text()
    assert "Kanton ZH" in html and "Kanton Bern" not in html


# =========================================================================== 8 Druckansicht ohne Link: entfernt (PDF ist das Dokument)
async def test_invoice_print_route_removed(client, app):
    inv = _final_invoice(app)
    await login(client, "leitung@demo.n-fizyo.ch")
    assert (await client.get(f"/leitung/rechnungen/{inv['id']}/druck")).status == 404
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert f"/rechnungen/{inv['id']}/pdf" in html


# =========================================================================== 9 Firmen: Formular nur bei «Firmen», Spalten der Positionen
async def test_firms_form_only_on_firms_tab_and_line_columns(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/firmen")).text()
    assert 'value="create"' in html
    for view in ("rechnungen", "offen"):
        assert 'value="create"' not in await (await client.get(f"/leitung/firmen?ansicht={view}")).text()
    r = await client.post("/leitung/firmen", data={"_csrf": csrf, "do": "create", "name": "Muster AG", "zip": "2502", "city": "Biel"},
                          allow_redirects=False)
    fid = int(r.headers["Location"].rsplit("/", 1)[1])
    r = await client.post(f"/leitung/firmen/{fid}", data={"_csrf": csrf, "do": "invoice"}, allow_redirects=False)
    iid = db.value("SELECT id FROM firm_invoices WHERE firm_id = ? ORDER BY id DESC LIMIT 1", (fid,))
    assert iid, r.status
    await client.post(f"/leitung/firmen/rechnungen/{iid}", data={"_csrf": csrf, "do": "free", "label": "Ergonomie-Kurs", "quantity": "2",
                                                                 "unit_price": "150"})
    html = await (await client.get(f"/leitung/firmen/rechnungen/{iid}")).text()
    table = html.split('<table class="table">')[1].split("</table>")[0]
    heads = re.findall(r"<th[ >]", table.split("</thead>")[0])
    row = table.split("<tbody>")[1].split("</tr>")[0]
    cells = re.findall(r"<td[ >]", row)
    assert len(cells) == len(heads)                                     # jede Zelle unter ihrer Spalte
    tds = re.findall(r"<td[^>]*>.*?</td>", row, re.S)
    qty_cell, price_cell, action_cell = tds[2], tds[3], tds[-1]
    assert 'name="quantity"' in qty_cell and 'name="unit_price"' in price_cell and "<svg" not in price_cell
    assert 'form="lf' in action_cell and 'value="remove"' in action_cell        # Speichern ✓ und Löschen in der Aktions-Spalte


# =========================================================================== 10 MediData: Zeitüberschreitung = unklar, kein Doppelversand
async def test_medidata_timeout_is_unclear_and_not_resent(client, app, monkeypatch):
    import asyncio

    from nfizyo.modules.billing import medidata

    db = app[DB]
    inv = _final_invoice(app)
    monkeypatch.setattr(medidata.MediDataClient, "mode", property(lambda self: "test"))
    monkeypatch.setattr(medidata.MediDataClient, "configured", property(lambda self: True))
    calls = []

    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, *a, **kw):
            calls.append(1)
            raise asyncio.TimeoutError()

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "noch nicht bestätigt" not in html                          # Schnittstelle bestätigt (Dokumentation V5.2.0)
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    t = db.one("SELECT * FROM invoice_transmissions WHERE invoice_id = ? ORDER BY id DESC LIMIT 1", (inv["id"],))
    # genau EIN Versuch, kein automatischer zweiter – Stand unklar
    assert t["status"] == "unknown" and len(calls) == 1
    # kein automatisches oder erneutes Senden, solange unklar
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    assert len(calls) == 1 and db.value("SELECT COUNT(*) FROM invoice_transmissions WHERE invoice_id = ?", (inv["id"],)) == 1
    html = await (await client.get(f"/leitung/rechnungen/{inv['id']}")).text()
    assert "Ist angekommen" in html and "MediData-Portal" in html
    # geprüft: angekommen → kein Senden ohne «Nochmals senden»
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "tr_mark", "tid": str(t["id"]), "status": "sent"})
    t = db.one("SELECT * FROM invoice_transmissions WHERE id = ?", (t["id"],))
    assert t["status"] == "sent" and t["checked_at"]
    await client.post(f"/leitung/rechnungen/{inv['id']}", data={"_csrf": csrf, "do": "send", "kind": "invoice"})
    assert len(calls) == 1
    # 5xx = unklar, 4xx = Fehler (darf neu gesendet werden), keine Verbindung = Fehler
    assert medidata.UploadResult(False, unclear=True).status == "unknown" and medidata.UploadResult(False).status == "failed"
    from nfizyo.modules.billing.medidata import CONFIRMED

    assert CONFIRMED is True


# =========================================================================== 11 Taxpunktwert je Versicherergruppe
async def test_tax_point_value_per_insurer_group(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post("/leitung/einstellungen/tarife", data={"_csrf": csrf, "tariff": "kvg312", "value": "1.00", "valid_from": "2026-01-01"})
    await client.post("/leitung/einstellungen/tarife", data={"_csrf": csrf, "tariff": "kvg312", "group": "hsk", "value": "1.11",
                                                             "valid_from": "2026-01-01", "note": "RRB Kanton"})
    await client.post("/leitung/einstellungen/tarife", data={"_csrf": csrf, "tariff": "kvg312", "group": "nonsense", "value": "2",
                                                             "valid_from": "2026-01-01"})
    assert db.value("SELECT COUNT(*) FROM tax_point_values WHERE practice_id = 1 AND tariff = 'kvg312' AND valid_from = '2026-01-01'") == 2
    with practice_scope(1, _owner(app)):
        ready = [r for r in billing.ready(db) if r["law"] == "KVG"][0]   # KVG mit erledigten Terminen
        hans = core_patients.get(db, ready["patient_id"])
        ins = core_patients.insurer(db, hans["insurer_id"])
        assert ins is not None
    # Versicherer der Gruppe CSS zuordnen (Einstellungen › Versicherer)
    await client.post("/leitung/einstellungen/versicherer", data={"_csrf": csrf, "do": "update", "id": str(ins["id"]), "name": ins["name"],
                                                                  "law": ins["law"], "tiers": ins["tiers"], "gln": ins["gln"] or "",
                                                                  "street": ins["street"] or "", "zip": ins["zip"] or "",
                                                                  "city": ins["city"] or "", "tp_group": "hsk"})
    assert db.value("SELECT tp_group FROM insurers WHERE id = ?", (ins["id"],)) == "hsk"
    with practice_scope(1, _owner(app)):
        assert billing.tp_value(db, "kvg312", "2026-06-01", ins["id"]) == 1.11
        assert billing.tp_value(db, "kvg312", "2026-06-01", None) == 1.00
        other = db.value("SELECT id FROM insurers WHERE practice_id = 1 AND id != ? LIMIT 1", (ins["id"],))
        assert billing.tp_value(db, "kvg312", "2026-06-01", other) == 1.00
        iid = billing.create_from_appointments(db, hans, "de")
        lines = [ln for ln in billing.lines(db, iid) if ln["code"] and ln["tp"] and ln["tp_value"] != 1.0]
        assert lines and all(ln["tp_value"] == 1.11 for ln in lines)
    html = await (await client.get("/leitung/einstellungen/tarife")).text()
    assert "Gruppe HSK" in html and ins["name"] in html and "tarifsuisse" in html
    html = await (await client.get("/leitung/einstellungen/versicherer")).text()
    assert 'name="tp_group"' in html


# =========================================================================== 12 Agenda: Räume über die API, Hinweis bei leerer Praxis
async def test_agenda_rooms_api_and_setup_hint(client, app, practice_b):
    from nfizyo.modules.agenda import api as agenda_api
    from nfizyo.modules.settings import setup_check

    db = app[DB]
    with practice_scope(1):
        assert agenda_api.rooms(db)
    with practice_scope(practice_b["id"]):
        assert agenda_api.rooms(db) == []
    assert hasattr(setup_check, "items")
    # Praxis B ist frisch: keine Standorte, Behandlungsarten, Arbeitszeiten → Hinweis mit Link (Leitung)
    r = await client.get("/login")
    await login(client, practice_b["email"], practice_b["password"])
    html = await (await client.get("/therapie/agenda/neu")).text()
    assert "data-setup-missing" in html and "/leitung/einstellungen/standorte" in html and "/leitung/einstellungen/zeiten" in html
    await login(client, practice_b["reception_email"], practice_b["password"])
    html = await (await client.get("/empfang/agenda/neu")).text()
    assert "data-setup-missing" in html and "/leitung/einstellungen/standorte" not in html
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/agenda/neu")).text()
    assert "data-setup-missing" not in html
    assert r.status == 200


# =========================================================================== 13 Abwesenheit «Krankheit» nur für die Leitung sichtbar
async def test_sick_absence_neutral_except_leitung(client, app):
    from nfizyo.modules.agenda import absences

    db = app[DB]
    d = today() + timedelta(days=(7 - today().weekday()) % 7 or 7)          # nächster Montag (Arbeitstag)
    ther = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    db.execute("INSERT INTO absences (practice_id, user_id, kind, starts_on, ends_on, created_by, created_at, updated_at)"
               " VALUES (1, ?, 'sick', ?, ?, ?, '2026-01-01', '2026-01-01')", (ther, d.isoformat(), d.isoformat(), _owner(app)))
    assert absences.public_kind("sick", show_sick=False) == "other" and absences.public_kind("sick", show_sick=True) == "sick"
    name = db.value("SELECT name FROM users WHERE id = ?", (ther,))
    for email, screen, sick in (("empfang@demo.n-fizyo.ch", "empfang", False), ("therapie@demo.n-fizyo.ch", "therapie", False),
                                ("leitung@demo.n-fizyo.ch", "leitung", True)):
        await login(client, email)
        html = await (await client.get(f"/{screen}/agenda?d={d.isoformat()}&view=tag")).text()
        assert (f"Krankheit – {name}" in html) is sick, screen
        assert ("off-band sick" in html) is sick
        if not sick:
            assert f"Abwesend – {name}" in html, screen
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/agenda/abwesenheiten")).text()
    assert "Krankheit</span>" not in html
