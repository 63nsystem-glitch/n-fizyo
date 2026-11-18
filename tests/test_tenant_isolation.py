"""Datentrennung zwischen Praxen.

WICHTIG: Diese Tests dürfen NIE gelöscht oder abgeschwächt werden. Sie stellen sicher, dass eine Praxis
niemals Daten einer anderen Praxis sieht oder verändern kann (Patientengeheimnis, Art. 321 StGB).
Sie prüfen alle drei Bildschirme (Therapie, Empfang, Leitung) und die Patienten-App.
"""
from __future__ import annotations

from conftest import login, pid_of

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope

# Seiten mit Daten der Praxis B – für jede Person aus Praxis A müssen sie «nicht gefunden» sein.
URLS = (
    # Therapie
    "/therapie/patienten/{patient}", "/therapie/patienten/{patient}/verlauf", "/therapie/patienten/{patient}/stammdaten",
    "/therapie/patienten/{patient}/berichte", "/therapie/patienten/{patient}/dokumente", "/therapie/patienten/{patient}/termine",
    "/therapie/patienten/{patient}/heimprogramm", "/therapie/patienten/{patient}/info", "/therapie/nachrichten/{patient}",
    "/therapie/notizen/{note}", "/therapie/berichte/{report}", "/therapie/berichte/{report}/druck",
    "/therapie/agenda/termine/{appointment}", "/therapie/posteingang/{document}", "/therapie/posteingang/{document}/datei",
    "/therapie/diktat/termin/{appointment}",
    # Empfang
    "/empfang/patienten/{patient}", "/empfang/patienten/{patient}/termine", "/empfang/agenda/termine/{appointment}",
    "/empfang/posteingang/{document}", "/empfang/rechnungen/{invoice}", "/empfang/rechnungen/{invoice}/druck",
    # Leitung
    "/leitung/rechnungen/{invoice}", "/leitung/rechnungen/{invoice}/druck",
    # PDF, Mahnung, XML (Abrechnung Stufe 2) und Bericht als PDF (HIN)
    "/empfang/rechnungen/{invoice}/pdf", "/leitung/rechnungen/{invoice}/pdf", "/leitung/rechnungen/{invoice}/mahnung/1/pdf",
    "/leitung/rechnungen/{invoice}/xml/1", "/therapie/berichte/{report}/pdf",
    # Kasse (Quittung, PDF)
    "/empfang/kasse/belege/{sale}", "/empfang/kasse/belege/{sale}/pdf", "/leitung/kasse/belege/{sale}", "/leitung/kasse/belege/{sale}/pdf",
    # Kurzüberblick vor dem Termin und Übergabe
    "/therapie/kurzinfo/{appointment}", "/therapie/patienten/{patient}/uebergabe",
    # MTT-Plan in der Akte
    "/therapie/patienten/{patient}/mtt",
    # Video-Sitzung
    "/therapie/video/{appointment}", "/therapie/video/{appointment}/start", "/empfang/video/{appointment}",
    # 0.9.0: Original eines Berichts im Rahmen der App
    "/therapie/posteingang/{document}/original",
)


async def test_other_practice_pages_are_not_found(client, practice_b):
    for email in ("leitung@demo.n-fizyo.ch", "therapie@demo.n-fizyo.ch", "empfang@demo.n-fizyo.ch"):
        await login(client, email)
        for url in URLS:
            r = await client.get(url.format(**practice_b), allow_redirects=False)
            assert r.status in (403, 404), f"{email} {url} gibt {r.status}"


async def test_other_practice_cannot_write(client, practice_b, app):
    db = app[DB]
    b = practice_b
    for email, posts in (
        ("leitung@demo.n-fizyo.ch", (
            (f"/therapie/berichte/{b['report']}", {"assessment": "überschrieben"}),
            (f"/therapie/patienten/{b['patient']}/befunde", {"kind": "pain", "title": "x"}),
            (f"/therapie/patienten/{b['patient']}/berichte", {"kind": "progress"}),
            (f"/therapie/patienten/{b['patient']}/info", {"home": "x"}),
            (f"/therapie/notizen/{b['note']}", {"befund": "x"}),
            (f"/therapie/notizen/{b['note']}/verwerfen", {}),
            (f"/leitung/rechnungen/{b['invoice']}", {"do": "delete"}),
            (f"/leitung/rechnungen/{b['invoice']}", {"do": "payment", "amount": "10"}),
            (f"/leitung/rechnungen/{b['invoice']}", {"do": "reminder", "level": "1"}),
            (f"/leitung/rechnungen/{b['invoice']}", {"do": "xml", "kind": "invoice"}),
            (f"/therapie/berichte/{b['report']}/hin", {}),
            (f"/therapie/agenda/termine/{b['appointment']}/status", {"status": "cancelled"}),
        )),
        ("empfang@demo.n-fizyo.ch", (
            (f"/empfang/patienten/{b['patient']}", {"first_name": "X", "last_name": "Y"}),
            (f"/empfang/patienten/{b['patient']}/team", {"do": "add", "user_id": "2"}),
            (f"/empfang/patienten/{b['patient']}/app-einladung", {}),
            (f"/empfang/agenda/termine/{b['appointment']}/status", {"status": "cancelled"}),
            (f"/empfang/rechnungen/{b['invoice']}", {"do": "delete"}),
            (f"/empfang/rechnungen/{b['invoice']}", {"do": "payment", "amount": "10"}),
            (f"/empfang/posteingang/{b['document']}/patient", {"patient_id": "1"}),
        )),
    ):
        csrf = await login(client, email)
        for url, data in posts:
            r = await client.post(url, data={"_csrf": csrf, **data}, allow_redirects=False)
            assert r.status in (403, 404), f"{email} POST {url} gibt {r.status}"
    assert db.value("SELECT assessment FROM reports WHERE id = ?", (b["report"],)) == "Geheim"
    assert db.value("SELECT COUNT(*) FROM invoices WHERE id = ?", (b["invoice"],)) == 1
    assert db.value("SELECT COUNT(*) FROM findings WHERE patient_id = ?", (b["patient"],)) == 0
    assert db.value("SELECT first_name FROM patients WHERE id = ?", (b["patient"],)) == "Zoraida"
    assert db.value("SELECT status FROM appointments WHERE id = ?", (b["appointment"],)) == "booked"
    assert db.value("SELECT patient_id FROM documents WHERE id = ?", (b["document"],)) == b["patient"]
    assert db.value("SELECT COUNT(*) FROM patient_team WHERE patient_id = ?", (b["patient"],)) == 1
    assert db.value("SELECT COUNT(*) FROM patient_invites WHERE patient_id = ?", (b["patient"],)) == 0
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE invoice_id = ?", (b["invoice"],)) == 0
    assert db.value("SELECT COUNT(*) FROM invoice_reminders WHERE invoice_id = ?", (b["invoice"],)) == 0
    assert db.value("SELECT COUNT(*) FROM invoice_transmissions WHERE invoice_id = ?", (b["invoice"],)) == 0
    assert db.value("SELECT status FROM reports WHERE id = ?", (b["report"],)) != "sent"


async def test_lists_never_show_other_practice(client, practice_b):
    pages = {
        "leitung@demo.n-fizyo.ch": ("/therapie/patienten", "/therapie/patienten?q=Zorai", "/therapie/heute", "/therapie/agenda",
                                    "/therapie/posteingang", "/therapie/nachrichten", "/therapie/anliegen", "/therapie/berichte",
                                    "/therapie/diktat", "/leitung/uebersicht", "/leitung/agenda", "/leitung/team", "/leitung/vertretungen",
                                    "/leitung/abrechnung?ansicht=entwuerfe", "/leitung/abrechnung?ansicht=offen",
                                    "/leitung/abrechnung?ansicht=zahlungen", "/leitung/abrechnung?ansicht=abgeschlossen",
                                    "/leitung/einstellungen/versicherer", "/leitung/einstellungen/zahlung", "/leitung/einstellungen/sms",
                                    "/leitung/einstellungen/hin",
                                    # 0.9.0: offene Arbeit, Einrichtung, Benachrichtigungen, Notizvorlagen
                                    "/leitung/offene-notizen?art=entwuerfe", "/leitung/offene-notizen?art=ohne-notiz",
                                    "/leitung/offene-berichte", "/leitung/offene-berichtsentwuerfe", "/leitung/einrichtung",
                                    "/leitung/einstellungen/benachrichtigungen", "/leitung/einstellungen/notizvorlagen",
                                    "/therapie/notizen/vorlagen"),
        "empfang@demo.n-fizyo.ch": ("/empfang/patienten", "/empfang/patienten?q=Zorai", "/empfang/patienten?archiv=1", "/empfang/heute",
                                    "/empfang/agenda", "/empfang/agenda/neu", "/empfang/agenda/warteliste", "/empfang/posteingang",
                                    "/empfang/posteingang?f=alle", "/empfang/abrechnung", "/empfang/abrechnung?ansicht=entwuerfe"),
    }
    for email, urls in pages.items():
        await login(client, email)
        for url in urls:
            r = await client.get(url)
            assert r.status == 200, url
            text = await r.text()
            assert "Zoraida" not in text and "Geheim" not in text and "Chef B" not in text, f"{email} {url}"


async def test_practice_b_sees_only_own(client, practice_b):
    await login(client, practice_b["email"], practice_b["password"])
    r = await client.get("/therapie/patienten")
    text = await r.text()
    assert "Zoraida" in text and "Muster" not in text
    r = await client.get("/therapie/patienten/1", allow_redirects=False)
    assert r.status == 404
    await login(client, practice_b["reception_email"], practice_b["password"])
    r = await client.get("/empfang/patienten")
    text = await r.text()
    assert "Zoraida" in text and "Muster" not in text
    r = await client.get("/empfang/patienten/1", allow_redirects=False)
    assert r.status == 404


async def test_patient_app_session_never_crosses_practices(client, app, practice_b):
    from nfizyo.core.auth import create_session

    token, _ = create_session(app[DB], "patient", patient_id=practice_b["patient"], practice_id=practice_b["id"])
    client.session.cookie_jar.update_cookies({"nf_p": token})
    for url in ("/p/heute", "/p/info", "/p/termine", "/p/nachrichten", "/p/uebungen"):
        r = await client.get(url)
        assert r.status == 200, url
        text = await r.text()
        assert "Muster" not in text and "Lea" not in text, url
    lea_item = app[DB].value("SELECT i.id FROM program_items i JOIN programs p ON p.id = i.program_id WHERE p.practice_id = 1 LIMIT 1")
    r = await client.get(f"/p/uebungen/{lea_item}")
    assert r.status == 404


async def test_payments_never_cross_practices(client, app, practice_b):
    """Zahlungen der Praxis B (auch ohne Zuordnung) sind für Praxis A unsichtbar; eine Bankdatei von A ordnet nie B zu."""
    from nfizyo.modules.billing import payment
    from nfizyo.modules.billing import repo as billing

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        billing.add_payment(db, amount=55.0, booked_on="2026-10-01", source="manual", debtor="Geheimzahler B")
    ref = payment.qr_reference(b["invoice"])
    camt = (f"""<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.08"><BkToCstmrDbtCdtNtfctn>"""
            f"""<Ntfctn><Ntry><Amt Ccy="CHF">10.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-10-20</Dt></BookgDt>"""
            f"""<NtryDtls><TxDtls><Refs><AcctSvcrRef>X1</AcctSvcrRef></Refs><Amt Ccy="CHF">10.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>"""
            f"""<RmtInf><Strd><CdtrRefInf><Ref>{ref}</Ref></CdtrRefInf></Strd></RmtInf></TxDtls></NtryDtls></Ntry></Ntfctn>"""
            f"""</BkToCstmrDbtCdtNtfctn></Document>""").encode()
    with practice_scope(1, 1):
        result = billing.import_payments(db, "a.xml", camt)
    assert result["matched"] == 0
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE invoice_id = ?", (b["invoice"],)) == 0
    await login(client, "leitung@demo.n-fizyo.ch")
    text = await (await client.get("/leitung/abrechnung?ansicht=zahlungen")).text()
    assert "Geheimzahler" not in text


def test_repositories_are_scoped(app, practice_b):
    from nfizyo.core import access
    from nfizyo.core import patients as core_patients
    from nfizyo.modules.agenda import repo as agenda
    from nfizyo.modules.documentation import repo as notes
    from nfizyo.modules.inbox import repo as inbox
    from nfizyo.modules.patientinfo import repo as pinfo
    from nfizyo.modules.reports import repo as reports

    db = app[DB]
    b = practice_b
    with practice_scope(1):
        assert core_patients.get(db, b["patient"]) is None
        assert notes.get(db, b["note"]) is None
        assert reports.get(db, b["report"]) is None
        assert agenda.get(db, b["appointment"]) is None
        assert inbox.get(db, b["document"]) is None
        assert pinfo.get(db, b["info"]) is None
        assert access.relation(db, b["user"], b["patient"]) is None
        assert all(p["practice_id"] == 1 for p in core_patients.search(db, ""))
        assert all(r["patient_id"] != b["patient"] for r in agenda.waitlist(db))


async def test_cashdesk_and_prices_never_cross_practices(client, app, practice_b):
    """Kasse, Abos und Preisliste der Praxis B: für Praxis A unsichtbar und unveränderbar."""
    db = app[DB]
    b = practice_b
    await login(client, "leitung@demo.n-fizyo.ch")
    for url in ("/leitung/kasse", "/leitung/kasse?ansicht=heute", "/leitung/kasse?ansicht=abos", "/leitung/kasse?ansicht=abschluss",
                "/leitung/einstellungen/preise", f"/leitung/einstellungen/preise?bearbeiten={b['price_item']}",
                f"/leitung/kasse?patient={b['patient']}", "/leitung/kasse?q=Zorai", "/leitung/uebersicht"):
        r = await client.get(url)
        assert r.status == 200, url
        text = await r.text()
        assert "Zoraida" not in text and "Geheim" not in text and "GEHEIM" not in text, url
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    for url, data in (
        (f"/empfang/kasse/belege/{b['sale']}", {"do": "cancel", "reason": "x"}),
    ):
        r = await client.post(url, data={"_csrf": csrf, **data}, allow_redirects=False)
        assert r.status in (403, 404), url
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "visit", "sub_id": str(b["subscription"])}, allow_redirects=False)
    assert r.status in (403, 404)
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "cash", f"qty_{b['price_item']}": "1",
                                                  "patient_id": str(b["patient"])}, allow_redirects=False)
    assert r.status == 400
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    await client.post("/leitung/einstellungen/preise", data={"_csrf": csrf, "do": "toggle", "item_id": str(b["price_item"])})
    await client.post("/leitung/einstellungen/preise", data={"_csrf": csrf, "do": "save", "item_id": str(b["price_item"]), "kind": "abo",
                                                             "code": "X1", "name_de": "Übernommen", "price_chf": "1.00", "vat_rate": "0.0",
                                                             "abo_days": "10"})
    item = db.one("SELECT * FROM price_items WHERE id = ?", (b["price_item"],))
    assert item["active"] == 1 and item["code"] == "GEHEIM-ABO" and item["price_chf"] == 99.0
    assert db.value("SELECT status FROM cash_sales WHERE id = ?", (b["sale"],)) == "paid"
    assert db.value("SELECT COUNT(*) FROM subscription_visits WHERE subscription_id = ?", (b["subscription"],)) == 0
    assert db.value("SELECT COUNT(*) FROM cash_sales WHERE practice_id = 1 AND patient_id = ?", (b["patient"],)) == 0
    with practice_scope(1, 1):
        from nfizyo.modules.billing import private
        from nfizyo.modules.cashdesk import repo as cash
        from nfizyo.core.timeutil import today

        assert cash.get(db, b["sale"]) is None and private.item(db, b["price_item"]) is None
        assert cash.subscription(db, b["subscription"], today().isoformat()) is None
        assert all(s["practice_id"] == 1 for s in cash.day_sales(db, today().isoformat()))
        assert cash.day_summary(db, today().isoformat())["by_method"]["cash"] == 0.0


async def test_responses_and_approvals_never_cross_practices(client, app, practice_b):
    """Versicherer-Antworten und Kostengutsprachen der Praxis B: für Praxis A unsichtbar, nie zugeordnet, unveränderbar."""
    from nfizyo.modules.billing import followup

    from test_responses import resp_xml

    db = app[DB]
    b = practice_b
    db.execute("UPDATE invoices SET status = 'final', number = '2026-9999' WHERE id = ?", (b["invoice"],))
    with practice_scope(b["id"], b["user"]):
        followup.import_responses(db, "b.xml", resp_xml("2026-9999", explanation="Geheime Rückweisung B"))
        patient_b = db.one("SELECT * FROM patients WHERE id = ?", (b["patient"],))
        appr_b = followup.add_approval(db, patient_b, {"requested_on": "2026-10-01", "reference": "Geheimreferenz B"})
    rid_b = db.value("SELECT id FROM invoice_responses WHERE practice_id = ?", (b["id"],))
    # Praxis A liest eine Antwort mit derselben Nummer ein → wird NIE der Rechnung von B zugeordnet
    with practice_scope(1, 1):
        followup.import_responses(db, "a.xml", resp_xml("2026-9999", explanation="Antwort A"))
    assert db.value("SELECT invoice_id FROM invoice_responses WHERE practice_id = 1") is None
    assert db.value("SELECT COUNT(*) FROM invoice_responses WHERE invoice_id = ?", (b["invoice"],)) == 1
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    for url in ("/leitung/abrechnung?ansicht=antworten", "/leitung/uebersicht"):
        text = await (await client.get(url)).text()
        assert "Geheim" not in text and "Zoraida" not in text, url
    r = await client.get(f"/leitung/abrechnung/antworten/{rid_b}/xml", allow_redirects=False)
    assert r.status == 404
    await client.post("/leitung/abrechnung", data={"_csrf": csrf, "do": "resp_handled", "response_id": str(rid_b)})
    await client.post("/leitung/abrechnung", data={"_csrf": csrf, "do": "resp_assign", "response_id": str(rid_b), "number": "2026-9999"})
    row = db.one("SELECT * FROM invoice_responses WHERE id = ?", (rid_b,))
    assert row["handled_at"] is None and row["invoice_id"] == b["invoice"]
    rid_a = db.value("SELECT id FROM invoice_responses WHERE practice_id = 1")
    await client.post("/leitung/abrechnung", data={"_csrf": csrf, "do": "resp_assign", "response_id": str(rid_a), "number": "2026-9999"})
    assert db.value("SELECT invoice_id FROM invoice_responses WHERE id = ?", (rid_a,)) is None
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post("/empfang/abrechnung/kostengutsprache", data={"_csrf": csrf, "do": "add", "patient_id": str(b["patient"])},
                          allow_redirects=False)
    assert r.status == 404
    r = await client.post("/empfang/abrechnung/kostengutsprache", data={"_csrf": csrf, "do": "decide", "patient_id": str(pid_of(app, "Lea")),
                                                                        "approval_id": str(appr_b), "status": "approved"}, allow_redirects=False)
    assert r.status == 404
    assert db.value("SELECT status FROM cost_approvals WHERE id = ?", (appr_b,)) == "requested"
    assert db.value("SELECT COUNT(*) FROM cost_approvals WHERE patient_id = ?", (b["patient"],)) == 1
    with practice_scope(1, 1):
        assert followup.get_response(db, rid_b) is None and followup.approval(db, appr_b) is None
        assert followup.approvals_for(db, b["patient"]) == []


async def test_booking_never_crosses_practices(client, app, practice_b):
    """Online-Anfragen der Praxis B: für Praxis A unsichtbar und nicht bestätigbar; Zeit-Token von A gilt nicht bei B."""
    import re
    from datetime import datetime, timedelta

    from nfizyo.core.timeutil import TZ
    from nfizyo.modules.booking import repo as booking

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        db.execute("INSERT INTO treatment_types (practice_id, name_de, name_fr, minutes) VALUES (?,?,?,?)", (b["id"], "Geheimtyp", "Type secret", 30))
        type_b = db.value("SELECT id FROM treatment_types WHERE practice_id = ?", (b["id"],))
        booking.save_settings(db, {"enabled": 1}, [type_b], [b["user"]])
        t = next(t for t in booking.bookable_types(db) if t["id"] == type_b)
        rid_b = booking.create_request(db, type_row=t, therapist_id=b["user"], start=datetime.now(TZ) + timedelta(days=2), location_id=None,
                                       data={"first_name": "Zoraida", "last_name": "Geheimnisvoll", "birthdate": "1970-01-01",
                                             "phone": "+41790000000", "lang": "fr", "note": "Geheimanfrage B"})
        db.execute("UPDATE booking_requests SET status = 'requested' WHERE id = ?", (rid_b,))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    text = await (await client.get("/empfang/online-anfragen")).text()
    assert "Zoraida" not in text and "Geheim" not in text
    for do in ("confirm", "decline"):
        r = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": do, "request_id": str(rid_b)}, allow_redirects=False)
        assert r.status == 404
    assert db.value("SELECT status FROM booking_requests WHERE id = ?", (rid_b,)) == "requested"
    assert db.value("SELECT COUNT(*) FROM appointments WHERE practice_id = 1 AND patient_id IN (SELECT id FROM patients WHERE practice_id = ?)",
                    (b["id"],)) == 0
    # öffentliche Seite von A zeigt nichts von B, und ein Zeit-Token von A gilt bei B nicht
    client.session.cookie_jar.clear()
    slug_a = db.value("SELECT slug FROM practices WHERE id = 1")
    html = await (await client.get(f"/p/termin/{slug_a}")).text()
    assert "Geheimtyp" not in html and "Chef B" not in html
    type_a = re.search(r'href="\?art=(\d+)"', html).group(1)
    html = await (await client.get(f"/p/termin/{slug_a}?art={type_a}")).text()
    token = re.findall(r'class="slot" href="\?art=\d+&(?:amp;)?s=([^"]+)"', html)[0]
    from urllib.parse import unquote

    html_b = await (await client.get("/p/termin/praxis-b")).text()
    csrf_b = re.search(r'name="csrf-token" content="([^"]+)"', html_b).group(1)
    r = await client.post("/p/termin/praxis-b", data={"_csrf": csrf_b, "step": "data", "slot": unquote(token), "first_name": "X",
                                                      "last_name": "Y", "birthdate": "1980-01-01", "phone": "0795554433", "consent": "1"})
    assert r.status == 400
    assert db.value("SELECT COUNT(*) FROM booking_requests WHERE practice_id = ?", (b["id"],)) == 1
    with practice_scope(1, 1):
        assert booking.get(db, rid_b) is None
        assert all(r["practice_id"] == 1 for r in booking.open_requests(db))


async def test_exports_and_calendar_never_cross_practices(client, app, practice_b):
    """Export-Dateien, Buchungsjournal und Kalender-Abo: nie Daten der anderen Praxis."""
    from nfizyo.core import export as core_export
    from nfizyo.core.keys import SETTINGS
    from nfizyo.core.timeutil import today
    from nfizyo.modules.agenda import ical
    from nfizyo.modules.exports import journal

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        path_b = core_export.build(db, app[SETTINGS], b["id"])
        token_b = ical.create(db, "B", "initials")
    db.execute("UPDATE invoices SET status = 'final', number = '2026-8888', finalized_at = ?, total_chf = 77.0 WHERE id = ?",
               (__import__("nfizyo.core.db", fromlist=["utcnow"]).utcnow(), b["invoice"]))
    await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.get(f"/leitung/export/datei/{path_b.name}", allow_redirects=False)
    assert r.status == 404
    text = (await (await client.get(f"/leitung/export/journal.csv?von={today().isoformat()}&bis={today().isoformat()}")).read()).decode()
    assert "2026-8888" not in text and "Geheimnisvoll" not in text
    with zipfile_open(path_b) as zf:
        patients = zf.read("tabellen/patients.csv").decode("utf-8-sig")
        assert "Zoraida" in patients and "Lea" not in patients and "Muster" not in patients
    client.session.cookie_jar.clear()
    feed = await (await client.get(f"/kalender/{token_b}.ics")).text()
    assert feed.count("BEGIN:VEVENT") == 1
    assert "Rendez-vous Z. G." in feed and "Lea" not in feed and "L. M." not in feed
    with practice_scope(1, 1):
        assert all(e.doc != "2026-8888" for e in journal.build(db, today(), today())["entries"])


def zipfile_open(path):
    import zipfile

    return zipfile.ZipFile(path)


async def test_prescription_watch_never_crosses_practices(client, app, practice_b):
    """Verordnungen im Blick: Liste, Anfrage und Abschluss bleiben in der eigenen Praxis."""
    from nfizyo.core import patients as core_patients
    from nfizyo.modules.rxwatch import repo as rx

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        prid = core_patients.add_prescription(db, b["patient"], sessions=9, issued_on="2026-01-01", diagnosis="Geheime Diagnose B")
        qid = rx.add_request(db, b["patient"], prid, "renewal", "phone", "Geheime Notiz B")
    for email, screen in (("therapie@demo.n-fizyo.ch", "therapie"), ("empfang@demo.n-fizyo.ch", "empfang"),
                          ("leitung@demo.n-fizyo.ch", "therapie")):
        csrf = await login(client, email)
        text = await (await client.get(f"/{screen}/verordnungen")).text()
        assert "Zoraida" not in text and "Geheim" not in text, email
        r = await client.post(f"/{screen}/verordnungen/{prid}/anfrage", data={"_csrf": csrf, "kind": "renewal", "via": "fax"},
                              allow_redirects=False)
        assert r.status == 404, email
        r = await client.post(f"/{screen}/verordnungen/anfrage/{qid}", data={"_csrf": csrf, "status": "done"}, allow_redirects=False)
        assert r.status == 404, email
    assert db.value("SELECT COUNT(*) FROM rx_requests WHERE prescription_id = ?", (prid,)) == 1
    assert db.value("SELECT status FROM rx_requests WHERE id = ?", (qid,)) == "open"
    with practice_scope(1):
        assert all(r["patient_id"] != b["patient"] for r in rx.active(db))
    await login(client, b["reception_email"], b["password"])
    text = await (await client.get("/empfang/verordnungen")).text()
    assert "Zoraida" in text and "Muster" not in text


async def test_home_program_package_never_crosses_practices(client, app, practice_b):
    """Eigenes Video, Gründe fürs Nicht-Üben und freigegebene Messreihen bleiben in der eigenen Praxis."""
    from nfizyo.core.keys import SETTINGS
    from nfizyo.modules.homeprogram import repo as hp
    from nfizyo.modules.homeprogram.views import ensure_library
    from nfizyo.modules.patients import repo as prepo

    db = app[DB]
    b = practice_b
    ensure_library(db, b["id"])
    video = app[SETTINGS].data_dir / "videos" / str(b["id"]) / "geheim.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    with practice_scope(b["id"], b["user"]):
        prog = hp.current_program(db, b["patient"], create=True)
        ex = hp.library(db)[0]
        iid = hp.add_item(db, prog, ex["id"], "Geheime Notiz B", "fr")
        hp.approve(db, prog)
        hp.set_video(db, iid, str(video), "video/mp4", 80)
        hp.log_skip(db, b["patient"], "pain", "Geheimer Grund B")
        prepo.add_measurement(db, b["patient"], "flexion", 40, "°", joint="knee", side="l", measured_on="2026-10-01")
        prepo.add_measurement(db, b["patient"], "flexion", 60, "°", joint="knee", side="l", measured_on="2026-10-08")
        prepo.set_share(db, b["patient"], "flexion", "knee", "l", True)
    for email in ("therapie@demo.n-fizyo.ch", "leitung@demo.n-fizyo.ch"):
        csrf = await login(client, email)
        assert (await client.get(f"/therapie/heimprogramm/{iid}/video")).status == 404, email
        for url, data in ((f"/therapie/heimprogramm/{iid}/video/loeschen", {}), (f"/therapie/heimprogramm/{iid}", {"essential": "1"}),
                          (f"/therapie/patienten/{b['patient']}/messwerte/teilen", {"kind": "flexion", "joint": "knee", "side": "l", "on": "0"})):
            r = await client.post(url, data={"_csrf": csrf, **data}, allow_redirects=False)
            assert r.status == 404, (email, url)
    assert video.exists() and db.value("SELECT video_path FROM program_items WHERE id = ?", (iid,)) == str(video)
    assert db.value("SELECT essential FROM program_items WHERE id = ?", (iid,)) == 0
    assert db.value("SELECT COUNT(*) FROM measurement_shares WHERE patient_id = ?", (b["patient"],)) == 1
    with practice_scope(1):
        assert hp.item(db, iid) is None
        assert not hp.skips_since(db, b["patient"], "2000-01-01")
        assert not prepo.shares(db, b["patient"]) and not prepo.shared_series(db, b["patient"])
    # Patienten-App der Praxis A: weder Video noch Verlauf der Praxis B
    from nfizyo.core.auth import create_session

    lea = pid_of(app, "Lea")
    token, _ = create_session(db, "patient", patient_id=lea, practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    assert (await client.get(f"/p/uebungen/{iid}/video")).status == 404
    home = await (await client.get("/p/heute")).text()
    assert "40°" not in home and "Geheim" not in home


async def test_exercise_library_never_crosses_practices(client, app, practice_b):
    """Übungsbibliothek: bearbeiten, deaktivieren und ins Programm übernehmen nur in der eigenen Praxis."""
    from nfizyo.modules.homeprogram import repo as hp
    from nfizyo.modules.homeprogram.views import ensure_library

    db = app[DB]
    b = practice_b
    ensure_library(db, b["id"])
    with practice_scope(b["id"], b["user"]):
        eid = hp.add_exercise(db, name_de="Geheimübung B", name_fr="Exercice secret B", instructions_de="Geheim",
                              instructions_fr="Secret", regions="knee", drawing="pendulum", sets=2, reps=5)
    before = dict(db.one("SELECT * FROM exercises WHERE id = ?", (eid,)))
    lea = pid_of(app, "Lea")
    for email in ("therapie@demo.n-fizyo.ch", "leitung@demo.n-fizyo.ch"):
        csrf = await login(client, email)
        assert (await client.get(f"/therapie/uebungen/{eid}")).status == 404, email
        for url, data in ((f"/therapie/uebungen/{eid}", {"name_de": "Übernommen", "instructions_de": "x", "drawing": "walk"}),
                          (f"/therapie/uebungen/{eid}/aktiv", {"on": "0"}),
                          (f"/therapie/patienten/{lea}/heimprogramm", {"exercise_id": str(eid)})):
            r = await client.post(url, data={"_csrf": csrf, **data}, allow_redirects=False)
            assert r.status == 404, (email, url)
        for url in ("/therapie/uebungen", "/therapie/uebungen?inaktiv=1", f"/therapie/patienten/{lea}/heimprogramm"):
            text = await (await client.get(url)).text()
            assert "Geheimübung B" not in text and f'name="exercise_id" value="{eid}"' not in text, (email, url)
            assert f'/therapie/uebungen/{eid}"' not in text, (email, url)
    assert dict(db.one("SELECT * FROM exercises WHERE id = ?", (eid,))) == before
    assert db.value("SELECT COUNT(*) FROM program_items WHERE exercise_id = ? AND practice_id = 1", (eid,)) == 0
    with practice_scope(1):
        assert hp.exercise(db, eid) is None and not hp.update_exercise(db, eid, name_de="X") and not hp.set_exercise_active(db, eid, False)
        assert all(e["practice_id"] == 1 for e in hp.library(db, active=None))
    # und umgekehrt: Praxis B sieht die Bibliothek der Praxis A nicht
    a_ex = db.value("SELECT id FROM exercises WHERE practice_id = 1 AND code = 'walk'")
    csrf = await login(client, b["email"], b["password"])
    assert (await client.get(f"/therapie/uebungen/{a_ex}")).status == 404
    r = await client.post(f"/therapie/uebungen/{a_ex}/aktiv", data={"_csrf": csrf, "on": "0"}, allow_redirects=False)
    assert r.status == 404 and db.value("SELECT active FROM exercises WHERE id = ?", (a_ex,)) == 1
    text = await (await client.get("/therapie/uebungen")).text()
    assert ("Geheimübung B" in text or "Exercice secret B" in text) and f'/therapie/uebungen/{a_ex}"' not in text


async def test_gap_filling_never_crosses_practices(client, app, practice_b):
    """Lücke füllen: Kandidaten, Angebote und Links bleiben in der eigenen Praxis."""
    import re as _re
    from datetime import datetime, timedelta

    from nfizyo.core import patients as core_patients
    from nfizyo.core.timeutil import TZ
    from nfizyo.modules.agenda import gapfill
    from nfizyo.modules.agenda import repo as agenda

    db = app[DB]
    b = practice_b
    start = (datetime.now(TZ) + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
    with practice_scope(b["id"], b["user"]):
        other = core_patients.create(db, {"first_name": "Geheimwartende", "last_name": "B", "birthdate": "1971-01-01", "sex": "f",
                                          "lang": "fr", "phone": "079 999 99 99"})
        agenda.add_waitlist(db, patient_id=other, wish="Geheimwunsch B", preferred="egal")
        gap_b = agenda.create(db, therapist_id=b["user"], start=start, minutes=30, patient_id=b["patient"])
        agenda.set_status(db, gap_b, "cancelled", "Geheim")
        a_b = agenda.get(db, gap_b)
        assert [c.entry["first_name"] for c in gapfill.candidates(db, a_b)["sms"]] == ["Geheimwartende"]
        oid = gapfill.create_offer(db, a_b)
        entry = next(w for w in gapfill.waitlist_rows(db) if w["patient_id"] == other)
        token_b = gapfill.add_recipient(db, oid, entry)
    # Praxis A: weder Lücke noch Kandidat noch Angebot der Praxis B
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    assert (await client.get(f"/empfang/agenda/termine/{gap_b}")).status == 404
    r = await client.post(f"/empfang/agenda/termine/{gap_b}/luecke", data={"_csrf": csrf, "action": "close"}, allow_redirects=False)
    assert r.status == 404 and db.value("SELECT status FROM slot_offers WHERE id = ?", (oid,)) == "open"
    wl = await (await client.get("/empfang/agenda/warteliste")).text()
    assert "Geheim" not in wl and "Zoraida" not in wl
    gap_a = db.value("SELECT id FROM appointments WHERE practice_id = 1 AND status = 'cancelled' AND patient_id IS NOT NULL LIMIT 1")
    text = await (await client.get(f"/empfang/agenda/termine/{gap_a}")).text()
    assert "Geheimwartende" not in text
    with practice_scope(1):
        assert gapfill.current_offer(db, gap_b) is None and not gapfill.recipients(db, oid)
        assert all(g["a"]["id"] != gap_b for g in gapfill.open_gaps(db))
    # Der Link der Praxis B bucht nur in Praxis B
    client.session.cookie_jar.clear()
    page = await (await client.get(f"/p/angebot/{token_b}")).text()
    assert "Muster" not in page and "Physio Demo" not in page
    csrf_b = _re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    await client.post(f"/p/angebot/{token_b}", data={"_csrf": csrf_b, "answer": "yes"}, allow_redirects=False)
    new = db.one("SELECT practice_id, therapist_id FROM appointments WHERE patient_id = ? AND status = 'booked'", (other,))
    assert new["practice_id"] == b["id"] and new["therapist_id"] == b["user"]
    assert db.value("SELECT COUNT(*) FROM appointments WHERE practice_id = 1 AND patient_id = ?", (other,)) == 0


async def test_mtt_never_crosses_practices(client, app, practice_b):
    """MTT: Geräte-QR-Codes, Pläne, Sätze, Vorschläge und die Live-Ansicht bleiben in der eigenen Praxis."""
    from nfizyo.core.auth import create_session
    from nfizyo.modules.mtt import repo as mtt

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        mid = mtt.add_machine(db, "Geheimpresse B")
        plan = mtt.current_plan(db, b["patient"], create=True)
        iid = mtt.add_item(db, plan, mtt.machine(db, mid))
        mtt.update_item(db, iid, load=40.0, note="Geheime Notiz B")
        mtt.approve(db, plan)
        vid, _ = mtt.start_visit(db, b["patient"], plan["id"], "2099-01-01", "qr")
        db.execute("UPDATE mtt_visits SET day = date('now', 'localtime'), started_at = ? WHERE id = ?",
                   (__import__("nfizyo.core.db", fromlist=["utcnow"]).utcnow(), vid))
        for _ in range(2):
            mtt.log_set(db, vid, mtt.item(db, iid), reps=12, load=40.0, minutes=None, effort=5, pain=7)
        mtt.refresh_suggestion(db, mtt.item(db, iid))
        sid = db.value("SELECT id FROM mtt_suggestions WHERE item_id = ?", (iid,))
        token_b = mtt.machine(db, mid)["qr_token"]
    assert sid
    for email in ("therapie@demo.n-fizyo.ch", "leitung@demo.n-fizyo.ch"):
        csrf = await login(client, email)
        live = await (await client.get("/therapie/mtt")).text()
        assert "Zoraida" not in live and "Geheim" not in live, email
        machines = await (await client.get("/therapie/mtt/geraete")).text()
        assert "Geheimpresse" not in machines, email
        assert "Geheimpresse" not in await (await client.get("/therapie/mtt/geraete/qr")).text()
        for url, data in ((f"/therapie/mtt/plan/{iid}", {"load": "1"}), (f"/therapie/mtt/vorschlag/{sid}", {"decision": "accept"})):
            r = await client.post(url, data={"_csrf": csrf, **data}, allow_redirects=False)
            assert r.status == 404, (email, url)
    assert db.value("SELECT load FROM mtt_items WHERE id = ?", (iid,)) == 40.0
    assert db.value("SELECT status FROM mtt_suggestions WHERE id = ?", (sid,)) == "open"
    with practice_scope(1):
        assert mtt.item(db, iid) is None and mtt.machine_by_token(db, token_b) is None
        assert not mtt.open_suggestions(db) or all(g["patient_id"] != b["patient"] for g in mtt.open_suggestions(db))
        assert all(v["patient_id"] != b["patient"] for v in mtt.live(db))
    # Patientin der Praxis A scannt den QR-Code der Praxis B → unbekannt, kein Besuch
    lea = pid_of(app, "Lea")
    token, _ = create_session(db, "patient", patient_id=lea, practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    assert (await client.get(f"/p/mtt/{token_b}")).status == 404
    assert (await client.get(f"/p/training/{iid}")).status == 404
    assert db.value("SELECT COUNT(*) FROM mtt_visits WHERE patient_id = ?", (lea,)) == 0


async def test_video_never_crosses_practices(client, app, practice_b):
    """Video: Räume, Links und Einwilligungen bleiben in der eigenen Praxis."""
    import re as _re

    from nfizyo.core.auth import create_session
    from nfizyo.modules.video import repo as video

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        rid = video.create_room(db, b["appointment"], b["patient"])
        tok = video.new_link(db, rid, b["patient"])
    for email, screen in (("therapie@demo.n-fizyo.ch", "therapie"), ("empfang@demo.n-fizyo.ch", "empfang")):
        csrf = await login(client, email)
        for action in ("setup", "sms"):
            r = await client.post(f"/{screen}/video/{b['appointment']}", data={"_csrf": csrf, "action": action}, allow_redirects=False)
            assert r.status == 404, (email, action)
    assert db.value("SELECT COUNT(*) FROM outbox WHERE body LIKE '%/p/video/%'") == 0
    with practice_scope(1):
        assert video.room_for(db, b["appointment"]) is None and not video.rooms_for_patient(db, b["patient"])
    # Patientin der Praxis A: Termin der Praxis B nicht erreichbar
    lea = pid_of(app, "Lea")
    token, _ = create_session(db, "patient", patient_id=lea, practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    assert (await client.get(f"/p/video/termin/{b['appointment']}")).status == 404
    # Der Link der Praxis B zeigt nur Praxis B; die Einwilligung landet in Praxis B
    client.session.cookie_jar.clear()
    page = await (await client.get(f"/p/video/{tok}")).text()
    assert "Praxis B" in page and "Physio Demo" not in page and "Muster" not in page
    await client.post(f"/p/video/{tok}", data={"_csrf": _re.search(r'name="_csrf" value="([^"]+)"', page).group(1), "consent": "1"},
                      allow_redirects=False)
    row = db.one("SELECT practice_id, patient_id FROM video_consents WHERE patient_id = ?", (b["patient"],))
    assert row["practice_id"] == b["id"]
    assert db.value("SELECT COUNT(*) FROM video_consents WHERE practice_id = 1 AND patient_id = ?", (b["patient"],)) == 0


async def test_firms_never_cross_practices(client, app, practice_b):
    """Firmenkunden der Praxis B: Firmen, Rechnungen, PDFs, Zahlungen und Export sind für Praxis A unsichtbar und unveränderbar;
    eine Bankdatei von A bucht nie auf eine Firmenrechnung von B."""
    from nfizyo.modules.billing import payment
    from nfizyo.modules.billing import repo as billing
    from nfizyo.modules.firms import api as firms_api
    from nfizyo.modules.firms import repo as firms

    db = app[DB]
    b = practice_b
    db.execute("UPDATE practices SET zip = '2000', city = 'Neuchâtel', street = 'Rue B', house_no = '1' WHERE id = ?", (b["id"],))
    with practice_scope(b["id"], b["user"]):
        payment.save_settings(db, {"iban": "CH4431999123000889012"})
        fid = firms.save_firm(db, {"name": "Geheimfirma B SA", "zip": "2000", "city": "Neuchâtel", "email": "secret@b.invalid", "lang": "fr"})
        inv = firms.get(db, firms.create(db, firms.get_firm(db, fid), subject="Atelier secret B"))
        firms.add_free_line(db, inv, label="Geheimleistung B", quantity=1, unit_price=777.0, vat_rate=0.0, kind="service")
        firms.finalize(db, firms.get(db, inv["id"]))
        inv = firms.get(db, inv["id"])
    for email, S in (("leitung@demo.n-fizyo.ch", "/leitung"), ("empfang@demo.n-fizyo.ch", "/empfang")):
        csrf = await login(client, email)
        for url in (f"{S}/firmen/{fid}", f"{S}/firmen/rechnungen/{inv['id']}", f"{S}/firmen/rechnungen/{inv['id']}/pdf",
                    f"{S}/firmen/rechnungen/{inv['id']}/mahnung/1/pdf"):
            assert (await client.get(url, allow_redirects=False)).status in (403, 404), f"{email} {url}"
        for url, data in ((f"{S}/firmen/{fid}", {"do": "save", "name": "Übernommen"}), (f"{S}/firmen/{fid}", {"do": "invoice"}),
                          (f"{S}/firmen/rechnungen/{inv['id']}", {"do": "payment", "amount": "777"}),
                          (f"{S}/firmen/rechnungen/{inv['id']}", {"do": "cancel", "reason": "x"}),
                          (f"{S}/firmen/rechnungen/{inv['id']}", {"do": "email"})):
            r = await client.post(url, data={"_csrf": csrf, **data}, allow_redirects=False)
            assert r.status in (403, 404), f"{email} POST {url}"
        for url in (f"{S}/firmen", f"{S}/firmen?ansicht=rechnungen", f"{S}/firmen?ansicht=offen", f"{S}/firmen?alle=1&q=Neuch"):
            text = await (await client.get(url)).text()
            assert "Geheimfirma" not in text and "Atelier secret" not in text, url
    assert db.value("SELECT name FROM firms WHERE id = ?", (fid,)) == "Geheimfirma B SA"
    assert db.value("SELECT status FROM firm_invoices WHERE id = ?", (inv["id"],)) == "final"
    assert db.value("SELECT COUNT(*) FROM firm_payments WHERE invoice_id = ?", (inv["id"],)) == 0
    assert db.value("SELECT COUNT(*) FROM firm_invoices WHERE firm_id = ?", (fid,)) == 1
    assert db.value("SELECT COUNT(*) FROM outbox WHERE recipient = 'secret@b.invalid'") == 0
    # Bankdatei von A mit der Referenz von B: wird nicht gebucht
    camt = (f"""<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.08"><BkToCstmrDbtCdtNtfctn>"""
            f"""<Ntfctn><Ntry><Amt Ccy="CHF">777.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-10-20</Dt></BookgDt>"""
            f"""<NtryDtls><TxDtls><Refs><AcctSvcrRef>FX1</AcctSvcrRef></Refs><Amt Ccy="CHF">777.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>"""
            f"""<RmtInf><Strd><CdtrRefInf><Ref>{inv['qr_reference']}</Ref></CdtrRefInf></Strd></RmtInf></TxDtls></NtryDtls></Ntry></Ntfctn>"""
            f"""</BkToCstmrDbtCdtNtfctn></Document>""").encode()
    with practice_scope(1, 1):
        assert firms.get_firm(db, fid) is None and firms.get(db, inv["id"]) is None
        assert firms_api.claim_payment(db, reference=inv["qr_reference"], amount=777.0, booked_on="2026-10-20", debtor=None, bank_ref="FY") is None
        result = billing.import_payments(db, "a.xml", camt)
        assert result["matched"] == 0
        assert all(x["name"] != "Geheimfirma B SA" for x in firms_api.open_items(db))
    assert db.value("SELECT COUNT(*) FROM firm_payments WHERE invoice_id = ?", (inv["id"],)) == 0
    await login(client, "leitung@demo.n-fizyo.ch")
    from datetime import date

    r = await client.get(f"/leitung/export/journal.csv?von={date.today().isoformat()}&bis={date.today().isoformat()}")
    text = (await r.read()).decode("utf-8-sig")
    assert "Geheimfirma" not in text and "777.00" not in text


async def test_absences_holidays_and_locations_never_cross_practices(client, app, practice_b):
    """Abwesenheiten, Schliessungen, Feiertage, Standorte/Räume und die Personenwahl bei Online-Anfragen (0.9.0):
    Praxis A sieht und ändert nichts von Praxis B; Abwesenheiten von B sperren bei A nichts."""
    from datetime import datetime, timedelta

    from nfizyo.core.timeutil import TZ, today
    from nfizyo.modules.agenda import absences
    from nfizyo.modules.agenda import repo as agenda
    from nfizyo.modules.booking import repo as booking

    db = app[DB]
    b = practice_b
    day = today() + timedelta(days=3)
    with practice_scope(b["id"], b["user"]):
        abs_b = absences.save(db, {"user_id": b["user"], "kind": "vacation", "starts_on": day.isoformat(), "ends_on": day.isoformat(),
                                   "start_time": None, "end_time": None, "note": "Geheimurlaub B"})
        clo_b = absences.save(db, {"user_id": None, "kind": "closure", "starts_on": day.isoformat(), "ends_on": day.isoformat(),
                                   "start_time": None, "end_time": None, "note": "Geheimschliessung B"})
        absences.save_canton(db, "GE")
        absences.set_enabled(db, "GE", "jeune_ge", False)
        loc_b = agenda.add_location(db, "Geheimstandort B", None, None)
        agenda.add_room_or_device(db, "room", loc_b, "Geheimraum B")
        room_b = db.value("SELECT id FROM rooms WHERE practice_id = ?", (b["id"],))
    for email, S in (("empfang@demo.n-fizyo.ch", "/empfang"), ("leitung@demo.n-fizyo.ch", "/leitung")):
        csrf = await login(client, email)
        assert (await client.get(f"{S}/agenda/abwesenheiten/{abs_b}", allow_redirects=False)).status == 404
        for data in ({"do": "delete"}, {"do": "save", "who": "praxis", "kind": "closure", "starts_on": "2027-01-01", "ends_on": "2027-01-01"}):
            r = await client.post(f"{S}/agenda/abwesenheiten/{clo_b}", data={"_csrf": csrf, **data}, allow_redirects=False)
            assert r.status == 404, f"{email} {data}"
        # neue Abwesenheit für eine Person der Praxis B: abgelehnt
        r = await client.post(f"{S}/agenda/abwesenheiten", data={"_csrf": csrf, "who": str(b["user"]), "kind": "vacation",
                                                                 "starts_on": "2027-01-04", "ends_on": "2027-01-04"})
        assert r.status == 400
        for url in (f"{S}/agenda/abwesenheiten?alle=1", f"{S}/agenda?view=tag&d={day}", f"{S}/agenda?view=woche&d={day}"):
            text = await (await client.get(url)).text()
            assert "Geheim" not in text and "Chef B" not in text, f"{email} {url}"
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    for url in ("/leitung/einstellungen/feiertage", "/leitung/einstellungen/standorte", "/therapie/agenda/abwesenheiten",
                f"/therapie/heute?d={day}", f"/leitung/einstellungen/standorte?bearbeiten={loc_b}"):
        text = await (await client.get(url)).text()
        assert "Geheim" not in text and "Chef B" not in text and "Kanton GE" not in text, url
    for data in ({"do": "rename", "table": "rooms", "id": str(room_b), "name": "Übernommen"},
                 {"do": "edit_location", "id": str(loc_b), "name": "Übernommen"},
                 {"do": "toggle", "table": "rooms", "id": str(room_b)}):
        await client.post("/leitung/einstellungen/standorte", data={"_csrf": csrf, **data})
    assert db.value("SELECT name FROM rooms WHERE id = ?", (room_b,)) == "Geheimraum B"
    assert db.value("SELECT active FROM rooms WHERE id = ?", (room_b,)) == 1
    assert db.value("SELECT name FROM locations WHERE id = ?", (loc_b,)) == "Geheimstandort B"
    await client.post("/leitung/einstellungen/feiertage", data={"_csrf": csrf, "do": "toggle", "code": "berchtold", "enabled": "0"})
    assert db.value("SELECT note FROM absences WHERE id = ?", (abs_b,)) == "Geheimurlaub B"
    assert db.value("SELECT COUNT(*) FROM absences WHERE id = ?", (clo_b,)) == 1
    with practice_scope(1, 1):
        assert absences.get(db, abs_b) is None and absences.canton(db) == "BE"
        assert all(o.absence_id not in (abs_b, clo_b) for o in absences.periods(db, day, day, everyone=True))
        assert not absences.closures_ahead(db, today())
        assert all(r["practice_id"] == 1 for r in absences.listing(db))
    with practice_scope(b["id"], b["user"]):
        assert absences.canton(db) == "GE" and absences.overrides(db, "BE") == {}
        assert absences.overrides(db, "GE") == {"jeune_ge": False}
    # öffentliche Seite und Patienten-App von A: keine Schliessung von B
    client.session.cookie_jar.clear()
    slug_a = db.value("SELECT slug FROM practices WHERE id = 1")
    import re

    html = await (await client.get(f"/p/termin/{slug_a}")).text()
    type_a = re.search(r'href="\?art=(\d+)"', html).group(1)
    html = await (await client.get(f"/p/termin/{slug_a}?art={type_a}")).text()
    assert "data-closures" not in html and "Geheim" not in html
    # Online-Anfrage von A: Vorschläge und Auswahl nie aus Praxis B
    with practice_scope(1, 1):
        t = next(iter(booking.bookable_types(db)))
        rid = booking.create_request(db, type_row=t, therapist_id=2, start=datetime.now(TZ) + timedelta(days=6), location_id=None,
                                     data={"first_name": "Zoraida", "last_name": "Geheimnisvoll", "birthdate": "1970-01-01",
                                           "phone": "+41790000000", "lang": "fr", "note": None})
        db.execute("UPDATE booking_requests SET status = 'requested' WHERE id = ?", (rid,))
        assert all(m["p"]["id"] != b["patient"] for m in booking.likely_patients(db, booking.get(db, rid)))
        assert booking.patient_for_choice(db, str(b["patient"])) is None
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    text = await (await client.get("/empfang/online-anfragen")).text()
    assert f'name="patient_choice" value="{b["patient"]}"' not in text and "P00001 · Zoraida" not in text
    await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid),
                                                        "patient_choice": str(b["patient"])})
    assert db.value("SELECT status FROM booking_requests WHERE id = ?", (rid,)) == "requested"
    # Suche über die Nummer: findet nur die Person mit dieser Nummer in Praxis A (Nummern sind je Praxis), nie Praxis B
    await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid),
                                                        "patient_choice": "other", "patient": f"Geheimnisvoll Zoraida · 01.01.1970 · "
                                                        f"{db.value('SELECT number FROM patients WHERE id = ?', (b['patient'],))}"})
    linked = db.value("SELECT patient_id FROM booking_requests WHERE id = ?", (rid,))
    assert linked != b["patient"] and (linked is None or db.value("SELECT practice_id FROM patients WHERE id = ?", (linked,)) == 1)
    assert db.value("SELECT COUNT(*) FROM appointments WHERE patient_id = ? AND practice_id = 1", (b["patient"],)) == 0


async def test_completion_090_never_crosses_practices(client, app, practice_b, monkeypatch):
    """0.9.0: Notizvorlagen, Benachrichtigungen, Einrichtungs-Checkliste und Datenübernahme (Verordnungen, offene Posten)
    bleiben in der eigenen Praxis."""
    import aiohttp
    import json

    from conftest import csrf_of

    from nfizyo.modules.documentation import repo as notes
    from nfizyo.modules.notify import api as notify_api
    from nfizyo.modules.notify import service as notify
    from nfizyo.modules.settings import setup_check

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        own_b = notes.save_template(db, {"name": "Geheimvorlage B", "lang": "fr", "befund": "Geheim"}, owner_id=b["user"])
        shared_b = notes.save_template(db, {"name": "Geheimpraxisvorlage B", "lang": "fr", "plan": "Geheim"}, owner_id=None)
        notify.save_settings(db, b["id"], "off", True, b["user"])
    qid = db.execute("INSERT INTO notify_queue (practice_id, patient_id, kind, ref_id, status, channel, due_at, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)", (b["id"], b["patient"], "message", b["message"], "pending", "sms",
                                                   "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"))
    lea = pid_of(app, "Lea")
    # Notizvorlagen: weder sehen, laden, ändern noch löschen
    for email in ("therapie@demo.n-fizyo.ch", "leitung@demo.n-fizyo.ch"):
        csrf = await login(client, email)
        assert (await client.get(f"/therapie/notizen/vorlagen?bearbeiten={own_b}")).status == 404
        page = await (await client.get(f"/therapie/patienten/{lea}/notizen/neu?vorlage={shared_b}")).text()
        assert "Geheim" not in page
        for tid in (own_b, shared_b):
            for data in ({"do": "delete", "id": str(tid)}, {"id": str(tid), "name": "Übernommen"}):
                r = await client.post("/therapie/notizen/vorlagen", data={"_csrf": csrf, **data}, allow_redirects=False)
                assert r.status == 404, (email, tid, data)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    assert (await client.get(f"/leitung/einstellungen/notizvorlagen?bearbeiten={shared_b}")).status == 404
    for data in ({"do": "delete", "id": str(shared_b)}, {"id": str(shared_b), "name": "Übernommen"}):
        r = await client.post("/leitung/einstellungen/notizvorlagen", data={"_csrf": csrf, **data}, allow_redirects=False)
        assert r.status == 404
    assert db.value("SELECT COUNT(*) FROM note_templates WHERE practice_id = ? AND name LIKE 'Geheim%'", (b["id"],)) == 2
    # Benachrichtigungen: Einstellungen und Stand nur der eigenen Praxis
    r = await client.post("/leitung/einstellungen/benachrichtigungen", data={"_csrf": csrf, "mode": "sms"}, allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT mode FROM notify_settings WHERE practice_id = ?", (b["id"],)) == "off"
    page = await (await client.get("/leitung/einstellungen/benachrichtigungen")).text()
    assert "Praxis B" not in page
    with practice_scope(1, 1):
        assert notify_api.status_for(db, "message", [b["message"]]) == {}
    notify.process_due(app)
    assert db.value("SELECT status FROM notify_queue WHERE id = ?", (qid,)) == "skipped"      # B: aus – nichts versendet
    assert db.value("SELECT COUNT(*) FROM outbox WHERE body LIKE '%Praxis B%'") == 0
    # Einrichtungs-Checkliste: Ausblenden betrifft nur die eigene Praxis
    real = setup_check.items
    monkeypatch.setattr(setup_check, "items", lambda request: [dict(it, done=True) for it in real(request)])
    r = await client.post("/leitung/einrichtung", data={"_csrf": csrf, "do": "dismiss"}, allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT COUNT(*) FROM practice_setup WHERE practice_id = ?", (b["id"],)) == 0
    monkeypatch.setattr(setup_check, "items", real)
    await login(client, b["email"], b["password"])
    page = await (await client.get("/leitung/uebersicht")).text()
    assert "Mise en place du cabinet" in page and "Lea" not in page and "Physio Demo" not in page
    # Datenübernahme: Personen der Praxis B werden nie gefunden
    await login(client, "leitung@demo.n-fizyo.ch")
    db.execute("UPDATE patients SET number = 'GEHEIM-1' WHERE id = ?", (b["patient"],))
    for kind, csv in (("prescriptions", "Patientennr;Name;Vorname;Geburtsdatum;Ausgestellt;Sitzungen\n"
                                        "GEHEIM-1;;;;01.10.2026;9\n;Geheimnisvoll;Zoraida;01.01.1970;01.10.2026;9\n"),
                      ("balances", "Patientennr;Name;Vorname;Geburtsdatum;Rechnungsnr;Rechnungsdatum;Offen\n"
                                   "GEHEIM-1;;;;9001;01.10.2026;50.00\n;Geheimnisvoll;Zoraida;01.01.1970;9002;01.10.2026;50.00\n")):
        html = await (await client.get("/leitung/import")).text()
        form = aiohttp.FormData()
        form.add_field("_csrf", csrf_of(html))
        form.add_field("kind", kind)
        form.add_field("file", csv.encode(), filename="b.csv")
        r = await client.post("/leitung/import", data=form, allow_redirects=False)
        url = r.headers["Location"]
        job = db.one("SELECT * FROM import_jobs ORDER BY id DESC LIMIT 1")
        assert job["practice_id"] == 1
        html = await (await client.get(url)).text()
        mapping = {f"f_{k}": str(v) for k, v in json.loads(job["mapping"]).items()}
        await client.post(url, data={"_csrf": csrf_of(html), "do": "map", **mapping}, allow_redirects=False)
        preview = json.loads(db.value("SELECT preview FROM import_jobs WHERE id = ?", (job["id"],)))
        assert preview["new"] == 0 and preview["error"] == 2, (kind, preview)
        assert all(r["label"] == "" and r["notes"] == ["unknown_patient"] for r in preview["rows"]), preview  # nie ein Treffer in B
    assert db.value("SELECT COUNT(*) FROM prescriptions WHERE patient_id = ?", (b["patient"],)) == 0
    assert db.value("SELECT COUNT(*) FROM invoices WHERE imported_from IS NOT NULL") == 0



async def test_reception_billing_090_never_crosses_practices(client, app, practice_b):
    """0.9.0 Empfang/Abrechnung: Doppelte Einträge (zusammenführen, löschen), Verordnungen bearbeiten, Zahlungen korrigieren,
    Kassen-Korrektur, Taxpunktwert je Gruppe – nie über die Praxis hinaus."""
    from nfizyo.core import patients as core_patients
    from nfizyo.core.timeutil import today
    from nfizyo.modules.billing import repo as billing
    from nfizyo.modules.cashdesk import repo as cash

    db = app[DB]
    b = practice_b
    day = today().isoformat()
    with practice_scope(b["id"], b["user"]):
        rid_b = core_patients.add_prescription(db, b["patient"], sessions=9)
        pay_b = billing.add_payment(db, amount=50, booked_on=day, source="manual", debtor="Geheimzahler B")
        cash.close_day(db, day, 0, None)
        billing.set_tp_value(db, "kvg312", 1.23, "2026-01-01", "B", group="css")
    lea = pid_of(app, "Lea")
    with practice_scope(1):
        rid_a = core_patients.add_prescription(db, lea, sessions=9)
        a_inv = db.value("SELECT id FROM invoices WHERE practice_id = 1 LIMIT 1")

    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    # Doppelte Einträge: Personen der Praxis B nie sichtbar, nie zusammenführbar, nie löschbar
    for url in ("/leitung/einstellungen/duplikate", f"/leitung/einstellungen/duplikate?behalten={b['patient']}&doppelt={lea}",
                "/leitung/einstellungen/duplikate?q=Zoraida", f"/leitung/einstellungen/duplikate?doppelt={b['patient']}"):
        html = await (await client.get(url)).text()
        assert "Geheimnisvoll" not in html and "1970" not in html, url        # Suchbegriff selbst steht im Feld
    for data in ({"do": "merge", "keep_id": str(lea), "drop_id": str(b["patient"]), "confirm": "1"},
                 {"do": "merge", "keep_id": str(b["patient"]), "drop_id": str(lea), "confirm": "1"},
                 {"do": "delete", "patient_id": str(b["patient"]), "confirm": "1"}):
        r = await client.post("/leitung/einstellungen/duplikate", data={"_csrf": csrf, **data}, allow_redirects=False)
        assert r.status == 404, data
    assert db.value("SELECT 1 FROM patients WHERE id = ?", (b["patient"],)) and db.value("SELECT 1 FROM patients WHERE id = ?", (lea,))
    assert db.value("SELECT COUNT(*) FROM patient_merges") == 0
    # Zahlungen der Praxis B: nicht ausblenden, zuordnen, aufheben, stornieren
    for do in ("dismiss", "undismiss", "unassign"):
        await client.post("/leitung/abrechnung", data={"_csrf": csrf, "do": do, "payment_id": str(pay_b), "reason": "x"})
    await client.post("/leitung/abrechnung", data={"_csrf": csrf, "do": "assign", "payment_id": str(pay_b), "number": "x"})
    if a_inv:
        r = await client.post(f"/leitung/rechnungen/{a_inv}", data={"_csrf": csrf, "do": "pay_reverse", "payment_id": str(pay_b), "reason": "x"},
                              allow_redirects=False)
        assert r.status in (302, 404)
    pb = db.one("SELECT * FROM invoice_payments WHERE id = ?", (pay_b,))
    assert pb["dismissed_at"] is None and pb["invoice_id"] is None
    assert db.value("SELECT COUNT(*) FROM invoice_payments WHERE reversal_of = ?", (pay_b,)) == 0
    assert db.value("SELECT COUNT(*) FROM payment_moves WHERE payment_id = ?", (pay_b,)) == 0
    html = await (await client.get("/leitung/abrechnung?ansicht=zahlungen")).text()
    assert "Geheimzahler" not in html
    # Kassen-Korrektur auf einer Quittung der Praxis B
    r = await client.post(f"/leitung/kasse/belege/{b['sale']}", data={"_csrf": csrf, "do": "correct_storno", "reason": "x"},
                          allow_redirects=False)
    assert r.status == 404 and db.value("SELECT COUNT(*) FROM cash_corrections") == 0
    # Taxpunktwert der Gruppe aus Praxis B gilt nicht in Praxis A
    with practice_scope(1):
        assert all(v["practice_id"] == 1 for v in billing.tp_values(db))
        assert billing.tp_value(db, "kvg312", "2026-06-01") != 1.23
    html = await (await client.get("/leitung/einstellungen/tarife")).text()
    assert "1.23" not in html
    # Verordnung der Praxis B bearbeiten oder beenden: 404 (auch unter der Adresse einer eigenen Person)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    for url in (f"/empfang/patienten/{lea}/verordnungen/{rid_b}", f"/empfang/patienten/{b['patient']}/verordnungen/{rid_b}"):
        r = await client.post(url, data={"_csrf": csrf, "do": "status", "status": "expired"}, allow_redirects=False)
        assert r.status == 404, url
    assert db.value("SELECT status FROM prescriptions WHERE id = ?", (rid_b,)) == "active"
    # und umgekehrt: Praxis B erreicht nichts aus Praxis A
    csrf = await login(client, b["email"], b["password"])
    r = await client.post("/leitung/einstellungen/duplikate", data={"_csrf": csrf, "do": "delete", "patient_id": str(lea), "confirm": "1"},
                          allow_redirects=False)
    assert r.status == 404
    html = await (await client.get(f"/leitung/einstellungen/duplikate?behalten={lea}")).text()
    assert "Muster" not in html
    await login(client, b["reception_email"], b["password"])
    r = await client.post(f"/empfang/patienten/{lea}/verordnungen/{rid_a}", data={"_csrf": csrf, "do": "edit", "sessions": "1"},
                          allow_redirects=False)
    assert r.status in (403, 404)
    assert db.value("SELECT sessions FROM prescriptions WHERE id = ?", (rid_a,)) == 9


async def test_medidata_simulator_history_never_crosses_practices(client, app, practice_b):
    """MediData-Seite der Leitung: Testsendungen an den Simulator (Protokoll) nur der eigenen Praxis."""
    from nfizyo.core.audit import audit
    from nfizyo.modules.billing import simulator

    db = app[DB]
    with practice_scope(practice_b["id"], practice_b["user"]):
        audit(db, simulator.AUDIT_ACTION, "billing_settings", practice_b["id"], None,
              {"target": "antwort", "flag": "PriorNotice", "number": "SIM-PRAXIS-B", "status": "sent", "reference": "TR-GEHEIM-B"})
    with practice_scope(1):
        assert simulator.recent(db) == []
    await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.get("/leitung/einstellungen/medidata")
    assert r.status == 200
    html = await r.text()
    assert "SIM-PRAXIS-B" not in html and "TR-GEHEIM-B" not in html
    with practice_scope(practice_b["id"]):
        assert [x["number"] for x in simulator.recent(db)] == ["SIM-PRAXIS-B"]


async def test_medidata_webservice_data_never_crosses_practices(client, app, practice_b, monkeypatch):
    """MediData-Webservice: Teilnehmerverzeichnis, abgeholte Dokumente, Benachrichtigungen, Abfrage-Stand und Status der
    Sendungen gehören je einer Praxis. Der Zugang der Box gehört genau einer Praxis (NFIZYO_MEDIDATA_PRACTICE)."""
    import dataclasses

    from nfizyo.core.keys import SETTINGS
    from nfizyo.modules.billing import api as billing_api
    from nfizyo.modules.billing import medidata, medidata_sync
    from nfizyo.modules.billing import repo as billing

    db = app[DB]
    b = practice_b
    with practice_scope(b["id"], b["user"]):
        db.execute("INSERT INTO medidata_participants (practice_id, gln_participant, gln_receiver, name, law_types, not_supported,"
                   " fetched_at) VALUES (?,?,?,?,?,?,?)", (b["id"], "2099990077777", "2099990077777", "Geheimkasse B", "[1]", "[]",
                                                           "2026-11-09T08:00:00+00:00"))
        tid_b = billing.add_transmission(db, billing.get(db, b["invoice"]), kind="invoice", guid="guid-geheim-b", modus="test",
                                         channel="medidata", status="sent", xml=b"<x/>", reference="tref-geheim-b",
                                         document_reference="dref-geheim-b")
        medidata_sync.store_notification(db, {"id": 777001, "severity": "ERROR", "subject": {"de": "Geheimmeldung B"},
                                              "message": {"de": "Geheimtext B"}, "errorCode": "UPLOAD:XML-NOT-VALID",
                                              "transmissionReference": "tref-geheim-b"})
        did_b = medidata_sync.store_download(db, {"transmissionReference": "dl-geheim-b", "docType": "application/x-geheim-b"},
                                             b'<?xml version="1.0"?><geheim>B</geheim>')
        medidata_sync._mark(db, "participants", ok=False, error="Fehler-geheim-B")
        nid_b = db.value("SELECT id FROM medidata_notifications WHERE notification_id = 777001")
        assert db.value("SELECT invoice_id FROM medidata_notifications WHERE id = ?", (nid_b,)) == b["invoice"]
        db.execute("UPDATE invoice_transmissions SET status = 'unknown', resolution = NULL WHERE id = ?", (tid_b,))
    # Praxis A: Funktionen sehen nichts aus Praxis B
    with practice_scope(1):
        assert medidata_sync.notifications(db) == [] and medidata_sync.unhandled_downloads(db) == []
        assert medidata_sync.open_count(db) == 0 and medidata_sync.state(db) == {}
        assert medidata_sync.directory_info(db)["count"] == 0 and medidata_sync.reach(db, "2099990077777") is None
        assert billing_api.medidata_search(db, "Geheimkasse") == [] and billing_api.medidata_participant(db, "2099990077777") is None
        assert medidata_sync.latest_states(db, [b["invoice"]]) == {}
        # gleiche Referenzen in einer Benachrichtigung für Praxis A: nie mit der Sendung/Rechnung von B verknüpft
        medidata_sync.store_notification(db, {"id": 777002, "severity": "ERROR", "errorCode": "UPLOAD:XML-NOT-VALID",
                                              "transmissionReference": "tref-geheim-b", "documentReference": "dref-geheim-b",
                                              "correlationReference": db.value("SELECT number FROM invoices WHERE id = ?",
                                                                               (b["invoice"],)) or "x"})
        row = db.one("SELECT * FROM medidata_notifications WHERE notification_id = 777002")
        assert row["practice_id"] == 1 and row["invoice_id"] is None and row["transmission_id"] is None
        medidata_sync.resolve_unknown(db, tid_b, "status")
        medidata_sync.mark_seen(db, "medidata_notifications", nid_b, 1)
        medidata_sync.mark_seen(db, "medidata_downloads", did_b, 1)
    t_b = db.one("SELECT * FROM invoice_transmissions WHERE id = ?", (tid_b,))
    assert t_b["status"] == "unknown" and t_b["resolution"] is None and t_b["md_status"] == "ERROR"
    assert db.value("SELECT seen_at FROM medidata_notifications WHERE id = ?", (nid_b,)) is None
    assert db.value("SELECT seen_at FROM medidata_downloads WHERE id = ?", (did_b,)) is None
    # Seiten der Leitung A
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.get(f"/leitung/einstellungen/medidata/dokument/{did_b}", allow_redirects=False)
    assert r.status == 404
    for do, rid in (("seen_note", nid_b), ("seen_doc", did_b)):
        await client.post("/leitung/einstellungen/medidata", data={"_csrf": csrf, "do": do, "id": str(rid)})
    assert db.value("SELECT seen_at FROM medidata_notifications WHERE id = ?", (nid_b,)) is None
    assert db.value("SELECT seen_at FROM medidata_downloads WHERE id = ?", (did_b,)) is None
    html = await (await client.get("/leitung/einstellungen/medidata")).text()
    for secret in ("Geheimmeldung B", "Geheimtext B", "application/x-geheim-b", "Fehler-geheim-B"):
        assert secret not in html
    html = await (await client.get("/leitung/einstellungen/versicherer?md=Geheimkasse")).text()
    assert "Geheimkasse B" not in html and "2099990077777" not in html
    n_ins = db.value("SELECT COUNT(*) FROM insurers WHERE practice_id = 1")
    await client.post("/leitung/einstellungen/versicherer", data={"_csrf": csrf, "do": "add_md", "gln": "2099990077777",
                                                                  "law": "KVG", "tiers": "payant"})
    assert db.value("SELECT COUNT(*) FROM insurers WHERE practice_id = 1") == n_ins
    assert db.value("SELECT COUNT(*) FROM insurers WHERE name = 'Geheimkasse B'") == 0
    # Zugang der Box: mehrere Praxen ohne NFIZYO_MEDIDATA_PRACTICE → niemand; mit Angabe → nur diese Praxis
    monkeypatch.setattr(medidata.MediDataClient, "configured", property(lambda self: True))
    s = app[SETTINGS]
    assert medidata_sync.owner_practice_id(db, s) is None
    s_b = dataclasses.replace(s, medidata_practice="praxis-b")
    assert medidata_sync.owner_practice_id(db, s_b) == b["id"]
    with practice_scope(1):
        assert not medidata_sync.active_here(db, s) and not medidata_sync.active_here(db, s_b)
    with practice_scope(b["id"]):
        assert medidata_sync.active_here(db, s_b)
        assert [n["notification_id"] for n in medidata_sync.notifications(db)] == [777001]
        assert medidata_sync.directory_info(db)["count"] == 1


async def test_nsystem_support_and_notices_never_cross_practices(client, app, practice_b):
    """Hilfe › Support: Anfragen und Antworten nur der eigenen Praxis; Patientennamen der anderen Praxis zählen nicht;
    Protokoll Einstellungen › NSystem ohne Namen aus der anderen Praxis; ausgeblendete Hinweise pro Person."""
    from nfizyo.core import nsystem, patientdata

    db = app[DB]
    b = practice_b
    uid_b = nsystem.create_request(db, practice_id=b["id"], user_id=b["user"], area="billing", urgency="urgent",
                                   body="Geheimanfrage B", with_access=False)
    nsystem.apply_reply(db, {"uid": uid_b, "rev": 1, "status": "done", "reply": "Geheimantwort B", "reply_by": "NSystem"})
    nsystem.log_event(db, "support_requested", detail={"hours": 4}, practice_id=b["id"], user_id=b["user"])
    # Patientendaten-Prüfung: nur die eigene Praxis (Namen aus B blockieren A nicht und umgekehrt)
    assert patientdata.scan("Zoraida Geheimnisvoll", db, b["id"]) == ["name"]
    assert patientdata.scan("Zoraida Geheimnisvoll", db, 1) == []
    for email, screen in (("leitung@demo.n-fizyo.ch", "leitung"), ("therapie@demo.n-fizyo.ch", "therapie"), ("empfang@demo.n-fizyo.ch", "empfang")):
        await login(client, email)
        html = await (await client.get(f"/{screen}/hilfe")).text()
        assert "Geheimanfrage B" not in html and "Geheimantwort B" not in html
    await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/nsystem")).text()
    assert "Chef B" not in html and "Leitung einer anderen Praxis auf diesem Server" in html
    assert nsystem.unread_replies(db, 1) == 0 and nsystem.unread_replies(db, b["id"]) == 1
    nsystem.mark_replies_seen(db, 1)
    assert nsystem.unread_replies(db, b["id"]) == 1
    # Wartungshinweis: Ausblenden gilt nur für diese Person
    nsystem.sync_notices(db, [{"id": "n-b", "text_de": "Wartung heute", "text_fr": "Maintenance", "starts_at": "2026-01-01T00:00:00+00:00",
                               "ends_at": "2099-01-01T00:00:00+00:00"}])
    chef_b = db.one("SELECT * FROM users WHERE id = ?", (b["user"],))
    nsystem.dismiss_notice(db, "n-b", chef_b)
    owner_a = db.one("SELECT * FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    assert nsystem.active_notices(db, chef_b, "fr") == [] and len(nsystem.active_notices(db, owner_a, "de")) == 1


async def test_patient_data_access_export_contains_only_that_person(client, app, practice_b):
    """Datenauskunft (Art. 25 DSG): ZIP mit den Daten EINER Person der eigenen Praxis – nie Daten anderer Personen oder Praxen,
    keine Geheimnisse. Nummer der fremden Praxis findet nichts."""
    import io
    import zipfile

    from conftest import csrf_of, login
    from nfizyo.core.keys import DB

    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    lea = db.one("SELECT id, number FROM patients WHERE practice_id = 1 AND first_name = 'Lea'")
    page = await (await client.get("/leitung/einstellungen/export")).text()
    assert 'action="/leitung/export/auskunft"' in page
    r = await client.post("/leitung/export/auskunft", data={"_csrf": csrf_of(page), "patient": lea["number"], "password": "falsch"},
                          allow_redirects=False)
    assert r.status == 302 and r.headers.get("Content-Type", "").startswith("application/zip") is False
    r = await client.post("/leitung/export/auskunft", data={"_csrf": csrf, "patient": lea["number"], "password": "demo-praxis"})
    assert r.status == 200 and r.headers["Content-Type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(await r.read()))
    names = zf.namelist()
    assert "tabellen/patients.csv" in names and "LIESMICH.txt" in names and "tabellen/appointments.csv" in names
    blob = b"".join(zf.read(n) for n in names if n.endswith(".csv")).decode("utf-8", "replace")
    others = db.query("SELECT number, first_name, last_name, birthdate FROM patients WHERE id != ? AND number != ?", (lea["id"], lea["number"]))
    leaked = [o["number"] for o in others if o["number"] in blob or (o["birthdate"] and f"{o['first_name']};{o['last_name']}" in blob)]
    assert not leaked, leaked
    assert "Geheimnisvoll" not in blob and "Geheimer Befund B" not in blob
    assert "password_hash" not in blob and "token_hash" not in blob and "code_hash" not in blob
    pcsv = zf.read("tabellen/patients.csv").decode("utf-8-sig").splitlines()
    assert len(pcsv) == 2                                   # Kopfzeile + genau diese Person
    # fremde Praxis: deren Person ist hier nicht auffindbar (Suche nur in der eigenen Praxis)
    r = await client.post("/leitung/export/auskunft", data={"_csrf": csrf, "patient": "Geheimnisvoll", "password": "demo-praxis"},
                          allow_redirects=False)
    assert r.status == 302 and "auskunft" in r.headers["Location"]
