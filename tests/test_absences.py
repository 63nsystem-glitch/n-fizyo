"""Abwesenheiten, Schliessungen und Feiertage (Agenda 2.3.0) und die Lücken aus dem UI-Audit (Agenda/Online-Termine 0.9.0).

Feiertage geprüft gegen amtliche Listen: BJ «Gesetzliche Feiertage in der Schweiz» (Stand 1.1.2011), ge.ch (Jours fériés 2026–2029),
zh.ch (Feiertage 2026/2027), lausanne.ch (Jours fériés VD 2024–2026), gl.ch (Feiertagsregelung 2026: Näfelser Fahrt 9. April).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html import unescape

from conftest import login, pid_of

from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import TZ, local_dt, to_utc_iso, today
from nfizyo.modules.agenda import absences, gapfill, holidays
from nfizyo.modules.agenda import repo as agenda
from nfizyo.modules.booking import repo as booking


def _days(canton: str, year: int) -> dict[str, date]:
    return {h.code: h.day for h in holidays.for_canton(canton, year)}


# --------------------------------------------------------------------------- Feiertage berechnen
def test_easter_and_moving_holidays():
    assert [holidays.easter(y) for y in (2024, 2025, 2026, 2027, 2028, 2029)] == [
        date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5), date(2027, 3, 28), date(2028, 4, 16), date(2029, 4, 1)]
    # Genf, laut ge.ch «Jours fériés officiels»
    expected_ge = {
        2026: {"karfreitag": date(2026, 4, 3), "ostermontag": date(2026, 4, 6), "auffahrt": date(2026, 5, 14),
               "pfingstmontag": date(2026, 5, 25), "jeune_ge": date(2026, 9, 10), "restauration_ge": date(2026, 12, 31)},
        2027: {"karfreitag": date(2027, 3, 26), "ostermontag": date(2027, 3, 29), "auffahrt": date(2027, 5, 6),
               "pfingstmontag": date(2027, 5, 17), "jeune_ge": date(2027, 9, 9)},
        2028: {"karfreitag": date(2028, 4, 14), "ostermontag": date(2028, 4, 17), "auffahrt": date(2028, 5, 25),
               "pfingstmontag": date(2028, 6, 5), "jeune_ge": date(2028, 9, 7)},
        2029: {"karfreitag": date(2029, 3, 30), "ostermontag": date(2029, 4, 2), "auffahrt": date(2029, 5, 10),
               "pfingstmontag": date(2029, 5, 21), "jeune_ge": date(2029, 9, 6)},
    }
    for year, exp in expected_ge.items():
        got = _days("GE", year)
        assert {k: got[k] for k in exp} == exp, year
        assert set(got) == {"neujahr", "karfreitag", "ostermontag", "auffahrt", "pfingstmontag", "bundesfeier", "jeune_ge",
                            "weihnachten", "restauration_ge"}
    # Zürich, laut zh.ch (Berchtoldstag steht in der BJ-Liste)
    zh = _days("ZH", 2027)
    assert (zh["karfreitag"], zh["ostermontag"], zh["mai"], zh["auffahrt"], zh["pfingstmontag"]) == (
        date(2027, 3, 26), date(2027, 3, 29), date(2027, 5, 1), date(2027, 5, 6), date(2027, 5, 17))
    assert "fronleichnam" not in zh and "berchtold" in zh
    # Waadt: Lundi du Jeûne fédéral (lausanne.ch)
    assert [_days("VD", y)["bettagsmontag"] for y in (2024, 2025, 2026)] == [date(2024, 9, 16), date(2025, 9, 22), date(2026, 9, 21)]
    # Fronleichnam = Ostern + 60 (LU, katholisch); Bern hat keinen Fronleichnam
    assert _days("LU", 2026)["fronleichnam"] == date(2026, 6, 4) and "fronleichnam" not in _days("BE", 2026)


def test_cantonal_specials_and_footnotes():
    # Näfelser Fahrt: erster Donnerstag im April, in der Karwoche eine Woche später (gl.ch 2026: 9. April; 2023: 13. April)
    assert [holidays.date_of("fahrt_gl", y) for y in (2023, 2024, 2025, 2026)] == [
        date(2023, 4, 13), date(2024, 4, 4), date(2025, 4, 3), date(2026, 4, 9)]
    # TI und VS: kein Karfreitag; GR: Karfreitag nur «wie Feiertag behandelt»
    assert "karfreitag" not in _days("TI", 2026) and "karfreitag" not in _days("VS", 2026)
    assert next(h for h in holidays.for_canton("GR", 2026) if h.code == "karfreitag").kind == "b"
    assert _days("TI", 2026)["peterpaul"] == date(2026, 6, 29) and _days("JU", 2026)["plebiscite_ju"] == date(2026, 6, 23)
    # UR/AR/AI: kein Stephanstag, wenn Weihnachten auf Montag (2023) oder Freitag (2026) fällt
    for canton in ("UR", "AR", "AI"):
        assert "stephan" not in _days(canton, 2023) and "stephan" not in _days(canton, 2026)
        assert _days(canton, 2024)["stephan"] == date(2024, 12, 26)   # Weihnachten 2024: Mittwoch
    # NE: 2.1. und 26.12. nur, wenn 1.1. bzw. 25.12. ein Sonntag ist
    assert _days("NE", 2023)["berchtold"] == date(2023, 1, 2) and "berchtold" not in _days("NE", 2026)
    assert _days("NE", 2022)["stephan"] == date(2022, 12, 26) and "stephan" not in _days("NE", 2026)
    assert _days("NE", 2026)["republique_ne"] == date(2026, 3, 1)
    # SO: 1. Mai erst ab 12 Uhr; Josefstag nur in einzelnen Gemeinden (standardmässig aus)
    so = {h.code: h for h in holidays.for_canton("SO", 2026)}
    assert so["mai"].from_time == "12:00" and so["josef"].default_on is False and so["josef"].note == "so_communes"
    # Bundesfeiertag überall, Neujahr/Auffahrt/Weihnachten in allen 26 Kantonen
    for canton in holidays.CANTONS:
        days = _days(canton, 2026)
        assert {"bundesfeier", "neujahr", "auffahrt", "weihnachten"} <= set(days), canton
    assert holidays.for_canton("XX", 2026) == holidays.for_canton("BE", 2026)


def test_practice_holidays_canton_and_overrides(app):
    db = app[DB]
    with practice_scope(1, 1):
        assert absences.holiday_setting(db) == {"canton": None, "default": "BE", "effective": "BE"}
        offs = absences.periods(db, date(2027, 3, 26), date(2027, 3, 26), user_id=2)
        assert [o.holiday for o in offs] == ["karfreitag"] and offs[0].whole_day
        assert absences.save_canton(db, "ti") and absences.canton(db) == "TI"
        assert not absences.periods(db, date(2027, 3, 26), date(2027, 3, 26), user_id=2)       # Tessin: kein Karfreitag
        assert not absences.save_canton(db, "XY")
        absences.save_canton(db, "")
        assert absences.canton(db) == "BE"
        assert absences.set_enabled(db, "BE", "karfreitag", False)
        assert not absences.periods(db, date(2027, 3, 26), date(2027, 3, 26), user_id=2)
        assert not absences.set_enabled(db, "BE", "erfunden", True)
        absences.set_enabled(db, "BE", "karfreitag", True)
        # SO: halber Tag am 1. Mai
        absences.save_canton(db, "SO")
        off = absences.periods(db, date(2027, 5, 1), date(2027, 5, 1))[0]
        assert not off.whole_day and off.start.hour == 12
        assert not absences.overlapping(db, 2, local_dt(date(2027, 5, 1), "10:00"), local_dt(date(2027, 5, 1), "10:30"))
        assert absences.overlapping(db, 2, local_dt(date(2027, 5, 1), "12:00"), local_dt(date(2027, 5, 1), "12:30"))
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity IN ('holiday_settings', 'holiday_override')") >= 4


# --------------------------------------------------------------------------- Abwesenheiten verwalten
async def test_absence_crud_roles_and_audit(client, app):
    db = app[DB]
    day = today() + timedelta(days=30)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/agenda/abwesenheiten")).text()
    assert "Neue Abwesenheit" in html and "Ganze Praxis" in html
    r = await client.post("/empfang/agenda/abwesenheiten", data={"_csrf": csrf, "who": "3", "kind": "vacation",
                                                                  "starts_on": day.isoformat(), "ends_on": (day + timedelta(days=4)).isoformat(),
                                                                  "part": "day", "note": "Sommerferien"}, allow_redirects=False)
    assert r.status == 302
    row = db.one("SELECT * FROM absences ORDER BY id DESC LIMIT 1")
    assert (row["user_id"], row["kind"], row["practice_id"], row["start_time"]) == (3, "vacation", 1, None)
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'absence' AND entity_id = ? AND action = 'create'", (row["id"],)) == 1
    assert "Sommerferien" not in (db.value("SELECT detail FROM audit_log WHERE entity = 'absence' AND entity_id = ?", (row["id"],)) or "")
    # Fehler: Ende vor Beginn, Zeitfenster ungültig, Person fehlt
    for bad in ({"who": "3", "kind": "sick", "starts_on": "2027-01-10", "ends_on": "2027-01-09"},
                {"who": "3", "kind": "training", "starts_on": "2027-01-10", "ends_on": "2027-01-10", "part": "time",
                 "start_time": "15:00", "end_time": "13:00"},
                {"who": "", "kind": "other", "starts_on": "2027-01-10", "ends_on": "2027-01-10"},
                {"who": "6", "kind": "other", "starts_on": "2027-01-10", "ends_on": "2027-01-10"}):        # Empfang ist keine Therapeutin
        r = await client.post("/empfang/agenda/abwesenheiten", data={"_csrf": csrf, **bad})
        assert r.status == 400, bad
    # Zeitfenster und Schliessung (Leitung)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/agenda/abwesenheiten", data={"_csrf": csrf, "who": "praxis", "kind": "vacation",
                                                                  "starts_on": "2027-12-24", "ends_on": "2028-01-02", "part": "day",
                                                                  "note": "Betriebsferien"}, allow_redirects=False)
    closure = db.one("SELECT * FROM absences ORDER BY id DESC LIMIT 1")
    assert closure["user_id"] is None and closure["kind"] == "closure"
    html = await (await client.get(r.headers["Location"])).text()
    assert "Praxis geschlossen" in html
    # Bearbeiten und Löschen
    r = await client.post(f"/leitung/agenda/abwesenheiten/{row['id']}", data={
        "_csrf": csrf, "do": "save", "who": "3", "kind": "training", "starts_on": day.isoformat(), "ends_on": day.isoformat(),
        "part": "time", "start_time": "13:00", "end_time": "17:00"}, allow_redirects=False)
    assert r.status == 302
    row = db.one("SELECT * FROM absences WHERE id = ?", (row["id"],))
    assert (row["kind"], row["start_time"], row["end_time"]) == ("training", "13:00", "17:00")
    # Therapie: sieht nur eigene Abwesenheiten (+ Schliessungen), kann nichts eintragen
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get("/therapie/agenda/abwesenheiten")).text()
    assert "Betriebsferien" in html and "A. Beispiel" not in html and "Neue Abwesenheit" not in html
    assert (await client.post("/therapie/agenda/abwesenheiten", data={"_csrf": csrf})).status in (403, 405)
    assert (await client.get(f"/therapie/agenda/abwesenheiten/{row['id']}", allow_redirects=False)).status == 404
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post(f"/empfang/agenda/abwesenheiten/{closure['id']}", data={"_csrf": csrf, "do": "delete"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM absences WHERE id = ?", (closure["id"],)) == 0
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'absence' AND action = 'delete'") == 1


async def test_absence_shows_affected_appointments(client, app):
    db = app[DB]
    day = today() + timedelta(days=40)
    with practice_scope(1, 1):
        aid = agenda.create(db, therapist_id=3, start=local_dt(day, "09:00"), minutes=30, patient_id=pid_of(app, "Sandra"))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post("/empfang/agenda/abwesenheiten", data={"_csrf": csrf, "who": "3", "kind": "sick", "starts_on": day.isoformat(),
                                                                  "ends_on": day.isoformat(), "part": "day"})
    html = await r.text()
    assert "1 geplanter Termin fällt in diese Zeit" in html and f"/empfang/agenda/termine/{aid}" in html


# --------------------------------------------------------------------------- Wirkung: Online-Termine, Lücken, Agenda, Warnung, Serie
def _type(db, minutes_type: int = 1):
    return next(t for t in booking.bookable_types(db) if t["id"] == minutes_type)


def test_booking_never_offers_absences_holidays_closures(app):
    db = app[DB]
    now = datetime(2027, 3, 22, 7, 0, tzinfo=TZ)            # Montag vor Ostern 2027
    with practice_scope(1, 1):
        t = _type(db)
        slots = booking.free_slots(db, t, start_day=now.date(), days=9, now=now)
        days = {s.start.date() for s in slots}
        assert date(2027, 3, 26) not in days and date(2027, 3, 29) not in days      # Karfreitag, Ostermontag (BE)
        assert date(2027, 3, 25) in days and date(2027, 3, 30) in days
        # Ferien A. Beispiel (3) am Dienstag, Weiterbildung T. Demo (2) am Mittwoch nachmittags, Schliesstag am Donnerstag
        for values in ({"user_id": 3, "kind": "vacation", "starts_on": "2027-03-23", "ends_on": "2027-03-23"},
                       {"user_id": 2, "kind": "training", "starts_on": "2027-03-24", "ends_on": "2027-03-24", "start_time": "13:00",
                        "end_time": "18:30"},
                       {"user_id": None, "kind": "closure", "starts_on": "2027-03-25", "ends_on": "2027-03-25"}):
            absences.save(db, {"start_time": None, "end_time": None, "note": None, **values})
        slots = booking.free_slots(db, t, start_day=now.date(), days=9, now=now)
        by_day = {}
        for s in slots:
            by_day.setdefault(s.start.date(), set()).add((s.therapist_id, s.start.hour))
        assert all(tid == 2 for tid, _ in by_day[date(2027, 3, 23)])
        assert all(h < 13 for tid, h in by_day[date(2027, 3, 24)] if tid == 2) and any(tid == 3 and h >= 13 for tid, h in by_day[date(2027, 3, 24)])
        assert date(2027, 3, 25) not in by_day
    # slot_still_free (zum Zeitpunkt der Anfrage) prüft ebenfalls
    d = today() + timedelta(days=7)
    while d.weekday() > 4:
        d += timedelta(days=1)
    start = local_dt(d, "08:00")
    with practice_scope(1, 1):
        assert booking.slot_still_free(db, t, 2, start, None) or agenda.conflicts(
            db, therapist_id=2, start_utc=to_utc_iso(start), end_utc=to_utc_iso(start + timedelta(minutes=30)))
        absences.save(db, {"user_id": 2, "kind": "sick", "starts_on": d.isoformat(), "ends_on": d.isoformat(), "start_time": None,
                           "end_time": None, "note": None})
        assert not booking.slot_still_free(db, t, 2, start, None)


async def test_public_booking_page_mentions_closure(client, app):
    db = app[DB]
    first = today() + timedelta(days=2)
    with practice_scope(1, 1):
        absences.save(db, {"user_id": None, "kind": "closure", "starts_on": first.isoformat(), "ends_on": (first + timedelta(days=2)).isoformat(),
                           "start_time": None, "end_time": None, "note": "Inventur"})
    slug = db.value("SELECT slug FROM practices WHERE id = 1")
    html = await (await client.get(f"/p/termin/{slug}")).text()
    type_id = re.search(r'href="\?art=(\d+)"', html).group(1)
    html = await (await client.get(f"/p/termin/{slug}?art={type_id}")).text()
    assert "Die Praxis ist geschlossen" in html and "Inventur" not in html
    assert first.strftime("%d.%m.%Y") in html


def test_gapfill_skips_absences(app):
    db = app[DB]
    day = today() + timedelta(days=10)
    with practice_scope(1, 1):
        agenda.add_waitlist(db, patient_id=pid_of(app, "Lea"), wish="früher", preferred="egal")
        aid = agenda.create(db, therapist_id=2, start=local_dt(day, "10:00"), minutes=30, patient_id=pid_of(app, "Hans"))
        agenda.set_status(db, aid, "cancelled", "krank")
        a = agenda.get(db, aid)
        assert gapfill.gap_open(db, a) and gapfill.candidates(db, a)["sms"]
        assert any(g["a"]["id"] == aid for g in gapfill.open_gaps(db))
        absences.save(db, {"user_id": 2, "kind": "vacation", "starts_on": day.isoformat(), "ends_on": day.isoformat(), "start_time": None,
                           "end_time": None, "note": None})
        assert not gapfill.gap_open(db, a) and gapfill.off_time(db, a)
        assert all(g["a"]["id"] != aid for g in gapfill.open_gaps(db))
        # schon versandtes Angebot: wer jetzt bestätigt, bekommt den Termin nicht
        absences.delete(db, db.value("SELECT MAX(id) FROM absences"))
        oid = gapfill.create_offer(db, a)
        entry = next(w for w in gapfill.waitlist_rows(db) if w["patient_id"] == pid_of(app, "Lea"))
        token = gapfill.add_recipient(db, oid, entry)
        absences.save(db, {"user_id": None, "kind": "closure", "starts_on": day.isoformat(), "ends_on": day.isoformat(), "start_time": None,
                           "end_time": None, "note": None})
        result, booked_id, _ = gapfill.accept(db, gapfill.by_token(db, token))
        assert result == "taken" and booked_id is None


async def test_agenda_shows_bands_and_warns_on_create_and_move(client, app):
    db = app[DB]
    good_friday = date(2027, 3, 26)
    with practice_scope(1, 1):
        absences.save(db, {"user_id": 3, "kind": "vacation", "starts_on": "2027-03-22", "ends_on": "2027-03-24", "start_time": None,
                           "end_time": None, "note": None})
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/agenda?view=tag&d={good_friday}")).text()
    assert "Karfreitag" in html and "data-off-notice" in html and "data-off-band" in html
    html = await (await client.get("/empfang/agenda?view=woche&d=2027-03-22&who=3")).text()
    assert "Ferien – A. Beispiel" in html and "Karfreitag" in html
    # Termin am Feiertag: Warnung, nichts angelegt; mit «Trotzdem speichern» angelegt
    lea = db.one("SELECT * FROM patients WHERE first_name = 'Lea'")
    data = {"_csrf": csrf, "patient": lea["number"], "therapist_id": "2", "date": good_friday.isoformat(), "time": "09:00",
            "treatment_type_id": "1"}
    before = db.value("SELECT COUNT(*) FROM appointments")
    html = await (await client.post("/empfang/agenda/neu", data=data)).text()
    assert "Feiertag, Schliessung oder Abwesenheit" in html and "Karfreitag" in html and 'name="force"' in html
    assert db.value("SELECT COUNT(*) FROM appointments") == before
    r = await client.post("/empfang/agenda/neu", data={**data, "force": "1"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM appointments") == before + 1
    aid = db.value("SELECT MAX(id) FROM appointments")
    assert "Dieser Termin liegt in" in await (await client.get(f"/empfang/agenda/termine/{aid}")).text()
    # Verschieben in die Ferien von A. Beispiel: zurück mit Warnung; mit force verschoben
    move = {"_csrf": csrf, "date": "2027-03-23", "time": "10:00", "therapist_id": "3"}
    r = await client.post(f"/empfang/agenda/termine/{aid}/verschieben", data=move, allow_redirects=False)
    assert r.status == 302 and "konflikt=1" in r.headers["Location"]
    html = await (await client.get(r.headers["Location"])).text()
    assert "Ferien – A. Beispiel" in html
    assert db.value("SELECT therapist_id FROM appointments WHERE id = ?", (aid,)) == 2
    r = await client.post(f"/empfang/agenda/termine/{aid}/verschieben", data={**move, "force": "1"}, allow_redirects=False)
    assert db.value("SELECT therapist_id FROM appointments WHERE id = ?", (aid,)) == 3


async def test_series_skips_holidays_and_absences(client, app):
    db = app[DB]
    lea = db.one("SELECT * FROM patients WHERE first_name = 'Lea'")
    with practice_scope(1, 1):
        absences.save(db, {"user_id": 2, "kind": "training", "starts_on": "2027-04-01", "ends_on": "2027-04-01", "start_time": None,
                           "end_time": None, "note": None})
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    form = {"_csrf": csrf, "patient": lea["number"], "therapist_id": "2", "start": "2027-03-22", "time": "07:30", "count": "6",
            "treatment_type_id": "1", "weekday": ["0", "3", "4"]}
    html = await (await client.post("/empfang/agenda/serie", data=form)).text()
    assert html.count("data-skipped") == 3                              # Karfreitag 26.3., Ostermontag 29.3., Weiterbildung 1.4.
    assert "Karfreitag" in html and "Ostermontag" in html and "Weiterbildung" in html
    before = db.value("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", (lea["id"],))
    r = await client.post("/empfang/agenda/serie", data={**form, "create": "1"}, allow_redirects=False)
    assert r.status == 302 and "uebersprungen=2027-03-26,2027-03-29,2027-04-01" in r.headers["Location"]
    rows = db.query("SELECT starts_at FROM appointments WHERE patient_id = ? ORDER BY starts_at DESC LIMIT 6", (lea["id"],))
    assert db.value("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", (lea["id"],)) == before + 6
    created = {r["starts_at"][:10] for r in rows}
    assert not created & {"2027-03-26", "2027-03-29", "2027-04-01"}
    html = await (await client.get(r.headers["Location"] if False else f"/empfang/patienten/{lea['id']}/termine?uebersprungen=2027-03-26,2027-03-29")).text()
    assert "Nicht angelegt" in html and "Fr 26.03.2027" in html


# --------------------------------------------------------------------------- B5–B9: Formulare und Ansichten
async def test_new_appointment_form_defaults(client, app):
    db = app[DB]
    sandra = db.one("SELECT * FROM patients WHERE first_name = 'Sandra'")      # Hauptbehandlerin: A. Beispiel (3)
    await login(client, "empfang@demo.n-fizyo.ch")
    label = f"{sandra['last_name']} {sandra['first_name']} · 17.01.1985 · {sandra['number']}"
    html = await (await client.get("/empfang/agenda/neu", params={"patient": label})).text()
    assert re.search(r'<option value="3" selected>A\. Beispiel', html)
    date_value = re.search(r'id="date" name="date" type="date" value="([^"]*)"', html).group(1)
    assert date_value and date.fromisoformat(date_value) >= today()
    assert re.search(r'data-blocker-only hidden', html) and "data-patient-only>" in html.replace(' data-patient-only>', 'data-patient-only>')
    # Vorschlag liegt nicht in Abwesenheit, Feiertag oder einem Termin
    t = re.search(r'id="time" name="time" type="time" step="300" value="([^"]*)"', html).group(1)
    with practice_scope(1, 1):
        start = local_dt(date.fromisoformat(date_value), t)
        assert not agenda.conflicts(db, therapist_id=3, start_utc=to_utc_iso(start), end_utc=to_utc_iso(start + timedelta(minutes=30)))
        assert not absences.overlapping(db, 3, start, start + timedelta(minutes=30))
    # Blocker angehakt (nach Fehler): Titel sichtbar, Patientenfeld versteckt
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.post("/empfang/agenda/neu", data={"_csrf": csrf, "blocker": "1", "therapist_id": "2", "date": "", "time": ""})).text()
    assert "data-blocker-only>" in html and "data-patient-only hidden" in html


async def test_done_appointment_offers_only_undo(client, app):
    db = app[DB]
    done = db.value("SELECT id FROM appointments WHERE practice_id = 1 AND status = 'done' AND patient_id IS NOT NULL LIMIT 1")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/agenda/termine/{done}")).text()
    assert 'data-to="booked"' in html
    for st in ("arrived", "no_show", "cancelled", "done"):
        assert f'data-to="{st}"' not in html, st
    assert "data-move" not in html and "data-final" in html
    r = await client.post(f"/empfang/agenda/termine/{done}/status", data={"_csrf": csrf, "status": "no_show"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT status FROM appointments WHERE id = ?", (done,)) == "done"
    r = await client.post(f"/empfang/agenda/termine/{done}/verschieben", data={"_csrf": csrf, "date": "2027-01-05", "time": "09:00"},
                          allow_redirects=False)
    assert db.value("SELECT status FROM appointments WHERE id = ?", (done,)) == "done"
    assert not db.value("SELECT starts_at FROM appointments WHERE id = ?", (done,)).startswith("2027-01-05")
    booked = db.value("SELECT id FROM appointments WHERE practice_id = 1 AND status = 'booked' AND patient_id IS NOT NULL LIMIT 1")
    html = await (await client.get(f"/empfang/agenda/termine/{booked}")).text()
    assert all(f'data-to="{st}"' in html for st in ("arrived", "done", "no_show", "cancelled")) and "data-move" in html


async def test_series_page_label_and_remaining(client, app):
    await login(client, "empfang@demo.n-fizyo.ch")
    html = unescape(await (await client.get("/empfang/agenda/serie?patient=P00004")).text())
    assert 'value="Muster, Peter · 02.11.1948 · P00004"' in html
    assert "noch 0 von 9 verordneten Sitzungen offen" in html and "data-rx-used" in html
    assert re.search(r'id="count" name="count" type="number" min="1" max="36" value="1"', html)
    from nfizyo.core import patients as core_patients

    with practice_scope(1, 1):
        core_patients.add_prescription(app[DB], pid_of(app, "Lea"), sessions=12)
    html = unescape(await (await client.get("/empfang/agenda/serie?patient=P00001")).text())
    lea_remaining = int(re.search(r"noch (\d+) von 12", html).group(1))
    assert lea_remaining == 12
    assert re.search(rf'id="count"[^>]*value="{lea_remaining}"', html) and "data-exceeds" not in html
    html = unescape(await (await client.get(f"/empfang/agenda/serie?patient=P00001&count={lea_remaining + 3}")).text())
    assert "data-exceeds" in html and "ohne Verordnung" in html


async def test_beyond_prescription_badge(client, app):
    peter = pid_of(app, "Peter")                     # Demo: Sitzungen 10/9 und 11/9
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/patienten/{peter}/termine")).text()
    assert html.count("data-beyond-rx") >= 2 and "11/9" in html and "data-beyond" in html
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get(f"/therapie/patienten/{peter}/termine")).text()
    assert "ohne Verordnung" in html
    html = await (await client.get("/therapie/agenda?view=woche&d=2026-11-09")).text()
    assert "ohne Verordnung" in html
    aid = app[DB].value("SELECT id FROM appointments WHERE patient_id = ? ORDER BY starts_at DESC LIMIT 1", (peter,))
    assert "ohne Verordnung" in await (await client.get(f"/therapie/agenda/termine/{aid}")).text()


async def test_leitung_week_defaults_and_empty_hints(client, app):
    db = app[DB]
    monday = today() - timedelta(days=today().weekday())
    lo, hi = local_dt(monday, "00:00").isoformat(), local_dt(monday + timedelta(days=7), "00:00").isoformat()
    db.execute("UPDATE appointments SET therapist_id = 2 WHERE practice_id = 1 AND therapist_id = 1")   # Leitung ohne Termine
    await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get(f"/leitung/agenda?view=woche&d={monday}")).text()
    who = int(re.search(r'<option value="(\d+)" selected>', html).group(1))
    assert who != 1 and db.value("SELECT COUNT(*) FROM appointments WHERE therapist_id = ? AND starts_at >= ? AND starts_at < ?",
                                 (who, lo, hi)) > 0
    html = await (await client.get(f"/leitung/agenda?view=woche&d={monday}&who=1")).text()
    assert "data-empty-hint" in html and "Keine Termine in dieser Woche für M. Leitung" in html
    # Tag ohne Personen: passende Wörter
    db.execute("DELETE FROM therapist_hours WHERE practice_id = 1")
    past, future = date(2026, 1, 4), today() + timedelta(days=60)
    db.execute("DELETE FROM appointments WHERE practice_id = 1 AND starts_at >= ?", (local_dt(future, "00:00").isoformat(),))
    html = await (await client.get(f"/leitung/agenda?view=tag&d={past}")).text()
    assert "An diesem Tag war niemand eingeplant." in html and "Heute niemand" not in html
    html = await (await client.get(f"/leitung/agenda?view=tag&d={future}")).text()
    assert "An diesem Tag ist niemand eingeplant." in html


# --------------------------------------------------------------------------- B10: Standorte, Räume, Geräte umbenennen
async def test_rename_locations_rooms_devices(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    loc = db.one("SELECT * FROM locations WHERE practice_id = 1 ORDER BY id LIMIT 1")
    room = db.one("SELECT * FROM rooms WHERE practice_id = 1 ORDER BY id LIMIT 1")
    device = db.one("SELECT * FROM devices WHERE practice_id = 1 ORDER BY id LIMIT 1")
    html = await (await client.get(f"/leitung/einstellungen/standorte?bearbeiten={loc['id']}")).text()
    assert "data-edit-form" in html and "Umbenennen" in html
    url = "/leitung/einstellungen/standorte"
    await client.post(url, data={"_csrf": csrf, "do": "edit_location", "id": str(loc["id"]), "name": "Standort Neu", "address": "Neugasse 1",
                                 "phone": "032 000 00 00"})
    await client.post(url, data={"_csrf": csrf, "do": "rename", "table": "rooms", "id": str(room["id"]), "name": "Raum Blau",
                                 "location_id": str(loc["id"])})
    await client.post(url, data={"_csrf": csrf, "do": "rename", "table": "devices", "id": str(device["id"]), "name": "Laser 2"})
    assert db.value("SELECT name FROM locations WHERE id = ?", (loc["id"],)) == "Standort Neu"
    assert db.value("SELECT address FROM locations WHERE id = ?", (loc["id"],)) == "Neugasse 1"
    assert db.value("SELECT name FROM rooms WHERE id = ?", (room["id"],)) == "Raum Blau"
    assert db.value("SELECT name FROM devices WHERE id = ?", (device["id"],)) == "Laser 2"
    await client.post(url, data={"_csrf": csrf, "do": "rename", "table": "users", "id": "1", "name": "X"})
    await client.post(url, data={"_csrf": csrf, "do": "rename", "table": "rooms", "id": str(room["id"]), "name": ""})
    assert db.value("SELECT name FROM users WHERE id = 1") != "X" and db.value("SELECT name FROM rooms WHERE id = ?", (room["id"],)) == "Raum Blau"
    await client.post(url, data={"_csrf": csrf, "do": "toggle", "table": "rooms", "id": str(room["id"])})
    assert db.value("SELECT active FROM rooms WHERE id = ?", (room["id"],)) == 0
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity IN ('location', 'room', 'device') AND action = 'update'") >= 4


# --------------------------------------------------------------------------- B11: Online-Anfragen
def _free_start(db, therapist_id: int = 2, *, base: date | None = None) -> datetime:
    """Feste, freie Zeit (nicht «jetzt + 5 Tage zur aktuellen Minute»): 20:00 am ersten Tag ab base + 5 Tage, an dem die Person
    zwei Stunden lang keinen Termin, keine Abwesenheit und keinen Feiertag hat. Früher hing der Test von der Uhrzeit des
    Testlaufs ab (CODE-10: um 09:47 lag die Anfrage auf einem Demo-Termin)."""
    d0 = base or today()
    for i in range(5, 60):
        start = local_dt(d0 + timedelta(days=i), "20:00")
        end = start + timedelta(hours=2)
        if not agenda.conflicts(db, therapist_id=therapist_id, start_utc=to_utc_iso(start), end_utc=to_utc_iso(end)) \
                and not absences.overlapping(db, therapist_id, start, end):
            return start
    raise AssertionError("keine freie Zeit gefunden")


def _request_row(db, *, start: datetime | None = None, **values):
    data = {"first_name": "Lia", "last_name": "Muster", "birthdate": "1992-03-05", "phone": "+41790000001", "lang": "de", "note": None}
    data.update(values)
    t = _type(db)
    start = start or _free_start(db)
    rid = booking.create_request(db, type_row=t, therapist_id=2, start=start, location_id=None, data=data)
    db.execute("UPDATE booking_requests SET status = 'requested', verified_at = ? WHERE id = ?", (datetime.now(TZ).isoformat(), rid))
    return booking.get(db, rid)


def test_likely_patients_finds_typos_birthdate_and_phone(app):
    db = app[DB]
    with practice_scope(1, 1):
        lea = pid_of(app, "Lea")
        r = _request_row(db)                                                  # «Lia» statt «Lea»
        likely = booking.likely_patients(db, r)
        assert likely and likely[0]["p"]["id"] == lea and {"name", "birthdate", "phone"} <= set(likely[0]["reasons"])
        assert booking.existing_patient(db, r) is None                        # kein exakter Treffer → früher: neue Person (Duplikat)
        r2 = _request_row(db, first_name="Muster", last_name="Lea", phone="+41799999999")   # vertauscht
        assert likely_ids(db, r2)[0] == lea
        r3 = _request_row(db, first_name="Xaver", last_name="Unbekannt", birthdate="2001-01-01", phone="+41798888888")
        assert booking.likely_patients(db, r3) == []
        assert booking.patient_for_choice(db, str(lea))["id"] == lea
        assert booking.patient_for_choice(db, "Muster Lea · 05.03.1992 · P00001")["id"] == lea
        assert booking.patient_for_choice(db, "gibt es nicht") is None


def likely_ids(db, r):
    return [m["p"]["id"] for m in booking.likely_patients(db, r)]


async def test_reception_picks_existing_patient_or_creates_new(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    with practice_scope(1, 1):
        rid = _request_row(db)["id"]
        rid2 = _request_row(db, first_name="Mira", last_name="Neu", birthdate="1985-02-03", phone="+41795554433")["id"]
        db.execute("UPDATE booking_requests SET starts_at = datetime(starts_at, '+1 hour'), ends_at = datetime(ends_at, '+1 hour')"
                   " WHERE id = ?", (rid2,))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/online-anfragen")).text()
    assert "Ähnliche Person gefunden" in html and f'name="patient_choice" value="{lea}" required>' in html
    assert 'data-dialog="decline-' in html and '<dialog class="dlg" id="decline-' in html and "booking.js" in html
    n_patients = db.value("SELECT COUNT(*) FROM patients")
    r = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid),
                                                            "patient_choice": str(lea)}, allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].endswith(f"/patienten/{lea}")
    assert db.value("SELECT patient_id FROM booking_requests WHERE id = ?", (rid,)) == lea
    assert db.value("SELECT COUNT(*) FROM patients") == n_patients
    # «andere Person» ohne gültige Auswahl → Hinweis, nichts passiert
    r = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid2),
                                                            "patient_choice": "other", "patient": "Niemand"}, allow_redirects=False)
    assert db.value("SELECT status FROM booking_requests WHERE id = ?", (rid2,)) == "requested"
    r = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(rid2),
                                                            "patient_choice": "new"}, allow_redirects=False)
    assert db.value("SELECT status FROM booking_requests WHERE id = ?", (rid2,)) == "confirmed"
    assert db.value("SELECT COUNT(*) FROM patients") == n_patients + 1


async def test_confirm_refused_during_absence(client, app):
    db = app[DB]
    with practice_scope(1, 1):
        r = _request_row(db, first_name="Ugo", last_name="Nuovo", birthdate="1977-07-07", phone="+41791112233")
        d = r["starts_at"]
        day = datetime.fromisoformat(d).astimezone(TZ).date()
        absences.save(db, {"user_id": 2, "kind": "sick", "starts_on": day.isoformat(), "ends_on": day.isoformat(), "start_time": None,
                           "end_time": None, "note": None})
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    assert "fällt in Abwesenheit" in await (await client.get("/empfang/online-anfragen")).text()
    await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(r["id"]), "patient_choice": "new"})
    assert db.value("SELECT status FROM booking_requests WHERE id = ?", (r["id"],)) == "requested"


def test_old_requests_purged_periodically(app):
    from nfizyo.modules.booking import views as booking_views

    db = app[DB]
    with practice_scope(1, 1):
        rid = _request_row(db)["id"]
    old = (datetime.now(TZ) - timedelta(days=120)).isoformat()
    db.execute("UPDATE booking_requests SET status = 'declined', created_at = ? WHERE id = ?", (old, rid))
    booking_views.purge_all(app)
    assert db.value("SELECT COUNT(*) FROM booking_requests WHERE id = ?", (rid,)) == 0
    assert booking_views.start_purge in app.on_startup and booking_views.stop_purge in app.on_cleanup
    assert booking_views.PURGE_INTERVAL <= 24 * 3600


# --------------------------------------------------------------------------- Feiertage-Einstellungen (Leitung)
async def test_holiday_settings_page(client, app):
    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/feiertage?jahr=2027")).text()
    assert "Karfreitag" in html and "wie Adresse der Praxis (BE)" in html and "Bundesamt für Justiz" in html
    await client.post("/leitung/einstellungen/feiertage", data={"_csrf": csrf, "do": "toggle", "code": "berchtold", "enabled": "0", "jahr": "2027"})
    await client.post("/leitung/einstellungen/feiertage", data={"_csrf": csrf, "do": "canton", "canton": "GE"})
    with practice_scope(1):
        assert absences.canton(db) == "GE" and absences.overrides(db, "BE") == {"berchtold": False}
    html = await (await client.get("/leitung/einstellungen/feiertage?jahr=2027")).text()
    assert "Genfer Bettag" in html and "Kanton GE" in html
    await login(client, "empfang@demo.n-fizyo.ch")
    assert (await client.get("/leitung/einstellungen/feiertage", allow_redirects=False)).status in (302, 403, 404)


async def test_today_pages_and_patient_app_show_offs(client, app):
    from nfizyo.core.auth import create_session

    db = app[DB]
    d = today() + timedelta(days=1)
    with practice_scope(1, 1):
        absences.save(db, {"user_id": 2, "kind": "training", "starts_on": d.isoformat(), "ends_on": d.isoformat(), "start_time": "13:00",
                           "end_time": "17:00", "note": None})
        absences.save(db, {"user_id": None, "kind": "closure", "starts_on": (d + timedelta(days=3)).isoformat(),
                           "ends_on": (d + timedelta(days=5)).isoformat(), "start_time": None, "end_time": None, "note": None})
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get(f"/therapie/heute?d={d}")).text()
    assert "Weiterbildung – T. Demo (13:00–17:00)" in html
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/heute?d={d}")).text()
    assert "Heute abwesend" in html and "Weiterbildung – T. Demo" in html
    html = await (await client.get(f"/empfang/heute?d={d + timedelta(days=4)}")).text()
    assert "Praxis geschlossen" in html
    token, _ = create_session(db, "patient", patient_id=pid_of(app, "Lea"), practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    html = await (await client.get("/p/termine")).text()
    assert "Die Praxis ist geschlossen" in html and (d + timedelta(days=3)).strftime("%d.%m.%Y") in html
