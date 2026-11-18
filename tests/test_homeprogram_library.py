"""Heimprogramm 0.9: eigene Zeichnung pro Übung (sonst neutrale Körperbereich-Kachel), Dosierung als Text in allen
App-Sprachen (mit Dauer), Bibliothek bearbeiten / deaktivieren / reaktivieren, Patientenseiten."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import login, pid_of

from nfizyo.core.auth import create_session
from nfizyo.core.i18n import PATIENT_LANGS
from nfizyo.core.keys import DB, I18N
from nfizyo.core.tenant import practice_scope
from nfizyo.modules.homeprogram import dose, drawings, library_data
from nfizyo.modules.homeprogram import repo as hp

STATIC = Path(__file__).resolve().parent.parent / "nfizyo" / "web" / "static" / "img" / "exercises"


def patient_cookie(client, app, first_name: str) -> None:
    token, _ = create_session(app[DB], "patient", patient_id=pid_of(app, first_name), practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})


def ex_id(app, code: str) -> int:
    return app[DB].value("SELECT id FROM exercises WHERE practice_id = 1 AND code = ?", (code,))


# --------------------------------------------------------------------------- Zeichnungen
def test_every_library_exercise_has_its_own_drawing_file():
    codes = [row[0] for row in library_data.EXERCISES]
    assert set(codes) == set(drawings.DRAWINGS) and len(codes) == 19
    for name in [*codes, *(f"region-{a}" for a in hp.AREAS)]:
        svg = (STATIC / f"{name}.svg").read_text(encoding="utf-8")
        assert 'viewBox="0 0 320 200"' in svg and '<g id="fig"' in svg and 'stroke="currentColor"' in svg, name
        assert "<script" not in svg and "href=" not in svg and "on" + "load" not in svg, name   # nur Striche, nichts Aktives
    # jede Zeichnung ist eine eigene Datei (keine Kopie für mehrere Übungen)
    contents = [(STATIC / f"{c}.svg").read_text(encoding="utf-8").split("</title>")[1] for c in codes]
    assert len(set(contents)) == len(contents)


def test_demo_exercises_all_mapped_to_their_drawing(app):
    rows = app[DB].query("SELECT code, drawing FROM exercises WHERE practice_id = 1 AND code IS NOT NULL")
    assert rows and all(r["drawing"] == r["code"] for r in rows)
    walk = app[DB].one("SELECT * FROM exercises WHERE practice_id = 1 AND code = 'walk'")
    assert walk["duration_min"] == 20 and walk["sets"] is None


def test_picture_falls_back_to_the_body_region_never_a_pose():
    assert drawings.picture({"drawing": "pendulum", "regions": "shoulder"})["src"] == "img/exercises/pendulum.svg"
    p = drawings.picture({"drawing": None, "regions": "knee,hip"})
    assert p == {"kind": "region", "src": "img/exercises/region-knee.svg", "code": None, "region": "knee"}
    for bad in ("../../core/secret", "pendulum.svg", "unknown", ""):
        assert drawings.picture({"drawing": bad, "regions": "lumbar"})["src"] == "img/exercises/region-lumbar.svg"
    assert drawings.picture({"drawing": None, "regions": "spleen"})["region"] == "general"
    assert drawings.picture({})["src"] == "img/exercises/region-general.svg"


# --------------------------------------------------------------------------- Dosierung
CASES = {
    "de": ("3\u00a0Serien à 10\u00a0Wiederholungen · 5\u00a0s halten", "20\u00a0Minuten", "1\u00a0Serie à 1\u00a0Wiederholung"),
    "fr": ("3\u00a0séries de 10\u00a0répétitions · tenir\u00a05\u00a0s", "20\u00a0minutes", "1\u00a0série de 1\u00a0répétition"),
    "it": ("3\u00a0serie da 10\u00a0ripetizioni · tenere\u00a05\u00a0s", "20\u00a0minuti", "1\u00a0serie da 1\u00a0ripetizione"),
    "en": ("3\u00a0sets of 10\u00a0repetitions · hold\u00a05\u00a0s", "20\u00a0minutes", "1\u00a0set of 1\u00a0repetition"),
    "pt": ("3\u00a0séries de 10\u00a0repetições · manter\u00a05\u00a0s", "20\u00a0minutos", "1\u00a0série de 1\u00a0repetição"),
    "es": ("3\u00a0series de 10\u00a0repeticiones · mantener\u00a05\u00a0s", "20\u00a0minutos", "1\u00a0serie de 1\u00a0repetición"),
    "sq": ("3\u00a0seri me nga 10\u00a0përsëritje · mbani\u00a05\u00a0s", "20\u00a0minuta", "1\u00a0seri me nga 1\u00a0përsëritje"),
    "bks": ("3\u00a0serije po 10\u00a0ponavljanja · držati\u00a05\u00a0s", "20\u00a0minuta", "1\u00a0serija po 1\u00a0ponavljanje"),
    "tr": ("10\u00a0tekrarlık 3\u00a0set · 5\u00a0sn tutun", "20\u00a0dakika", "1\u00a0tekrarlık 1\u00a0set"),
    "ar": ("3\u00a0مجموعات من 10\u00a0تكرارات · اثبت\u00a05\u00a0ثوانٍ", "20\u00a0دقيقة", "1\u00a0مجموعة من 1\u00a0تكرار"),
    "uk": ("3\u00a0підходи по 10\u00a0повторень · утримувати\u00a05\u00a0с", "20\u00a0хвилин", "1\u00a0підхід по 1\u00a0повторення"),
}


@pytest.mark.parametrize("lang", PATIENT_LANGS)
def test_dosage_reads_naturally_in_every_app_language(app, lang):
    i18n = app[I18N]
    full, walk, single = CASES[lang]
    assert dose.text(i18n, lang, {"sets": 3, "reps": 10, "hold_sec": 5, "per_day": 1}) == full
    assert dose.text(i18n, lang, {"sets": None, "reps": None, "hold_sec": None, "duration_min": 20, "per_day": 1}) == walk
    assert dose.text(i18n, lang, {"sets": 1, "reps": 1}) == single
    assert dose.text(i18n, lang, {"sets": None, "reps": None, "hold_sec": None, "per_day": 1}) == ""
    for row in ({"sets": 2}, {"reps": 12}, {"sets": 3, "hold_sec": 30}, {"sets": 2, "per_day": 3}, {"duration_min": 1, "per_day": 2}):
        txt = dose.text(i18n, lang, row)
        assert txt and "{" not in txt and not txt.rstrip().endswith("×"), (lang, row, txt)
    # jede Mehrzahlform der Sprache ist übersetzt (kein Rückfall auf Deutsch)
    for unit in ("sets", "reps", "hold", "minutes", "per_day"):
        for n in range(1, 112):
            cat = dose.plural_category(lang, n)
            key = f"p.ex.dose.{unit}.{cat}"
            assert i18n.has(lang, key) or (cat not in ("one", "other") and i18n.has(lang, f"p.ex.dose.{unit}.other")), (lang, key)


def test_dosage_plural_forms(app):
    i18n = app[I18N]
    assert dose.text(i18n, "uk", {"sets": 2, "reps": 5}) == "2\u00a0підходи по 5\u00a0повторень"
    assert dose.text(i18n, "uk", {"sets": 21, "reps": 22}) == "21\u00a0підхід по 22\u00a0повторення"
    assert dose.text(i18n, "bks", {"sets": 5, "reps": 2}) == "5\u00a0serija po 2\u00a0ponavljanja"
    assert dose.text(i18n, "ar", {"sets": 2, "reps": 15}) == "مجموعتان من 15\u00a0تكرارًا"
    assert dose.text(i18n, "ar", {"duration_min": 5, "per_day": 2}) == "5\u00a0دقائق · مرتين يوميًا"
    assert dose.text(i18n, "de", {"sets": 2, "per_day": 2}) == "2\u00a0Serien · 2× täglich"
    assert dose.text(i18n, "fr", {"sets": 1, "hold_sec": 30}) == "1\u00a0série · tenir\u00a030\u00a0s"


def test_short_dosage_for_the_therapy_screens(app):
    i18n = app[I18N]
    assert dose.text(i18n, "de", {"sets": 3, "reps": 10, "hold_sec": 5}, short=True) == "3\u00a0×\u00a010 · 5\u00a0s halten"
    assert dose.text(i18n, "de", {"sets": 2}, short=True) == "2\u00a0Serien"
    assert dose.text(i18n, "fr", {"reps": 12, "per_day": 2}, short=True) == "12\u00a0rép. · 2× par jour"
    assert dose.text(i18n, "de", {"duration_min": 20}, short=True) == "20\u00a0min"
    assert dose.text(i18n, "fr", {"sets": 3, "hold_sec": 30}, short=True) == "3\u00a0séries · tenir\u00a030\u00a0s"


# --------------------------------------------------------------------------- Patienten-App
async def test_patient_pages_show_drawings_dosage_and_tidy_translation_note(client, app):
    db = app[DB]
    patient_cookie(client, app, "Ayşe")                       # Türkisch, Programm mit Beckenkippen … Spazieren
    page = await (await client.get("/p/uebungen")).text()
    for code in ("pelvic_tilt", "cat_cow", "bridge", "walk"):
        assert re.search(rf'<use href="/static/img/exercises/{code}\.svg\?v=[^"#]*#fig"', page), code
    assert "self_improvement" not in page and "region-" not in page
    assert "20\u00a0dakika" in page and "15\u00a0tekrarlık 2\u00a0set" in page
    assert not re.search(r"\d ×\s*<", page)                    # nie ein nacktes «2 ×»
    iid = db.value("SELECT i.id FROM program_items i JOIN exercises e ON e.id = i.exercise_id JOIN programs p ON p.id = i.program_id"
                   " WHERE p.patient_id = ? AND e.code = 'pelvic_tilt'", (pid_of(app, "Ayşe"),))
    detail = await (await client.get(f"/p/uebungen/{iid}")).text()
    assert 'class="ex-media-pic ex-drawing" viewBox="0 0 320 200" focusable="false" role="img" aria-label="' in detail
    assert "img/exercises/pelvic_tilt.svg" in detail and "css/homeprogram.css" in detail
    # Hinweis «maschinell übersetzt»: eine Zeile (Symbol, Text, Pfeil in der Zusammenfassung), Original zum Aufklappen
    m = re.search(r'<details class="mt-note"><summary>(.*?)</summary><p dir="auto" lang="de">(.*?)</p></details>', detail, re.S)
    assert m and m.group(1).count("<svg") == 2 and "makine çevirisi" in m.group(1) and "Rückenlage" in m.group(2)
    assert "<script" not in detail.split("</main>")[0]          # kein Inline-JavaScript

    # Übung ohne Zeichnung: neutrale Kachel mit Körperbereich und Name
    with practice_scope(1):
        eid = hp.add_exercise(db, name_de="Bauchatmung", name_fr="Respiration abdominale", instructions_de="Ruhig atmen.",
                              instructions_fr="Respirer calmement.", regions="lumbar", duration_min=5, per_day=2)
        prog = hp.current_program(db, pid_of(app, "Ayşe"))
        new_iid = hp.add_item(db, prog, eid, None, "de")
        hp.approve(db, prog)
    detail = await (await client.get(f"/p/uebungen/{new_iid}")).text()
    assert "img/exercises/region-lumbar.svg" in detail and 'class="ex-media ex-media-region"' in detail
    assert re.search(r"<figcaption dir=\"auto\">[^<]+</figcaption>", detail)
    assert "5\u00a0dakika · günde\u00a02\u00a0kez" in detail


async def test_own_video_stays_preferred_over_the_drawing(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    iid = db.value("SELECT i.id FROM program_items i JOIN programs p ON p.id = i.program_id WHERE p.patient_id = ? ORDER BY i.position"
                   " LIMIT 1", (lea,))
    db.execute("UPDATE program_items SET video_path = ?, video_mime = 'video/mp4' WHERE id = ?", ("/nicht/da.mp4", iid))
    patient_cookie(client, app, "Lea")
    detail = await (await client.get(f"/p/uebungen/{iid}")).text()
    assert "<video" in detail and "ex-media-pic" not in detail
    page = await (await client.get("/p/uebungen")).text()
    assert 'class="ex-badge"' in page                         # Liste: Zeichnung mit Video-Zeichen


# --------------------------------------------------------------------------- Bibliothek (Therapie)
async def test_library_lists_drawings_and_new_exercise_has_its_own_page(client, app):
    db = app[DB]
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get("/therapie/uebungen")).text()
    assert "img/exercises/bird_dog.svg" in page and "Vierfüssler diagonal" in page
    assert 'name="name_de"' not in page and "/therapie/uebungen/neu" in page          # Formular hinter dem Knopf
    assert "20\u00a0min" in page and "3\u00a0×\u00a010" in page and not re.search(r"\d ×\s*<", page)
    form = await (await client.get("/therapie/uebungen/neu")).text()
    assert 'name="drawing" value=""' in form and 'name="drawing" value="pendulum"' in form
    r = await client.post("/therapie/uebungen/neu", data={"_csrf": csrf, "name_de": "Treppensteigen", "instructions_de": "Langsam steigen.",
                                                          "area": ["knee", "hip"], "duration_min": "10", "drawing": "walk"},
                          allow_redirects=False)
    assert r.status == 302
    row = db.one("SELECT * FROM exercises WHERE name_de = 'Treppensteigen'")
    assert row["name_fr"] == "Treppensteigen" and row["regions"] == "knee,hip" and row["drawing"] == "walk"
    assert row["duration_min"] == 10 and row["per_day"] == 1 and row["active"] == 1 and row["practice_id"] == 1
    # unbekannte Zeichnung → keine; fehlende Anleitung / falsche Zahl → Fehler, nichts gespeichert
    r = await client.post("/therapie/uebungen/neu", data={"_csrf": csrf, "name_de": "X1", "instructions_de": "Y", "drawing": "../x"},
                          allow_redirects=False)
    assert r.status == 302 and db.value("SELECT drawing FROM exercises WHERE name_de = 'X1'") is None
    r = await client.post("/therapie/uebungen/neu", data={"_csrf": csrf, "name_de": "X2", "instructions_de": "", "sets": "drei"})
    assert r.status == 400 and db.value("SELECT COUNT(*) FROM exercises WHERE name_de = 'X2'") == 0


async def test_edit_deactivate_and_reactivate_exercise(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    eid = ex_id(app, "mini_squat")                             # steckt in Leas freigegebenem Programm
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/uebungen/{eid}")).text()
    assert "Mini-Kniebeuge" in page and "In 1 laufenden Programm verwendet" in page and "Deaktivieren" in page
    r = await client.post(f"/therapie/uebungen/{eid}", data={
        "_csrf": csrf, "name_de": "Mini-Kniebeuge am Stuhl", "name_fr": "Mini-squat à la chaise", "instructions_de": "Neu DE",
        "instructions_fr": "Nouveau FR", "area": "knee", "sets": "2", "reps": "8", "hold_sec": "", "duration_min": "", "per_day": "2",
        "drawing": "mini_squat"}, allow_redirects=False)
    assert r.status == 302
    row = db.one("SELECT * FROM exercises WHERE id = ?", (eid,))
    assert (row["name_de"], row["sets"], row["reps"], row["per_day"], row["regions"]) == ("Mini-Kniebeuge am Stuhl", 2, 8, 2, "knee")
    item = db.one("SELECT * FROM program_items WHERE exercise_id = ? AND removed = 0", (eid,))
    assert (item["sets"], item["reps"]) == (3, 12)              # Dosierung im Programm bleibt
    assert db.value("SELECT COUNT(*) FROM audit_log WHERE entity = 'exercise' AND entity_id = ? AND action = 'update'", (eid,)) == 1

    # deaktivieren: weg aus der Auswahl, bleibt im Programm und in der App
    r = await client.post(f"/therapie/uebungen/{eid}/aktiv", data={"_csrf": csrf, "on": "0"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT active FROM exercises WHERE id = ?", (eid,)) == 0
    picker = await (await client.get(f"/therapie/patienten/{lea}/heimprogramm")).text()
    assert f'name="exercise_id" value="{eid}"' not in picker and "Mini-Kniebeuge am Stuhl" in picker
    lib = await (await client.get("/therapie/uebungen")).text()
    assert "Mini-Kniebeuge am Stuhl" not in lib and "Deaktivierte anzeigen (1)" in lib
    assert "Mini-Kniebeuge am Stuhl" in await (await client.get("/therapie/uebungen?inaktiv=1")).text()
    n = db.value("SELECT COUNT(*) FROM program_items WHERE exercise_id = ?", (eid,))
    r = await client.post(f"/therapie/patienten/{lea}/heimprogramm", data={"_csrf": csrf, "exercise_id": str(eid)}, allow_redirects=False)
    assert r.status == 404 and db.value("SELECT COUNT(*) FROM program_items WHERE exercise_id = ?", (eid,)) == n
    patient_cookie(client, app, "Lea")
    assert "Mini-Kniebeuge am Stuhl" in await (await client.get("/p/uebungen")).text()

    # reaktivieren
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/uebungen/{eid}/aktiv", data={"_csrf": csrf, "on": "1"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT active FROM exercises WHERE id = ?", (eid,)) == 1
    picker = await (await client.get(f"/therapie/patienten/{lea}/heimprogramm")).text()
    assert f'name="exercise_id" value="{eid}"' in picker
    actions = [r["action"] for r in db.query("SELECT action FROM audit_log WHERE entity = 'exercise' AND entity_id = ? ORDER BY id", (eid,))]
    assert actions == ["update", "deactivate", "activate"]


async def test_programme_item_takes_duration(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    await client.post(f"/therapie/patienten/{lea}/heimprogramm", data={"_csrf": csrf, "exercise_id": str(ex_id(app, "walk"))})
    iid = db.value("SELECT i.id FROM program_items i JOIN programs p ON p.id = i.program_id WHERE p.patient_id = ? AND i.exercise_id = ?",
                   (lea, ex_id(app, "walk")))
    assert db.value("SELECT duration_min FROM program_items WHERE id = ?", (iid,)) == 20
    await client.post(f"/therapie/heimprogramm/{iid}", data={"_csrf": csrf, "duration_min": "30", "per_day": "1"})
    assert db.value("SELECT duration_min FROM program_items WHERE id = ?", (iid,)) == 30
    page = await (await client.get(f"/therapie/patienten/{lea}/heimprogramm")).text()
    assert 'name="duration_min" value="30"' in page and "In der App: 30\u00a0Minuten" in page
