"""QA 0.9.0, Bereich B (Abrechnung, Kasse, Export, Firmen, Kostengutsprache) – Regressionstests zu den Befunden
CODE-04/05/06/13, F05/06/07/08/09/10/16/17/19/22/24 und SEC-03."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from conftest import login, pid_of

from nfizyo.core import money
from nfizyo.core import patients as core_patients
from nfizyo.core.db import utcnow
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import today
from nfizyo.modules.billing import api as billing_api
from nfizyo.modules.billing import private
from nfizyo.modules.billing import repo as billing
from nfizyo.modules.cashdesk import api as cash_api
from nfizyo.modules.cashdesk import repo as cash
from nfizyo.modules.exports import journal

LEITUNG, EMPFANG = "leitung@demo.n-fizyo.ch", "empfang@demo.n-fizyo.ch"


def _owner(app) -> int:
    return app[DB].value("SELECT id FROM users WHERE email = ?", (LEITUNG,))


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return " ".join(" ".join((p.extract_text() or "").split()) for p in PdfReader(BytesIO(data)).pages)


def _final_kvg(app, first_name: str = "Sandra", tpw: float | None = 1.11) -> dict:
    """Abgeschlossene KVG-Rechnung (Tiers garant). TPW 1.11: Sandra CHF 186.48, Ayşe CHF 79.92 (Demo-Daten)."""
    db = app[DB]
    with practice_scope(1, _owner(app)):
        if tpw:
            billing.set_tp_value(db, "kvg312", tpw, "2018-01-01")
        if first_name == "Sandra" and today().weekday() >= 5:
            # Demo am Wochenende: Sandras Termin «heute» liegt auf Montag (gebucht) → wie unter der Woche erledigt, zwei Tage zurück
            a = db.one("SELECT id, starts_at, ends_at FROM appointments WHERE patient_id = ? AND status = 'booked' ORDER BY starts_at LIMIT 1",
                       (pid_of(app, "Sandra"),))
            if a is not None:
                from datetime import datetime as _dt
                shift = _dt.fromisoformat(a["starts_at"]).date() - (today() - timedelta(days=2))
                db.execute("UPDATE appointments SET status = 'done', starts_at = ?, ends_at = ? WHERE id = ?",
                           ((_dt.fromisoformat(a["starts_at"]) - shift).isoformat(), (_dt.fromisoformat(a["ends_at"]) - shift).isoformat(), a["id"]))
        iid = billing.create_from_appointments(db, core_patients.get(db, pid_of(app, first_name)), "de")
        billing.finalize(db, billing.get(db, iid))
        return dict(billing.get(db, iid))


def _item(db, price: float, code: str = "QA-1", kind: str = "product") -> dict:
    iid = private.save_item(db, {"kind": kind, "code": code, "name_de": f"Testartikel {code}", "name_fr": f"Article {code}",
                                 "price_chf": price, "vat_rate": 0.0})
    return private.item(db, iid)


# --------------------------------------------------------------------------- F09 / CODE-06: Geld-Helfer
def test_money_helper_commercial_and_cash_rounding():
    assert money.rp(2.675) == 2.68 and money.rp(-2.675) == -2.68 and money.rp(0.1 + 0.2) == 0.3
    assert money.times(1.5, 180.35) == 270.53 and money.times(1, 48, 1.11, 1.0) == 53.28
    assert money.vat_in(108.10, 8.1) == 8.10 and money.vat_in(10, 0) == 0.0
    assert [money.cash(x) for x in (186.48, 186.47, 186.42, 186.43, 0.025, 0.075)] == [186.5, 186.45, 186.4, 186.45, 0.05, 0.1]
    assert money.cash_settle(186.48, 186.50) == (186.48, 186.5, 0.02)       # bezahlt, +2 Rappen Rundung
    assert money.cash_settle(186.48, 186.45) == (186.48, 186.45, -0.03)     # weniger als 5 Rappen offen → bezahlt
    assert money.cash_settle(186.48, 100) == (100.0, 100.0, 0.0)            # Teilzahlung
    assert money.cash_settle(-10.02, -10.0) == (-10.02, -10.0, 0.02)        # Rückzahlung bar
    for bad in ("1e999", "nan", "9" * 5000, "", None, "-1", "abc"):
        assert money.parse(bad) is None
    assert money.parse("1'290,50") == 1290.5 and money.parse("1’290.50") == 1290.5


def test_f09_firm_line_rounds_half_up(app):
    from nfizyo.modules.firms import repo as firms

    db = app[DB]
    with practice_scope(1, _owner(app)):
        fid = db.value("SELECT id FROM firms WHERE practice_id = 1 LIMIT 1")
        inv = firms.get(db, firms.create(db, firms.get_firm(db, fid)))
        firms.add_free_line(db, inv, label="Workshop Ergonomie", quantity=1.5, unit_price=180.35, vat_rate=0.0, kind="service")
        assert [ln["amount_chf"] for ln in firms.lines(db, inv["id"])] == [270.53]
        assert firms.get(db, inv["id"])["total_chf"] == 270.53


def test_f09_tariff_price_and_private_lines_use_half_up():
    assert billing.price(1.5, 180.35, 1.0, 1.0) == 270.53                 # float round() gäbe 270.52
    assert private.vat_in(2.675 * 108.1 / 8.1, 8.1) == money.vat_in(2.675 * 108.1 / 8.1, 8.1)


# --------------------------------------------------------------------------- CODE-04 / F07: abgeschlossener Kassentag
async def test_code04_f07_no_cash_payment_on_closed_cash_day(client, app):
    db = app[DB]
    inv = _final_kvg(app)
    day = today().isoformat()
    past = (today() - timedelta(days=20)).isoformat()
    with practice_scope(1, _owner(app)):
        cash.close_day(db, day, 0.0, None)
        cash.close_day(db, past, 0.0, None)
    csrf = await login(client, EMPFANG)
    url = f"/empfang/rechnungen/{inv['id']}"
    for booked in (day, past):                                             # heute und rückdatiert
        for method in ("cash", "twint", "card"):
            r = await client.post(url, data={"_csrf": csrf, "do": "payment", "amount": "80", "method": method, "booked_on": booked})
            assert "Kassentag ist schon abgeschlossen" in await r.text()
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE invoice_id = ?", (inv["id"],)) == 0
    with practice_scope(1):
        assert cash.day_summary(db, day)["by_method"]["cash"] == 0.0 and cash.day_summary(db, past)["cash"] == 0.0
    # Bank zählt nicht zur Kasse: geht auch an einem abgeschlossenen Tag
    r = await client.post(url, data={"_csrf": csrf, "do": "payment", "amount": "50", "method": "bank", "booked_on": day})
    assert "Zahlung erfasst" in await r.text() or db.value("SELECT COUNT(*) FROM invoice_payments WHERE invoice_id = ?", (inv["id"],)) == 1
    # Offener Tag (gestern): bar geht
    yesterday = (today() - timedelta(days=1)).isoformat()
    await client.post(url, data={"_csrf": csrf, "do": "payment", "amount": "10", "method": "cash", "booked_on": yesterday})
    pm = db.one("SELECT * FROM invoice_payments WHERE invoice_id = ? AND method = 'cash'", (inv["id"],))
    assert pm is not None and pm["booked_on"] == yesterday
    # Leitung: Rückzahlung bar / Storno einer Barzahlung nicht auf den abgeschlossenen heutigen Tag
    csrf = await login(client, LEITUNG)
    url = f"/leitung/rechnungen/{inv['id']}"
    r = await client.post(url, data={"_csrf": csrf, "do": "refund", "amount": "5", "method": "cash", "booked_on": day, "reason": "zu viel"})
    assert "Kassentag ist schon abgeschlossen" in await r.text()
    r = await client.post(url, data={"_csrf": csrf, "do": "pay_reverse", "payment_id": str(pm["id"]), "reason": "doppelt"})
    assert "Kassentag ist schon abgeschlossen" in await r.text()
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE invoice_id = ? AND kind != 'payment'", (inv["id"],)) == 0
    with practice_scope(1):
        assert cash.day_summary(db, day)["cash"] == 0.0


# --------------------------------------------------------------------------- CODE-05: Storno nach Korrektur der Zahlungsart
def test_code05_storno_after_method_correction_uses_current_method(app):
    db = app[DB]
    with practice_scope(1, _owner(app)):
        item = _item(db, 100.0)
        p = core_patients.get(db, pid_of(app, "Peter"))
        d0 = (today() - timedelta(days=3)).isoformat()
        sid, _ = cash.sell(db, patient=p, entries=[(item, 1)], method="cash", lang="de", day=d0)
        cash.close_day(db, d0, cash.day_summary(db, d0)["cash"], None)
        assert cash.correct_method(db, cash.get(db, sid), "card", "war Karte")
        td = today().isoformat()
        cid = cash.correct_storno(db, cash.get(db, sid), "Rückgabe", refund_day=td)
        corr = db.one("SELECT * FROM cash_corrections WHERE id = ?", (cid,))
        assert corr["method"] == "card"
        summary = cash.day_summary(db, td)
        assert summary["cash"] == 0.0 and summary["by_method"]["card"] == -100.0
        data = journal.build(db, date.fromisoformat(d0), today(), cash_sales=cash_api.accounting_sales(db, d0, td),
                             cash_corrections=cash_api.accounting_corrections(db, d0, td))
        bal: dict[str, float] = {}
        for e in data["entries"]:
            if e.doc.startswith(cash.get(db, sid)["number"]):
                bal[e.debit] = round(bal.get(e.debit, 0) + e.amount, 2)
                bal[e.credit] = round(bal.get(e.credit, 0) - e.amount, 2)
        acc = journal.settings(db)
        assert bal.get(acc["acc_cash"], 0) == 0 and bal.get(acc["acc_transit"], 0) == 0


# --------------------------------------------------------------------------- CODE-06: 5 Rappen bar
def test_code06_cash_at_desk_rounds_to_5_rappen_and_invoice_is_paid(app):
    db = app[DB]
    inv = _final_kvg(app)
    with practice_scope(1, _owner(app)):
        total = inv["total_chf"]
        assert money.cash(total) != total, f"Test braucht ein krummes Total, nicht {total}"
        day = today().isoformat()
        info = billing_api.open_invoice_by_number(db, inv["number"])
        assert info["cash_due"] == money.cash(total)
        pid = billing_api.pay_at_desk(db, inv["id"], info["cash_due"], "cash", day)
        pm = db.one("SELECT * FROM invoice_payments WHERE id = ?", (pid,))
        assert pm["amount_chf"] == total and pm["rounding_chf"] == money.rp(money.cash(total) - total)
        full = billing.get(db, inv["id"])
        assert billing.balance(db, full)["state"] == "paid" and billing.balance(db, full)["open"] == 0.0
        db.execute("UPDATE invoices SET due_on = '2026-01-01' WHERE id = ?", (inv["id"],))
        assert billing.next_reminder(db, billing.get(db, inv["id"])) is None
        assert cash.day_summary(db, day)["cash"] == money.cash(total)           # Kasse = Bargeld
        # Journal: Kasse bekommt das Bargeld, Debitoren die Rechnung, Rundung eigenes Konto (Standard: Ertrag Leistungen)
        data = journal.build(db, today(), today(), cash_sales=[], cash_corrections=[])
        acc = journal.settings(db)
        kasse = sum(e.amount for e in data["entries"] if e.debit == acc["acc_cash"] and e.src.startswith(f"pay:{pid}")) \
            - sum(e.amount for e in data["entries"] if e.credit == acc["acc_cash"] and e.src.startswith(f"pay:{pid}"))
        assert round(kasse, 2) == money.cash(total)
        assert any(e.src == f"pay:{pid}:rounding" and "Rundung 5 Rappen" in e.text for e in data["entries"])
        # Storno der Barzahlung: Rundung wird mit storniert
        rid = billing.reverse_payment(db, pid, "falsch")
        rv = db.one("SELECT * FROM invoice_payments WHERE id = ?", (rid,))
        assert rv["amount_chf"] == -total and rv["rounding_chf"] == -pm["rounding_chf"]
        assert cash.day_summary(db, day)["cash"] == 0.0


def test_code06_bank_stays_exact_and_tolerance_below_5_rappen(app):
    db = app[DB]
    inv = _final_kvg(app)                                                   # CHF 186.48
    with practice_scope(1, _owner(app)):
        assert inv["total_chf"] == 186.48
        billing.add_manual_payment(db, billing.get(db, inv["id"]), amount=186.45, booked_on=today().isoformat(), method="bank")
        assert billing.balance(db, billing.get(db, inv["id"]))["open"] == 0.03       # Bank: auf den Rappen genau
    inv2 = _final_kvg(app, "Ayşe")                                          # CHF 79.92 → bar 79.90
    with practice_scope(1, _owner(app)):
        assert inv2["total_chf"] == 79.92
        pid = billing.add_manual_payment(db, billing.get(db, inv2["id"]), amount=79.9, booked_on=today().isoformat(), method="cash")
        pm = db.one("SELECT * FROM invoice_payments WHERE id = ?", (pid,))
        assert (pm["amount_chf"], pm["rounding_chf"]) == (79.92, -0.02)
        assert billing.balance(db, billing.get(db, inv2["id"]))["state"] == "paid"
        # Teilzahlung bar bleibt Teilzahlung (keine Rundung)
    inv3 = _final_kvg(app, "Maria")
    with practice_scope(1, _owner(app)):
        pid = billing.add_manual_payment(db, billing.get(db, inv3["id"]), amount=50.02, booked_on=today().isoformat(), method="cash")
        pm = db.one("SELECT * FROM invoice_payments WHERE id = ?", (pid,))
        assert (pm["amount_chf"], pm["rounding_chf"]) == (50.0, 0.0)
        assert billing.balance(db, billing.get(db, inv3["id"]))["state"] in ("partial", "open")


async def test_code06_cash_sale_rounding_on_receipt_and_journal(client, app):
    db = app[DB]
    with practice_scope(1, _owner(app)):
        item = _item(db, 33.33)
        twint = _item(db, 33.33, "QA-2")
        sid, _ = cash.sell(db, patient=None, entries=[(item, 1)], method="cash", lang="de", day=today().isoformat(), given=50.0)
        sale = cash.get(db, sid)
        assert sale["total_chf"] == 33.35 and sale["rounding_chf"] == 0.02
        tid, _ = cash.sell(db, patient=None, entries=[(twint, 1)], method="twint", lang="de", day=today().isoformat())
        assert cash.get(db, tid)["total_chf"] == 33.33 and cash.get(db, tid)["rounding_chf"] == 0.0
        assert cash.day_summary(db, today().isoformat())["cash"] == 33.35
        assert cash.totals(db, [(item, 1)], 50.0, "cash") | {} == {"total": 33.35, "items_total": 33.33, "rounding": 0.02, "vat": 0.0,
                                                                  "given": 50.0, "change": 16.65}
        rows = cash_api.accounting_sales(db, today().isoformat(), today().isoformat())
        data = journal.build(db, today(), today(), cash_sales=rows, cash_corrections=[])
        mine = [e for e in data["entries"] if e.src.startswith(f"sale:{sid}:")]
        assert sum(e.amount for e in mine if e.debit == journal.settings(db)["acc_cash"]) == 33.35
        assert any(e.src == f"sale:{sid}:rounding" for e in mine)
    await login(client, EMPFANG)
    html = await (await client.get(f"/empfang/kasse/belege/{sid}")).text()
    assert "Rundung auf 5 Rappen" in html and "+0.02" in html
    text = _pdf_text(await (await client.get(f"/empfang/kasse/belege/{sid}/pdf")).read())
    assert "Rundung auf 5 Rappen" in text and "33.35" in text and "33.33" in text


# --------------------------------------------------------------------------- CODE-13: absurde Zahlen an der Kasse
async def test_code13_kasse_absurd_numbers_are_form_errors(client, app):
    csrf = await login(client, EMPFANG)
    db = app[DB]
    with practice_scope(1):
        item_ids = [r["id"] for r in db.query("SELECT id FROM price_items WHERE practice_id = 1 AND active = 1 AND kind = 'product'")]
    for qty, given in (("1e999", ""), ("9" * 40, ""), ("9" * 5000, ""), ("1", "1e999"), ("1", "9" * 5000), ("nan", "nan")):
        data = {"_csrf": csrf, "do": "sell", "method": "cash", "given": given, **{f"qty_{i}": qty for i in item_ids}}
        for do in ("sell", "preview"):
            r = await client.post("/empfang/kasse", data={**data, "do": do})
            assert r.status < 500, (qty, given, do)
    for field, value in (("sub_id", "9" * 5000), ("amount", "1e999"), ("counted", "1e999")):
        for do in ("visit", "pay_invoice", "close"):
            r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": do, field: value, "number": "2026-0001", "method": "cash"})
            assert r.status < 500, (field, do)
    assert db.value("SELECT COUNT(*) FROM cash_sales WHERE practice_id = 1") is not None


async def test_code13_kostengutsprache_huge_numbers_are_form_errors(client, app):
    csrf = await login(client, EMPFANG)
    data = {"_csrf": csrf, "do": "add", "patient_id": "1", "requested_on": "x" * 40, "sessions_requested": "9" * 40,
            "reference": "x" * 40, "note": "x" * 40}
    r = await client.post("/empfang/abrechnung/kostengutsprache", data=data, allow_redirects=False)
    assert r.status < 500, await r.text()
    for field in ("sessions_requested", "patient_id"):
        r = await client.post("/empfang/abrechnung/kostengutsprache", data={**data, field: "9" * 5000}, allow_redirects=False)
        assert r.status < 500, field
    r = await client.post("/empfang/abrechnung/kostengutsprache", data={**data, "do": "decide", "approval_id": "9" * 5000,
                                                                       "sessions_approved": "9" * 5000}, allow_redirects=False)
    assert r.status < 500


# --------------------------------------------------------------------------- F05: Mitteilung aus der Bankdatei
def _camt_ustrd(amount: float, text: str, bank_ref: str = "U1") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.08"><BkToCstmrDbtCdtNtfctn><GrpHdr><MsgId>M9</MsgId></GrpHdr>
<Ntfctn><Id>N9</Id><Ntry><Amt Ccy="CHF">{amount:.2f}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
<BookgDt><Dt>{today().isoformat()}</Dt></BookgDt><AcctSvcrRef>E9</AcctSvcrRef><NtryDtls><TxDtls><Refs><AcctSvcrRef>{bank_ref}</AcctSvcrRef></Refs>
<Amt Ccy="CHF">{amount:.2f}</Amt><CdtDbtInd>CRDT</CdtDbtInd><RltdPties><Dbtr><Pty><Nm>Unbekannt AG</Nm></Pty></Dbtr></RltdPties>
<RmtInf><Ustrd>{text}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry></Ntfctn></BkToCstmrDbtCdtNtfctn></Document>""".encode()


async def test_f05_camt_keeps_free_text_and_suggests_invoice(client, app):
    from nfizyo.modules.billing import camt

    inv = _final_kvg(app)
    data = _camt_ustrd(50.0, f"Rechnung {inv['number']} Zahlung")
    assert camt.parse(data)[0].message == f"Rechnung {inv['number']} Zahlung"
    db = app[DB]
    with practice_scope(1, _owner(app)):
        res = billing.import_payments(db, "c1.xml", data)
        assert res["unmatched"] == 1
        row = billing.unmatched_payments(db)[0]
        assert row["message"] == f"Rechnung {inv['number']} Zahlung" and row["suggestion"] == inv["number"]
    await login(client, EMPFANG)
    html = await (await client.get("/empfang/abrechnung?ansicht=zahlungen")).text()
    assert f"Rechnung {inv['number']} Zahlung" in html and f'value="{inv["number"]}"' in html


# --------------------------------------------------------------------------- F06: XML-Datum in Schweizer Zeit
def test_f06_xml_request_date_is_zurich_local_with_offset(app):
    from lxml import etree

    from nfizyo.modules.billing import xml500

    inv = _final_kvg(app, "Lea", tpw=None)                                   # UVG Tiers payant
    db = app[DB]
    with practice_scope(1, _owner(app)):
        data = billing.xml_data(db, billing.get(db, inv["id"]))
        data.finalized_at = "2026-11-11T23:30:00+00:00"                     # 00:30 am 12.11. in Zürich
        xml, _ = xml500.build(data, modus="test")
    el = etree.fromstring(xml).find(".//{http://www.forum-datenaustausch.ch/invoice}invoice")
    assert el.get("request_date") == "2026-11-12T00:30:00+01:00"
    assert int(el.get("request_timestamp")) == int(datetime(2026, 11, 11, 23, 30, tzinfo=timezone.utc).timestamp())


# --------------------------------------------------------------------------- F08: Quittung nach Korrektur
async def test_f08_receipt_pdf_shows_corrections_after_close(client, app):
    db = app[DB]
    with practice_scope(1, _owner(app)):
        item = _item(db, 60.70)
        d0 = (today() - timedelta(days=2)).isoformat()
        s1, _ = cash.sell(db, patient=None, entries=[(item, 1)], method="cash", lang="de", day=d0)
        s2, _ = cash.sell(db, patient=None, entries=[(item, 1)], method="card", lang="de", day=d0)
        cash.close_day(db, d0, cash.day_summary(db, d0)["cash"], None)
        assert cash.correct_storno(db, cash.get(db, s1), "Falsch erfasst", refund_day=None)
        assert cash.correct_method(db, cash.get(db, s2), "twint", "war Twint")
    await login(client, LEITUNG)
    t1 = _pdf_text(await (await client.get(f"/leitung/kasse/belege/{s1}/pdf")).read())
    assert "STORNIERT nach dem Tagesabschluss" in t1 and "Falsch erfasst" in t1 and "dankend erhalten" not in t1
    t2 = _pdf_text(await (await client.get(f"/leitung/kasse/belege/{s2}/pdf")).read())
    assert "Zahlungsart korrigiert" in t2 and "Twint" in t2 and "war Twint" in t2


# --------------------------------------------------------------------------- F16: Patientenkopie UVG/IV/MV, Gesetz auf Französisch
async def test_f16_payant_copy_without_cost_share_and_french_law_names(client, app):
    inv = _final_kvg(app, "Lea", tpw=None)                                   # UVG Suva, Tiers payant
    await login(client, LEITUNG)
    text = _pdf_text(await (await client.get(f"/leitung/rechnungen/{inv['id']}/pdf")).read())
    assert "Tiers payant" in text and "Kostenbeteiligung" not in text and "UVG" in text
    db = app[DB]
    db.execute("UPDATE patients SET lang = 'fr' WHERE id = ?", (inv["patient_id"],))
    text = _pdf_text(await (await client.get(f"/leitung/rechnungen/{inv['id']}/pdf")).read())
    assert "LAA" in text and "Loi UVG" not in text and "participation aux coûts" not in text
    # KVG Tiers payant: die Kostenbeteiligung bleibt erwähnt
    from nfizyo.core.keys import I18N

    assert "Kostenbeteiligung" in app[I18N].t("de", "billing.pdf.note_payant_kvg")
    assert app[I18N].t("fr", "billing.law.KVG") == "LAMal" and app[I18N].t("fr", "billing.law.IVG") == "LAI"
    assert app[I18N].t("fr", "billing.law.MVG") == "LAM"


# --------------------------------------------------------------------------- F17: keine 7350 nach Datenübernahme
def test_f17_no_first_treatment_supplement_with_imported_sessions(app):
    db = app[DB]
    pid = pid_of(app, "Sandra")
    with practice_scope(1, _owner(app)):
        db.execute("UPDATE prescriptions SET sessions_before = 5 WHERE patient_id = ? AND practice_id = 1", (pid,))
        iid = billing.create_from_appointments(db, core_patients.get(db, pid), "de")
        codes = [ln["code"] for ln in billing.lines(db, iid)]
        assert "7350" not in codes, codes
        billing.delete_draft(db, billing.get(db, iid))
        # Gegenprobe ohne übernommene Sitzungen: 7350 wird vorgeschlagen (erste Behandlung)
        db.execute("UPDATE prescriptions SET sessions_before = 0 WHERE patient_id = ? AND practice_id = 1", (pid,))
        iid = billing.create_from_appointments(db, core_patients.get(db, pid), "de")
        assert "7350" in [ln["code"] for ln in billing.lines(db, iid)]
        # «mehr als 9 Sitzungen» zählt die übernommenen mit
        inv = billing.get(db, iid)
        if inv["prescription_id"]:
            db.execute("UPDATE prescriptions SET sessions_before = 8 WHERE id = ?", (inv["prescription_id"],))
            assert "over_nine" in billing.problems(db, inv, billing.lines(db, iid))


# --------------------------------------------------------------------------- F22: Entwurf nach Stammdaten-Korrektur
async def test_f22_draft_unblocks_after_master_data_fix(client, app):
    db = app[DB]
    pid = pid_of(app, "Luca")                                                # UVG AXA
    db.execute("UPDATE patients SET case_number = NULL WHERE id = ?", (pid,))
    with practice_scope(1, _owner(app)):
        iid = billing.create_from_appointments(db, core_patients.get(db, pid), "de")
        assert "missing_case_number" in billing.problems(db, billing.get(db, iid), billing.lines(db, iid))
    db.execute("UPDATE patients SET case_number = '33.44556.26.7' WHERE id = ?", (pid,))
    await login(client, LEITUNG)
    html = await (await client.get(f"/leitung/rechnungen/{iid}")).text()
    assert "Schadennummer fehlt" not in html and "33.44556.26.7" in html
    assert db.value("SELECT case_number FROM invoices WHERE id = ?", (iid,)) == "33.44556.26.7"


# --------------------------------------------------------------------------- F24: Text nach Testsendung
def test_f24_simulator_flash_points_to_responses(app):
    from nfizyo.core.keys import I18N

    for lang, where in (("de", "Abrechnung › Antworten"), ("fr", "Facturation › Réponses")):
        text = app[I18N].t(lang, "billing.md.sim_sent")
        assert where in text and "Portal" not in text and "portail" not in text


# --------------------------------------------------------------------------- F19: Export-Kacheln und Blatt «Kasse»
async def test_f19_export_summary_and_kasse_sheet_net_of_storno_after_close(client, app):
    from openpyxl import load_workbook

    db = app[DB]
    d0 = (today() - timedelta(days=2)).isoformat()
    with practice_scope(1, _owner(app)):
        item = _item(db, 60.70)
        s1, _ = cash.sell(db, patient=None, entries=[(item, 1)], method="card", lang="de", day=d0)
        s2, _ = cash.sell(db, patient=None, entries=[(item, 2)], method="card", lang="de", day=d0)
        cash.close_day(db, d0, cash.day_summary(db, d0)["cash"], None)
        cash.correct_storno(db, cash.get(db, s1), "Falsch erfasst", refund_day=None)
        num = cash.get(db, s1)["number"]
    await login(client, LEITUNG)
    q = f"von={d0}&bis={today().isoformat()}"
    html = await (await client.get(f"/leitung/einstellungen/export?{q}")).text()
    assert "CHF 121.40" in html and "−CHF 60.70" in html                     # netto (182.10 − 60.70) und Storno sichtbar
    wb = load_workbook(BytesIO(await (await client.get(f"/leitung/export/buchhaltung.xlsx?{q}")).read()))
    rows = list(wb["Kasse"].values)
    header = rows[0]
    row = next(r for r in rows if r[1] == num)
    assert "Storno" in row[header.index("Korrektur nach Abschluss")] and row[header.index("Netto nach Korrektur")] == 0
    total = rows[-1]
    assert total[1] == "Total" and total[header.index("Netto nach Korrektur")] == 121.4


# --------------------------------------------------------------------------- F10: bexio-Schlüssel aus festen IDs
def test_f10_bexio_keys_are_stable_and_legacy_rows_are_not_resent(app):
    from nfizyo.modules.exports import bexio
    from nfizyo.modules.firms import api as firms_api
    from nfizyo.modules.firms import repo as firms

    db = app[DB]
    with practice_scope(1, _owner(app)):
        inv = _final_kvg(app)
        billing.add_manual_payment(db, billing.get(db, inv["id"]), amount=100.0, booked_on=today().isoformat(), method="bank")
        fid = db.value("SELECT id FROM firms WHERE practice_id = 1 LIMIT 1")
        finv = firms.get(db, firms.create(db, firms.get_firm(db, fid)))
        firms.add_free_line(db, finv, label="Workshop", quantity=1.5, unit_price=180.35, vat_rate=0.0, kind="service")
        assert firms.finalize(db, firms.get(db, finv["id"]))

        def entries():
            lo, hi = journal.bounds(today() - timedelta(days=1), today())
            fm = firms_api.accounting(db, lo, hi, (today() - timedelta(days=1)).isoformat(), today().isoformat())
            return journal.build(db, today() - timedelta(days=1), today(), cash_sales=[], firms=fm, cash_corrections=[])["entries"]

        before = entries()
        assert before and all(e.src for e in before) and len({bexio.entry_key(e) for e in before}) == len(before)
        # 0.8.0: alle Zeilen schon mit dem ALTEN Schlüssel (mit Text) gesendet
        for e in before:
            db.execute("INSERT INTO bexio_bookings (practice_id, entry_key, day, doc, debit, credit, amount_chf, text, status, bexio_id,"
                       " created_by, updated_at) VALUES (1,?,?,?,?,?,?,?, 'sent', 1, ?, ?)",
                       (bexio.legacy_key(e), e.day, e.doc, e.debit, e.credit, e.amount, e.text, _owner(app), utcnow()))
        # Stammdaten ändern sich (Firma umbenannt, Patientenname korrigiert) – Texte ändern sich
        db.execute("UPDATE firms SET name = name || ' (neu)' WHERE id = ?", (fid,))
        db.execute("UPDATE patients SET last_name = 'Neuname' WHERE id = ?", (inv["patient_id"],))
        after = entries()
        assert [e.text for e in after] != [e.text for e in before]
        assert bexio.plan(db, after) == {"new": 0, "sent": len(after), "failed": 0, "unknown": 0}
        # alte Zeilen haben jetzt den festen Schlüssel; nochmals umbenennen ändert nichts
        assert db.value("SELECT COUNT(*) FROM bexio_bookings WHERE practice_id = 1 AND entry_key LIKE 'src:%'") == len(after)
        db.execute("UPDATE firms SET name = 'Ganz anders AG' WHERE id = ?", (fid,))
        assert bexio.plan(db, entries())["new"] == 0
        # Eine neue Zahlung ist neu (und nur sie)
        billing.add_manual_payment(db, billing.get(db, inv["id"]), amount=10.0, booked_on=today().isoformat(), method="bank")
        assert bexio.plan(db, entries())["new"] == 1


async def test_f10_send_after_rename_posts_nothing_twice(aiohttp_client, aiohttp_server, tmp_path):
    from test_bexio import TOKEN, _client, _period

    state: dict = {}
    client, app = await _client(aiohttp_client, aiohttp_server, tmp_path, state, bexio_token=TOKEN)
    db = app[DB]
    csrf = await login(client, LEITUNG)
    await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()})
    sent = len(state.get("posted", []))
    assert sent > 0
    db.execute("UPDATE firms SET name = name || ' umbenannt' WHERE practice_id = 1")
    db.execute("UPDATE patients SET last_name = last_name || 'x' WHERE practice_id = 1")
    await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()})
    assert len(state["posted"]) == sent


# --------------------------------------------------------------------------- SEC-03: bexio-Token gehört einer Praxis
async def test_sec03_bexio_token_bound_to_one_practice(aiohttp_client, aiohttp_server, tmp_path):
    from test_bexio import TOKEN, _client, _period

    from nfizyo.core.auth import create_user
    from nfizyo.modules.exports import bexio

    async def setup(**over):
        state: dict = {}
        client, app = await _client(aiohttp_client, aiohttp_server, tmp_path / str(len(over)), state, bexio_token=TOKEN, **over)
        db = app[DB]
        pid_b = db.execute("INSERT INTO practices (name, slug, default_lang, created_at) VALUES ('Praxis B', 'praxis-b', 'de', ?)",
                           (utcnow(),))
        create_user(db, pid_b, "chef@praxis-b.invalid", "Chef B", "owner", "passwort-b-123", "de")
        return client, app, state, pid_b

    # Zwei Praxen, keine Zuordnung: niemand nutzt das Token
    client, app, state, pid_b = await setup()
    with practice_scope(1):
        assert bexio.mode_here(app[DB], app[app_settings()]) == "other" and not bexio.connected(app[DB], app[app_settings()])
    # Zuordnung zu Praxis B: Praxis A (Demo) sieht «nicht verfügbar» und kann nicht senden
    client, app, state, pid_b = await setup(bexio_practice="praxis-b")
    csrf = await login(client, LEITUNG)
    html = await (await client.get("/leitung/einstellungen/export?von={von}&bis={bis}".format(**_period()))).text()
    assert "nicht verfügbar" in html and "Mit bexio verbunden" not in html
    r = await client.post("/leitung/einstellungen/export", data={"_csrf": csrf, "do": "bexio_send", **_period()}, allow_redirects=False)
    assert "posted" not in state and r.status == 302
    with practice_scope(pid_b):
        assert bexio.mode_here(app[DB], app[app_settings()]) == "token"
    with practice_scope(1):
        try:
            await bexio.access_token(app[DB], app[app_settings()])
            raise AssertionError("Praxis A darf das Token nicht bekommen")
        except bexio.BexioError:
            pass
    # Zuordnung über die Nummer der Praxis
    client, app, state, pid_b = await setup(bexio_practice="1", bexio_client_id="")
    with practice_scope(1):
        assert bexio.mode_here(app[DB], app[app_settings()]) == "token"


def app_settings():
    from nfizyo.core.keys import SETTINGS

    return SETTINGS


# --------------------------------------------------------------------------- Seiten mit Rundung/Korrekturen (de und fr)
async def test_pages_render_with_rounding_and_corrections(client, app):
    db = app[DB]
    inv = _final_kvg(app)
    d0 = (today() - timedelta(days=1)).isoformat()
    with practice_scope(1, _owner(app)):
        item = _item(db, 33.33)
        s1, _ = cash.sell(db, patient=None, entries=[(item, 1)], method="cash", lang="de", day=d0)
        billing_api.pay_at_desk(db, inv["id"], 186.5, "cash", d0)
        cash.close_day(db, d0, cash.day_summary(db, d0)["cash"], None)
        cash.correct_method(db, cash.get(db, s1), "card", "Karte")
        assert cash.closing(db, d0)["cash_expected"] == 219.85          # 33.35 + 186.50
    pages = ["/kasse", "/kasse?ansicht=heute&tag=" + d0, "/kasse?ansicht=abschluss&tag=" + d0, f"/kasse/belege/{s1}", f"/kasse/belege/{s1}/pdf",
             f"/kasse?rechnung={inv['number']}", "/abrechnung?ansicht=zahlungen", "/abrechnung?ansicht=abgeschlossen", "/abrechnung?ansicht=bereit",
             f"/rechnungen/{inv['id']}", f"/rechnungen/{inv['id']}/pdf"]
    for lang in ("de", "fr"):
        db.execute("UPDATE users SET lang = ? WHERE practice_id = 1", (lang,))
        for who, S in ((EMPFANG, "/empfang"), (LEITUNG, "/leitung")):
            await login(client, who)
            for path in pages:
                r = await client.get(S + path)
                assert r.status == 200, (lang, S + path, r.status)
        r = await client.get(f"/leitung/einstellungen/export?von={d0}&bis={today().isoformat()}")
        assert r.status == 200
        for path in ("/leitung/export/buchhaltung.xlsx", "/leitung/export/journal.csv"):
            r = await client.get(f"{path}?von={d0}&bis={today().isoformat()}")
            assert r.status == 200
    csv_text = (await (await client.get(f"/leitung/export/journal.csv?von={d0}&bis={today().isoformat()}")).read()).decode("utf-8-sig")
    assert "Rundung 5 Rappen" in csv_text
