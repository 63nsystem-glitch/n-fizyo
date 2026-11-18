"""QA-Fixes 0.9.0 (Bereich C): Agenda, Online-Termine, Verordnungen im Blick, Datenübernahme, Patient/innen, Warteliste,
Telefonnummern und SMS-Bremsen, Leistung.

Jeder Test nennt die QA-Nummer (F.. = Abläufe, SEC-.. = Sicherheit, CODE-.. = Code)."""
from __future__ import annotations

import io
import re
import time
import zipfile
from datetime import datetime, timedelta

import aiohttp
from conftest import csrf_of, login, pid_of

from nfizyo.core import patients as core_patients
from nfizyo.core import sms
from nfizyo.core.db import utcnow
from nfizyo.core.keys import AI, DB, JOBS
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import TZ, local_dt, to_utc_iso, today
from nfizyo.modules.agenda import absences
from nfizyo.modules.agenda import api as agenda_api
from nfizyo.modules.agenda import repo as agenda
from nfizyo.modules.booking import repo as booking
from nfizyo.modules.dataimport import engine, reader
from nfizyo.modules.rxwatch import repo as rx
from nfizyo.modules.rxwatch import views as rx_views


def _free_day_start(db, therapist_id: int = 2, hhmm: str = "20:00", days: int = 5):
    """Feste freie Zeit (nicht «jetzt»): hhmm am ersten Tag ab heute + days ohne Termin/Abwesenheit/Feiertag der Person."""
    for i in range(days, days + 60):
        start = local_dt(today() + timedelta(days=i), hhmm)
        end = start + timedelta(hours=2)
        if not agenda.conflicts(db, therapist_id=therapist_id, start_utc=to_utc_iso(start), end_utc=to_utc_iso(end)) \
                and not absences.overlapping(db, therapist_id, start, end):
            return start
    raise AssertionError("keine freie Zeit")


def _request(db, start, **values):
    data = {"first_name": "Nora", "last_name": "Neuperson", "birthdate": "1990-01-01", "phone": "+41790000077", "lang": "de",
            "note": None}
    data.update(values)
    t = next(t for t in booking.bookable_types(db) if t["id"] == 1)
    rid = booking.create_request(db, type_row=t, therapist_id=2, start=start, location_id=None, data=data)
    db.execute("UPDATE booking_requests SET status = 'requested', verified_at = ? WHERE id = ?", (utcnow(), rid))
    return booking.get(db, rid)


class _Count:
    def __init__(self):
        self.n = 0

    def __call__(self, *_):
        self.n += 1


# --------------------------------------------------------------------------- CODE-01 / CODE-09 Leistung
def test_code01_index_and_badge_with_few_queries(app):
    db = app[DB]
    assert db.value("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'appointments_prescription'")
    with practice_scope(1, 1):
        start = local_dt(today() + timedelta(days=3), "07:00")
        for i in range(60):                      # viele aktive Verordnungen mit Terminen
            pid = core_patients.create(db, {"first_name": f"Viel{i}", "last_name": "Daten"})
            rid = core_patients.add_prescription(db, pid, sessions=9)
            for k in range(3):
                agenda.create(db, therapist_id=2, start=start - timedelta(days=7 * k, minutes=i), minutes=30, patient_id=pid,
                              prescription_id=rid, status="done" if k else "booked")
        count = _Count()
        db.conn.set_trace_callback(count)
        try:
            n_act = rx_views.badge_act(db, 1, None)
            n_badge = count.n
            appts = db.query("SELECT * FROM appointments WHERE practice_id = 1 AND prescription_id IS NOT NULL")
            count.n = 0
            numbers = agenda.session_numbers(db, appts)
            n_numbers = count.n
        finally:
            db.conn.set_trace_callback(None)
    assert isinstance(n_act, int)
    assert n_badge <= 10, n_badge             # früher: mehrere Abfragen pro Verordnung (≈ 15 000 mit einem Jahr Daten)
    assert n_numbers == 1 and len(numbers) == len(appts)


def test_code09_session_numbers_same_as_single(app):
    db = app[DB]
    with practice_scope(1, 1):
        appts = db.query("SELECT * FROM appointments WHERE practice_id = 1 AND prescription_id IS NOT NULL")
        many = agenda.session_numbers(db, appts)
        for a in appts:
            n, total = many[a["id"]]
            expected = db.value("SELECT COUNT(*) FROM appointments WHERE practice_id = 1 AND prescription_id = ? AND starts_at <= ?"
                                " AND status NOT IN ('cancelled', 'no_show')", (a["prescription_id"], a["starts_at"]))
            assert n == expected and total == 9


# --------------------------------------------------------------------------- CODE-02 Bestätigen ist alles oder nichts
async def test_code02_confirm_taken_slot_creates_nobody(client, app):
    db = app[DB]
    with practice_scope(1, 6):
        start = _free_day_start(db)
        r = _request(db, start)
        agenda_api.book(db, patient_id=pid_of(app, "Hans"), therapist_id=2, start=start, minutes=30, treatment_type_id=None,
                        location_id=None, note=None)                # inzwischen am Telefon vergeben
        n0 = db.value("SELECT COUNT(*) FROM patients")
        assert booking.confirm(db, r, create_new=True) == (None, None)
        assert booking.confirm(db, booking.get(db, r["id"]), create_new=True) == (None, None)
        assert db.value("SELECT COUNT(*) FROM patients") == n0
        assert db.value("SELECT status FROM booking_requests WHERE id = ?", (r["id"],)) == "requested"
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    for _ in range(2):
        resp = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(r["id"]),
                                                                   "patient_choice": "new"}, allow_redirects=False)
        assert resp.status == 302 and resp.headers["Location"].endswith("/online-anfragen")
    assert db.value("SELECT COUNT(*) FROM patients WHERE last_name = 'Neuperson'") == 0
    assert "inzwischen belegt" in await (await client.get("/empfang/online-anfragen")).text()


async def test_code02_already_confirmed_is_not_confirmed_twice(client, app):
    db = app[DB]
    with practice_scope(1, 6):
        r = _request(db, _free_day_start(db))
        pid, aid = booking.confirm(db, r, create_new=True)
        assert pid and aid
        assert booking.confirm(db, r, create_new=True) == (None, None)        # alte Zeile (zweiter Klick)
    assert db.value("SELECT COUNT(*) FROM patients WHERE last_name = 'Neuperson'") == 1
    assert db.value("SELECT COUNT(*) FROM appointments WHERE patient_id = ?", (pid,)) == 1


# --------------------------------------------------------------------------- CODE-03 Übersetzungsaufträge
async def test_code03_anonymous_visits_do_not_flood_the_job_queue(client, app):
    from nfizyo.ai.translate import TranslationResult
    from nfizyo.modules.booking import views as booking_views

    db = app[DB]
    db.execute("UPDATE booking_settings SET intro_de = ? WHERE practice_id = 1", ("Neuer Hinweis der Praxis.",))
    ai = app[AI]
    ai.can_translate = lambda: True

    class Down:                                   # Übersetzer erreichbar, liefert aber nichts Brauchbares
        name = "remote"

        def available(self):
            return True

        def translate(self, text, s, t):
            return TranslationResult(text, "none")

    ai.translator = Down()
    for _ in range(30):
        assert (await client.get("/p/termin/demo?lang=tr")).status == 200
        await app[JOBS].run_all(app)
    assert db.value("SELECT COUNT(*) FROM jobs WHERE kind = 'translate_booking'") == 1
    # nach der Pause (15 Minuten) wieder genau ein Versuch
    tries = app[booking_views.TRANSLATE_TRIES]
    for key, (n, _) in list(tries.items()):
        tries[key] = (n, time.time() - 16 * 60)
    for _ in range(5):
        await client.get("/p/termin/demo?lang=tr")
        await app[JOBS].run_all(app)
    assert db.value("SELECT COUNT(*) FROM jobs WHERE kind = 'translate_booking'") == 2
    # Bremse pro Adresse: andere Sprachen (andere Texte) – höchstens TR_PER_IP neue Aufträge pro Stunde und Adresse
    for lang in ("en", "it", "pt", "es", "sq", "uk"):
        await client.get(f"/p/termin/demo?lang={lang}")
        await app[JOBS].run_all(app)
    assert db.value("SELECT COUNT(*) FROM jobs WHERE kind = 'translate_booking'") <= 2 + booking_views.TR_PER_IP


# --------------------------------------------------------------------------- CODE-08 / F21 / F11 eine Zählregel, Verknüpfung
def test_code08_f21_past_unmarked_session_counts_everywhere(app):
    db = app[DB]
    with practice_scope(1, 1):
        pid = core_patients.create(db, {"first_name": "Imma", "last_name": "Importiert", "birthdate": "1970-01-01", "law": "KVG"})
        rid = core_patients.add_prescription(db, pid, sessions=9, issued_on=today().isoformat(), sessions_before=7)
        past = local_dt(today() - timedelta(days=1), "10:00")
        agenda.create(db, therapist_id=2, start=past, minutes=30, patient_id=pid, prescription_id=rid)   # nicht als erledigt markiert
        future = [agenda.create(db, therapist_id=2, start=local_dt(today() + timedelta(days=d), "10:00"), minutes=30, patient_id=pid,
                                prescription_id=rid) for d in (2, 4)]
        nums = agenda.session_numbers(db, [agenda.get(db, a) for a in future])
        assert [nums[a][0] for a in future] == [9, 10]                     # 7 bisher + vergangener + …
        assert agenda_api.used_sessions(db, rid) == 8
        state = rx.evaluate(db, rx.active(db, patient_id=pid)[0])
        assert state["used"] == 8 and state["left"] == 1                     # früher 7 (vergangener Termin zählte nirgends)
        assert state["first_uncovered"] == (today() + timedelta(days=4))     # = der Termin, den die Agenda «10/9» zeigt
        from nfizyo.modules.agenda import views as agenda_views

        assert agenda_views._remaining(db, core_patients.get(db, pid)) == (0, 9)   # Serienplaner: nichts mehr frei


def test_f11_online_booking_links_active_prescription(app):
    db = app[DB]
    with practice_scope(1, 6):
        hans = pid_of(app, "Hans")
        r = _request(db, _free_day_start(db), first_name="Hans", last_name="Beispiel",
                     birthdate=core_patients.get(db, hans)["birthdate"])
        pid, aid = booking.confirm(db, r, patient_id=hans)
        assert pid == hans and agenda.get(db, aid)["prescription_id"] == core_patients.active_prescription(db, hans)["id"]
        assert agenda_api.session_number(db, agenda.get(db, aid)) is not None


def test_f11_import_links_appointments_to_prescription(app):
    db = app[DB]
    with practice_scope(1, 1):
        pid = core_patients.create(db, {"first_name": "Bernard", "last_name": "Ancien", "birthdate": "1950-05-05"})
        # Reihenfolge laut Anleitung: zuerst Termine (alte und künftige) …
        old = agenda_api.import_appointment(db, patient_id=pid, therapist_id=2, start=local_dt(today() - timedelta(days=10), "09:00"),
                                            minutes=30, status="done")
        new = [agenda_api.import_appointment(db, patient_id=pid, therapist_id=2, start=local_dt(today() + timedelta(days=d), "09:00"),
                                             minutes=30, status="booked") for d in (3, 6, 9)]
        assert all(agenda.get(db, a)["prescription_id"] is None for a in [old, *new])
        # … dann die Verordnung mit «bisher 8»
        rows = engine.check(db, "prescriptions", [["Ancien", "Bernard", "05.05.1950", (today() - timedelta(days=20)).strftime("%d.%m.%Y"),
                                                   "9", "8"]],
                            {"last_name": 0, "first_name": 1, "birthdate": 2, "issued_on": 3, "sessions": 4, "sessions_used": 5}, agenda_api)
        result = engine.apply(db, "prescriptions", rows, agenda_api)
        assert result["created"] == 1 and result["linked"] == 3
        rid = core_patients.active_prescription(db, pid)["id"]
        assert agenda.get(db, old)["prescription_id"] is None                 # schon in «bisher 8» enthalten – nicht doppelt
        nums = agenda.session_numbers(db, [agenda.get(db, a) for a in new])
        assert [nums[a] for a in new] == [(9, 9), (10, 9), (11, 9)]
        assert agenda.beyond_prescription(nums[new[1]])                       # «10/9 ohne Verordnung»
        # umgekehrte Reihenfolge: Termin nach der Verordnung → gleich verknüpft (künftig) bzw. nicht (vergangen, «bisher»)
        later = agenda_api.import_appointment(db, patient_id=pid, therapist_id=2, start=local_dt(today() + timedelta(days=12), "09:00"),
                                              minutes=30, status="booked")
        past = agenda_api.import_appointment(db, patient_id=pid, therapist_id=2, start=local_dt(today() - timedelta(days=3), "09:00"),
                                             minutes=30, status="done")
        assert agenda.get(db, later)["prescription_id"] == rid and agenda.get(db, past)["prescription_id"] is None


# --------------------------------------------------------------------------- CODE-13 absurde Zahlen → Formularfehler, nie 500
async def test_code13_absurd_numbers_never_500(client, app):
    db = app[DB]
    huge = "9" * 5000
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    n = db.value("SELECT COUNT(*) FROM waitlist")
    for tid in ("-1", "999999", huge, "1e9"):
        r = await client.post("/empfang/agenda/warteliste", data={"_csrf": csrf, "name": "Wer", "therapist_id": tid, "location_id": tid,
                                                                  "treatment_type_id": tid}, allow_redirects=False)
        assert r.status == 302, (tid, r.status)
    assert db.value("SELECT COUNT(*) FROM waitlist") == n + 4
    assert db.value("SELECT COUNT(*) FROM waitlist WHERE therapist_id IS NOT NULL AND name = 'Wer'") == 0
    r = await client.post("/empfang/agenda/warteliste", data={"_csrf": csrf, "done": huge}, allow_redirects=False)
    assert r.status in (302, 400)
    for minutes in (huge, "99999999", "0"):
        r = await client.post("/empfang/agenda/neu", data={"_csrf": csrf, "patient": "P00001", "therapist_id": "2", "minutes": minutes,
                                                          "date": today().isoformat(), "time": "21:00"}, allow_redirects=False)
        assert r.status in (200, 302), (minutes, r.status)
    r = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": huge}, allow_redirects=False)
    assert r.status == 404
    r = await client.post("/empfang/patienten/1", data={"_csrf": csrf, "first_name": "Lea", "last_name": "Muster", "insurer_id": "999999",
                                                       "referrer_id": huge, "therapist_id": "-1"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT insurer_id FROM patients WHERE id = 1") is None
    big = "9" * 30                                           # früher: SQLite-Überlauf → 500
    assert (await client.get(f"/p/termin/demo?art={big}&ort={big}")).status == 200
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/einstellungen/online-termine", data={"_csrf": csrf, "enabled": "1", "days_ahead": huge,
                                                                         "lead_hours": huge, "slot_step": huge, "types": huge,
                                                                         "therapists": huge}, allow_redirects=False)
    assert r.status == 302
    with practice_scope(1):
        st = booking.settings(db)
    assert st["days_ahead"] == 21 and st["lead_hours"] == 12 and st["slot_step"] == 15


# --------------------------------------------------------------------------- SEC-04 IPv6 pro /64, Obergrenze pro Praxis
def test_sec04_ip_bucket():
    assert sms.ip_bucket("2001:db8:1234:5678::1") == sms.ip_bucket("2001:db8:1234:5678:ffff::14") == "2001:db8:1234:5678::/64"
    assert sms.ip_bucket("2001:db8:1234:5679::1") != sms.ip_bucket("2001:db8:1234:5678::1")
    assert sms.ip_bucket("203.0.113.5") == "203.0.113.5" and sms.ip_bucket("::ffff:203.0.113.5") == "203.0.113.5"


async def _slot_tokens(client) -> tuple[list[str], str]:
    html = await (await client.get("/p/termin/demo?art=1")).text()
    return list(dict.fromkeys(m.replace("&amp;", "&") for m in re.findall(r"[?&](?:amp;)?s=([^\"&]+)", html))), csrf_of(html)


async def _book(client, token, phone, ip, csrf):
    form = {"_csrf": csrf, "step": "data", "slot": token, "first_name": "Test", "last_name": "Person", "birthdate": "1980-01-01",
            "phone": phone, "consent": "1"}
    return await client.post("/p/termin/demo", data=form, headers={"X-Forwarded-For": ip}, allow_redirects=False)


async def test_sec04_rotating_ipv6_and_practice_cap(client, app, monkeypatch):
    db = app[DB]
    tokens, csrf = await _slot_tokens(client)
    assert len(tokens) > 100
    before = db.value("SELECT COUNT(*) FROM outbox")
    for i in range(12):                       # zwölf Adressen aus EINEM /64, zwölf Nummern
        await _book(client, tokens[i * 3], f"079 000 11 {i:02d}", f"2001:db8:1234:5678::{i + 1:x}", csrf)
    assert db.value("SELECT COUNT(*) FROM outbox") - before == 6            # wie eine Adresse: 6 pro Stunde
    # Obergrenze pro Praxis (alle Adressen zusammen)
    monkeypatch.setattr(sms, "ANON_SMS_PER_HOUR", 8)
    for i in range(10):
        await _book(client, tokens[60 + 3 * i], f"079 000 22 {i:02d}", f"198.51.100.{i + 1}", csrf)
    assert db.value("SELECT COUNT(*) FROM outbox") - before == 8
    r = await _book(client, tokens[100], "079 000 33 00", "198.51.100.200", csrf)
    assert r.status == 429 and "keine weiteren Codes" in await r.text()


# --------------------------------------------------------------------------- SEC-07 Excel-Bombe
def _bomb(mb: int = 300) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Name", "Vorname"])
    buf = io.BytesIO()
    wb.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                head, tail = data.split(b"</sheetData>", 1)
                info = zipfile.ZipInfo(item.filename)
                info.compress_type = zipfile.ZIP_DEFLATED
                with z.open(info, "w", force_zip64=True) as f:
                    f.write(head + b'<row r="2"><c r="A2" t="inlineStr"><is><t>')
                    for _ in range(mb):
                        f.write(b"A" * (1 << 20))
                    f.write(b"</t></is></c></row></sheetData>" + tail)
            else:
                z.writestr(item, data)
    return out.getvalue()


def _xlsx_with_cell(text: str) -> bytes:
    """Excel-Datei mit einer (nicht komprimierbaren) langen Zelle, ohne die Grenze von openpyxl beim Schreiben."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Name", "Vorname"])
    wb.active.append(["PLATZHALTER", "x"])
    buf = io.BytesIO()
    wb.save(buf)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("xl/") and b"PLATZHALTER" in data:     # Zelltext (inline oder sharedStrings)
                data = data.replace(b"PLATZHALTER", text.encode())
            z.writestr(item, data)
    return out.getvalue()


def test_sec07_xlsx_bomb_rejected_before_parsing():
    import random
    import resource
    import string

    data = _bomb(150)
    assert len(data) < 1_000_000
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = time.time()
    try:
        reader.read_xlsx(data)
        raise AssertionError("Bombe wurde gelesen")
    except reader.ReadError as exc:
        assert exc.args[0] == "too_big"
    assert time.time() - t < 5
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss0 < 100 * 1024     # < 100 MB mehr (früher ~860 MB)
    random.seed(7)
    long_cell = "".join(random.choice(string.ascii_letters) for _ in range(reader.MAX_CELL_CHARS + 50))
    try:
        reader.read_xlsx(_xlsx_with_cell(long_cell))
        raise AssertionError("zu lange Zelle gelesen")
    except reader.ReadError as exc:
        assert exc.args[0] == "too_big"
    headers, rows = reader.read_xlsx(_xlsx_with_cell("Muster"))
    assert headers == ["Name", "Vorname"] and rows == [["Muster", "x"]]
    try:
        reader.read_xlsx(b"PK\x03\x04 kaputt")
        raise AssertionError
    except reader.ReadError as exc:
        assert exc.args[0] == "xlsx"


async def test_sec07_upload_bomb_gives_message(client, app):
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    form = aiohttp.FormData()
    form.add_field("_csrf", csrf)
    form.add_field("kind", "patients")
    form.add_field("file", _bomb(120), filename="bombe.xlsx")
    r = await client.post("/leitung/import", data=form)
    assert r.status == 200 and "zu gross oder ungewöhnlich" in await r.text()
    assert app[DB].value("SELECT COUNT(*) FROM import_jobs") == 0


# --------------------------------------------------------------------------- F12 Archivieren mit künftigen Terminen
async def test_f12_archive_shows_future_appointments_and_cancels(client, app):
    db = app[DB]
    with practice_scope(1, 6):
        pid = core_patients.create(db, {"first_name": "Nina", "last_name": "Neumann", "phone": "079 000 00 55", "lang": "fr"})
        start = _free_day_start(db)
        future = [agenda.create(db, therapist_id=2, start=start + timedelta(days=d), minutes=30, patient_id=pid) for d in (0, 1)]
        soon = agenda.create(db, therapist_id=2, start=datetime.now(TZ) + timedelta(hours=3), minutes=30, patient_id=pid)
        assert any(a["id"] == soon for a in agenda.due_reminders(db, 1))
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/patienten/{pid}")).text()
    assert "Noch 3 künftige Termine" in html
    r = await client.post(f"/empfang/patienten/{pid}/archivieren", data={"_csrf": csrf, "archive": "1"}, allow_redirects=False)
    html = await r.text()
    assert r.status == 200 and "data-archive-future" in html and "Diese Person hat noch 3 künftige Termine" in html
    assert db.value("SELECT archived FROM patients WHERE id = ?", (pid,)) == 0           # noch nichts passiert
    out0 = db.value("SELECT COUNT(*) FROM outbox")
    r = await client.post(f"/empfang/patienten/{pid}/archivieren", data={"_csrf": csrf, "archive": "1", "confirm": "1",
                                                                        "cancel_future": "1", "notify": "1"}, allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT archived FROM patients WHERE id = ?", (pid,)) == 1
    assert {db.value("SELECT status FROM appointments WHERE id = ?", (a,)) for a in [*future, soon]} == {"cancelled"}
    sms_row = db.one("SELECT * FROM outbox ORDER BY id DESC LIMIT 1")
    assert db.value("SELECT COUNT(*) FROM outbox") == out0 + 1 and "annulés" in sms_row["body"]


async def test_f12_archive_keep_appointments_but_no_reminders(client, app):
    db = app[DB]
    with practice_scope(1, 6):
        pid = core_patients.create(db, {"first_name": "Olga", "last_name": "Bleibt", "phone": "079 000 00 56"})
        soon = agenda.create(db, therapist_id=2, start=datetime.now(TZ) + timedelta(hours=3), minutes=30, patient_id=pid)
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post(f"/empfang/patienten/{pid}/archivieren", data={"_csrf": csrf, "archive": "1", "confirm": "1"},
                          allow_redirects=False)
    assert r.status == 302 and db.value("SELECT status FROM appointments WHERE id = ?", (soon,)) == "booked"
    with practice_scope(1):
        assert all(a["id"] != soon for a in agenda.due_reminders(db, 1))           # archiviert: keine Erinnerung


# --------------------------------------------------------------------------- F13 IDs und Nummern nie wiederverwenden
async def test_f13_ids_and_numbers_never_reused(app):
    import sqlite3

    db = app[DB]
    with practice_scope(1, 1):
        keep = core_patients.create(db, {"first_name": "Behalten", "last_name": "Eintrag"})
        drop = core_patients.create(db, {"first_name": "Doppelt", "last_name": "Eintrag"})
        drop_number = core_patients.get(db, drop)["number"]
        await core_patients.merge(app, core_patients.get(db, drop), core_patients.get(db, keep))
        new = core_patients.create(db, {"first_name": "Neu", "last_name": "Person"})
        assert new > drop and core_patients.get(db, new)["number"] != drop_number
        empty = core_patients.create(db, {"first_name": "Leer", "last_name": "Eintrag"})
        empty_number = core_patients.get(db, empty)["number"]
        assert await core_patients.delete_empty(app, core_patients.get(db, empty))
        again = core_patients.create(db, {"first_name": "Noch", "last_name": "Eine"})
        assert again > empty and core_patients.get(db, again)["number"] not in (empty_number, drop_number)
        try:
            db.execute("INSERT INTO patients (id, practice_id, number, first_name, last_name, created_at, updated_at)"
                       " VALUES (?,1,'PX','x','y',?,?)", (drop, utcnow(), utcnow()))
            raise AssertionError("alte ID wieder vergeben")
        except sqlite3.IntegrityError:
            pass
    assert db.value("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'patients_id_never_reused'")


async def test_f13_numbers_per_practice_and_isolated(app, practice_b):
    db = app[DB]
    with practice_scope(practice_b["id"]):
        assert core_patients.next_number(db) == "P00002"        # Praxis B: eigene Nummern (eine Person angelegt)
    with practice_scope(1):
        assert core_patients.next_number(db) == "P00010"        # Demo: P00001–P00009
    # IDs sind über alle Praxen eindeutig: die nächste ID liegt über allen bisherigen
    assert core_patients.next_id(db) > db.value("SELECT MAX(id) FROM patients")


# --------------------------------------------------------------------------- F18 gleicher Tag
async def test_f18_same_day_hint_on_request(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    with practice_scope(1, 6):
        start = _free_day_start(db)
        agenda.create(db, therapist_id=3, start=start.replace(hour=8, minute=15), minutes=30, patient_id=lea)
        lea_row = core_patients.get(db, lea)
        r = _request(db, start, first_name="Lea", last_name="Muster", birthdate=lea_row["birthdate"], phone="+41790000001")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get("/empfang/online-anfragen")).text()
    assert "am selben Tag schon ein Termin: 08:15" in html
    resp = await client.post("/empfang/online-anfragen", data={"_csrf": csrf, "do": "confirm", "request_id": str(r["id"]),
                                                               "patient_choice": str(lea)})
    assert "am selben Tag schon einen anderen Termin" in await resp.text()


# --------------------------------------------------------------------------- F25 Schweizer Nummern ohne 0
def test_f25_normalize_phone():
    assert sms.normalize_phone("791234567") == "+41791234567"
    assert sms.normalize_phone("79 555 66 77") == "+41795556677"
    assert sms.normalize_phone("41791234567") == "+41791234567"
    assert sms.normalize_phone("+41 (0)79 123 45 67") == "+41791234567"
    assert sms.normalize_phone("079 123 45 67") == "+41791234567" and sms.normalize_phone("0041 79 123 45 67") == "+41791234567"
    assert sms.normalize_phone("+33 6 12 34 56 78") == "+33612345678"            # Ausland nur ausdrücklich
    for bad in ("7912345678", "12345", "079 123 45", "+41 79 123 45 678", "abc"):
        assert sms.normalize_phone(bad) is None, bad
    assert sms.classify_phone("791234567")[1] == "guessed" and sms.classify_phone("+49 151 2345 6789")[1] == "foreign"


async def test_f25_patient_form_and_invitation_stay_in_switzerland(client, app):
    db = app[DB]
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post("/empfang/patienten/neu", data={"_csrf": csrf, "first_name": "Erika", "last_name": "Tippt", "phone": "79 555 66 77"},
                          allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT phone FROM patients WHERE last_name = 'Tippt'") == "079 555 66 77"
    r = await client.post("/empfang/patienten/neu", data={"_csrf": csrf, "first_name": "Falsch", "last_name": "Nummer", "phone": "7955566"})
    assert r.status == 200 and "Telefonnummer ist ungültig" in await r.text()
    assert db.value("SELECT COUNT(*) FROM patients WHERE last_name = 'Nummer'") == 0


async def test_f25_import_warns_about_guessed_and_impossible_numbers(client, app):
    db = app[DB]
    rows = [["Excelmann", "Erika", "01.01.1970", "791234567"], ["Ausland", "Anna", "02.02.1970", "+33 6 12 34 56 78"],
            ["Kaputt", "Karl", "03.03.1970", "12345"], ["Normal", "Nina", "04.04.1970", "079 123 45 68"]]
    m = {"last_name": 0, "first_name": 1, "birthdate": 2, "phone": 3}
    with practice_scope(1, 1):
        checked = engine.check(db, "patients", rows, m)
        assert [r.notes for r in checked] == [["phone_guessed"], ["phone_foreign"], ["bad_phone"], []]
        engine.apply(db, "patients", checked)
    assert db.value("SELECT phone FROM patients WHERE last_name = 'Excelmann'") == "079 123 45 67"
    karl = db.one("SELECT phone, admin_note FROM patients WHERE last_name = 'Kaputt'")
    assert karl["phone"] is None and "12345" in karl["admin_note"]
    # App-Einladung an die importierte Person geht an +41…, nie an +79…
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    erika = db.value("SELECT id FROM patients WHERE last_name = 'Excelmann'")
    await client.post(f"/empfang/patienten/{erika}/app-einladung", data={"_csrf": csrf})
    rows = db.query("SELECT recipient FROM outbox WHERE recipient LIKE '%791234567'")
    assert rows and all(r["recipient"] == "+41791234567" for r in rows)
