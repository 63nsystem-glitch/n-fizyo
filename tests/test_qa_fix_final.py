"""QA-Fixes 0.9.0 (Abschluss): Zürcher Datum überall (CODE-11/SEC-10), riesige Zahlen in Adressen (CODE-13)."""
from __future__ import annotations

import re
from pathlib import Path

from conftest import login

ROOT = Path(__file__).resolve().parent.parent / "nfizyo"


def test_code11_no_server_local_date_anywhere():
    """date.today() wäre das Datum des Servers (UTC), nicht das Zürcher Datum – nach 23 Uhr falsch."""
    hits = [f"{p.relative_to(ROOT)}:{i}" for p in ROOT.rglob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"\bdate\.today\b|datetime\.now\(\)|datetime\.today\b", line)]
    assert hits == []


async def test_code13_huge_numbers_in_urls_are_not_found(client):
    await login(client, "empfang@demo.n-fizyo.ch")
    huge = "9" * 30
    for url in (f"/empfang/patienten/{huge}", f"/therapie/diktat?patient={huge}"):
        r = await client.get(url, allow_redirects=False)
        assert r.status in (302, 303, 403, 404), (url, r.status)
    await login(client, "therapie@demo.n-fizyo.ch")
    for url in (f"/therapie/patienten/{huge}", f"/therapie/notiz?termin={huge}", f"/therapie/diktat?patient={huge}"):
        r = await client.get(url, allow_redirects=False)
        assert r.status in (200, 302, 303, 403, 404), (url, r.status)


# --------------------------------------------------------------------------- Runde 2: Sprache der Dokumente, Gesetzesnamen
async def test_invoice_lines_follow_document_language_not_screen_language(client, app):
    from nfizyo.core.keys import DB
    from conftest import csrf_of, pid_of

    db = app[DB]
    db.execute("UPDATE users SET lang = 'fr' WHERE email = 'empfang@demo.n-fizyo.ch'")
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    peter = pid_of(app, "Peter")                      # spricht Deutsch → Rechnung deutsch
    r = await client.post("/empfang/abrechnung", data={"_csrf": csrf, "do": "create", "patient_id": str(peter)}, allow_redirects=False)
    assert r.status == 302
    iid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    labels = [x["label"] for x in db.query("SELECT label FROM invoice_lines WHERE invoice_id = ?", (iid,))]
    assert labels and not any("Physiothérapie" in x for x in labels), labels
    # Zeile von Hand ergänzen: ebenfalls deutsch
    page = await (await client.get(f"/empfang/rechnungen/{iid}")).text()
    await client.post(f"/empfang/rechnungen/{iid}", data={"_csrf": csrf_of(page), "do": "add", "code": "7301", "quantity": "1"})
    labels = [x["label"] for x in db.query("SELECT label FROM invoice_lines WHERE invoice_id = ?", (iid,))]
    assert not any("Physiothérapie" in x for x in labels), labels


async def test_law_names_in_french_screens(client, app):
    from nfizyo.core.keys import DB

    app[DB].execute("UPDATE users SET lang = 'fr' WHERE email = 'empfang@demo.n-fizyo.ch'")
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get("/empfang/patienten")).text()
    assert "LAMal" in page and " KVG " not in page
    page = await (await client.get("/empfang/patienten/neu")).text()
    assert '<option value="privat"' in page and ">Autopayeur<" in page


def test_payant_patient_copy_has_no_due_date():
    from nfizyo.modules.billing import pdf
    src = Path(pdf.__file__).read_text(encoding="utf-8")
    assert '"–" if (not garant and not priv and not reminder)' in src


def test_ukrainian_nav_label_fits():
    import json
    uk = json.loads((ROOT / "modules/patientapp/locales/uk.json").read_text(encoding="utf-8"))
    assert all(len(v) <= 10 for v in uk["p"]["nav"].values() if v != uk["p"]["nav"]["main"])


def test_processed_scans_are_removed_after_14_days(tmp_path):
    import os
    import time
    from nfizyo.modules.inbox.views import PROCESSED_KEEP_DAYS, _prune_processed

    done = tmp_path / "verarbeitet"
    done.mkdir()
    old, new = done / "alt.pdf", done / "neu.pdf"
    old.write_bytes(b"%PDF-1.4 alt")
    new.write_bytes(b"%PDF-1.4 neu")
    past = time.time() - (PROCESSED_KEEP_DAYS + 1) * 86400
    os.utime(old, (past, past))
    assert _prune_processed(done) == 1
    assert not old.exists() and new.exists()
    assert _prune_processed(tmp_path / "fehlt") == 0


async def test_practice_can_log_out_patient_app_everywhere(client, app):
    """Telefon verloren: Empfang/Therapie beenden alle App-Anmeldungen; die Einwilligung bleibt."""
    from conftest import csrf_of
    from nfizyo.core.auth import create_session
    from nfizyo.core.keys import DB

    db = app[DB]
    pid = db.value("SELECT patient_id FROM patient_consents WHERE kind = 'app' AND revoked_at IS NULL ORDER BY patient_id")
    create_session(db, "patient", patient_id=pid, practice_id=1)
    create_session(db, "patient", patient_id=pid, practice_id=1)
    assert db.value("SELECT COUNT(*) FROM sessions WHERE kind = 'patient' AND patient_id = ?", (pid,)) >= 2
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get(f"/empfang/patienten/{pid}")).text()
    assert f"/empfang/patienten/{pid}/app-abmelden" in page and "App abmelden" in page
    r = await client.post(f"/empfang/patienten/{pid}/app-abmelden", data={"_csrf": csrf_of(page)}, allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == f"/empfang/patienten/{pid}"
    assert db.value("SELECT COUNT(*) FROM sessions WHERE kind = 'patient' AND patient_id = ?", (pid,)) == 0
    assert db.value("SELECT COUNT(*) FROM patient_consents WHERE kind = 'app' AND patient_id = ? AND revoked_at IS NULL", (pid,)) == 1
    # ohne App keine Schaltfläche
    other = db.value("SELECT id FROM patients WHERE id NOT IN (SELECT patient_id FROM patient_consents WHERE kind = 'app'"
                     " AND revoked_at IS NULL) ORDER BY id")
    page = await (await client.get(f"/empfang/patienten/{other}")).text()
    assert "/app-abmelden" not in page


def test_nav_rail_fits_short_laptop_screens():
    """1280×720 / 1366×768: alle Menüpunkte sichtbar (Empfang hat 11) – Regel am Ende der CSS, damit sie gewinnt."""
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    block = css[css.rindex("@media (max-height: 860px)"):]
    assert ".rail a.nav .pill { height: 28px; }" in block and ".rail-mark" in block
    assert css.rstrip().endswith("}")


def test_one_minimum_password_length_everywhere():
    from nfizyo.core import auth, views
    assert auth.MIN_PASSWORD == views.MIN_PASSWORD == 10


async def test_agenda_without_locations_shows_no_empty_selector(aiohttp_client, tmp_path):
    from conftest import login
    from nfizyo.app import create_app
    from nfizyo.core.auth import create_user
    from nfizyo.core.config import load_settings
    from nfizyo.core.db import utcnow
    from nfizyo.core.keys import DB

    app = create_app(load_settings(None, data_dir=tmp_path, secret_key=b"e" * 32, demo=False, require_2fa=False), start_workers=False)
    db = app[DB]
    pid = db.execute("INSERT INTO practices (name, slug, default_lang, created_at) VALUES (?,?,?,?)", ("Leer", "leer", "de", utcnow()))
    create_user(db, pid, "chef@leer.test", "Chefin", ("owner", "therapist", "reception"), "leer-passwort-123", "de")
    client = await aiohttp_client(app)
    await login(client, "chef@leer.test", "leer-passwort-123")
    page = await (await client.get("/empfang/agenda")).text()
    assert 'id="loc"' not in page


def test_open_items_include_old_invoices_beyond_2000(app):
    """Grosse Praxis nach einem Jahr: eine alte offene Rechnung darf nicht aus den Offenen Posten fallen."""
    from nfizyo.core.db import utcnow
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.billing import repo

    db = app[DB]
    pid = db.value("SELECT id FROM patients ORDER BY id")
    uid = db.value("SELECT id FROM users ORDER BY id")
    sql = ("INSERT INTO invoices (practice_id, patient_id, law, tariff, tiers, number, status, total_chf, due_on, created_by, created_at)"
           " VALUES (1, ?, 'KVG', 'kvg312', 'garant', ?, 'final', ?, '2025-01-31', ?, ?)")
    old = db.execute(sql, (pid, "2025-0001", 120.0, uid, utcnow()))
    db.executemany(sql, [(pid, f"2025-{i:05d}", 0.0, uid, utcnow()) for i in range(2, 2203)])
    with practice_scope(1, uid):
        items = repo.open_items(db)
    assert old in [x["inv"]["id"] for x in items]


async def test_imported_past_sessions_are_not_billed_twice(client, app):
    """Datenübernahme: vergangene erledigte Termine sind im alten Programm schon verrechnet → nicht unter «Bereit»
    (Standard). Ohne Häkchen erscheinen sie zum Abrechnen. Als Sitzung zählen sie in beiden Fällen."""
    import aiohttp
    from datetime import timedelta
    from conftest import csrf_of
    from nfizyo.core.keys import DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.core.timeutil import today
    from nfizyo.modules.billing import repo as billing

    db = app[DB]
    await login(client, "leitung@demo.n-fizyo.ch")
    lea = db.value("SELECT id FROM patients WHERE first_name = 'Lea'")
    results = {}
    for flag, days in (("1", 400), ("", 401)):
        d = (today() - timedelta(days=days)).strftime("%d.%m.%Y")
        html = await (await client.get("/leitung/import")).text()
        form = aiohttp.FormData()
        form.add_field("_csrf", csrf_of(html)); form.add_field("kind", "appointments")
        form.add_field("file", f"Name;Vorname;Geb.;Datum;Zeit;Dauer;Therapeut;Status\nMuster;Lea;05.03.1992;{d};09:30;30;TD;erledigt\n".encode(),
                       filename="t.csv")
        url = (await client.post("/leitung/import", data=form, allow_redirects=False)).headers["Location"]
        html = await (await client.get(url)).text()
        await client.post(url, data={"_csrf": csrf_of(html), "do": "map", "f_last_name": "0", "f_first_name": "1", "f_birthdate": "2",
                                     "f_date": "3", "f_start": "4", "f_minutes": "5", "f_therapist": "6", "f_status": "7"})
        html = await (await client.get(url)).text()
        assert 'name="billed_externally"' in html
        data = {"_csrf": csrf_of(html), "do": "run"}
        if flag:
            data["billed_externally"] = flag
        await client.post(url, data=data)
        aid = db.value("SELECT id FROM appointments WHERE patient_id = ? AND status = 'done' ORDER BY id DESC", (lea,))
        results[flag] = aid
    with practice_scope(1, 1):
        unbilled = {a["id"] for a in billing.unbilled_for(db, lea)}
    assert results["1"] not in unbilled and results[""] in unbilled
    assert db.value("SELECT billed_externally FROM appointments WHERE id = ?", (results["1"],)) == 1


async def test_accessibility_regressions(client, app):
    """axe-core (WCAG 2.1 AA): Körperkarte zum Antippen ist eine Gruppe (nicht «Bild» mit Knöpfen darin); Kontrast der
    Schmerz-Skala; keine einzelnen Dunkel-Regeln auf der hellen Oberfläche; App abmelden nur am Empfang."""
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme" not in css and ".faces .f4 { color: #7A6000; }" in css
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get("/therapie/patienten/1")).text()
    assert 'class="bm-svg" role="group"' in page and "/app-abmelden" not in page


async def test_patient_logout_clears_offline_copies(client, app):
    """Abmelden / Zugang beenden löscht die Offline-Kopien (Startseite, Übungen) auf dem Telefon; der Service Worker
    vergisst sie auch, sobald der Server auf die Anmeldung umleitet."""
    from nfizyo.core.auth import create_session
    from nfizyo.core.keys import DB
    from nfizyo.core.web import PATIENT_COOKIE

    db = app[DB]
    token, _ = create_session(db, "patient", patient_id=1, practice_id=1)
    client.session.cookie_jar.update_cookies({PATIENT_COOKIE: token})
    r = await client.get("/p/abmelden", allow_redirects=False)
    assert r.status == 302 and r.headers.get("Clear-Site-Data") == '"cache", "storage"'
    sw = await (await client.get("/p/sw.js")).text()
    assert "nfizyo-p-v2" in sw and "forgetPages" in sw and "res.redirected" in sw


async def test_dictation_errors_end_visibly_not_in_endless_spinner(client, app, tmp_path):
    """Pilot mit KI-Server: ein unerwarteter Fehler beim Erkennen oder beim Entwurf lässt das Diktat nicht ewig «in Arbeit».
    Es wird «fehlgeschlagen», der erkannte Text bleibt und steht im Formular zum Bearbeiten."""
    from nfizyo.core.keys import AI, DB, JOBS
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.dictation import repo

    db, ai = app[DB], app[AI]
    uid = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"\x1aE\xdf\xa3kaputt")
    with practice_scope(1, uid):
        rid = repo.create(db, client_uuid="u1", patient_id=1, appointment_id=None, language="de", recorded_at=None, path=str(audio))

    def broken_stt(*a, **k):
        raise RuntimeError("Datei beschädigt")
    ai.can_transcribe = lambda: True
    ai.transcribe = broken_stt
    app[JOBS].enqueue(1, "transcribe", rid)
    await app[JOBS].run_all(app)
    rec = db.one("SELECT status, error FROM recordings WHERE id = ?", (rid,))
    assert (rec["status"], rec["error"]) == ("failed", "unknown")

    with practice_scope(1, uid):
        rid2 = repo.create(db, client_uuid="u2", patient_id=1, appointment_id=None, language="de", recorded_at=None,
                           transcript="Flexion Knie rechts 100 Grad")

    def broken_draft(*a, **k):
        raise RuntimeError("Entwurf kaputt")
    ai.draft_note = broken_draft
    app[JOBS].enqueue(1, "draft_note", rid2)
    await app[JOBS].run_all(app)
    rec = db.one("SELECT status, error, transcript FROM recordings WHERE id = ?", (rid2,))
    assert (rec["status"], rec["error"]) == ("failed", "draft") and rec["transcript"]
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/diktat/{rid2}")).text()
    assert "Flexion Knie rechts 100 Grad</textarea>" in page and "Entwurf konnte nicht erstellt werden" in page
