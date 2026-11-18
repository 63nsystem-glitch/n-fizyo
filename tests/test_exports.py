"""Export: Buchungsjournal (CSV/Excel), vollständiger Datenexport (ZIP), Kalender-Abo (iCal)."""
from __future__ import annotations

import io
import os
import re
import time
import zipfile
from datetime import datetime, timedelta

from conftest import PASSWORD, login

from nfizyo.core import export as core_export
from nfizyo.core import patients as core_patients
from nfizyo.core.keys import DB, SETTINGS
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import TZ, today
from nfizyo.modules.billing import api as billing_api
from nfizyo.modules.billing import payment
from nfizyo.modules.billing import repo as billing
from nfizyo.modules.cashdesk import api as cash_api
from nfizyo.modules.cashdesk import repo as cash
from nfizyo.modules.exports import journal


def _owner(app) -> int:
    return app[DB].value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")


def _business(app) -> dict:
    """Ein Tag Geschäft: Selbstzahler-Rechnung mit Produkt (MWST), Zahlung bar, Storno, Kassenverkauf Twint."""
    db = app[DB]
    with practice_scope(1, _owner(app)):
        payment.save_settings(db, {"vat_number": "CHE-123.456.788"})
        ready = [r for r in billing.ready(db) if r["law"] == "KVG"]
        p1 = core_patients.get(db, ready[0]["patient_id"])
        iid = billing.create_from_appointments(db, p1, "de", private_=True)
        billing.add_line(db, billing.get(db, iid), code="TB-ROT", quantity=1, service_date=today().isoformat(), lang="de")
        billing.finalize(db, billing.get(db, iid))
        inv = billing.get(db, iid)
        billing_api.pay_at_desk(db, iid, inv["total_chf"], "cash", today().isoformat())
        p2 = core_patients.get(db, ready[1]["patient_id"])
        iid2 = billing.create_from_appointments(db, p2, "de", private_=True)
        billing.finalize(db, billing.get(db, iid2))
        billing.cancel(db, billing.get(db, iid2), "Fehler")
        items = {it["code"]: it for it in billing_api.price_items(db)}
        sid, _ = cash.sell(db, patient=None, entries=[(items["ROLLE"], 1)], method="twint", lang="de", day=today().isoformat())
    return {"inv": dict(inv), "inv2": iid2, "sale": sid}


def test_journal_entries(app):
    db = app[DB]
    b = _business(app)
    with practice_scope(1):
        sales = cash_api.accounting_sales(db, today().isoformat(), today().isoformat())
        data = journal.build(db, today(), today(), cash_sales=sales)
    inv = b["inv"]
    by_doc: dict[str, list] = {}
    for e in data["entries"]:
        by_doc.setdefault(e.doc, []).append(e)
    inv_entries = [e for e in by_doc[inv["number"]] if e.debit == "1100"]
    assert round(sum(e.amount for e in inv_entries), 2) == inv["total_chf"]
    vat = [e for e in inv_entries if e.credit == "2200"]
    assert vat and vat[0].amount == inv["vat_chf"] == 0.97                 # 12.90 inkl. 8,1 %
    assert any(e.credit == "3200" and e.amount == round(12.90 - 0.97, 2) for e in inv_entries)
    pay = [e for e in by_doc[inv["number"]] if e.credit == "1100"]
    assert pay and pay[0].debit == "1000" and pay[0].amount == inv["total_chf"]
    num2 = db.value("SELECT number FROM invoices WHERE id = ?", (b["inv2"],))
    assert {e.debit for e in by_doc[num2]} == {"1100", "3400"}           # gebucht und storniert (Gegenbuchung)
    sale_no = db.value("SELECT number FROM cash_sales WHERE id = ?", (b["sale"],))
    assert all(e.debit == "1091" for e in by_doc[sale_no]) and round(sum(e.amount for e in by_doc[sale_no]), 2) == 34.90
    assert {e.credit for e in by_doc[sale_no]} == {"3200", "2200"}
    # Kontenplan ändern
    with practice_scope(1, 1):
        journal.save_settings(db, {"acc_cash": "1001"})
        assert journal.build(db, today(), today())["accounts"]["acc_cash"] == "1001"


async def test_downloads(client, app):
    _business(app)
    await login(client, "leitung@demo.n-fizyo.ch")
    q = f"von={today().isoformat()}&bis={today().isoformat()}"
    html = await (await client.get(f"/leitung/einstellungen/export?{q}")).text()
    assert "Buchhaltung" in html and "Journal (CSV)" in html
    r = await client.get(f"/leitung/export/journal.csv?{q}")
    body = await r.read()
    assert r.status == 200 and body.startswith("﻿".encode()) and b"Datum;Beleg;Soll;Haben;Betrag;Text;MWST" in body
    r = await client.get(f"/leitung/export/buchhaltung.xlsx?{q}")
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(await r.read()))
    assert {"Journal", "Rechnungen", "Zahlungen", "Kasse", "Offene Posten (heute)"} <= set(wb.sheetnames)
    assert wb["Journal"].max_row > 3
    # Empfang hat keinen Zugang
    await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.get(f"/leitung/export/journal.csv?{q}", allow_redirects=False)
    assert r.status in (302, 403, 404)


async def test_full_export(client, app, practice_b):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/export/voll", data={"_csrf": csrf, "password": "falsch"}, allow_redirects=False)
    assert r.status == 302 and core_export.list_exports(app[SETTINGS], 1) == []
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'export_denied'") == 1
    r = await client.post("/leitung/export/voll", data={"_csrf": csrf, "password": PASSWORD}, allow_redirects=False)
    files = core_export.list_exports(app[SETTINGS], 1)
    assert r.status == 302 and len(files) == 1
    html = await (await client.get("/leitung/einstellungen/export")).text()
    assert files[0]["name"] in html
    r = await client.get(f"/leitung/export/datei/{files[0]['name']}")
    assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
    zf = zipfile.ZipFile(io.BytesIO(await r.read()))
    names = set(zf.namelist())
    assert {"LIESMICH.txt", "tabellen/patients.csv", "tabellen/invoices.csv", "tabellen/users.csv"} <= names
    assert "tabellen/sessions.csv" not in names
    patients = zf.read("tabellen/patients.csv").decode("utf-8-sig")
    assert "Lea" in patients and "Zoraida" not in patients
    users = zf.read("tabellen/users.csv").decode("utf-8-sig")
    header = users.splitlines()[0]
    assert "password_hash" not in header and "totp_secret" not in header and "recovery_codes" not in header
    assert "Chef B" not in users
    for name in names:
        if name.startswith("tabellen/"):
            assert "Geheim" not in zf.read(name).decode("utf-8-sig"), name
    # Praxis B sieht die Datei von A nicht
    with practice_scope(practice_b["id"]):
        assert core_export.file_for(app[SETTINGS], practice_b["id"], files[0]["name"]) is None
    # Löschen und Aufräumen nach 24 Stunden
    await client.post(f"/leitung/export/datei/{files[0]['name']}/loeschen", data={"_csrf": csrf})
    assert core_export.list_exports(app[SETTINGS], 1) == []
    with practice_scope(1):
        path = core_export.build(db, app[SETTINGS], 1)
    old = time.time() - 25 * 3600
    os.utime(path, (old, old))
    assert core_export.cleanup(app[SETTINGS]) == 1 and not path.exists()
    r = await client.get("/leitung/export/datei/n-fizyo-export-x.zip", allow_redirects=False)
    assert r.status == 404


async def test_ical_feed(client, app, practice_b):
    db = app[DB]
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get("/therapie/agenda/kalender-abo")).text()
    assert "Kalender-Abo" in html
    r = await client.post("/therapie/agenda/kalender-abo", data={"_csrf": csrf, "do": "create", "label": "iPhone", "detail": "initials"})
    html = await r.text()
    url = re.search(r"(http[^<\s]+/kalender/[A-Za-z0-9_-]+\.ics)", html).group(1)
    path = url.split("://", 1)[1].split("/", 1)[1]
    assert db.value("SELECT COUNT(*) FROM ical_tokens WHERE token_hash LIKE ?", (path.rsplit("/", 1)[1][:10] + "%",)) == 0  # nur Hash
    client.session.cookie_jar.clear()
    r = await client.get("/" + path)
    text = await r.text()
    assert r.status == 200 and r.content_type == "text/calendar" and text.startswith("BEGIN:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" in text and re.search(r"SUMMARY:Termin \w\. \w\.", text)
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    td = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    full_names = [f"{p['first_name']} {p['last_name']}" for p in db.query(
        "SELECT p.first_name, p.last_name FROM appointments a JOIN patients p ON p.id = a.patient_id WHERE a.therapist_id = ?", (td,))]
    assert full_names and not any(n in text for n in full_names)
    assert "Zoraida" not in text and "Geheim" not in text
    from nfizyo.core.timeutil import to_utc_iso

    now = datetime.now(TZ)
    assert text.count("BEGIN:VEVENT") == db.value(
        "SELECT COUNT(*) FROM appointments WHERE therapist_id = ? AND status IN ('booked','arrived','done','blocked')"
        " AND starts_at >= ? AND starts_at < ?", (td, to_utc_iso(now - timedelta(days=30)), to_utc_iso(now + timedelta(days=120))))
    # widerrufen → nicht mehr erreichbar
    tid = db.value("SELECT id FROM ical_tokens")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    await client.post("/therapie/agenda/kalender-abo", data={"_csrf": csrf, "do": "revoke", "token_id": str(tid)})
    client.session.cookie_jar.clear()
    assert (await client.get("/" + path)).status == 404
    assert (await client.get("/kalender/" + "x" * 43 + ".ics")).status == 404


async def test_ical_other_users_cannot_revoke_and_inactive_users_stop(client, app):
    db = app[DB]
    from nfizyo.modules.agenda import ical

    td = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    with practice_scope(1, td):
        token = ical.create(db, "x", "none")
    tid = db.value("SELECT id FROM ical_tokens")
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post("/therapie/agenda/kalender-abo", data={"_csrf": csrf, "do": "revoke", "token_id": str(tid)})
    assert db.value("SELECT revoked_at FROM ical_tokens WHERE id = ?", (tid,)) is None
    client.session.cookie_jar.clear()
    text = await (await client.get(f"/kalender/{token}.ics")).text()
    assert "Termin L." not in text and "SUMMARY:Termin" in text          # Variante ohne Namen
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (td,))
    assert (await client.get(f"/kalender/{token}.ics")).status == 404
