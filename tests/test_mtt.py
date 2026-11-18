"""MTT-Fläche: Steigerungs-Regeln, Plan in der Akte, Live-Ansicht, Training in der App mit QR-Code, Abo-Eintritt per Check-in."""
from __future__ import annotations

import re

from conftest import csrf_of, login, pid_of

from nfizyo.core.auth import create_session
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.mtt import repo as mtt
from nfizyo.modules.mtt import rules


# --------------------------------------------------------------------------- Regeln
def _day(reps, load, effort, pain, n=3):
    return rules.Day(reps=[reps] * n, loads=[load] * n, effort=[effort] * n, pain=[pain] * n)


def test_progression_rules():
    kw = {"sets": 3, "reps": 12, "load": 40.0, "step": 2.5}
    s = rules.suggest(**kw, days=[_day(12, 40, 5, 1), _day(12, 40, 6, 2)])
    assert (s.kind, s.reason, s.to_load) == ("increase", "easy", 42.5)
    assert rules.suggest(**kw, days=[_day(12, 40, 5, 1)]) is None                       # ein Training reicht nicht
    assert rules.suggest(**kw, days=[_day(12, 40, 5, 1), _day(12, 40, 7, 1)]) is None   # nicht leicht genug
    assert rules.suggest(**kw, days=[_day(12, 40, 5, 1), _day(10, 40, 5, 1)]) is None   # nicht alle Wiederholungen
    s = rules.suggest(**kw, days=[_day(12, 40, 5, 1), _day(12, 40, 5, 6)])
    assert (s.kind, s.reason, s.to_load) == ("decrease", "pain", 32.5)
    s = rules.suggest(**kw, days=[_day(12, 40, 5, 1), _day(12, 40, 5, 3)])
    assert (s.kind, s.reason) == ("hold", "pain_rising")
    s = rules.suggest(**kw, days=[_day(8, 40, 9, 2), _day(9, 40, 9, 2)])
    assert (s.kind, s.reason, s.to_load) == ("decrease", "too_hard", 35.0)
    assert rules.suggest(sets=3, reps=10, load=100.0, step=5, days=[_day(10, 100, 4, 0)] * 2).to_load == 105.0


# --------------------------------------------------------------------------- Therapie
async def test_plan_suggestion_needs_the_therapist(client, app):
    db = app[DB]
    luca = pid_of(app, "Luca")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/patienten/{luca}/mtt")).text()
    assert "Vorschläge zur Steigerung" in page and "40 → 42.5 kg" in page and "Beinpresse" in page and "Sitz 5" in page
    item = db.value("SELECT i.id FROM mtt_items i JOIN mtt_machines m ON m.id = i.machine_id WHERE m.name = 'Beinpresse'")
    assert db.value("SELECT load FROM mtt_items WHERE id = ?", (item,)) == 40.0           # noch nichts geändert
    sid = db.value("SELECT id FROM mtt_suggestions WHERE item_id = ? AND status = 'open'", (item,))
    r = await client.post(f"/therapie/mtt/vorschlag/{sid}", data={"_csrf": csrf, "decision": "accept"}, allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT load FROM mtt_items WHERE id = ?", (item,)) == 42.5
    assert db.value("SELECT decided_by FROM mtt_suggestions WHERE id = ?", (sid,)) is not None
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'mtt_suggestion'") == 1
    akte = await (await client.get(f"/therapie/patienten/{luca}")).text()
    assert "Trainings in 4 Wochen" in akte


async def test_live_room_shows_own_patients_only(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get("/therapie/mtt")).text()
    assert "Luca Esempio" in page and "seit" in page and "mtt_live.js" in page
    assert "Sandra" not in page and "nicht in Ihrem Team: 1" in page
    frag = await (await client.get("/therapie/mtt/live")).text()
    assert "Luca Esempio" in frag and "<html" not in frag
    sandra = pid_of(app, "Sandra")
    assert (await client.get(f"/therapie/patienten/{sandra}/mtt")).status == 404
    sid_item = app[DB].value("SELECT i.id FROM mtt_items i JOIN mtt_plans p ON p.id = i.plan_id WHERE p.patient_id = ? LIMIT 1", (sandra,))
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/mtt/plan/{sid_item}", data={"_csrf": csrf, "load": "99"}, allow_redirects=False)
    assert r.status == 404 and app[DB].value("SELECT load FROM mtt_items WHERE id = ?", (sid_item,)) != 99


async def test_machines_and_qr_sheet(client, app):
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post("/therapie/mtt/geraete", data={"_csrf": csrf, "name": "Adduktoren", "kind": "strength", "step": "5"},
                          allow_redirects=False)
    assert r.status == 302
    sheet = await (await client.get("/therapie/mtt/geraete/qr")).text()
    assert "Adduktoren" in sheet and "<svg" in sheet
    token = app[DB].value("SELECT qr_token FROM mtt_machines WHERE name = 'Adduktoren'")
    assert len(token) >= 12


# --------------------------------------------------------------------------- Patienten-App
async def _patient(client, phone: str, birthdate: str) -> str:
    client.session.cookie_jar.clear()
    r = await client.get("/p/anmelden")
    r = await client.post("/p/anmelden", data={"_csrf": csrf_of(await r.text()), "step": "phone", "phone": phone, "birthdate": birthdate})
    text = await r.text()
    code = re.search(r"(\d{6})", re.search(r"banner info.*?</div>", text, re.S).group(0)).group(1)
    lid = re.search(r'name="lid" value="([^"]+)"', text).group(1)
    await client.post("/p/anmelden", data={"_csrf": csrf_of(text), "step": "code", "code": code, "lid": lid}, allow_redirects=False)
    return csrf_of(await (await client.get("/p/heute")).text())


async def test_training_with_qr_code(client, app):
    db = app[DB]
    luca = pid_of(app, "Luca")
    token, _ = create_session(db, "patient", patient_id=luca, practice_id=1)
    client.session.cookie_jar.update_cookies({"nf_p": token})
    home = await (await client.get("/p/heute")).text()
    csrf = csrf_of(home)
    assert "Allenamento (MTT)" in home
    page = await (await client.get("/p/training")).text()
    assert "Beinpresse" in page and "144" in page
    assert "Tornare a giocare a calcio" in page and "Wieder Fussball spielen" in page        # übersetzt, Original abrufbar
    balance = db.value("SELECT i.id FROM mtt_items i JOIN mtt_machines m ON m.id = i.machine_id WHERE m.name = 'Balance-Pad'")
    assert "Su una gamba sola" in await (await client.get(f"/p/training/{balance}")).text()
    token = db.value("SELECT qr_token FROM mtt_machines WHERE name = 'Kniestrecker'")
    r = await client.get(f"/p/mtt/{token}", allow_redirects=False)
    item = db.value("SELECT i.id FROM mtt_items i JOIN mtt_machines m ON m.id = i.machine_id WHERE m.name = 'Kniestrecker'")
    assert r.status == 302 and r.headers["Location"] == f"/p/training/{item}"
    page = await (await client.get(f"/p/training/{item}")).text()
    assert "Lehne 3" in page and "Quanto è stato faticoso?" in page
    before = db.value("SELECT COUNT(*) FROM mtt_sets WHERE item_id = ?", (item,))
    r = await client.post(f"/p/training/{item}", data={"_csrf": csrf, "reps": "12", "load": "15", "effort": "7", "pain": "6"},
                          allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT COUNT(*) FROM mtt_sets WHERE item_id = ?", (item,)) == before + 1
    assert db.value("SELECT pain FROM mtt_sets WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item,)) == 6
    g = db.one("SELECT * FROM mtt_suggestions WHERE item_id = ? AND status = 'open'", (item,))
    assert g["kind"] == "decrease" and g["reason"] == "pain"
    assert db.value("SELECT COUNT(*) FROM mtt_visits WHERE patient_id = ? AND day = date('now', 'localtime')", (luca,)) == 1
    r = await client.post("/p/training", data={"_csrf": csrf, "action": "end"}, allow_redirects=False)
    assert db.value("SELECT ended_at FROM mtt_visits WHERE patient_id = ? AND day = date('now', 'localtime')", (luca,))
    # Die Aufsicht sieht den Schmerz in der Akte
    await login(client, "therapie@demo.n-fizyo.ch")
    plan = await (await client.get(f"/therapie/patienten/{luca}/mtt")).text()
    assert "Schmerz 5 oder mehr" in plan


async def test_foreign_items_and_unknown_codes(client, app):
    db = app[DB]
    await _patient(client, "079 000 00 08", "1980-07-14")                 # Ayşe – kein MTT-Plan
    assert "Henüz onaylanmış bir antrenman planı yok" in await (await client.get("/p/training")).text()
    luca_item = db.value("SELECT i.id FROM mtt_items i JOIN mtt_plans p ON p.id = i.plan_id WHERE p.patient_id = ? LIMIT 1",
                         (pid_of(app, "Luca"),))
    assert (await client.get(f"/p/training/{luca_item}")).status == 404
    token = db.value("SELECT qr_token FROM mtt_machines WHERE name = 'Beinpresse'")
    r = await client.get(f"/p/mtt/{token}", allow_redirects=False)
    assert r.status == 200 and "antrenman planınız yok" in await r.text()
    assert db.value("SELECT COUNT(*) FROM mtt_visits WHERE patient_id = ?", (pid_of(app, "Ayşe"),)) == 0
    assert (await client.get("/p/mtt/unbekannt123")).status == 404


async def test_subscription_counts_one_checkin_per_day(client, app):
    db = app[DB]
    sandra = pid_of(app, "Sandra")
    sub = db.value("SELECT id FROM subscriptions WHERE patient_id = ?", (sandra,))
    # Demo hat Sandra heute schon eingecheckt: zurücksetzen und über die App neu starten
    db.execute("DELETE FROM mtt_visits WHERE patient_id = ? AND day = date('now', 'localtime')", (sandra,))
    db.execute("DELETE FROM subscription_visits WHERE subscription_id = ? AND visited_on = date('now', 'localtime')", (sub,))
    before = db.value("SELECT COUNT(*) FROM subscription_visits WHERE subscription_id = ?", (sub,))
    token, _ = create_session(db, "patient", patient_id=sandra, practice_id=1)
    client.session.cookie_jar.update_cookies({"nf_p": token})
    page = await (await client.get("/p/training")).text()
    assert "noch 4 Eintritte" in page
    csrf = csrf_of(page)
    for _ in range(2):
        await client.post("/p/training", data={"_csrf": csrf, "action": "start"}, allow_redirects=False)
    rows = db.query("SELECT * FROM subscription_visits WHERE subscription_id = ? AND visited_on = date('now', 'localtime')", (sub,))
    assert len(rows) == 1 and rows[0]["source"] == "checkin" and rows[0]["created_by"] is None
    assert db.value("SELECT COUNT(*) FROM subscription_visits WHERE subscription_id = ?", (sub,)) == before + 1
    assert "noch 3 Eintritte" in await (await client.get("/p/training")).text()
    with practice_scope(1):
        assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'subscription' AND detail LIKE '%checkin%'") >= 1
    assert mtt  # Modul geladen


async def test_briefing_shows_mtt_since_last_session(client, app):
    db = app[DB]
    await login(client, "therapie@demo.n-fizyo.ch")
    aid = db.value("SELECT a.id FROM appointments a JOIN users u ON u.id = a.therapist_id WHERE a.patient_id = ? AND u.email = ?"
                   " AND a.status IN ('booked', 'arrived') AND date(a.starts_at) >= date('now', '-1 day') ORDER BY a.starts_at LIMIT 1",
                   (pid_of(app, "Luca"), "therapie@demo.n-fizyo.ch"))
    brief = await (await client.get(f"/therapie/kurzinfo/{aid}")).text()
    assert "MTT seit letzter Sitzung" in brief and "Trainings" in brief and "Vorschläge zur Steigerung" in brief
