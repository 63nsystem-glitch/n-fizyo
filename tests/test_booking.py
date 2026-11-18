"""Online-Termine (öffentliche Anfrage mit SMS-Code, Bestätigung durch den Empfang) und E-Mail an Patient/innen."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import unescape

from conftest import csrf_of, login, pid_of

from nfizyo.core import mail as core_mail
from nfizyo.core.keys import DB, MAIL, SETTINGS
from nfizyo.core.modules import set_module_enabled
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import TZ
from nfizyo.modules.booking import repo as booking

URL = "/p/termin/demo"


def _slug(app) -> str:
    return app[DB].value("SELECT slug FROM practices WHERE id = 1")


def _first_slot(html: str, pick: int = 0) -> str:
    return unescape(re.findall(r'class="slot" href="(\?art=\d+&(?:amp;)?s=[^"]+)"', html)[pick])


async def _request(client, app, *, email_ok: bool = True, first="Mira", last="Neu", bd="1985-02-03", pick: int = 0) -> int:
    url = f"/p/termin/{_slug(app)}"
    html = await (await client.get(url)).text()
    type_link = re.search(r'href="\?art=(\d+)"', html).group(1)
    html = await (await client.get(f"{url}?art={type_link}")).text()
    slot = _first_slot(html, pick)
    html = await (await client.get(url + slot)).text()
    token = re.search(r'name="slot" value="([^"]+)"', html).group(1)
    data = {"_csrf": csrf_of(html), "step": "data", "slot": token, "first_name": first, "last_name": last, "birthdate": bd,
            "phone": "079 555 44 33", "email": "mira.neu@example.invalid", "insurance": "KVG", "consent": "1", "website": ""}
    if email_ok:
        data["email_ok"] = "1"
    r = await client.post(url, data=data)
    html = await r.text()
    assert r.status == 200, html[-800:]
    code = re.search(r"Code lautet (\d{6})", html).group(1)
    rid = re.search(r'name="rid" value="([^"]+)"', html).group(1)
    r = await client.post(url, data={"_csrf": csrf_of(html), "step": "code", "rid": rid, "code": code})
    assert "Anfrage erhalten" in await r.text()
    return app[DB].value("SELECT MAX(id) FROM booking_requests WHERE status = 'requested'")


async def test_public_flow_holds_slot_and_sends_code(client, app):
    db = app[DB]
    url = f"/p/termin/{_slug(app)}"
    html = await (await client.get(url)).text()
    assert "Termin anfragen" in html and "144" in html and "Erstbehandlung" in html
    rid = await _request(client, app)
    r = db.one("SELECT * FROM booking_requests WHERE id = ?", (rid,))
    assert r["status"] == "requested" and r["phone"] == "+41795554433" and r["email_ok"] == 1 and r["code_hash"] is None
    sms = db.one("SELECT * FROM outbox WHERE channel = 'sms' AND recipient = '+41795554433' ORDER BY id DESC")
    assert re.search(r"\d{6}", sms["body"]) and sms["status"] == "logged"
    # die angefragte Zeit ist nicht mehr frei
    with practice_scope(1):
        t = next(t for t in booking.bookable_types(db) if t["id"] == r["treatment_type_id"])
        start = datetime.fromisoformat(r["starts_at"]).astimezone(TZ)
        assert not booking.slot_still_free(db, t, r["therapist_id"], start, r["location_id"])


async def test_public_validation_and_limits(client, app):
    url = f"/p/termin/{_slug(app)}"
    html = await (await client.get(url)).text()
    type_id = re.search(r'href="\?art=(\d+)"', html).group(1)
    html = await (await client.get(f"{url}?art={type_id}")).text()
    html = await (await client.get(url + _first_slot(html))).text()
    token = re.search(r'name="slot" value="([^"]+)"', html).group(1)
    base = {"_csrf": csrf_of(html), "step": "data", "slot": token, "first_name": "A", "last_name": "B", "birthdate": "1980-01-01",
            "phone": "079 555 44 33"}
    r = await client.post(url, data=base)                                   # ohne Einverständnis
    assert r.status == 400
    r = await client.post(url, data={**base, "consent": "1", "phone": "12"})  # falsche Nummer
    assert r.status == 400
    before = app[DB].value("SELECT COUNT(*) FROM booking_requests")
    r = await client.post(url, data={**base, "consent": "1", "website": "http://spam"})  # Roboter
    assert r.status == 200 and app[DB].value("SELECT COUNT(*) FROM booking_requests") == before
    r = await client.post(url, data={**base, "consent": "1", "slot": "gefälscht"})
    assert r.status == 400
    # falscher Code: nach 5 Versuchen gesperrt
    r = await client.post(url, data={**base, "consent": "1"})
    html = await r.text()
    rid_token = re.search(r'name="rid" value="([^"]+)"', html).group(1)
    for _ in range(5):
        await client.post(url, data={"_csrf": csrf_of(html), "step": "code", "rid": rid_token, "code": "000000"})
    rid = app[DB].value("SELECT MAX(id) FROM booking_requests")
    assert app[DB].value("SELECT code_attempts FROM booking_requests WHERE id = ?", (rid,)) == 5
    with practice_scope(1):
        assert booking.verify(app[DB], app[SETTINGS].secret_key, rid, "123456") == "blocked"


async def test_closed_and_unknown(client, app):
    db = app[DB]
    r = await client.get("/p/termin/gibt-es-nicht", allow_redirects=False)
    assert r.status == 404
    db.execute("UPDATE booking_settings SET enabled = 0 WHERE practice_id = 1")
    html = await (await client.get(f"/p/termin/{_slug(app)}")).text()
    assert "zurzeit nicht möglich" in html and "art=" not in html
    set_module_enabled(db, 1, "booking", False)
    r = await client.get(f"/p/termin/{_slug(app)}", allow_redirects=False)
    assert r.status == 404


async def test_language_french(client, app):
    client.session.cookie_jar.update_cookies({"nf_lang": "fr"})
    html = await (await client.get(f"/p/termin/{_slug(app)}")).text()
    assert "Demander un rendez-vous" in html and "Premier traitement" in html


async def test_reception_confirms_new_patient(client, app):
    db = app[DB]
    rid = await _request(client, app)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/online-anfragen")).text()
    assert "Mira" in html and "Neue Person" in html and "Nora" in html
    r = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid)}, allow_redirects=False)
    assert r.status == 302
    req = db.one("SELECT * FROM booking_requests WHERE id = ?", (rid,))
    assert req["status"] == "confirmed" and req["patient_id"] and req["appointment_id"]
    p = db.one("SELECT * FROM patients WHERE id = ?", (req["patient_id"],))
    assert (p["first_name"], p["phone"], p["law"]) == ("Mira", "+41795554433", "KVG") and p["email_ok_at"]
    a = db.one("SELECT * FROM appointments WHERE id = ?", (req["appointment_id"],))
    assert a["patient_id"] == p["id"] and a["starts_at"] == req["starts_at"] and a["status"] == "booked"
    assert db.value("SELECT COUNT(*) FROM patient_team WHERE patient_id = ? AND user_id = ?", (p["id"], req["therapist_id"])) == 1
    sms = db.one("SELECT body FROM outbox WHERE channel = 'sms' AND recipient = '+41795554433' ORDER BY id DESC")["body"]
    assert "bestätigt" in sms
    mail = db.one("SELECT * FROM outbox WHERE channel = 'email' ORDER BY id DESC")
    assert mail["recipient"] == "mira.neu@example.invalid" and "bestätigt" in mail["subject"] and "144" in mail["body"]
    # gleiche Person nochmals: wird erkannt
    rid2 = await _request(client, app, pick=-1)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    assert "Bekannt" in await (await client.get("/empfang/online-anfragen")).text()
    await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid2)})
    assert db.value("SELECT patient_id FROM booking_requests WHERE id = ?", (rid2,)) == p["id"]
    assert db.value("SELECT COUNT(*) FROM patients WHERE first_name = 'Mira' AND last_name = 'Neu'") == 1


async def test_decline_and_taken(client, app):
    db = app[DB]
    rid = await _request(client, app, email_ok=False)
    req = db.one("SELECT * FROM booking_requests WHERE id = ?", (rid,))
    # in der Zwischenzeit belegt der Empfang diese Zeit
    from nfizyo.modules.agenda import repo as agenda

    with practice_scope(1, 1):
        agenda.create(db, therapist_id=req["therapist_id"], start=datetime.fromisoformat(req["starts_at"]).astimezone(TZ), minutes=30,
                      status="blocked", title="Sitzung")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid)}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT status FROM booking_requests WHERE id = ?", (rid,)) == "requested"
    assert db.value("SELECT COUNT(*) FROM patients WHERE first_name = 'Mira'") <= 1
    n_mail = db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'email'")
    await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "decline", "request_id": str(rid), "reason": "belegt"})
    assert db.value("SELECT status FROM booking_requests WHERE id = ?", (rid,)) == "declined"
    assert "nicht bestätigen" in db.one("SELECT body FROM outbox WHERE channel = 'sms' ORDER BY id DESC")["body"]
    assert db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'email'") == n_mail   # ohne Einverständnis keine E-Mail


async def test_settings_page(client, app):
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/online-termine")).text()
    assert "/p/termin/" in html and "keine öffentliche Internet-Adresse" in html
    r = await client.post("/leitung/einstellungen/online-termine", data={"_csrf": csrf, "enabled": "1", "days_ahead": "14", "lead_hours": "24",
                                                                         "slot_step": "30", "types": ["1"], "therapists": ["999"]},
                          allow_redirects=False)
    assert r.status == 302
    with practice_scope(1):
        st = booking.settings(app[DB])
        assert st["days_ahead"] == 14 and st["slot_step"] == 30 and booking.therapist_ids(app[DB]) == set()


# --------------------------------------------------------------------------- E-Mail
def test_mail_service_log_live_retry(app, monkeypatch):
    db = app[DB]
    svc = app[MAIL]
    assert core_mail.valid_email("a.b@example.ch") and not core_mail.valid_email("a@b") and not core_mail.valid_email("x y@z.ch")
    mid = svc.send(1, "a.b@example.ch", "Betreff\nBcc: boese@example.ch", "Text", attachment=("r.pdf", b"%PDF-1"))
    row = db.one("SELECT * FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "logged" and row["attachment"] is None and "\n" not in row["subject"]
    assert db.one("SELECT status FROM outbox WHERE id = ?", (svc.send(1, "kaputt", "S", "T"),))["status"] == "failed"
    # live
    live = core_mail.MailService(app[SETTINGS].with_overrides(mail_engine="live", smtp_host="smtp.example.invalid",
                                                              smtp_from="termine@example.invalid"), db)
    sent = []
    monkeypatch.setattr(live, "_smtp_send", lambda msg: sent.append(msg))
    mid = live.send(1, "a.b@example.ch", "Rechnung", "Text", attachment=("r.pdf", b"%PDF-1"))
    assert db.one("SELECT status, attachment FROM outbox WHERE id = ?", (mid,))["attachment"] == b"%PDF-1"
    import asyncio

    assert asyncio.run(live.deliver(mid))
    row = db.one("SELECT * FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "sent" and row["attachment"] is None
    msg = sent[0]
    assert msg["To"] == "a.b@example.ch" and "termine@example.invalid" in msg["From"] and msg["Reply-To"]
    assert any(part.get_filename() == "r.pdf" for part in msg.iter_attachments())

    def boom(msg):
        raise OSError("down")

    monkeypatch.setattr(live, "_smtp_send", boom)
    mid = live.send(1, "a.b@example.ch", "S", "T", attachment=("r.pdf", b"x"))
    for _ in range(core_mail.MAX_ATTEMPTS):
        db.execute("UPDATE outbox SET next_try_at = NULL WHERE id = ?", (mid,))
        asyncio.run(live.deliver(mid))
    row = db.one("SELECT * FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "failed" and row["attempts"] == core_mail.MAX_ATTEMPTS and row["attachment"] is None
    # SMS-Wiederholung fasst E-Mails nicht an
    assert mid not in app.__getitem__(__import__("nfizyo.core.keys", fromlist=["SMS"]).SMS).due_retries()


def test_reminder_by_email(app):
    from nfizyo.modules.agenda import hooks
    from nfizyo.modules.agenda import repo as agenda

    db = app[DB]
    hans = pid_of(app, "Hans")
    db.execute("UPDATE patients SET email_ok_at = '2026-01-01T00:00:00+00:00', reminder_channel = 'both' WHERE id = ?", (hans,))
    db.execute("UPDATE appointments SET reminder_sent_at = '2026-01-01' WHERE reminder_sent_at IS NULL")
    with practice_scope(1, 1):
        aid = agenda.create(db, therapist_id=2, start=datetime.now(TZ) + timedelta(hours=5), minutes=30, patient_id=hans)
    hooks.send_reminders(app)
    assert db.value("SELECT reminder_sent_at FROM appointments WHERE id = ?", (aid,))
    mail = db.one("SELECT * FROM outbox WHERE channel = 'email' ORDER BY id DESC")
    assert mail["recipient"].startswith("hans.") and "Erinnerung" in mail["subject"] and "144" in mail["body"]
    assert db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'sms' AND body LIKE 'Erinnerung%'") >= 1


async def test_invoice_and_receipt_by_email(client, app):
    db = app[DB]
    from nfizyo.core import patients as core_patients
    from nfizyo.modules.billing import repo as billing

    owner = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    peter = pid_of(app, "Peter")
    with practice_scope(1, owner):
        billing.set_tp_value(db, "kvg312", 1.0, "2026-01-01")
        iid = billing.create_from_appointments(db, core_patients.get(db, peter), "de")
        billing.finalize(db, billing.get(db, iid))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/rechnungen/{iid}")).text()
    assert 'value="email"' not in html                       # ohne Einverständnis kein Knopf
    await client.post(f"/empfang/rechnungen/{iid}", data={"_csrf": csrf, "do": "email"})
    assert db.value("SELECT COUNT(*) FROM outbox WHERE channel = 'email'") == 0
    db.execute("UPDATE patients SET email_ok_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (peter,))
    html = await (await client.get(f"/empfang/rechnungen/{iid}")).text()
    assert 'value="email"' in html
    await client.post(f"/empfang/rechnungen/{iid}", data={"_csrf": csrf, "do": "email"})
    mail = db.one("SELECT * FROM outbox WHERE channel = 'email' ORDER BY id DESC")
    number = db.value("SELECT number FROM invoices WHERE id = ?", (iid,))
    assert number in mail["subject"] and mail["attachment_name"] == f"rechnung-{number}.pdf" and mail["status"] == "logged"
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE action = 'email' AND entity = 'invoice' AND entity_id = ?", (iid,)) == 1
    # Quittung
    with practice_scope(1):
        from nfizyo.modules.billing import api as billing_api

        item = next(it for it in billing_api.price_items(db) if it["code"] == "TAPE")
    r = await client.post("/empfang/kasse", data={"_csrf": csrf, "do": "sell", "method": "card", "patient_id": str(peter),
                                                  f"qty_{item['id']}": "1"}, allow_redirects=False)
    sid = int(r.headers["Location"].rsplit("/", 1)[1])
    await client.post(f"/empfang/kasse/belege/{sid}", data={"_csrf": csrf, "do": "email"})
    assert "Quittung" in db.one("SELECT subject FROM outbox WHERE channel = 'email' ORDER BY id DESC")["subject"]


async def test_patient_form_consent(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    p = db.one("SELECT * FROM patients WHERE id = ?", (lea,))
    form = {"_csrf": csrf, "first_name": p["first_name"], "last_name": p["last_name"], "birthdate": p["birthdate"], "lang": "de",
            "email": "lea.neu@example.invalid", "email_ok": "1", "reminder_channel": "email"}
    await client.post(f"/empfang/patienten/{lea}", data=form)
    p = db.one("SELECT * FROM patients WHERE id = ?", (lea,))
    assert p["email_ok_at"] and p["reminder_channel"] == "email"
    first = p["email_ok_at"]
    await client.post(f"/empfang/patienten/{lea}", data=form)
    assert db.value("SELECT email_ok_at FROM patients WHERE id = ?", (lea,)) == first   # bleibt beim gleichen Einverständnis
    await client.post(f"/empfang/patienten/{lea}", data={**form, "email_ok": ""})
    assert db.value("SELECT email_ok_at FROM patients WHERE id = ?", (lea,)) is None
    await client.post(f"/empfang/patienten/{lea}", data={**form, "email": ""})
    p = db.one("SELECT * FROM patients WHERE id = ?", (lea,))
    assert p["email_ok_at"] is None and p["reminder_channel"] == "sms"
