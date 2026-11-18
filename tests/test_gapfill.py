"""Lücke füllen: abgesagten Termin per SMS an passende Personen der Warteliste anbieten – wer zuerst bestätigt, bekommt ihn."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from conftest import login, pid_of

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import TZ
from nfizyo.modules.agenda import gapfill


# --------------------------------------------------------------------------- Regeln
def test_wish_is_read_in_several_languages():
    w = gapfill.parse_wish("Mo/Mi morgens")
    assert w.days == {0, 2} and [n for n, _ in w.parts] == ["morning"]
    w = gapfill.parse_wish("mardi après-midi")
    assert w.days == {1} and [n for n, _ in w.parts] == ["afternoon"]
    assert [n for n, _ in gapfill.parse_wish("über Mittag").parts] == ["noon"]
    assert gapfill.parse_wish("Mo–Fr").days == {0, 1, 2, 3, 4}
    assert gapfill.parse_wish("egal, auch kurzfristig").flexible
    w = gapfill.parse_wish("salı akşam")
    assert w.days == {1} and [n for n, _ in w.parts] == ["evening"]
    assert gapfill.parse_wish("").empty and gapfill.parse_wish("wenn möglich bald").empty


def _entry(**kw):
    base = {"id": 1, "therapist_id": None, "location_id": None, "treatment_type_id": None, "preferred": None,
            "created_at": "2026-10-01T08:00:00Z"}
    base.update(kw)
    return base


def test_matching_rules():
    tue = datetime(2026, 11, 3, 15, 30, tzinfo=TZ)                  # Dienstag Nachmittag
    slot = gapfill.Slot(tue, tue + timedelta(minutes=45), therapist_id=7, location_id=1)
    now = datetime(2026, 11, 2, 9, 0, tzinfo=TZ)
    ok = gapfill.Facts()
    assert gapfill.evaluate(slot, _entry(therapist_id=8), ok, now) is None              # andere Therapeutin gewünscht
    assert gapfill.evaluate(slot, _entry(location_id=2), ok, now) is None               # anderer Standort
    assert gapfill.evaluate(slot, _entry(), gapfill.Facts(overlaps=True), now) is None  # hat zur selben Zeit einen Termin
    c = gapfill.evaluate(slot, _entry(therapist_id=7, preferred="Di nachmittags"), ok, now)
    assert c.fits and {"therapist", "day", "daypart"} <= set(c.reasons)
    c = gapfill.evaluate(slot, _entry(preferred="Mo/Mi morgens"), ok, now)
    assert not c.fits and {"day_no", "daypart_no"} <= set(c.warnings)
    c = gapfill.evaluate(slot, _entry(preferred="morgens, aber egal"), ok, now)
    assert c.fits and "flexible" in c.reasons
    c = gapfill.evaluate(slot, _entry(), gapfill.Facts(rx="used", same_day=True), now)
    assert {"rx_used", "same_day"} <= set(c.warnings) and c.fits
    a = gapfill.evaluate(slot, _entry(id=1, preferred="Di"), ok, now)
    b = gapfill.evaluate(slot, _entry(id=2, preferred="Do"), ok, now)
    assert [x.entry["id"] for x in gapfill.rank([b, a])] == [1, 2]


# --------------------------------------------------------------------------- Ablauf
def _gap(app) -> int:
    return app[DB].value("SELECT id FROM appointments WHERE patient_id = ? AND status = 'cancelled' AND practice_id = 1",
                         (pid_of(app, "Hans"),))


def _wid(app, first_name: str) -> int:
    return app[DB].value("SELECT id FROM waitlist WHERE patient_id = ? AND done = 0", (pid_of(app, first_name),))


def _tokens(app) -> dict[str, str]:
    """Telefonnummer → Token aus den SMS im Postausgang."""
    out = {}
    for r in app[DB].query("SELECT recipient, body FROM outbox WHERE body LIKE '%/p/angebot/%' ORDER BY id"):
        out[r["recipient"]] = re.search(r"/p/angebot/([A-Za-z0-9_-]+)", r["body"]).group(1)
    return out


def _csrf(html: str) -> str:
    m = re.search(r'name="_csrf" value="([^"]+)"', html) or re.search(r'name="csrf-token" content="([^"]+)"', html)
    return m.group(1)


async def test_reception_sees_matching_people(client, app):
    await login(client, "empfang@demo.n-fizyo.ch")
    text = await (await client.get(f"/empfang/agenda/termine/{_gap(app)}")).text()
    assert "Lücke füllen" in text
    fits, maybe = text.split("Passt vielleicht")[0], text.split("Passt vielleicht")[1]
    assert "Jonas" in fits and "Ayşe" in fits and "flexibel" in fits
    assert "Yves" in maybe and "andere Tageszeit" in maybe
    assert "Hans" not in text.split("Lücke füllen")[1].split("Passt vielleicht")[0]      # wer abgesagt hat, nicht
    assert "Frau K. Beispielkundin" in text                                            # nur telefonisch
    wl = await (await client.get("/empfang/agenda/warteliste")).text()
    assert "Frei gewordene Termine" in wl and "passende Personen" in wl


async def test_first_confirmation_wins(client, app):
    db = app[DB]
    gap = _gap(app)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post(f"/empfang/agenda/termine/{gap}/luecke",
                          data={"_csrf": csrf, "action": "send", "w": [str(_wid(app, "Ayşe")), str(_wid(app, "Jonas"))]},
                          allow_redirects=False)
    assert r.status == 302
    tokens = _tokens(app)
    assert set(tokens) == {"+41790000008", "+41790000009"}
    assert db.value("SELECT COUNT(*) FROM slot_offer_recipients") == 2
    assert not db.value("SELECT COUNT(*) FROM slot_offer_recipients WHERE token_hash LIKE ?", (tokens["+41790000008"][:10] + "%",))
    tr_sms = db.value("SELECT body FROM outbox WHERE recipient = '+41790000008' AND body LIKE '%/p/angebot/%'")
    assert "randevu boşaldı" in tr_sms                                              # in Ayşes Sprache

    client.session.cookie_jar.clear()
    page = await (await client.get(f"/p/angebot/{tokens['+41790000008']}")).text()
    assert "Boş randevu" in page and "144" in page
    r = await client.post(f"/p/angebot/{tokens['+41790000008']}", data={"_csrf": _csrf(page), "answer": "yes"}, allow_redirects=False)
    assert r.status == 302
    page = await (await client.get(f"/p/angebot/{tokens['+41790000008']}")).text()
    assert "Randevunuz alındı" in page
    ayse = pid_of(app, "Ayşe")
    new = db.one("SELECT * FROM appointments WHERE patient_id = ? AND status = 'booked' AND starts_at = "
                 "(SELECT starts_at FROM appointments WHERE id = ?)", (ayse, gap))
    assert new is not None and new["therapist_id"] == db.value("SELECT therapist_id FROM appointments WHERE id = ?", (gap,))
    assert db.value("SELECT done FROM waitlist WHERE patient_id = ? AND wish LIKE 'Zweiter%'", (ayse,)) == 1
    assert db.value("SELECT COUNT(*) FROM outbox WHERE recipient = '+41790000009' AND body LIKE '%nicht mehr frei%'") == 1
    assert db.value("SELECT COUNT(*) FROM outbox WHERE recipient = '+41790000008' AND body LIKE '%onaylandı%'") == 1

    # Jonas ist zu spät – kein zweiter Termin
    page = await (await client.get(f"/p/angebot/{tokens['+41790000009']}")).text()
    assert "schon vergeben" in page and 'value="yes"' not in page
    await client.post(f"/p/angebot/{tokens['+41790000009']}", data={"_csrf": _csrf(page), "answer": "yes"}, allow_redirects=False)
    jonas = pid_of(app, "Jonas")
    assert db.value("SELECT COUNT(*) FROM appointments WHERE patient_id = ? AND starts_at = (SELECT starts_at FROM appointments WHERE id = ?)"
                    " AND status = 'booked'", (jonas, gap)) == 0

    await login(client, "empfang@demo.n-fizyo.ch")
    text = await (await client.get(f"/empfang/agenda/termine/{gap}")).text()
    assert "Vergeben an Ayşe Örnek" in text and "Neuen Termin öffnen" in text and "SMS-Angebot senden" not in text
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'slot_offer'") >= 2


async def test_decline_close_and_expiry(client, app):
    db = app[DB]
    gap = _gap(app)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    await client.post(f"/empfang/agenda/termine/{gap}/luecke", data={"_csrf": csrf, "action": "send", "w": str(_wid(app, "Jonas"))},
                      allow_redirects=False)
    token = _tokens(app)["+41790000009"]
    client.session.cookie_jar.clear()
    page = await (await client.get(f"/p/angebot/{token}")).text()
    await client.post(f"/p/angebot/{token}", data={"_csrf": _csrf(page), "answer": "no"}, allow_redirects=False)
    page = await (await client.get(f"/p/angebot/{token}")).text()
    assert "Sie bleiben auf der Warteliste" in page
    assert db.value("SELECT status FROM slot_offer_recipients") == "declined"
    # Empfang schreibt weitere an (gleiches Angebot) und zieht es dann zurück → Angeschriebene werden informiert
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    text = await (await client.get(f"/empfang/agenda/termine/{gap}")).text()
    assert "Weitere anschreiben" in text and "abgelehnt" in text
    await client.post(f"/empfang/agenda/termine/{gap}/luecke", data={"_csrf": csrf, "action": "send", "w": str(_wid(app, "Ayşe"))},
                      allow_redirects=False)
    assert db.value("SELECT COUNT(*) FROM slot_offers") == 1
    await client.post(f"/empfang/agenda/termine/{gap}/luecke", data={"_csrf": csrf, "action": "close"}, allow_redirects=False)
    assert db.value("SELECT status FROM slot_offers") == "closed"
    assert db.value("SELECT COUNT(*) FROM outbox WHERE recipient = '+41790000008' AND body LIKE '%artık boş değil%'") == 1
    assert db.value("SELECT COUNT(*) FROM outbox WHERE recipient = '+41790000009' AND body LIKE '%nicht mehr frei%'") == 0
    # neues Angebot, das abläuft
    await client.post(f"/empfang/agenda/termine/{gap}/luecke", data={"_csrf": csrf, "action": "send", "w": str(_wid(app, "Ayşe"))},
                      allow_redirects=False)
    token = _tokens(app)["+41790000008"]
    db.execute("UPDATE slot_offers SET expires_at = '2000-01-01T00:00:00Z' WHERE status = 'open'")
    client.session.cookie_jar.clear()
    page = await (await client.get(f"/p/angebot/{token}")).text()
    assert "Süresi doldu" in page or "süresi doldu" in page
    assert (await client.get("/p/angebot/" + "x" * 32)).status == 404


async def test_slot_taken_meanwhile_is_not_double_booked(client, app):
    """Empfang vergibt die Zeit am Telefon, bevor jemand den SMS-Link öffnet."""
    db = app[DB]
    gap = _gap(app)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    await client.post(f"/empfang/agenda/termine/{gap}/luecke", data={"_csrf": csrf, "action": "send", "w": str(_wid(app, "Jonas"))},
                      allow_redirects=False)
    token = _tokens(app)["+41790000009"]
    a = db.one("SELECT * FROM appointments WHERE id = ?", (gap,))
    from nfizyo.core.timeutil import from_iso
    from nfizyo.modules.agenda import repo

    with practice_scope(1):
        repo.create(db, therapist_id=a["therapist_id"], start=from_iso(a["starts_at"]).astimezone(TZ), minutes=45,
                    patient_id=pid_of(app, "Lea"))
    client.session.cookie_jar.clear()
    page = await (await client.get(f"/p/angebot/{token}")).text()
    await client.post(f"/p/angebot/{token}", data={"_csrf": _csrf(page), "answer": "yes"}, allow_redirects=False)
    assert db.value("SELECT COUNT(*) FROM appointments WHERE patient_id = ? AND starts_at = ?", (pid_of(app, "Jonas"), a["starts_at"])) == 0
    page = await (await client.get(f"/p/angebot/{token}")).text()
    assert "schon vergeben" in page


async def test_therapy_screen_cannot_send_offers(client, app):
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/agenda/termine/{_gap(app)}/luecke", data={"_csrf": csrf, "action": "send"}, allow_redirects=False)
    assert r.status in (404, 405)
