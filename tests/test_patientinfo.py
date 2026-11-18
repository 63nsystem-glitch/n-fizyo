"""Information für Patient/innen: aus dem Diktat vorgeschlagen, von der Therapie freigegeben, übersetzt, in der App lesbar."""
from __future__ import annotations

import re

from conftest import csrf_of, login, pid_of

from nfizyo.ai.patientinfo import draft_rules, draft_with_llm
from nfizyo.core.auth import create_session
from nfizyo.core.keys import DB, JOBS


def test_rules_take_only_dictated_sentences():
    d = draft_rules("Flexion 100°. Zu Hause die Übungen zweimal täglich machen. Melden Sie sich, wenn das Knie heiss wird.", "de")
    assert "zweimal täglich" in d.home and "heiss" in d.contact and "Flexion" not in d.home + d.contact


def test_llm_with_invented_numbers_is_rejected():
    class FakeLLM:
        model = "fake"

        def generate(self, *a, **k):
            return '{"home": "Kühlen Sie 20 Minuten.", "contact": ""}'

    d = draft_with_llm("Zu Hause kühlen.", "de", FakeLLM())
    assert "patientinfo.llm_numbers_rejected" in d.warnings and "20" not in d.home


async def test_dictation_to_patient_app(client, app):
    db = app[DB]
    ayse = pid_of(app, "Ayşe")
    aid = db.value("SELECT id FROM appointments WHERE patient_id = ? AND status IN ('booked','arrived') ORDER BY starts_at", (ayse,))
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    text = "Weniger Nachtschmerz. Zu Hause die Übungen zweimal täglich machen. Sie soll sich melden, wenn das Bein taub wird."
    r = await client.post("/therapie/diktat/text", data={"_csrf": csrf, "text": text, "patient_id": ayse, "appointment_id": aid},
                          allow_redirects=False)
    review = r.headers["Location"]
    await app[JOBS].run_all(app)
    page = await (await client.get(review)).text()
    assert 'name="pi_home"' in page and "zweimal täglich" in page
    form = {"_csrf": csrf_of(page), "pi_home": "Übungen zweimal täglich.", "pi_contact": "Melden, wenn das Bein taub wird."}
    for sec in ("befund", "behandlung", "beurteilung", "plan"):
        m = re.search(rf'name="{sec}"[^>]*>([^<]*)<', page)
        form[sec] = m.group(1) if m else ""
    r = await client.post(review + "/freigeben", data={**form, "pi_send": "1"}, allow_redirects=False)
    assert r.status == 302
    info = db.one("SELECT * FROM patient_infos WHERE recording_id IS NOT NULL")
    assert info["status"] == "approved" and info["home"] == "Übungen zweimal täglich." and info["target_lang"] == "tr"
    assert db.value("SELECT status FROM appointments WHERE id = ?", (aid,)) == "done"
    await app[JOBS].run_all(app)
    token, _ = create_session(db, "patient", patient_id=ayse, practice_id=1)
    client.session.cookie_jar.clear()
    client.session.cookie_jar.update_cookies({"nf_p": token})
    r = await client.get("/p/info")
    text = await r.text()
    assert "Übungen zweimal täglich." in text and "data-speak" in text and "144" in text
    assert db.value("SELECT read_at FROM patient_infos WHERE id = ?", (info["id"],)) is not None


async def test_unapproved_info_is_never_shown(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    db.execute("INSERT INTO patient_infos (practice_id, patient_id, author_id, status, source_lang, home, created_at, updated_at)"
               " VALUES (1, ?, 2, 'draft', 'de', 'ENTWURF-GEHEIM', 'x', 'x')", (lea,))
    token, _ = create_session(db, "patient", patient_id=lea, practice_id=1)
    client.session.cookie_jar.update_cookies({"nf_p": token})
    for url in ("/p/info", "/p/heute"):
        assert "ENTWURF-GEHEIM" not in await (await client.get(url)).text()


async def test_therapist_writes_info_without_dictation(client, app):
    db = app[DB]
    lea = pid_of(app, "Lea")
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.post(f"/therapie/patienten/{lea}/info", data={"_csrf": csrf, "home": "Knie hochlagern.", "contact": ""},
                          allow_redirects=False)
    assert r.status == 302
    assert db.value("SELECT status FROM patient_infos WHERE patient_id = ? AND home = 'Knie hochlagern.'", (lea,)) == "approved"
