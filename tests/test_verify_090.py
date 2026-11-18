"""0.9.0 – letzte Prüfung gegen die Befunde aus 0.8.0: doppelte Überschrift im KI-Vorschlag, Trennpunkt am Zeilenanfang,
«Deutsch · Deutsch», Kiefergelenk am Skelett, Einladungslink im Demo-/Praxisnetz, übersetzte Online-Termine,
ganztägige Abwesenheit im Wochenkopf, Wochenansicht mit Raum zuerst."""
from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest

from conftest import login, pid_of
from nfizyo.ai.extract import extract_rules
from nfizyo.core.keys import DB
from nfizyo.core.tenant import practice_scope
from nfizyo.core.timeutil import today
from nfizyo.core.web import link_is_public
from nfizyo.knowledge import anatomy
from nfizyo.knowledge.figures import DATA_DIR


# --------------------------------------------------------------------------- Posteingang: Vorschläge
def test_prescription_suggestion_without_repeated_heading():
    text = ("Zuweisung Physiotherapie\nDiagnose: Lumboischialgie rechts\n\n"
            "Verordnung: 9 Behandlungen Physiotherapie, Rumpfstabilisation, Instruktion Heimprogramm.\n")
    rx = [s for s in extract_rules(text).suggestions if s.kind == "prescription"]
    assert rx and rx[0].sessions == 9
    assert rx[0].text.startswith("9 Behandlungen"), rx[0].text
    assert "Verordnung: 9" in rx[0].excerpt          # das Zitat bleibt wörtlich


async def test_inbox_suggestions_no_double_label_no_leading_dot(client, app):
    await login(client, "therapie@demo.n-fizyo.ch")
    doc = app[DB].value("SELECT id FROM documents WHERE practice_id = 1 AND text LIKE '%Verordnung: 9 Behandlungen%'")
    html = await (await client.get(f"/therapie/posteingang/{doc}")).text()
    assert "Verordnung: Verordnung" not in html
    assert "9 Behandlungen" in html
    # Zeile mit Angaben (Gesetz, Zitat …) beginnt nie mit «·»
    for meta in re.findall(r'<div class="small muted" style="margin-top:2px">(.*?)</div>', html, flags=re.S):
        assert not re.sub(r"<[^>]+>", "", meta).strip().startswith("·"), meta


# --------------------------------------------------------------------------- Stammblatt: Sprachen
@pytest.mark.parametrize("ui_lang,bad,good", [("de", "Deutsch · Deutsch", "Französisch · Français"),
                                              ("fr", "Français · Français", "Allemand · Deutsch")])
async def test_spoken_language_without_duplicate_name(client, app, ui_lang, bad, good):
    app[DB].execute("UPDATE users SET lang = ? WHERE email = 'empfang@demo.n-fizyo.ch'", (ui_lang,))
    await login(client, "empfang@demo.n-fizyo.ch")
    html = await (await client.get(f"/empfang/patienten/{pid_of(app, 'Hans')}")).text()
    assert bad not in html and "Schweizerdeutsch · Schweizerdeutsch" not in html
    assert good in html


# --------------------------------------------------------------------------- Anatomie: Kiefergelenk
def test_tmj_marks_jaw_joint_not_whole_skull():
    tmj = anatomy.get("tmj")
    ids = anatomy.skeleton_ids(tmj, None)
    assert "skull" not in ids and {"joint_tmj_l", "joint_tmj_r", "mandible"} <= set(ids)
    assert anatomy.skeleton_ids(tmj, "r") == ["mandible", "joint_tmj_r"]
    skeleton = json.loads((DATA_DIR / "skeleton.json").read_text(encoding="utf-8"))
    assert {"joint_tmj_l", "joint_tmj_r"} <= {e["id"] for e in skeleton["front"]}
    assert anatomy.bone_structure("joint_tmj_l") == (tmj, "l")


async def test_anatomy_sheet_shows_neighbours_not_only_names(client):
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get("/therapie/anatomie/tmj/blatt")).text()
    assert "anatomie?s=mandible" in html and "anatomie?s=skull" in html
    html = await (await client.get("/therapie/anatomie/femur/blatt")).text()      # Knochen → Gelenke
    assert "anatomie?s=hip_joint" in html and "anatomie?s=knee_joint" in html
    assert "fachliche Prüfung durch Physiotherapie ausstehend" not in html
    assert "noch nicht von einer Fachperson" in html


# --------------------------------------------------------------------------- Einladung: Link nur im Demo-/Praxisnetz
@pytest.mark.parametrize("url,ok", [
    ("https://physio-beispiel.ch/p/einladung/x", True),
    ("https://praxis.example.ch:8443/p/x", True),
    ("http://physio-beispiel.ch/p/x", False),
    ("http://127.0.0.1:8080/p/x", False),
    ("https://localhost/p/x", False),
    ("https://192.168.1.20/p/x", False),
    ("https://box.local/p/x", False),
    ("https://n-fizyo/p/x", False),
])
def test_link_is_public(url, ok):
    assert link_is_public(url) is ok


async def test_invite_page_says_demo_link_only_works_here(client, app):
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.post(f"/therapie/patienten/{pid_of(app, 'Lea')}/app-einladung", data={"_csrf": csrf})).text()
    assert "data-link-note" in html and "Demo: Der Link zeigt auf diesen Demo-Rechner" in html


async def test_invite_page_without_note_for_public_address(client, app, monkeypatch):
    from nfizyo.modules.patientapp import views as papp_views

    # wie im Betrieb mit eingestellter Adresse (NFIZYO_BASE_URL=https://…)
    monkeypatch.setattr(papp_views, "public_url", lambda request, path="": "https://physio-beispiel.ch" + path)
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.post(f"/therapie/patienten/{pid_of(app, 'Lea')}/app-einladung", data={"_csrf": csrf})).text()
    assert "https://physio-beispiel.ch/p/einladung/" in html and "data-link-note" not in html


# --------------------------------------------------------------------------- Online-Termine in der Sprache der Besucherin
@pytest.mark.parametrize("lang,intro,name", [("tr", "Yeni hastalar", "Fizyoterapi"), ("pt", "Novos pacientes", "Fisioterapia"),
                                             ("ar", "المرضى الجدد", "العلاج الطبيعي")])
async def test_booking_page_practice_texts_translated_in_demo(client, lang, intro, name):
    html = await (await client.get(f"/p/termin/demo?lang={lang}")).text()
    assert intro in html and name in html
    # Original bleibt abrufbar (aufklappbar), steht aber nicht als Haupttext da
    main = html.split("<details", 1)[0]
    assert "Neue Patientinnen und Patienten" not in main and ">Physiotherapie<" not in main


# --------------------------------------------------------------------------- Agenda: Woche
async def test_week_header_names_whole_day_absence_and_room_first(client, app):
    from nfizyo.modules.agenda import absences

    from datetime import datetime

    from nfizyo.core.timeutil import TZ

    db = app[DB]
    first = db.value("SELECT starts_at FROM appointments WHERE practice_id = 1 AND therapist_id = 2 AND room_id IS NOT NULL"
                     " AND status = 'booked' AND starts_at > ? ORDER BY starts_at LIMIT 1", (today().isoformat(),))
    day = datetime.fromisoformat(first).astimezone(TZ).date()
    monday = day - timedelta(days=day.weekday())
    thursday = monday + timedelta(days=3)
    with practice_scope(1, 1):
        absences.save(db, {"user_id": 2, "kind": "training", "starts_on": thursday.isoformat(), "ends_on": thursday.isoformat(),
                           "start_time": None, "end_time": None, "note": None})
    await login(client, "therapie@demo.n-fizyo.ch")
    html = await (await client.get(f"/therapie/agenda?view=woche&d={monday}&who=2")).text()
    heads = re.findall(r'<div class="agenda-head"[^>]*>(.*?)</div>', html, flags=re.S)
    assert any("Weiterbildung" in h for h in heads), heads
    subs = re.findall(r'<span class="s"[^>]*>(.*?)</span>', html, flags=re.S)
    with_room = [s for s in subs if "Raum" in s]
    assert with_room and all(s.strip().startswith("Raum") for s in with_room), with_room[:3]
