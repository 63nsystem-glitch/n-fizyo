"""Video-Sitzung über den eigenen Jitsi-Server: Schlüssel, Raum, SMS-Link, Einwilligung, Zeitfenster, Zugriff."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import csrf_of, login, pid_of

from nfizyo.app import create_app
from nfizyo.core.auth import create_session
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB
from nfizyo.modules.video import jitsi

SECRET = "t" * 40
SERVER = "https://video.example.invalid"


@pytest.fixture
def vapp(tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, video_url=SERVER, video_secret=SECRET)
    return create_app(settings, start_workers=False)


@pytest.fixture
async def vclient(aiohttp_client, vapp):
    return await aiohttp_client(vapp)


def _video_appt(app) -> int:
    return app[DB].value("SELECT appointment_id FROM video_rooms WHERE patient_id = ?", (pid_of(app, "Ayşe"),))


def _move_to_now(app, aid: int, minutes: int = 10) -> None:
    start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=minutes)
    app[DB].execute("UPDATE appointments SET starts_at = ?, ends_at = ? WHERE id = ?",
                    (start.isoformat(), (start + timedelta(minutes=30)).isoformat(), aid))


def test_token_is_signed_and_limited():
    jwt = jitsi.token(SECRET, "nfizyo", SERVER, "nf-abc", "T. Demo", moderator=True, nbf=100, exp=200)
    data = jitsi.decode(SECRET, jwt)
    assert data["room"] == "nf-abc" and data["iss"] == "nfizyo" and data["aud"] == "jitsi" and data["sub"] == "video.example.invalid"
    assert data["context"]["user"] == {"name": "T. Demo", "moderator": True} and (data["nbf"], data["exp"]) == (100, 200)
    assert jitsi.decode("falsch", jwt) is None
    head, body, sig = jwt.split(".")
    assert jitsi.decode(SECRET, f"{head}.{body}x.{sig}") is None


async def test_therapist_sets_up_and_starts(vclient, vapp):
    db = vapp[DB]
    aid = _video_appt(vapp)
    csrf = await login(vclient, "therapie@demo.n-fizyo.ch")
    appt = await (await vclient.get(f"/therapie/agenda/termine/{aid}")).text()
    assert f"/therapie/video/{aid}" in appt
    page = await (await vclient.get(f"/therapie/video/{aid}")).text()
    assert "per SMS gesendet" in page and "noch nicht eingerichtet" not in page
    r = await vclient.post(f"/therapie/video/{aid}", data={"_csrf": csrf, "action": "sms"}, allow_redirects=False)
    assert r.status == 302
    body = db.value("SELECT body FROM outbox WHERE body LIKE '%/p/video/%' ORDER BY id DESC LIMIT 1")
    assert "görüntülü randevunuz" in body and "144" in body
    token = re.search(r"/p/video/([A-Za-z0-9_-]+)", body).group(1)
    assert db.value("SELECT COUNT(*) FROM video_rooms WHERE link_hash = ?", (token,)) == 0          # nur der Hash
    # zu früh (Termin morgen)
    r = await vclient.get(f"/therapie/video/{aid}/start", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].startswith("/therapie/video/")
    _move_to_now(vapp, aid)
    r = await vclient.get(f"/therapie/video/{aid}/start", allow_redirects=False)
    loc = r.headers["Location"]
    room = db.value("SELECT room FROM video_rooms WHERE appointment_id = ?", (aid,))
    assert loc.startswith(f"{SERVER}/{room}?jwt=")
    data = jitsi.decode(SECRET, parse_qs(urlparse(loc).query)["jwt"][0])
    assert data["context"]["user"]["moderator"] is True and data["room"] == room
    end = datetime.fromisoformat(db.value("SELECT ends_at FROM appointments WHERE id = ?", (aid,)))
    assert data["exp"] == int((end + timedelta(minutes=30)).timestamp())
    assert db.value("SELECT therapist_joined_at FROM video_rooms WHERE appointment_id = ?", (aid,))

    # Patientin über den SMS-Link: Einwilligung, dann in den Raum (ohne Moderatorrechte)
    vclient.session.cookie_jar.clear()
    page = await (await vclient.get(f"/p/video/{token}")).text()
    assert "Katılmadan önce" in page and "144" in page
    r = await vclient.post(f"/p/video/{token}", data={"_csrf": re.search(r'name="_csrf" value="([^"]+)"', page).group(1)},
                           allow_redirects=False)
    assert r.headers["Location"] == f"/p/video/{token}"                                         # ohne Einwilligung nicht
    r = await vclient.post(f"/p/video/{token}", data={"_csrf": re.search(r'name="_csrf" value="([^"]+)"', page).group(1), "consent": "1"},
                           allow_redirects=False)
    loc = r.headers["Location"]
    data = jitsi.decode(SECRET, parse_qs(urlparse(loc).query)["jwt"][0])
    assert data["context"]["user"] == {"name": "Ayşe Ö.", "moderator": False}
    assert "%C3%96rnek" not in loc and "Örnek" not in loc                                         # nur Initiale
    assert db.value("SELECT via FROM video_consents WHERE patient_id = ?", (pid_of(vapp, "Ayşe"),)) == "link"
    assert db.value("SELECT patient_joined_at FROM video_rooms WHERE appointment_id = ?", (aid,))
    assert (await vclient.get("/p/video/" + "y" * 32)).status == 404


async def test_patient_early_and_app_card(vclient, vapp):
    aid = _video_appt(vapp)
    ayse = pid_of(vapp, "Ayşe")
    token, _ = create_session(vapp[DB], "patient", patient_id=ayse, practice_id=1)
    vclient.session.cookie_jar.update_cookies({"nf_p": token})
    _move_to_now(vapp, aid, minutes=120)
    home = await (await vclient.get("/p/heute")).text()
    assert "Görüntülü randevu" in home and f"/p/video/termin/{aid}" in home
    page = await (await vclient.get(f"/p/video/termin/{aid}")).text()
    assert "itibarıyla katılabilirsiniz" in page
    csrf = csrf_of(page)
    r = await vclient.post(f"/p/video/termin/{aid}", data={"_csrf": csrf, "consent": "1"}, allow_redirects=False)
    assert r.headers["Location"] == f"/p/video/termin/{aid}"                                    # zu früh: kein Raum
    assert vapp[DB].value("SELECT via FROM video_consents WHERE patient_id = ?", (ayse,)) == "app"
    _move_to_now(vapp, aid, minutes=5)
    r = await vclient.post(f"/p/video/termin/{aid}", data={"_csrf": csrf}, allow_redirects=False)
    assert r.headers["Location"].startswith(SERVER)
    # fremder Termin
    other = vapp[DB].value("SELECT id FROM appointments WHERE patient_id = ? LIMIT 1", (pid_of(vapp, "Lea"),))
    assert (await vclient.get(f"/p/video/termin/{other}")).status == 404


async def test_without_server_nothing_leaves(client, app):
    aid = _video_appt(app)
    _move_to_now(app, aid)
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/video/{aid}")).text()
    assert "noch nicht eingerichtet" in page
    for technical in ("NFIZYO_", ".env", "docs/", "install-jitsi"):                           # keine Technik für die Therapie
        assert technical not in page
    today = await (await client.get("/therapie/heute")).text()
    assert "/therapie/video/" not in today                                                    # kein Video-Knopf im Tagesplan
    r = await client.get(f"/therapie/video/{aid}/start", allow_redirects=False)
    assert r.headers["Location"].startswith("/therapie/video/")
    assert csrf


async def test_access_rules(client, app):
    db = app[DB]
    sandra = pid_of(app, "Sandra")
    sandra_appt = db.value("SELECT a.id FROM appointments a WHERE a.patient_id = ? AND a.status = 'booked' LIMIT 1", (sandra,))
    if sandra_appt is None:              # werktags ist Sandras Termin von heute schon erledigt → einen gebuchten anlegen
        from nfizyo.core.tenant import practice_scope
        from nfizyo.core.timeutil import local_dt
        from nfizyo.modules.agenda import repo as arepo

        last = db.one("SELECT therapist_id, location_id FROM appointments WHERE patient_id = ? ORDER BY starts_at DESC LIMIT 1", (sandra,))
        with practice_scope(1):
            sandra_appt = arepo.create(db, therapist_id=last["therapist_id"], start=local_dt(datetime.now().date() + timedelta(days=3), "07:15"),
                                       minutes=30, patient_id=sandra, location_id=last["location_id"])
    csrf = await login(client, "therapie@demo.n-fizyo.ch")
    assert (await client.get(f"/therapie/video/{sandra_appt}")).status == 404
    r = await client.post(f"/therapie/video/{sandra_appt}", data={"_csrf": csrf, "action": "setup"}, allow_redirects=False)
    assert r.status == 404 and db.value("SELECT COUNT(*) FROM video_rooms WHERE appointment_id = ?", (sandra_appt,)) == 0
    csrf = await login(client, "empfang@demo.n-fizyo.ch")
    r = await client.post(f"/empfang/video/{sandra_appt}", data={"_csrf": csrf, "action": "setup"}, allow_redirects=False)
    assert r.status == 302 and db.value("SELECT COUNT(*) FROM video_rooms WHERE appointment_id = ?", (sandra_appt,)) == 1
    appt = await (await client.get(f"/empfang/agenda/termine/{sandra_appt}")).text()
    assert f"/empfang/video/{sandra_appt}" not in appt                                         # ohne Video-Server kein Video-Knopf
    assert (await client.get(f"/empfang/video/{sandra_appt}/start")).status == 404               # starten nur die Therapie


async def test_server_check_in_settings(aiohttp_client, aiohttp_server, tmp_path):
    """Leitung › Einstellungen › Video: prüft den eigenen Server (erreichbar, Datenschutz-Konfiguration, keine Gäste)."""
    from aiohttp import web

    good = "var config = {};\nconfig.disableThirdPartyRequests = true;\nconfig.p2p = Object.assign({}, config.p2p || {}, { enabled: false });"

    state = {"body": good}

    async def config_js(request):
        return web.Response(text=state["body"], content_type="application/javascript")

    fake = web.Application()
    fake.router.add_get("/config.js", config_js)
    server = await aiohttp_server(fake)
    url = str(server.make_url("")).rstrip("/")
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, video_url=url, video_secret=SECRET)
    client = await aiohttp_client(create_app(settings, start_workers=False))
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/video")).text()
    assert url in page and "eingetragen (wird nie angezeigt)" in page and SECRET not in page
    page = await (await client.post("/leitung/einstellungen/video", data={"_csrf": csrf})).text()
    assert "Server erreichbar" in page and page.count('class="rx-line info"') == 5 and page.count('class="rx-line act"') == 1  # nur https fehlt (Testserver)
    state["body"] = good + "\nconfig.hosts.anonymousdomain = 'guest.meet.jitsi';"
    page = await (await client.post("/leitung/einstellungen/video", data={"_csrf": csrf})).text()
    assert page.count('class="rx-line act"') == 2                                              # Gäste erlaubt → rot
