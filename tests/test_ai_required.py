"""Lokale KI gehört zum Produkt: fehlt sie in einer echten Praxis, sehen Leitung und Therapie einen Hinweis."""
from __future__ import annotations

import pytest
from conftest import csrf_of

from nfizyo.app import create_app
from nfizyo.core.auth import create_user
from nfizyo.core.config import load_settings
from nfizyo.core.db import utcnow
from nfizyo.core.keys import DB


@pytest.fixture
def real_app(tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"r" * 32, demo=False, require_2fa=False)  # 2FA: test_mfa.py
    app = create_app(settings, start_workers=False, seed_demo=False)
    db = app[DB]
    pid = db.execute("INSERT INTO practices (name, slug, default_lang, created_at) VALUES (?,?,?,?)", ("Echt", "echt", "de", utcnow()))
    create_user(db, pid, "t@echt.invalid", "T. Echt", "therapist", "passwort-echt-1")
    create_user(db, pid, "e@echt.invalid", "E. Empfang", "reception", "passwort-echt-2")
    return app


async def _page(client, email, password, url="/therapie/heute"):
    r = await client.get("/login")
    await client.post("/login", data={"_csrf": csrf_of(await r.text()), "email": email, "password": password})
    r = await client.get(url)
    assert r.status == 200
    return await r.text()


async def test_banner_when_ai_missing(aiohttp_client, real_app):
    client = await aiohttp_client(real_app)
    text = await _page(client, "t@echt.invalid", "passwort-echt-1")
    assert "KI nicht bereit" in text and "Spracherkennung" in text
    client.session.cookie_jar.clear()
    text = await _page(client, "e@echt.invalid", "passwort-echt-2", "/empfang/heute")
    assert "KI nicht bereit" not in text  # Empfang braucht den Hinweis nicht


async def test_no_banner_in_demo(client):
    from conftest import login
    await login(client, "therapie@demo.n-fizyo.ch")
    r = await client.get("/therapie/heute")
    assert "KI nicht bereit" not in await r.text()


def test_waiting_dictations_are_retried(app):
    """Wenn die KI zurück ist (z. B. KI-Server nach Internet-Ausfall), werden wartende Diktate nachgeholt."""
    from nfizyo.core.db import utcnow
    from nfizyo.core.keys import AI, DB, JOBS
    from nfizyo.modules.dictation.views import retry_waiting

    db = app[DB]
    rid = db.execute("INSERT INTO recordings (practice_id, user_id, path, status, error, language, recorded_at, created_at, updated_at,"
                     " client_uuid) VALUES (1, 2, '/tmp/x.webm', 'failed', 'stt_unavailable', 'de', ?, ?, ?, 'retry-test')",
                     (utcnow(), utcnow(), utcnow()))
    assert retry_waiting(app) == 0  # KI noch nicht da
    app[AI].stt.available = lambda: True
    assert retry_waiting(app) >= 1
    assert app[JOBS].pending(1, "transcribe", rid)
