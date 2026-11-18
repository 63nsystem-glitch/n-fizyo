"""QA-Fixes 0.9.0 (Bereich D): Diktat/Notizentwurf, Patienteninformation, Benachrichtigungs-Link, Support-Anfragen,
Übergabe, Fehlerseiten, Heimprogramm-Formular, Notizdatum, Einrichtungs-Checkliste.

Jeder Test nennt die QA-Nummer (F.. = Abläufe, SEC-.. = Sicherheit, CODE-.. = Code, UPG-.. = Update)."""
from __future__ import annotations

import json
import logging
import re

from conftest import csrf_of, login, pid_of

from nfizyo.ai.notes import draft_rules, extract_measurements
from nfizyo.ai.patientinfo import draft_rules as info_rules
from nfizyo.ai.types import NoteContext, NoteDraft
from nfizyo.core import patientdata
from nfizyo.core.auth import create_session
from nfizyo.core.keys import AI, DB, JOBS, SETTINGS
from nfizyo.core.tenant import practice_scope

DICTATION = ("Befund: Schulter rechts, Flexion 120 Grad, Schmerz 4 von 10 bei Elevation. Behandlung: Mobilisation glenohumeral. "
             "Beurteilung: besser. Plan: Heimprogramm weiter, Pendelübungen zweimal täglich. "
             "Zu Hause: Arm nicht über Kopf heben, Kühlen bei Schmerzen. Melden Sie sich, wenn Taubheit im Arm auftritt.")


# --------------------------------------------------------------------------- F01 Diktierte Seite / diktiertes Gelenk gelten
def _loc(text: str, ctx: NoteContext) -> list[tuple]:
    return [(m.kind, m.value, m.joint, m.side) for m in extract_measurements(text, ctx)]


def test_f01_dictated_side_and_joint_win_over_the_record():
    left_shoulder = NoteContext(joint="shoulder", side="l")
    assert _loc("Knie rechts Flexion 90 Grad", left_shoulder) == [("flexion", 90.0, "knee", "r")]
    assert _loc("Flexion 90 Grad im rechten Knie", left_shoulder) == [("flexion", 90.0, "knee", "r")]
    assert _loc("Schulter rechts, Flexion 120 Grad", left_shoulder) == [("flexion", 120.0, "shoulder", "r")]
    # anderes Gelenk ohne Seite: nie die Seite der Schulter aus der Akte übernehmen
    assert _loc("Knie Flexion 90 Grad", left_shoulder) == [("flexion", 90.0, "knee", None)]
    # nichts diktiert: Akte als Ersatz
    assert _loc("Flexion 90 Grad", left_shoulder) == [("flexion", 90.0, "shoulder", "l")]
    # beide Seiten in einem Diktat: jeder Wert mit seiner Seite
    assert _loc("Knie rechts Flexion 120 Grad, Knie links Flexion 90 Grad", NoteContext(joint="knee", side="l")) == [
        ("flexion", 120.0, "knee", "r"), ("flexion", 90.0, "knee", "l")]
    # beidseits: lieber keine Seite als die falsche
    assert _loc("Beidseits Knie Flexion 90 Grad", NoteContext(joint="knee", side="l")) == [("flexion", 90.0, "knee", None)]
    # Seite gehört zum anderen Gelenk → für die Schulter gilt die Akte
    assert _loc("Schulter Flexion 150 Grad, Knie rechts Flexion 90 Grad", left_shoulder) == [
        ("flexion", 150.0, "shoulder", "l"), ("flexion", 90.0, "knee", "r")]
    # Seite später im Diktat («Erguss rechts») gilt auch für die Werte davor
    assert _loc("Flexion 90 Grad. Leichter Erguss rechts.", NoteContext(joint="knee", side="l")) == [("flexion", 90.0, "knee", "r")]
    # Ausstrahlung ist kein gemessenes Gelenk / keine gemessene Seite
    assert _loc("Schmerz VAS 5 von 10, Ausstrahlung ins linke Bein bis zum Knie.", NoteContext(joint="lumbar")) == [
        ("pain_vas", 5.0, "lumbar", None)]
    assert _loc("Genou droit : flexion 110 degrés", NoteContext(joint="knee", side="l")) == [("flexion", 110.0, "knee", "r")]


def test_f01_draft_reports_the_difference_and_keeps_it_through_json():
    ctx = NoteContext(joint="shoulder", side="l")
    d = draft_rules(DICTATION, ctx)
    assert {(m.joint, m.side) for m in d.measurements} == {("shoulder", "r")}
    assert d.context_differs == {"dictated": [{"joint": "shoulder", "side": "r"}], "stored": {"joint": "shoulder", "side": "l"}}
    assert NoteDraft.from_dict(json.loads(json.dumps(d.to_dict()))).context_differs == d.context_differs
    assert draft_rules("Schulter links Flexion 120 Grad", ctx).context_differs is None
    assert draft_rules("Flexion 120 Grad", NoteContext()).context_differs is None

    class FakeLLM:
        model = "fake"

        def generate(self, *a, **k):
            return json.dumps({"befund": "Schulter rechts, Flexion 120 Grad, Schmerz 4 von 10.", "behandlung": "", "beurteilung": "",
                               "plan": ""})

    from nfizyo.ai.notes import draft_with_llm

    llm = draft_with_llm("Schulter rechts, Flexion 120 Grad, Schmerz 4 von 10.", "de", FakeLLM(), ctx)
    assert llm.engine.startswith("llm") and llm.context_differs and {m.side for m in llm.measurements} == {"r"}


async def test_f01_review_card_shows_hint_and_files_the_dictated_side(client, app):
    db = app[DB]
    maria = pid_of(app, "Maria")
    with practice_scope(1):
        finding = db.one("SELECT structure_id, side FROM findings WHERE patient_id = ? AND side IN ('l', 'r')"
                         " ORDER BY id LIMIT 1", (maria,))
    assert finding is not None and finding["side"] == "l"         # Demo: Rotatorenmanschette links
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post("/therapie/diktat/text", data={"_csrf": csrf, "text": DICTATION, "patient_id": maria}, allow_redirects=False)
    review = r.headers["Location"]
    await app[JOBS].run_all(app)
    page = await (await client.get(review)).text()
    assert "Im Diktat: Schulter rechts – in der Akte: Schulter links" in page
    assert re.search(r"Flexion · Schulter rechts: <strong>120°", page)
    form = {"_csrf": csrf_of(page), "m0": "1", "m1": "1"}
    for sec in ("befund", "behandlung", "beurteilung", "plan"):
        m = re.search(rf'name="{sec}"[^>]*>([^<]*)<', page)
        form[sec] = m.group(1) if m else ""
    assert "Zu Hause" not in form["befund"] and "Melden Sie sich" in form["plan"]      # F02
    r = await client.post(review + "/freigeben", data=form, allow_redirects=False)
    assert r.status == 302
    rows = db.query("SELECT kind, joint, side FROM measurements WHERE patient_id = ? AND note_id IS NOT NULL ORDER BY id DESC LIMIT 2",
                    (maria,))
    assert {(x["kind"], x["joint"], x["side"]) for x in rows} == {("flexion", "shoulder", "r"), ("pain_vas", "shoulder", "r")}


# --------------------------------------------------------------------------- F02 «Zu Hause» / «Melden Sie sich» nicht im Befund
def test_f02_patient_sentences_go_to_plan_and_info_without_labels():
    d = draft_rules(DICTATION)
    assert "Zu Hause" not in d.befund and "Melden" not in d.befund
    assert "Zu Hause: Arm nicht über Kopf heben" in d.plan and "Melden Sie sich" in d.plan
    assert d.befund.startswith("Schulter rechts")
    info = info_rules(DICTATION, "de")
    assert not re.search(r"\b(Plan|Zu Hause|Befund|Behandlung)\s*:", info.home + info.contact)
    assert info.home.startswith("Heimprogramm weiter") and "Arm nicht über Kopf heben" in info.home
    assert info.contact == "Melden Sie sich, wenn Taubheit im Arm auftritt."
    assert "Mobilisation" not in info.home + info.contact and "Flexion" not in info.home + info.contact
    # Befund-Sätze vor der ersten Überschrift sind keine Info für zu Hause
    knee = info_rules("Knie etwas geschwollen. Schmerz VAS 3 bei Belastung. Plan: Zu Hause kühlen.", "de")
    assert knee.home == "Zu Hause kühlen." and knee.contact == ""
    # französisch
    fr = draft_rules("Bilan : flexion 100 degrés. Plan : poursuivre. À la maison : glace 3 fois par jour. Appelez-nous si la douleur augmente.")
    assert "maison" not in fr.befund and "Appelez" in fr.plan
    # frei diktiert (ohne Überschriften) wie bisher
    free = info_rules("Weniger Nachtschmerz. Zu Hause die Übungen zweimal täglich machen. Sie soll sich melden, wenn das Bein taub wird.",
                      "de")
    assert "zweimal täglich" in free.home and "taub" in free.contact


# --------------------------------------------------------------------------- F03 ohne Übersetzung: ehrlich sagen
async def test_f03_without_translator_no_promise_in_the_app(client, app):
    db = app[DB]
    assert not app[AI].can_translate()
    maria = pid_of(app, "Maria")                                    # App-Sprache Portugiesisch
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post("/therapie/diktat/text", data={"_csrf": csrf, "text": DICTATION, "patient_id": maria}, allow_redirects=False)
    review = r.headers["Location"]
    await app[JOBS].run_all(app)
    page = await (await client.get(review)).text()
    assert "die automatische Übersetzung ist noch nicht eingerichtet" in page and "wird er auf Portugiesisch übersetzt" not in page
    form = {"_csrf": csrf_of(page), "pi_home": "Pendelübungen zweimal täglich.", "pi_contact": "", "pi_send": "1"}
    for sec in ("befund", "behandlung", "beurteilung", "plan"):
        m = re.search(rf'name="{sec}"[^>]*>([^<]*)<', page)
        form[sec] = m.group(1) if m else ""
    assert (await client.post(review + "/freigeben", data=form, allow_redirects=False)).status == 302
    info = db.one("SELECT * FROM patient_infos WHERE patient_id = ? AND status = 'approved' ORDER BY id DESC", (maria,))
    # Übersetzung noch nicht gelaufen: «folgt in Kürze» ist ehrlich
    token, _ = create_session(db, "patient", patient_id=maria, practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    assert "A tradução segue em breve." in await (await client.get("/p/info")).text()
    await app[JOBS].run_all(app)
    assert db.value("SELECT translation_engine FROM patient_infos WHERE id = ?", (info["id"],)) == "none"
    text = await (await client.get("/p/info")).text()
    assert "Pendelübungen zweimal täglich." in text and "A tradução segue em breve." not in text
    assert "Este texto não está traduzido." in text
    await login(client, "therapie@demo.n-fizyo.ch")
    tab = await (await client.get(f"/therapie/patienten/{maria}/info")).text()
    assert "Nicht übersetzt" in tab and "Übersetzung läuft" not in tab


# --------------------------------------------------------------------------- F04 Link der Benachrichtigung
async def test_f04_notification_link_uses_public_url_fallback(client, app):
    from nfizyo.core.web import background_public_url
    from nfizyo.modules.notify import service

    assert app[SETTINGS].base_url == "http://127.0.0.1:8080"
    assert background_public_url(app, 1).startswith("http://127.0.0.1:")
    await login(client, "therapie@demo.n-fizyo.ch")
    host = client.server.host, client.server.port
    url = background_public_url(app, 1, "/p/nachrichten")
    assert url == f"http://{host[0]}:{host[1]}/p/nachrichten"            # Adresse, unter der die Praxis den Server benutzt
    msg = service.compose(app, "de", "Praxis", {"message"}, practice_id=1)
    assert msg["url"] == url and url in msg["text"]
    # eingestellte öffentliche Adresse geht immer vor
    app[SETTINGS].__dict__["base_url"] = "https://praxis.example.ch"
    try:
        assert service.compose(app, "de", "Praxis", {"info"}, practice_id=1)["url"] == "https://praxis.example.ch/p/info"
    finally:
        app[SETTINGS].__dict__["base_url"] = "http://127.0.0.1:8080"


def test_f04_background_url_uses_real_port_without_any_request(tmp_path):
    from nfizyo.app import create_app
    from nfizyo.core.config import load_settings
    from nfizyo.core.web import background_public_url

    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True)
    settings.__dict__["port"] = 8141
    app2 = create_app(settings, start_workers=False)
    assert background_public_url(app2, 1, "/p/info") == "http://127.0.0.1:8141/p/info"


# --------------------------------------------------------------------------- F14 / SEC-06 Patientendaten in Support-Anfragen
def test_f14_sec06_scanner_catches_everyday_variants(app):
    db = app[DB]
    db.execute("UPDATE patients SET number = '1002' WHERE first_name = 'Jonas' AND practice_id = 1")
    blocked = {
        "Frau Örnek hat keine SMS bekommen": "name",
        "Herr Muster kann sich nicht in der App anmelden": "name",
        "Mme Exemple n'a pas reçu le SMS": "name",
        "M. Exemple appelle": "name",
        "Monsieur Esempio": "name",
        "Patientin Muster": "name",
        "L. Muster hat zwei Rechnungen": "name",
        "Frau Oernek": "name",
        "Ayse Oernek kann sich nicht anmelden": "name",
        "Le​a Mus­ter": "name",
        "Problem mit Dossier 1002": "number",
        "Bei P00004 fehlt der Termin": "number",
        "geb. 12. März 1985": "birthdate",
        "née le 3 avril 1979": "birthdate",
        "geb. 12-03-1985": "birthdate",
        "Jg. 1948, Knie": "birthdate",
        "AHV 756-1234-5678-97": "ahv",
        "AHV 756–1234–5678–97": "ahv",
        "Tel 79 123 45 67": "phone",
        "lea(at)example.ch": "email",
    }
    for text, cat in blocked.items():
        assert cat in patientdata.scan(text, db, 1), text
    for ok in ("Sandra hat zwei Rechnungen",                    # Vorname allein: bewusst nicht blockiert (Entscheid Inhaber)
               "Fehler 1002 beim Speichern", "Termin am 12. März 2026 fehlt", "Frau Keller vom Empfang ruft an",
               "Rechnung R2026-0012 lässt sich seit 03.11.2026 nicht stornieren", "Fr. 50.00 zu viel verbucht",
               "IBAN CH93 0076 2011 6238 5295 7", "Version 0.9.0, Fehler 500 beim Speichern"):
        assert patientdata.scan(ok, db, 1) == [], ok
    assert patientdata.scan("Herr Muster", db, 2) == []                 # nur Namen der eigenen Praxis
    assert patientdata.scan("Patient P00004") == ["number"]             # Panel: nur Muster


async def test_f14_support_page_blocks_salutation_and_number(client, app):
    db = app[DB]
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    for text, label in (("Frau Örnek hat keine SMS bekommen", "Name einer Patientin"), ("Bei P00004 fehlt der Termin", "Patientennummer")):
        r = await client.post("/empfang/hilfe", data={"_csrf": csrf, "area": "billing", "urgency": "normal", "body": text})
        page = await r.text()
        assert r.status == 422 and "Nicht gesendet" in page and label in page, text
    assert db.value("SELECT COUNT(*) FROM box_support_requests") == 0


# --------------------------------------------------------------------------- F15 Support ohne Verbindung zu NSystem
async def test_f15_support_without_central_connection_says_so(client, app):
    db = app[DB]
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    page = await (await client.get("/empfang/hilfe")).text()
    assert "nicht mit NSystem verbunden" in page and "info@nsystem.ch" in page
    r = await client.post("/empfang/hilfe", data={"_csrf": csrf, "area": "agenda", "urgency": "normal", "body": "Agenda zeigt nichts"},
                          allow_redirects=False)
    assert r.status == 302
    page = await (await client.get("/empfang/hilfe")).text()
    assert "noch nicht gesendet" in page and "automatisch" in page and "spätestens in 5 Minuten" not in page
    assert "Nicht gesendet" in page and "Wird gesendet" not in page
    assert db.value("SELECT COUNT(*) FROM box_support_requests WHERE sent_at IS NULL") == 1


async def test_f15_connected_server_keeps_the_promise(client, app):
    s = app[SETTINGS].__dict__
    old = (s["central_url"], s["central_box_id"], s["central_box_key"])
    s.update(central_url="https://panel.example.invalid", central_box_id="box-1", central_box_key="k" * 32)
    try:
        csrf = await login(client, "empfang@demo.n-fizyo.ch")
        page = await (await client.get("/empfang/hilfe")).text()
        assert "nicht mit NSystem verbunden" not in page
        await client.post("/empfang/hilfe", data={"_csrf": csrf, "area": "agenda", "urgency": "normal", "body": "Agenda leer"})
        page = await (await client.get("/empfang/hilfe")).text()
        assert "Wird gesendet" in page and "Nicht gesendet" not in page
    finally:
        s["central_url"], s["central_box_id"], s["central_box_key"] = old


# --------------------------------------------------------------------------- F20 Übergabe: Zahl der Übungen statt Python-Text
async def test_f20_handover_shows_number_of_exercises(client, app):
    db = app[DB]
    await login(client, "a.beispiel@demo.n-fizyo.ch")
    peter = pid_of(app, "Peter")
    with practice_scope(1):
        n = db.value("SELECT COUNT(*) FROM program_items i JOIN programs p ON p.id = i.program_id WHERE p.patient_id = ?"
                     " AND p.status = 'active'", (peter,))
    r = await client.get(f"/therapie/patienten/{peter}/uebergabe")
    page = await r.text()
    assert r.status == 200 and "built-in method" not in page and "<built-in" not in page
    assert n and re.search(rf"Heimprogramm \(14 Tage\) · {n} Übung", page)


# --------------------------------------------------------------------------- F23 Fehlerseiten im Rahmen der Praxis
async def test_f23_error_pages_render_badges_inside_practice_scope(client, app, caplog):
    await login(client, "a.beispiel@demo.n-fizyo.ch")
    caplog.set_level(logging.ERROR, logger="nfizyo.core.web")
    html = {"Accept": "text/html"}
    r = await client.get(f"/therapie/patienten/{pid_of(app, 'Lea')}/uebergabe", headers=html)
    page = await r.text()
    assert r.status == 404 and "Nicht gefunden" in page
    r = await client.get("/therapie/gibt-es-nicht", headers=html)
    assert r.status == 404
    assert not [rec for rec in caplog.records if "Badge" in rec.getMessage() or "NoPracticeError" in (rec.exc_text or "")]


# --------------------------------------------------------------------------- CODE-07 Übungsformular: Zahl ausserhalb 1–10
async def test_code07_exercise_form_out_of_range_is_a_form_error(client, app):
    db = app[DB]
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    before = db.value("SELECT COUNT(*) FROM exercises")
    for value in ("0", "11", "99", "abc", "99999999999999999999"):
        r = await client.post("/therapie/uebungen/neu", data={"_csrf": csrf, "name_de": "Testübung", "instructions_de": "So geht es.",
                                                               "per_day": value})
        page = await r.text()
        assert r.status == 400, value
        assert "Ganze Zahl von 1 bis 10" in page and 'value="Testübung"' in page
    assert db.value("SELECT COUNT(*) FROM exercises") == before
    eid = db.value("SELECT id FROM exercises WHERE practice_id = 1 ORDER BY id LIMIT 1")
    r = await client.post(f"/therapie/uebungen/{eid}", data={"_csrf": csrf, "name_de": "X", "instructions_de": "Y", "sets": "21"})
    assert r.status == 400 and "Ganze Zahl von 1 bis 20" in await r.text()


# --------------------------------------------------------------------------- CODE-12 Notizdatum
async def test_code12_invalid_note_date_is_rejected_and_old_bad_values_do_not_break(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    before = db.value("SELECT COUNT(*) FROM notes WHERE patient_id = ?", (lea,))
    for bad in ("31.02.2026", "2026-02-31", "kaputt", "2099-01-01", "1850-01-01"):
        r = await client.post(f"/therapie/patienten/{lea}/notizen/neu", data={"_csrf": csrf, "session_date": bad, "befund": "Text bleibt"})
        page = await r.text()
        assert r.status == 400 and "gültiges Datum" in page and "Text bleibt" in page, bad
    assert db.value("SELECT COUNT(*) FROM notes WHERE patient_id = ?", (lea,)) == before
    r = await client.post(f"/therapie/patienten/{lea}/notizen/neu", data={"_csrf": csrf, "session_date": "", "befund": "Heute"},
                          allow_redirects=False)
    assert r.status == 302
    # alter, ungültiger Wert in der Datenbank (vor dem Fix gespeichert): Verlauf, Akte und Kurzinfo bleiben benutzbar
    with practice_scope(1):
        nid = db.value("SELECT id FROM notes WHERE patient_id = ? ORDER BY id DESC LIMIT 1", (lea,))
    db.execute("UPDATE notes SET session_date = 'kaputt' WHERE id = ?", (nid,))
    for path in (f"/therapie/patienten/{lea}/verlauf", f"/therapie/patienten/{lea}", f"/therapie/notizen/{nid}"):
        r = await client.get(path)
        assert r.status == 200, path
    from nfizyo.core.timeutil import fmt_date

    assert fmt_date("kaputt") == "kaputt" and fmt_date("2026-11-03") == "03.11.2026"


# --------------------------------------------------------------------------- UPG-03 Einrichtungs-Checkliste
async def test_upg03_setup_checklist_rooms_optional_sms_optional_always_hideable(client, app, practice_b):
    from nfizyo.modules.settings import setup_check

    db = app[DB]
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    # Standort ohne Räume: erledigt (Räume freiwillig)
    db.execute("UPDATE rooms SET active = 0 WHERE practice_id = 1")
    page = await (await client.get("/leitung/einrichtung")).text()
    block = page.split("Standorte und Räume", 1)[1][:600]
    assert "erledigt" in block and "noch keine Räume (freiwillig)" in block
    # SMS-Anbieter freiwillig: zählt nicht, hält nichts offen
    block = page.split("SMS und E-Mail", 1)[1][:600]
    assert "freiwillig" in block and "offen" not in block
    real = setup_check.items

    def all_but_sms(request):
        return [dict(it, done=it["key"] == "messaging" and it["done"] or it["key"] != "messaging") for it in real(request)]

    import pytest

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(setup_check, "items", all_but_sms)
        page = await (await client.get("/leitung/uebersicht")).text()
        assert not re.search(r'class="tile" href="/leitung/einrichtung"', page)       # alles Pflicht erledigt → keine Kachel
        assert "Alles eingerichtet." in page
    finally:
        mp.undo()
    # mit offenen Pflichtpunkten jederzeit ausblendbar – nur für die eigene Praxis
    r = await client.post("/leitung/einrichtung", data={"_csrf": csrf, "do": "dismiss"}, allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT COUNT(*) FROM practice_setup WHERE practice_id = 1") == 1
    assert db.value("SELECT COUNT(*) FROM practice_setup WHERE practice_id = ?", (practice_b["id"],)) == 0
    page = await (await client.get("/leitung/einrichtung")).text()
    assert "Checkliste wieder auf der Übersicht zeigen" in page
