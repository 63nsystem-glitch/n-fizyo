"""Vier getrennte Bildschirme: Therapie, Empfang, Leitung, Patienten-App.

- Jede Rolle kommt nur in ihren Bildschirm (sonst 403).
- Empfang und Leitung sehen keine medizinischen Inhalte (Befunde, Notizen, Berichtstexte).
- Therapie sieht nur eigene Patient/innen (Behandlungsteam) und Vertretungen.
"""
from __future__ import annotations

import re

from conftest import csrf_of, login, pid_of

from nfizyo.core import access
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope

MEDICAL = ("Kreuzband", "Semitendinosus", "Teilbelastung", "Erstbefund", "Flexion aktiv", "Lumbalgie mit Ausstrahlung")


async def test_each_role_only_reaches_its_screens(client):
    cases = {
        "therapie@demo.n-fizyo.ch": ("/empfang/heute", "/empfang/patienten", "/leitung/uebersicht", "/leitung/team"),
        "empfang@demo.n-fizyo.ch": ("/therapie/heute", "/therapie/patienten", "/therapie/diktat", "/leitung/uebersicht"),
    }
    for email, urls in cases.items():
        await login(client, email)
        for url in urls:
            r = await client.get(url, allow_redirects=False)
            assert r.status == 403, f"{email} {url} → {r.status}"


async def test_home_and_screen_chooser(client):
    r = await client.get("/login")
    r = await client.post("/login", data={"_csrf": csrf_of(await r.text()), "email": "therapie@demo.n-fizyo.ch", "password": "demo-praxis"},
                          allow_redirects=False)
    assert r.headers["Location"] == "/therapie/heute"
    await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.get("/", allow_redirects=False)
    assert r.headers["Location"] == "/empfang/heute"
    # Leitung ist zugleich Therapeutin: Auswahl, dann gemerkt
    await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.get("/start")
    text = await r.text()
    assert "/start?s=therapie" in text and "/start?s=leitung" in text and "/start?s=empfang" not in text
    r = await client.get("/start?s=leitung", allow_redirects=False)
    assert r.headers["Location"] == "/leitung/uebersicht"
    r = await client.get("/", allow_redirects=False)
    assert r.headers["Location"] == "/leitung/uebersicht"
    r = await client.get("/therapie/heute")
    assert r.status == 200


async def test_reception_and_management_see_no_medical_content(client, app):
    lea = pid_of(app, "Lea")
    db = app[DB]
    did = db.value("SELECT id FROM documents WHERE patient_id = ?", (lea,))
    await login(client, "empfang@demo.n-fizyo.ch")
    for url in (f"/empfang/patienten/{lea}", f"/empfang/patienten/{lea}/termine", "/empfang/heute", "/empfang/agenda",
                "/empfang/posteingang?f=alle", f"/empfang/posteingang/{did}", "/empfang/abrechnung"):
        r = await client.get(url)
        assert r.status == 200, url
        text = await r.text()
        assert not any(m in text for m in MEDICAL), url
    for url in (f"/empfang/patienten/{lea}/verlauf", f"/empfang/posteingang/{did}/datei", f"/empfang/posteingang/{did}/blatt",
                "/empfang/notizen/1", "/empfang/berichte"):
        r = await client.get(url, allow_redirects=False)
        assert r.status in (404, 405), url
    await login(client, "leitung@demo.n-fizyo.ch")
    for url in ("/leitung/uebersicht", "/leitung/agenda", "/leitung/team", "/leitung/abrechnung"):
        r = await client.get(url)
        assert r.status == 200
        text = await r.text()
        assert not any(m in text for m in MEDICAL), url
    r = await client.get(f"/leitung/patienten/{lea}", allow_redirects=False)
    assert r.status == 404


async def test_therapist_sees_only_own_patients(client, app):
    lea, sandra, luca = pid_of(app, "Lea"), pid_of(app, "Sandra"), pid_of(app, "Luca")
    await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.get("/therapie/patienten")
    text = await r.text()
    assert "Lea" in text and "Sandra" not in text
    assert (await client.get(f"/therapie/patienten/{lea}")).status == 200
    for url in (f"/therapie/patienten/{sandra}", f"/therapie/patienten/{sandra}/verlauf", f"/therapie/nachrichten/{sandra}"):
        r = await client.get(url, allow_redirects=False)
        assert r.status == 404, url
    # A. Beispiel vertritt L. Esempio (Demo): sieht Luca, aber nicht Lea
    await login(client, "a.beispiel@demo.n-fizyo.ch")
    assert (await client.get(f"/therapie/patienten/{luca}")).status == 200
    assert (await client.get(f"/therapie/patienten/{lea}", allow_redirects=False)).status == 404


def test_delegation_ends_automatically(app):
    from datetime import date, timedelta

    db = app[DB]
    ab = db.value("SELECT id FROM users WHERE email = 'a.beispiel@demo.n-fizyo.ch'")
    luca = pid_of(app, "Luca")
    with practice_scope(1):
        assert access.relation(db, ab, luca) in ("team", "delegated")
        db.execute("DELETE FROM patient_team WHERE user_id = ? AND patient_id = ?", (ab, luca))
        assert access.relation(db, ab, luca) == "delegated"
        assert access.relation(db, ab, luca, today=date.today() + timedelta(days=30)) is None
        for d in access.delegations(db):
            access.revoke(db, d["id"])
        assert access.relation(db, ab, luca) is None


async def test_booking_adds_therapist_to_team_and_note_marks_done(client, app):
    db = app[DB]
    sandra = pid_of(app, "Sandra")
    td = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    patient = db.one("SELECT * FROM patients WHERE id = ?", (sandra,))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    label = f"{patient['last_name']} {patient['first_name']} · 17.01.1985 · {patient['number']}"
    r = await client.post("/empfang/agenda/neu", data={"_csrf": csrf, "patient": label, "therapist_id": str(td), "date": "2031-03-04",
                                                       "time": "07:00", "force": "1"}, allow_redirects=False)
    assert r.status == 302
    aid = db.value("SELECT id FROM appointments WHERE patient_id = ? AND therapist_id = ? ORDER BY id DESC", (sandra, td))
    assert db.value("SELECT reason FROM patient_team WHERE patient_id = ? AND user_id = ?", (sandra, td)) == "appointment"
    # Therapie schreibt eine Notiz zum Termin und gibt frei → Termin erledigt (Ereignis note.approved)
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/patienten/{sandra}/notizen/neu?termin={aid}", data={"_csrf": csrf, "befund": "ok", "approve": "1"},
                          allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT status FROM appointments WHERE id = ?", (aid,)) == "done"


async def test_reception_cannot_reach_clinical_actions(client, app):
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    lea = pid_of(app, "Lea")
    for url, data in ((f"/therapie/patienten/{lea}/berichte", {"kind": "progress"}), (f"/therapie/patienten/{lea}/befunde", {"kind": "pain", "title": "x"}),
                      (f"/therapie/patienten/{lea}/heimprogramm", {"exercise_id": "1"})):
        r = await client.post(url, data={"_csrf": csrf, **data}, allow_redirects=False)
        assert r.status == 403, url


async def test_post_without_csrf_is_rejected(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/patienten/{pid_of(app, 'Lea')}/berichte", data={"kind": "progress"}, allow_redirects=False)
    assert r.status == 403


async def test_wrong_password_and_inactive_user(client, app):
    r = await client.get("/login")
    r = await client.post("/login", data={"_csrf": csrf_of(await r.text()), "email": "leitung@demo.n-fizyo.ch", "password": "falsch"},
                          allow_redirects=False)
    assert r.status == 401
    app[DB].execute("UPDATE users SET active = 0 WHERE email = 'empfang@demo.n-fizyo.ch'")
    r = await client.get("/login")
    r = await client.post("/login", data={"_csrf": csrf_of(await r.text()), "email": "empfang@demo.n-fizyo.ch", "password": "demo-praxis"},
                          allow_redirects=False)
    assert r.status == 401


async def test_management_creates_user_with_roles_and_one_time_password(client, app):
    from nfizyo.core.auth import authenticate

    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/team", data=[("_csrf", csrf), ("do", "add"), ("name", "N. Neu"), ("email", "neu@demo.n-fizyo.ch"),
                                                 ("role", "therapist"), ("role", "reception"), ("lang", "fr")])
    text = await r.text()
    pw = re.search(r'user-select:all">([a-z0-9-]+)<', text).group(1)
    user = authenticate(app[DB], "neu@demo.n-fizyo.ch", pw)
    assert user is not None and user["roles"] == "therapist,reception"
    r = await client.get("/leitung/team")
    assert pw not in await r.text()  # nur einmal angezeigt


async def test_last_owner_cannot_lose_owner_role(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    me = db.value("SELECT id FROM users WHERE email = 'leitung@demo.n-fizyo.ch'")
    await client.post("/leitung/team", data=[("_csrf", csrf), ("do", "update"), ("user_id", str(me)), ("role", "therapist")])
    assert "owner" in db.value("SELECT roles FROM users WHERE id = ?", (me,))


async def test_management_delegation_page(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    td = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    se = db.value("SELECT id FROM users WHERE email = 's.exemple@demo.n-fizyo.ch'")
    r = await client.post("/leitung/vertretungen", data={"_csrf": csrf, "from_user": str(td), "to_user": str(se), "starts_on": "2026-01-01",
                                                         "ends_on": "2099-01-01"}, allow_redirects=False)
    assert r.status == 302
    with practice_scope(1):
        assert access.relation(db, se, pid_of(app, "Lea")) == "delegated"
