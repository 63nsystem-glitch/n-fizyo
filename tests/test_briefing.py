"""Kurzüberblick vor dem Termin und Übergabe beim Wechsel der Therapeutin / des Therapeuten."""
from __future__ import annotations

from conftest import login, pid_of

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope


def _today_appt(app, first_name: str, therapist_email: str) -> int:
    db = app[DB]
    return db.value("SELECT a.id FROM appointments a JOIN users u ON u.id = a.therapist_id WHERE a.patient_id = ? AND u.email = ?"
                    " AND a.status IN ('booked', 'arrived') AND date(a.starts_at) >= date('now', '-1 day') ORDER BY a.starts_at LIMIT 1",
                    (pid_of(app, first_name), therapist_email))


async def test_brief_page_shows_what_happened_since_last_session(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    aid = _today_appt(app, "Lea", "therapie@demo.n-fizyo.ch")
    r = await client.get(f"/therapie/kurzinfo/{aid}")
    assert r.status == 200
    text = await r.text()
    assert "Letztes Mal" in text or "Velo-Ergometer" in text      # Plan der letzten freigegebenen Notiz
    assert "Übungen an" in text                                    # Heimprogramm seit der letzten Sitzung
    assert "geschwollen" in text                                   # Problem-Meldung aus der App
    assert "Flexion" in text and "Zurück zum Unihockey" in text    # Messwerte und Ziel
    assert "Keine Knieextension" in text                           # Einschränkung des Operateurs
    assert "ohne KI" in text


async def test_brief_only_for_own_patients(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    db = app[DB]
    sandra_appt = db.value("SELECT id FROM appointments WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (pid_of(app, "Sandra"),))
    assert (await client.get(f"/therapie/kurzinfo/{sandra_appt}", allow_redirects=False)).status == 404
    assert (await client.get(f"/therapie/patienten/{pid_of(app, 'Sandra')}/uebergabe", allow_redirects=False)).status == 404


async def test_takeover_shows_handover_from_colleague(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    luca = pid_of(app, "Luca")
    text = await (await client.get(f"/therapie/patienten/{luca}")).text()
    assert "Übergabe von L. Esempio" in text
    assert "Richtungswechsel" in text                     # Einschränkung
    full = await (await client.get(f"/therapie/patienten/{luca}/uebergabe")).text()
    assert "Laufband" in full and "Beinpresse" in full    # Plan und Behandlung aus den Notizen von L. Esempio
    assert "Asphalt" in full                              # Nachricht auf Italienisch, übersetzt
    assert "/therapie/notizen/" in full                   # jede Angabe mit Quelle
    aid = _today_appt(app, "Luca", "therapie@demo.n-fizyo.ch")
    brief = await (await client.get(f"/therapie/kurzinfo/{aid}")).text()
    assert "Übernahme" in brief and "L. Esempio" in brief
    # Wer zuletzt selbst behandelt hat, braucht keine Übergabe
    lea = await (await client.get(f"/therapie/patienten/{pid_of(app, 'Lea')}")).text()
    assert "Übergabe von" not in lea


async def test_back_from_holiday_shows_what_happened(client, app):
    db = app[DB]
    luca = pid_of(app, "Luca")
    with practice_scope(1):
        aid = _today_appt(app, "Luca", "therapie@demo.n-fizyo.ch")
        db.execute("UPDATE appointments SET status = 'done', starts_at = datetime('now', '-2 hours'), ends_at = datetime('now', '-90 minutes')"
                   " WHERE id = ?", (aid,))
    await login(client, "l.esempio@demo.n-fizyo.ch")
    text = await (await client.get(f"/therapie/patienten/{luca}")).text()
    assert "Während Ihrer Abwesenheit" in text and "T. Demo" in text
    # T. Demo hat jetzt selbst zuletzt behandelt: keine Übergabe mehr
    await login(client, "therapie@demo.n-fizyo.ch")
    text = await (await client.get(f"/therapie/patienten/{luca}")).text()
    assert "Übergabe von" not in text


async def test_today_screen_still_renders_with_brief(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.get("/therapie/heute")
    assert r.status == 200
    text = await r.text()
    if "Als Nächstes" in text:
        assert 'class="brief"' in text
