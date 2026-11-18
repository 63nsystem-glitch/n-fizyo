"""Patienten-App: Einladung per QR/Code (Therapie oder Empfang), Anmeldung per Telefon + Geburtsdatum, nur eigene Daten."""
from __future__ import annotations

import re

from conftest import csrf_of, login, pid_of

from nfizyo.core.keys import DB


async def test_invite_and_wrong_code(client, app):
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    hans = pid_of(app, "Hans")
    r = await client.post(f"/empfang/patienten/{hans}/app-einladung", data={"_csrf": csrf})
    text = await r.text()
    url = re.search(r'value="(http[^"]+/p/einladung/[^"]+)"', text).group(1)
    code = "".join(re.search(r'class="mono">(\d{3}) (\d{3})<', text).groups())
    path = url[url.index("/p/"):]
    client.session.cookie_jar.clear()
    r = await client.get(path)
    page = await r.text()
    wrong = "000000" if code != "000000" else "111111"
    r = await client.post(path, data={"_csrf": csrf_of(page), "code": wrong, "consent": "1"}, allow_redirects=False)
    assert r.status != 302 or "/p/heute" not in r.headers.get("Location", "")
    r = await client.get(path)
    r = await client.post(path, data={"_csrf": csrf_of(await r.text()), "code": code, "consent": "1"}, allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].startswith("/p/")
    r = await client.get("/p/heute")
    assert r.status == 200 and "Hans" in await r.text()
    # Patienten-Sitzung öffnet keine Praxis-Seiten
    for url in ("/therapie/heute", "/empfang/heute", "/leitung/uebersicht"):
        r = await client.get(url, allow_redirects=False)
        assert r.status == 302 and r.headers["Location"].startswith("/login"), url


async def test_therapist_can_invite_only_own_patients(client, app):
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/patienten/{pid_of(app, 'Lea')}/app-einladung", data={"_csrf": csrf})
    assert r.status == 200 and "/p/einladung/" in await r.text()
    r = await client.post(f"/therapie/patienten/{pid_of(app, 'Sandra')}/app-einladung", data={"_csrf": csrf}, allow_redirects=False)
    assert r.status == 404


async def test_phone_login_shows_only_own_data(client, app):
    r = await client.get("/p/anmelden")
    r = await client.post("/p/anmelden", data={"_csrf": csrf_of(await r.text()), "step": "phone", "phone": "079 000 00 08", "birthdate": "1980-07-14"})
    text = await r.text()
    code = re.search(r"(\d{6})", re.search(r"banner info.*?</div>", text, re.S).group(0)).group(1)
    lid = re.search(r'name="lid" value="([^"]+)"', text).group(1)
    r = await client.post("/p/anmelden", data={"_csrf": csrf_of(text), "step": "code", "code": code, "lid": lid}, allow_redirects=False)
    assert r.headers["Location"] == "/p/heute"
    r = await client.get("/p/heute")
    text = await r.text()
    assert "Ayşe" in text and "Lea" not in text
    # fremde Übung (anderer Patient) ist nicht erreichbar
    db = app[DB]
    other = db.value("SELECT i.id FROM program_items i JOIN programs p ON p.id = i.program_id WHERE p.patient_id = ? LIMIT 1", (pid_of(app, "Lea"),))
    r = await client.get(f"/p/uebungen/{other}")
    assert r.status == 404


async def test_patient_reports_problem_and_describes_complaint(client, app):
    from nfizyo.core.auth import create_session

    db = app[DB]
    ayse = pid_of(app, "Ayşe")
    token, _ = create_session(db, "patient", patient_id=ayse, practice_id=1)
    client.session.cookie_jar.update_cookies({"nf_p": token})
    r = await client.get("/p/anliegen")
    text = await r.text()
    r = await client.post("/p/anliegen", data=[("_csrf", csrf_of(text)), ("region", "lower_back_l"), ("pain", "7"), ("text", "Belim ağrıyor")],
                          allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT COUNT(*) FROM intakes WHERE patient_id = ? AND channel = 'app' AND text_original = 'Belim ağrıyor'", (ayse,)) == 1
    r = await client.get("/p/melden")
    r = await client.post("/p/melden", data=[("_csrf", csrf_of(await r.text())), ("region", "knees_r"), ("text", "Schwellung")],
                          allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT COUNT(*) FROM messages WHERE patient_id = ? AND kind = 'problem' AND body = 'Schwellung'", (ayse,)) == 1
