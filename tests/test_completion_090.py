"""0.9.0 – Vervollständigung: Notizvorlagen, Antwortzeit in der App, Nachrichtenliste, Datenübernahme von Verordnungen und
offenen Posten, Einrichtungs-Checkliste, Links der offenen Arbeit, Ziel im Kurzüberblick, Posteingang (Original, Titel, Scan)."""
from __future__ import annotations

import json
import re

import aiohttp
from conftest import csrf_of, login, pid_of

from nfizyo.core.auth import create_session
from nfizyo.core.db import utcnow
from nfizyo.core.keys import AI, DB, JOBS
from nfizyo.core.tenant import practice_scope


# --------------------------------------------------------------------------- 2. Notizvorlagen
async def test_note_templates_own_and_practice(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    r = await client.post("/leitung/einstellungen/notizvorlagen", data={"_csrf": csrf, "name": "Knie-TEP Verlauf", "lang": "de",
                                                                        "befund": "ROM Flexion/Extension:", "plan": "Heimprogramm"},
                          allow_redirects=False)
    assert r.status == 302
    shared = db.one("SELECT * FROM note_templates WHERE name = 'Knie-TEP Verlauf'")
    assert shared["owner_id"] is None and shared["practice_id"] == 1
    page = await (await client.get("/leitung/einstellungen/notizvorlagen")).text()
    assert "Knie-TEP Verlauf" in page and "Notizvorlagen" in page
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/patienten/{lea}/notizen/neu")).text()
    assert 'id="tpl"' in page and "Knie-TEP Verlauf" in page and "/therapie/notizen/vorlagen" in page
    r = await client.post("/therapie/notizen/vorlagen", data={"_csrf": csrf, "name": "Meine Schulter", "lang": "de",
                                                             "behandlung": "Mobilisation GH-Gelenk"}, allow_redirects=False)
    assert r.status == 302
    own = db.one("SELECT * FROM note_templates WHERE name = 'Meine Schulter'")
    td = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    assert own["owner_id"] == td
    page = await (await client.get(f"/therapie/patienten/{lea}/notizen/neu?vorlage={own['id']}")).text()
    assert "Mobilisation GH-Gelenk</textarea>" in page
    page = await (await client.get(f"/therapie/patienten/{lea}/notizen/neu?vorlage={shared['id']}")).text()
    assert "ROM Flexion/Extension:</textarea>" in page
    # Praxis-Vorlage kann die Therapie nicht ändern oder löschen
    r = await client.post("/therapie/notizen/vorlagen", data={"_csrf": csrf, "do": "delete", "id": str(shared["id"])}, allow_redirects=False)
    assert r.status == 404
    r = await client.post("/therapie/notizen/vorlagen", data={"_csrf": csrf, "id": str(shared["id"]), "name": "x"}, allow_redirects=False)
    assert r.status == 404 and db.value("SELECT name FROM note_templates WHERE id = ?", (shared["id"],)) == "Knie-TEP Verlauf"
    # eigene ändern und löschen
    r = await client.post("/therapie/notizen/vorlagen", data={"_csrf": csrf, "id": str(own["id"]), "name": "Schulter neu", "lang": "fr"},
                          allow_redirects=False)
    assert db.one("SELECT name, lang FROM note_templates WHERE id = ?", (own["id"],))["name"] == "Schulter neu"
    # eine andere Therapeutin sieht die persönliche Vorlage nicht
    await login(client, "a.beispiel@demo.n-fizyo.ch")
    page = await (await client.get("/therapie/notizen/vorlagen")).text()
    assert "Schulter neu" not in page and "Knie-TEP Verlauf" in page
    assert (await client.get(f"/therapie/notizen/vorlagen?bearbeiten={own['id']}")).status == 404
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post("/therapie/notizen/vorlagen", data={"_csrf": csrf, "do": "delete", "id": str(own["id"])}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM note_templates WHERE id = ?", (own["id"],)) == 0


# --------------------------------------------------------------------------- 3. Antwortzeit (App)
async def test_reply_time_from_practice_in_patient_language(client, app):
    db = app[DB]
    ayse = pid_of(app, "Ayşe")
    token, _ = create_session(db, "patient", patient_id=ayse, practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    page = await (await client.get("/p/nachrichten")).text()
    assert "bir iş günü içinde" in page                                  # Standardtext (noch keine Wahl)
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/praxis")).text()
    assert 'name="reply_time"' in page
    pr = db.one("SELECT * FROM practices WHERE id = 1")
    r = await client.post("/leitung/einstellungen/praxis", data={"_csrf": csrf, "name": pr["name"], "canton": pr["canton"],
                                                                "default_lang": "de", "reply_time": "same_day"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT reply_time FROM practices WHERE id = 1") == "same_day"
    page = await (await client.get("/leitung/einstellungen/praxis")).text()
    assert "am selben Arbeitstag" in page
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    for url in ("/p/nachrichten", "/p/melden"):
        page = await (await client.get(url)).text()
        assert "aynı iş günü içinde" in page and "bir iş günü içinde" not in page, url


# --------------------------------------------------------------------------- 7. Nachrichten (Therapie)
async def test_message_list_preview_and_plain_translation_wording(client, app):
    maria = pid_of(app, "Maria")
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get("/therapie/nachrichten")).text()
    previews = re.findall(r'msg-preview[^>]*>(.*?)</span></span>', page)
    assert previews and any(p.startswith("Praxis: ") for p in previews) and "Treppensteigen" in page
    assert len(re.findall(r'class="small muted" style="white-space:nowrap">[^<]+<', page)) == len(previews)
    page = await (await client.get(f"/therapie/nachrichten/{maria}")).text()
    assert "kein Übersetzungsmodell" not in page and "maschinell" not in page
    assert "automatisch übersetzt aus: Portugiesisch" in page and "nicht automatisch übersetzt" in page


# --------------------------------------------------------------------------- 4. Datenübernahme: Verordnungen und offene Posten
async def _upload_and_run(client, app, kind: str, data: bytes) -> dict:
    db = app[DB]
    html = await (await client.get("/leitung/import")).text()
    form = aiohttp.FormData()
    form.add_field("_csrf", csrf_of(html))
    form.add_field("kind", kind)
    form.add_field("file", data, filename="alt.csv")
    r = await client.post("/leitung/import", data=form, allow_redirects=False)
    assert r.status == 302, await r.text()
    url = r.headers["Location"]
    job = db.one("SELECT * FROM import_jobs ORDER BY id DESC LIMIT 1")
    html = await (await client.get(url)).text()
    mapping = {f"f_{k}": str(v) for k, v in json.loads(job["mapping"]).items()}
    await client.post(url, data={"_csrf": csrf_of(html), "do": "map", **mapping}, allow_redirects=False)
    preview = json.loads(db.value("SELECT preview FROM import_jobs WHERE id = ?", (job["id"],)))
    html = await (await client.get(url)).text()
    await client.post(url, data={"_csrf": csrf_of(html), "do": "run"})
    return {"preview": preview, "result": json.loads(db.value("SELECT result FROM import_jobs WHERE id = ?", (job["id"],)))}


async def test_import_prescriptions_with_sessions_already_done(client, app):
    db = app[DB]
    db.execute("UPDATE patients SET number = 'P-7' WHERE first_name = 'Peter' AND practice_id = 1")
    peter = pid_of(app, "Peter")
    csv = ("Patientennr;Name;Vorname;Geburtsdatum;Ausgestellt;Arzt;Sitzungen;Durchgeführt;Diagnose\n"
           "P-7;;;;12.09.2026;Dr. med. C. Muster;9;6;Gonarthrose links\n"
           ";Muster;Peter;02.11.1948;01.06.2026;;9;9;\n"                     # ältere, aufgebraucht
           ";Niemand;Nobody;01.01.1990;01.10.2026;;9;0;\n"                   # unbekannt
           "P-7;;;;12.09.2026;Dr. med. C. Muster;9;6;Gonarthrose links\n"   # zweimal in der Datei
           "P-7;;;;99.99.2026;;9;0;\n").encode("cp1252")                      # Datum unklar
    await login(client, "leitung@demo.n-fizyo.ch")
    out = await _upload_and_run(client, app, "prescriptions", csv)
    p = out["preview"]
    assert (p["new"], p["duplicate"], p["error"]) == (2, 1, 2), p
    assert out["result"]["created"] == 2
    rows = db.query("SELECT * FROM prescriptions WHERE patient_id = ? ORDER BY id DESC LIMIT 2", (peter,))
    newest, older = rows
    assert newest["issued_on"] == "2026-09-12" and newest["sessions"] == 9 and newest["sessions_before"] == 6
    assert newest["status"] == "active" and newest["diagnosis"] == "Gonarthrose links"
    assert newest["referrer_id"] == db.value("SELECT id FROM referrers WHERE name = 'Dr. med. C. Muster' AND practice_id = 1")
    assert older["status"] == "used" and older["sessions_before"] == 9
    # zweimal übernehmen: alles doppelt
    out = await _upload_and_run(client, app, "prescriptions", csv)
    assert out["result"]["created"] == 0 and out["preview"]["duplicate"] >= 2


async def test_import_open_items_keep_reminders_and_stay_out_of_accounting(client, app):
    from datetime import date

    from nfizyo.modules.billing import api as billing_api
    from nfizyo.modules.billing import repo as billing_repo

    db = app[DB]
    db.execute("UPDATE patients SET number = 'H-2' WHERE first_name = 'Hans' AND practice_id = 1")
    csv = ("Patientennr;Rechnungsnr;Rechnungsdatum;Betrag;Offen;Fällig;Mahnstufe\n"
           "H-2;4711;15.08.2026;1'290.00;290.50;14.09.2026;2\n"
           "H-2;4712;20.09.2026;120.00;0.00;;\n"                             # nichts offen → Fehler
           "H-2;4711;15.08.2026;1'290.00;290.50;;\n").encode("utf-8")       # zweimal
    await login(client, "leitung@demo.n-fizyo.ch")
    out = await _upload_and_run(client, app, "balances", csv)
    assert (out["preview"]["new"], out["preview"]["duplicate"], out["preview"]["error"]) == (1, 1, 1)
    inv = db.one("SELECT * FROM invoices WHERE imported_from = '4711'")
    assert inv["number"] == "V-4711" and inv["status"] == "final" and inv["total_chf"] == 290.5 and inv["due_on"] == "2026-09-14"
    assert db.value("SELECT COUNT(*) FROM invoice_reminders WHERE invoice_id = ?", (inv["id"],)) == 2
    with practice_scope(1, 1):
        full = billing_repo.get(db, inv["id"])
        assert billing_repo.balance(db, full)["open"] == 290.5
        assert billing_repo.next_reminder(db, full) in (None, 3)      # Stufe 3 folgt nach Ablauf der Frist
        assert any(x["number"] == "V-4711" for x in billing_api.open_items(db))
        today = date.today().isoformat()
        assert all(x["number"] != "V-4711" for x in billing_api.accounting_invoices(db, "2000-01-01T00:00:00+00:00", today + "T23:59:59+00:00"))
    page = await (await client.get("/leitung/abrechnung?ansicht=offen")).text()
    assert "V-4711" in page
    out = await _upload_and_run(client, app, "balances", csv)
    assert out["result"]["created"] == 0 and db.value("SELECT COUNT(*) FROM invoices WHERE imported_from = '4711'") == 1


def test_import_parsers():
    from nfizyo.modules.dataimport import engine

    assert engine.parse_amount("1'290.50") == 1290.5 and engine.parse_amount("CHF 120.–") == 120.0
    assert engine.parse_amount("1290,5") == 1290.5 and engine.parse_amount("abc") is None
    m = engine.guess_mapping("prescriptions", ["Patientennr", "Datum", "Arzt", "Anzahl Sitzungen", "Durchgeführt", "Diagnose"])
    assert m["issued_on"] == 1 and m["prescriber"] == 2 and m["sessions"] == 3 and m["sessions_used"] == 4 and m["diagnosis"] == 5
    m = engine.guess_mapping("balances", ["N° patient", "N° facture", "Date facture", "Solde", "Échéance", "Rappel"])
    assert m["invoice_number"] == 1 and m["invoice_date"] == 2 and m["amount_open"] == 3 and m["due_on"] == 4 and m["reminder_level"] == 5


# --------------------------------------------------------------------------- 5. Einrichtungs-Checkliste
async def test_setup_checklist_for_a_fresh_practice(client, app, practice_b, monkeypatch):
    from nfizyo.modules.settings import setup_check

    db = app[DB]
    csrf = await login(client, practice_b["email"], practice_b["password"])
    page = await (await client.get("/leitung/uebersicht")).text()
    assert "Mise en place du cabinet" in page and "/leitung/einrichtung" in page
    for href in ("/leitung/einstellungen/standorte", "/leitung/einstellungen/behandlungen", "/leitung/einstellungen/zeiten",
                 "/leitung/einstellungen/versicherer", "/leitung/einstellungen/zahlung", "/leitung/einstellungen/tarife",
                 "/leitung/einstellungen/praxis", "/leitung/einstellungen/sms", "/leitung/team"):
        assert f'href="{href}"' in page, href
    assert re.search(r'class="tile" href="/leitung/einrichtung"', page)
    # Jederzeit ausblendbar – auch mit offenen Punkten (QA UPG-03); «wieder zeigen» holt Karte und Kachel zurück
    r = await client.post("/leitung/einrichtung", data={"_csrf": csrf, "do": "dismiss"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM practice_setup WHERE practice_id = ?", (practice_b["id"],)) == 1
    page = await (await client.get("/leitung/uebersicht")).text()
    assert not re.search(r'class="tile" href="/leitung/einrichtung"', page) and "Mise en place du cabinet" not in page
    r = await client.post("/leitung/einrichtung", data={"_csrf": csrf, "do": "show"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM practice_setup") == 0
    # Demo-Praxis: Standorte, Behandlungsarten, Arbeitszeiten und Versicherer sind erledigt
    await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einrichtung")).text()
    for label in ("Standorte und Räume", "Behandlungsarten", "Arbeitszeiten", "Versicherer"):
        block = page.split(label, 1)[1][:400]
        assert "erledigt" in block, label
    # alles erledigt → ausblenden
    real = setup_check.items
    monkeypatch.setattr(setup_check, "items", lambda request: [dict(it, done=True) for it in real(request)])
    csrf = csrf_of(page)
    r = await client.post("/leitung/einrichtung", data={"_csrf": csrf, "do": "dismiss"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT practice_id FROM practice_setup WHERE dismissed_at IS NOT NULL") == 1
    page = await (await client.get("/leitung/uebersicht")).text()
    assert "Einrichtung der Praxis" not in page
    assert db.value("SELECT COUNT(*) FROM practice_setup WHERE practice_id = ?", (practice_b["id"],)) == 0


# --------------------------------------------------------------------------- 6. Offene Arbeit: jede Zeile ist ein Link
async def test_every_open_work_row_links_to_a_list_without_medical_content(client, app):
    db = app[DB]
    await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/uebersicht")).text()
    work = page.split("Offene Arbeit", 1)[1].split("</section>", 1)[0]
    assert 'class="list-item"' in work and '<div class="list-item"' not in work
    hrefs = re.findall(r'<a class="list-item" href="([^"]+)"', work)
    assert len(hrefs) >= 9
    for href in ("/leitung/offene-notizen?art=entwuerfe", "/leitung/offene-notizen?art=ohne-notiz", "/leitung/offene-berichte",
                 "/leitung/offene-berichtsentwuerfe"):
        assert href in hrefs, href
        r = await client.get(href)
        assert r.status == 200, href
        text = await r.text()
        for secret in ("Lasègue", "Operationsbericht", "Belastungsaufbau", "Kreuzband"):
            assert secret not in text, (href, secret)
    note = db.one("SELECT n.id, p.last_name FROM notes n JOIN patients p ON p.id = n.patient_id WHERE n.status = 'draft' LIMIT 1")
    if note:
        assert note["last_name"] in await (await client.get("/leitung/offene-notizen?art=entwuerfe")).text()
    for email in ("therapie@demo.n-fizyo.ch", "empfang@demo.n-fizyo.ch"):
        await login(client, email)
        assert (await client.get("/leitung/offene-notizen", allow_redirects=False)).status in (302, 403, 404)


# --------------------------------------------------------------------------- 8. Ziel im Kurzüberblick
async def test_goal_is_shown_in_therapist_language_with_original(client, app, monkeypatch):
    db = app[DB]
    ayse = pid_of(app, "Ayşe")
    aid = db.value("SELECT id FROM appointments WHERE patient_id = ? AND status IN ('booked','arrived') ORDER BY starts_at", (ayse,))
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/kurzinfo/{aid}")).text()
    assert "Nachts ohne Schmerzen schlafen" in page and "automatisch übersetzt" in page and "Gece ağrısız uyumak" in page
    assert page.index("Nachts ohne Schmerzen schlafen") < page.index("Gece ağrısız uyumak")      # Original nur aufklappbar
    # Neues Ziel → Übersetzung im Hintergrund (lokales Modell), danach in der Sprache der Therapie
    from nfizyo.ai.translate import TranslationResult, store_translation

    db.execute("UPDATE patients SET goal = ?, updated_at = ? WHERE id = ?", ("Merdiven çıkabilmek", utcnow(), ayse))
    calls = []

    def fake_translate(db_, practice_id, text, source, target):
        calls.append((text, source, target))
        store_translation(db_, practice_id, text, source, target, {"de": "Treppen steigen können", "fr": "Pouvoir monter les escaliers"}[target], "test")
        return TranslationResult("x", "test")

    monkeypatch.setattr(app[AI], "translate", fake_translate)
    monkeypatch.setattr(app[AI], "can_translate", lambda: True)
    page = await (await client.get(f"/therapie/kurzinfo/{aid}")).text()
    assert "Merdiven çıkabilmek" in page
    await app[JOBS].run_all(app)
    assert ("Merdiven çıkabilmek", "tr", "de") in calls
    page = await (await client.get(f"/therapie/kurzinfo/{aid}")).text()
    assert "Treppen steigen können" in page
    n = db.value("SELECT COUNT(*) FROM jobs WHERE kind = 'translate_goal'")
    await client.get(f"/therapie/kurzinfo/{aid}")
    assert db.value("SELECT COUNT(*) FROM jobs WHERE kind = 'translate_goal'") == n          # nicht bei jedem Aufruf neu


# --------------------------------------------------------------------------- 9. Posteingang
async def test_inbox_original_titles_and_scan_status(client, app):
    db = app[DB]
    op = db.one("SELECT * FROM documents WHERE filename = 'OP-Bericht_Muster_Lea.txt'")
    await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.get(f"/therapie/posteingang/{op['id']}/datei")
    assert r.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert "Orthopädie" in (await r.read()).decode("utf-8")
    page = await (await client.get(f"/therapie/posteingang/{op['id']}/original")).text()
    assert "Orthopädie" in page and f'href="/therapie/posteingang/{op["id"]}"' in page and "<nav" in page
    detail = await (await client.get(f"/therapie/posteingang/{op['id']}")).text()
    assert f"/therapie/posteingang/{op['id']}/original" in detail
    done = await (await client.get("/therapie/posteingang?f=done")).text()
    assert "OP-Bericht_Muster_Lea.txt" not in done and ("Operationsbericht" in done or "OP-Bericht" in done)
    db.execute("UPDATE documents SET title = NULL, kind = 'surgery' WHERE id = ?", (op["id"],))
    done = await (await client.get("/therapie/posteingang?f=done")).text()
    assert "OP-Bericht_Muster_Lea.txt" not in done and "OP-Bericht" in done
    await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get("/empfang/posteingang")).text()
    assert "scan-eingang" not in page and "Noch kein Scan eingegangen." in page
    # Dateiname mit Umlaut zerbricht den Header nicht
    db.execute("UPDATE documents SET filename = 'Bericht_Rüegg.txt' WHERE id = ?", (op["id"],))
    await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.get(f"/therapie/posteingang/{op['id']}/datei")
    assert r.status == 200 and "UTF-8''Bericht_R%C3%BCegg.txt" in r.headers["Content-Disposition"]
