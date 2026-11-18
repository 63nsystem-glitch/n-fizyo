"""Heimprogramm-Paket: eigenes Video (nur mit Einwilligung), «bei wenig Zeit zuerst», Grund fürs Nicht-Üben,
Vorlesen und der freigegebene Verlauf in der Patienten-App."""
from __future__ import annotations

import re
from pathlib import Path

from aiohttp import FormData
from conftest import csrf_of, login, pid_of

from nfizyo.core.keys import DB, SETTINGS

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 2048


async def patient_login(client, phone: str, birthdate: str) -> str:
    client.session.cookie_jar.clear()
    r = await client.get("/p/anmelden")
    r = await client.post("/p/anmelden", data={"_csrf": csrf_of(await r.text()), "step": "phone", "phone": phone, "birthdate": birthdate})
    text = await r.text()
    code = re.search(r"(\d{6})", re.search(r"banner info.*?</div>", text, re.S).group(0)).group(1)
    lid = re.search(r'name="lid" value="([^"]+)"', text).group(1)
    r = await client.post("/p/anmelden", data={"_csrf": csrf_of(text), "step": "code", "code": code, "lid": lid}, allow_redirects=False)
    assert r.status == 302
    return csrf_of(await (await client.get("/p/heute")).text())


async def lea(client) -> str:
    return await patient_login(client, "079 000 00 01", "1992-03-05")


def first_item(app, first_name: str) -> int:
    return app[DB].value("SELECT i.id FROM program_items i JOIN programs p ON p.id = i.program_id WHERE p.patient_id = ? AND i.removed = 0"
                         " ORDER BY i.position LIMIT 1", (pid_of(app, first_name),))


def upload(csrf: str, data: bytes, consent: bool = True, name: str = "uebung.mp4") -> FormData:
    fd = FormData()
    fd.add_field("_csrf", csrf)
    if consent:
        fd.add_field("consent", "1")
    fd.add_field("video", data, filename=name, content_type="video/mp4")
    return fd


async def test_own_video_needs_consent_and_a_real_video(client, app):
    db = app[DB]
    iid = first_item(app, "Lea")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/heimprogramm/{iid}/video", data=upload(csrf, MP4, consent=False), allow_redirects=False)
    assert r.status == 302 and db.value("SELECT video_path FROM program_items WHERE id = ?", (iid,)) is None
    r = await client.post(f"/therapie/heimprogramm/{iid}/video", data=upload(csrf, b"%PDF-1.4 kein Video", name="x.mp4"),
                          allow_redirects=False)
    assert r.status == 302 and db.value("SELECT video_path FROM program_items WHERE id = ?", (iid,)) is None
    r = await client.post(f"/therapie/heimprogramm/{iid}/video", data=upload(csrf, MP4), allow_redirects=False)
    assert r.status == 302
    row = db.one("SELECT * FROM program_items WHERE id = ?", (iid,))
    path = Path(row["video_path"])
    root = (app[SETTINGS].data_dir / "videos" / "1").resolve()
    assert path.exists() and path.resolve().is_relative_to(root) and row["video_consent_at"] and row["video_mime"] == "video/mp4"
    assert "uebung" not in path.name                                 # zufälliger Dateiname, nicht der hochgeladene
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'exercise_video' AND action = 'create'") == 1
    page = await (await client.get(f"/therapie/patienten/{pid_of(app, 'Lea')}/heimprogramm")).text()
    assert "Eigenes Video" in page and "Einwilligung vom" in page
    r = await client.get(f"/therapie/heimprogramm/{iid}/video")
    assert r.status == 200 and (await r.read()) == MP4 and r.headers["Cache-Control"] == "private, no-store"

    # Patientin sieht es in der App und kann es selbst löschen
    pcsrf = await lea(client)
    detail = await (await client.get(f"/p/uebungen/{iid}")).text()
    assert "<video" in detail and "Ihr eigenes Video" in detail and "Mein Video löschen" in detail
    assert (await client.get(f"/p/uebungen/{iid}/video")).status == 200
    r = await client.post(f"/p/uebungen/{iid}/video/loeschen", data={"_csrf": pcsrf}, allow_redirects=False)
    assert r.status == 302 and not path.exists()
    assert db.value("SELECT video_path FROM program_items WHERE id = ?", (iid,)) is None
    assert (await client.get(f"/p/uebungen/{iid}/video")).status == 404


async def test_video_stays_with_the_patient(client, app):
    """Fremde Patientin und Therapeut ohne Zugang sehen das Video nicht."""
    iid = first_item(app, "Lea")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    await client.post(f"/therapie/heimprogramm/{iid}/video", data=upload(csrf, MP4), allow_redirects=False)
    await patient_login(client, "079 000 00 08", "1980-07-14")          # Ayşe
    assert (await client.get(f"/p/uebungen/{iid}/video")).status == 404
    r = await client.post(f"/p/uebungen/{iid}/video/loeschen", data={"_csrf": csrf_of(await (await client.get('/p/heute')).text())},
                          allow_redirects=False)
    assert r.status == 404 and app[DB].value("SELECT video_path FROM program_items WHERE id = ?", (iid,))
    sandra_item = first_item(app, "Sandra")
    if sandra_item:
        csrf = await login(client, "therapie@demo.n-fizyo.ch")
        r = await client.post(f"/therapie/heimprogramm/{sandra_item}/video", data=upload(csrf, MP4), allow_redirects=False)
        assert r.status == 404


async def test_star_and_reason_for_not_practising(client, app):
    db = app[DB]
    # Demo: bei Lea sind zwei Übungen «bei wenig Zeit zuerst»
    pcsrf = await lea(client)
    text = await (await client.get("/p/uebungen")).text()
    assert "zuerst die Übungen mit dem Stern" in text and "Heute nicht geschafft?" in text
    r = await client.post("/p/uebungen/nicht-geschafft", data={"_csrf": pcsrf, "reason": "pain", "note": "Knie dick"}, allow_redirects=False)
    assert r.headers["Location"] == "/p/uebungen?grund=pain"
    text = await (await client.get(r.headers["Location"])).text()
    assert "144" in text and "Der Praxis schreiben" in text
    r = await client.post("/p/uebungen/nicht-geschafft", data={"_csrf": pcsrf, "reason": "no_time"}, allow_redirects=False)
    text = await (await client.get(r.headers["Location"])).text()
    assert "nur die Übungen mit dem Stern" in text
    lea_id = pid_of(app, "Lea")
    assert db.value("SELECT COUNT(*) FROM exercise_skips WHERE patient_id = ? AND day = date('now', 'localtime')", (lea_id,)) == 1
    assert db.value("SELECT reason FROM exercise_skips WHERE patient_id = ? AND day = date('now', 'localtime')", (lea_id,)) == "no_time"
    r = await client.post("/p/uebungen/nicht-geschafft", data={"_csrf": pcsrf, "reason": "egal"}, allow_redirects=False)
    assert r.headers["Location"] == "/p/uebungen"

    # Therapie sieht Gründe im Heimprogramm und im Kurzüberblick
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/patienten/{lea_id}/heimprogramm")).text()
    assert "Nicht geübt – Grund laut Patient/in" in page and "Spätdienst" in page and "keine Zeit" in page
    aid = db.value("SELECT a.id FROM appointments a JOIN users u ON u.id = a.therapist_id WHERE a.patient_id = ? AND u.email = ?"
                   " AND a.status IN ('booked', 'arrived') AND date(a.starts_at) >= date('now', '-1 day') ORDER BY a.starts_at LIMIT 1",
                   (lea_id, "therapie@demo.n-fizyo.ch"))
    brief = await (await client.get(f"/therapie/kurzinfo/{aid}")).text()
    assert "Nicht geübt, weil" in brief and "keine Zeit" in brief

    # Stern setzen / entfernen
    iid = db.value("SELECT i.id FROM program_items i JOIN programs p ON p.id = i.program_id WHERE p.patient_id = ? AND i.essential = 0"
                   " AND i.removed = 0 LIMIT 1", (lea_id,))
    await client.post(f"/therapie/heimprogramm/{iid}", data={"_csrf": csrf, "sets": "2", "reps": "10", "per_day": "1", "essential": "1"},
                      allow_redirects=False)
    assert db.value("SELECT essential FROM program_items WHERE id = ?", (iid,)) == 1
    await client.post(f"/therapie/heimprogramm/{iid}", data={"_csrf": csrf, "sets": "2", "reps": "10", "per_day": "1"}, allow_redirects=False)
    assert db.value("SELECT essential FROM program_items WHERE id = ?", (iid,)) == 0


async def test_exercise_can_be_read_aloud(client, app):
    await lea(client)
    text = await (await client.get(f"/p/uebungen/{first_item(app, 'Lea')}")).text()
    assert 'data-speak="ex-text"' in text and 'id="ex-text"' in text and "speak.js" in text and "Vorlesen" in text


async def test_progress_curve_only_when_released(client, app):
    db = app[DB]
    lea_id = pid_of(app, "Lea")
    await lea(client)
    home = await (await client.get("/p/heute")).text()
    assert "Ihr Fortschritt" in home and "Beugen" in home and "Ziel 120°" in home
    assert "Schmerz (0–10)" not in home                              # nicht freigegeben
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    akte = await (await client.get(f"/therapie/patienten/{lea_id}")).text()
    assert "in der App sichtbar" in akte and "In der App zeigen" in akte
    r = await client.post(f"/therapie/patienten/{lea_id}/messwerte/teilen",
                          data={"_csrf": csrf, "kind": "flexion", "joint": "knee", "side": "r", "on": "0"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM measurement_shares WHERE patient_id = ?", (lea_id,)) == 0
    await client.post(f"/therapie/patienten/{lea_id}/messwerte/teilen",
                      data={"_csrf": csrf, "kind": "pain_vas", "joint": "knee", "side": "r", "on": "1"}, allow_redirects=False)
    await lea(client)
    home = await (await client.get("/p/heute")).text()
    assert "Schmerz (0–10)" in home and "Beugen" not in home
    # fremde Patientin (nicht im Team): nicht erlaubt
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/patienten/{pid_of(app, 'Sandra')}/messwerte/teilen",
                          data={"_csrf": csrf, "kind": "flexion", "on": "1"}, allow_redirects=False)
    assert r.status == 404
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'measurement_share'") >= 3
