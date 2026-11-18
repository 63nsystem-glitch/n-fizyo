"""Benachrichtigungen an Patient/innen (Modul «notify»): E-Mail nur mit Einverständnis, sonst SMS; ohne Inhalt; gebündelt;
keine SMS in der Nacht; nur mit verbundener App; schon Gelesenes löst nichts aus; Termine ohne Doppel zur Erinnerung."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import login, pid_of

from nfizyo.core.auth import create_session
from nfizyo.core.db import utcnow
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import TZ
from nfizyo.modules.notify import service


def _day(hour: int, minute: int = 0, days: int = 1) -> datetime:
    """Ein fester Zeitpunkt (Zürich) – Tests hängen nicht von der Uhrzeit ab."""
    d = (datetime.now(TZ) + timedelta(days=days)).date()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def _reply(db, patient_id: int, body: str = "Bitte das Knie kühlen.") -> int:
    from nfizyo.modules.messages import repo as messages

    with practice_scope(1, 1):
        return messages.add(db, patient_id, direction="out", body=body, lang="de")


def _sms_to(db, phone_end: str):
    return db.query("SELECT * FROM outbox WHERE channel = 'sms' AND recipient LIKE ? ORDER BY id", (f"%{phone_end}",))


async def test_reply_is_announced_once_without_content(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    for body in ("Bitte das Knie heute kühlen.", "Und die Übungen nur bis 45°."):
        r = await client.post(f"/therapie/nachrichten/{lea}", data={"_csrf": csrf, "body": body}, allow_redirects=False)
        assert r.status == 302
    rows = db.query("SELECT * FROM notify_queue WHERE patient_id = ? ORDER BY id", (lea,))
    assert [r["status"] for r in rows] == ["pending", "pending"] and rows[0]["kind"] == "message"
    assert rows[0]["due_at"] == rows[1]["due_at"]                     # zweite Antwort geht gebündelt mit
    page = await (await client.get(f"/therapie/nachrichten/{lea}")).text()
    assert "Benachrichtigung geplant (SMS" in page
    before = len(_sms_to(db, "0000001"))
    sent = service.process_due(app, now=max(service._dt(rows[0]["due_at"]), _day(10)))
    assert sent == 1
    sms = _sms_to(db, "0000001")[before:]
    assert len(sms) == 1
    text = sms[0]["body"]
    assert "neue Nachricht von Physio Demo Seeland" in text and "/p/nachrichten" in text
    assert "Knie" not in text and "45°" not in text and "Übungen" not in text   # keine Inhalte (Port der Testadresse kann «45» enthalten)
    rows = db.query("SELECT * FROM notify_queue WHERE patient_id = ?", (lea,))
    assert {r["status"] for r in rows} == {"sent"} and len({r["outbox_id"] for r in rows}) == 1
    page = await (await client.get(f"/therapie/nachrichten/{lea}")).text()
    assert "Patient/in benachrichtigt (SMS" in page


def test_no_sms_at_night_and_bundle_window(app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    night = _day(21, 30)
    nid = service.enqueue(app, 1, lea, "message", _reply(db, lea), now=night)
    row = db.one("SELECT * FROM notify_queue WHERE id = ?", (nid,))
    assert row["channel"] == "sms" and service._dt(row["due_at"]).astimezone(TZ).strftime("%H:%M") == "08:00"
    assert service.process_due(app, now=_day(23, 0)) == 0               # nicht fällig in der Nacht
    assert service.process_due(app, now=_day(8, 1, days=2)) == 1
    first = db.one("SELECT sent_at FROM notify_queue WHERE id = ?", (nid,))["sent_at"]
    # 5 Minuten später: nächste Antwort wartet bis 15 Minuten nach der letzten Benachrichtigung
    later = service._dt(first) + timedelta(minutes=5)
    nid2 = service.enqueue(app, 1, lea, "message", _reply(db, lea, "Noch etwas"), now=later)
    due = service._dt(db.one("SELECT due_at FROM notify_queue WHERE id = ?", (nid2,))["due_at"])
    assert due == service._dt(first) + timedelta(minutes=15)
    assert service.process_due(app, now=later + timedelta(minutes=3)) == 0
    # und eine SMS, die am Abend nach 20 Uhr fällig würde, wartet auch bei schon geplanter Zeit bis 8 Uhr
    assert service.next_morning(_day(19, 59)) == _day(19, 59)
    assert service.next_morning(_day(20, 0)).astimezone(TZ).strftime("%H:%M") == "08:00"
    assert service.next_morning(_day(7, 59)) == _day(8, 0)


def test_channel_rules_and_language(app):
    db = app[DB]
    ayse, maria, peter = pid_of(app, "Ayşe"), pid_of(app, "Maria"), pid_of(app, "Peter")
    noon = _day(12)
    # Ayşe mit E-Mail-Einverständnis → E-Mail auf Türkisch (Standard: E-Mail mit Einverständnis, sonst SMS)
    db.execute("UPDATE patients SET email_ok_at = ? WHERE id = ?", (utcnow(), ayse))
    service.enqueue(app, 1, ayse, "message", _reply(db, ayse), now=noon)
    # Maria ohne Einverständnis → SMS auf Portugiesisch
    service.enqueue(app, 1, maria, "message", _reply(db, maria), now=noon)
    # Peter hat die App nicht verbunden → nicht benachrichtigt
    nid = service.enqueue(app, 1, peter, "message", _reply(db, peter), now=noon)
    assert db.one("SELECT status, reason FROM notify_queue WHERE id = ?", (nid,))["reason"] == "no_app"
    assert service.process_due(app, now=noon + timedelta(minutes=5)) == 2
    mail = db.one("SELECT * FROM outbox WHERE channel = 'email' ORDER BY id DESC LIMIT 1")
    assert "yeni bir mesaj" in mail["body"] and "/p/nachrichten" in mail["body"] and "yenilikler" in mail["subject"]
    sms = _sms_to(db, "0000003")[-1]
    assert "Tem uma nova mensagem" in sms["body"]
    # Nur E-Mail: ohne Einverständnis keine Benachrichtigung; aus: gar keine
    with practice_scope(1, 1):
        service.save_settings(db, 1, "email", False, 1)
    nid = service.enqueue(app, 1, maria, "message", _reply(db, maria), now=noon)
    assert db.one("SELECT reason FROM notify_queue WHERE id = ?", (nid,))["reason"] == "no_consent"
    with practice_scope(1, 1):
        service.save_settings(db, 1, "off", False, 1)
    nid = service.enqueue(app, 1, ayse, "message", _reply(db, ayse), now=noon)
    assert db.one("SELECT reason FROM notify_queue WHERE id = ?", (nid,))["reason"] == "off"
    with practice_scope(1, 1):
        service.save_settings(db, 1, "sms", False, 1)
    nid = service.enqueue(app, 1, ayse, "message", _reply(db, ayse), now=noon)
    assert db.one("SELECT channel FROM notify_queue WHERE id = ?", (nid,))["channel"] == "sms"


async def test_read_in_app_means_no_notification(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    nid = service.enqueue(app, 1, lea, "message", _reply(db, lea), now=_day(12))
    token, _ = create_session(db, "patient", patient_id=lea, practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    assert (await client.get("/p/nachrichten")).status == 200
    before = db.value("SELECT COUNT(*) FROM outbox")
    assert service.process_due(app, now=_day(12, 10)) == 0
    assert db.one("SELECT status, reason FROM notify_queue WHERE id = ?", (nid,))["reason"] == "read"
    assert db.value("SELECT COUNT(*) FROM outbox") == before


async def test_released_information_is_announced(client, app):
    db = app[DB]
    maria = pid_of(app, "Maria")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/patienten/{maria}/info", data={"_csrf": csrf, "home": "Arm 3× täglich pendeln.", "contact": ""},
                          allow_redirects=False)
    assert r.status == 302
    row = db.one("SELECT * FROM notify_queue WHERE patient_id = ? AND kind = 'info'", (maria,))
    assert row is not None and row["status"] == "pending"
    page = await (await client.get(f"/therapie/patienten/{maria}/info")).text()
    assert "Benachrichtigung geplant" in page
    assert service.process_due(app, now=max(service._dt(row["due_at"]), _day(10))) == 1
    sms = _sms_to(db, "0000003")[-1]
    assert "nova informação" in sms["body"] and "/p/info" in sms["body"] and "pendeln" not in sms["body"]


def test_new_appointment_only_when_enabled_and_never_twice_with_reminder(app):
    from nfizyo.modules.agenda import repo as agenda

    db = app[DB]
    lea = pid_of(app, "Lea")
    with practice_scope(1, 1):
        soon = agenda.create(db, therapist_id=1, start=datetime.now(TZ) + timedelta(hours=10), minutes=30, patient_id=lea)
        later = agenda.create(db, therapist_id=1, start=datetime.now(TZ) + timedelta(days=4), minutes=30, patient_id=lea)
        service.on_appointment_booked(app, appointment_id=later, patient_id=lea)
    assert db.value("SELECT COUNT(*) FROM notify_queue WHERE kind = 'appointment'") == 0     # Standard: aus
    with practice_scope(1, 1):
        service.save_settings(db, 1, "email_or_sms", True, 1)
        service.on_appointment_booked(app, appointment_id=soon, patient_id=lea)              # Erinnerung kommt ohnehin
        service.on_appointment_booked(app, appointment_id=later, patient_id=lea)
    rows = db.query("SELECT * FROM notify_queue WHERE kind = 'appointment'")
    assert [r["ref_id"] for r in rows] == [later]
    msg = service.compose(app, "uk", "Praxis Test", {"appointment"})
    assert "новий запис" in msg["text"] and msg["url"].endswith("/p/termine")
    # Termin abgesagt, bevor die Benachrichtigung rausgeht → keine Benachrichtigung
    db.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (later,))
    assert service.process_due(app, now=_day(12) + timedelta(days=1)) == 0
    assert db.value("SELECT reason FROM notify_queue WHERE ref_id = ? AND kind = 'appointment'", (later,)) == "read"


async def test_settings_page_for_leitung_only(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/benachrichtigungen")).text()
    assert "E-Mail mit Einverständnis, sonst SMS" in page and "Sie haben eine neue Nachricht von" in page
    r = await client.post("/leitung/einstellungen/benachrichtigungen", data={"_csrf": csrf, "mode": "sms", "appointments": "1"},
                          allow_redirects=False)
    assert r.status == 302
    assert dict(db.one("SELECT mode, appointments FROM notify_settings WHERE practice_id = 1")) == {"mode": "sms", "appointments": 1}
    r = await client.post("/leitung/einstellungen/benachrichtigungen", data={"_csrf": csrf, "mode": "fax"}, allow_redirects=False)
    assert db.value("SELECT mode FROM notify_settings WHERE practice_id = 1") == "sms"
    for email in ("therapie@demo.n-fizyo.ch", "empfang@demo.n-fizyo.ch"):
        await login(client, email)
        r = await client.get("/leitung/einstellungen/benachrichtigungen", allow_redirects=False)
        assert r.status in (302, 403, 404)


def test_disabled_module_queues_nothing(app):
    from nfizyo.core.modules import set_module_enabled

    db = app[DB]
    lea = pid_of(app, "Lea")
    set_module_enabled(db, 1, "notify", False)
    assert service.enqueue(app, 1, lea, "message", _reply(db, lea), now=_day(12)) is None
    assert db.value("SELECT COUNT(*) FROM notify_queue") == 0
