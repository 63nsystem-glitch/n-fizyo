"""Datenschutzerklärung in der Patienten-App (Informationspflicht nDSG): öffentlich, in allen App-Sprachen, mit der Praxis."""
from __future__ import annotations

from conftest import pid_of

from nfizyo.core.auth import create_session
from nfizyo.core.i18n import PATIENT_LANGS
from nfizyo.core.keys import DB


async def test_privacy_page_public_and_with_practice(client, app):
    page = await (await client.get("/p/datenschutz")).text()
    assert "Verantwortlich ist die Praxis, die Sie in die App eingeladen hat" in page and "EDÖB" in page and "144" in page
    page = await (await client.get("/p/datenschutz?praxis=demo")).text()
    assert "Physio Demo Seeland" in page and "praxis@demo.n-fizyo.ch" in page
    assert "Lea" not in page and "Muster" not in page
    token, _ = create_session(app[DB], "patient", patient_id=pid_of(app, "Ayşe"), practice_id=1)
    client.session.cookie_jar.update_cookies({"nf_p": token})
    page = await (await client.get("/p/datenschutz")).text()
    assert "Physio Demo Seeland" in page and "Veri koruma" in page and "yurt dışına aktarılmaz" in page
    assert "/p/datenschutz" in await (await client.get("/p/profil")).text()


async def test_privacy_page_in_every_app_language(client, app):
    for lang in PATIENT_LANGS:
        client.session.cookie_jar.update_cookies({"nf_lang": lang})
        r = await client.get("/p/datenschutz")
        assert r.status == 200, lang
        assert "p.privacy." not in await r.text(), lang
