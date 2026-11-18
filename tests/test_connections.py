"""python -m nfizyo check-connections: liest und meldet an, sendet ohne Zusatz nichts, zeigt nie Zugangsdaten."""
from __future__ import annotations

import socket

from aiohttp import web

from nfizyo import connections
from nfizyo.app import create_app
from nfizyo.core.config import load_settings

SECRET_PW = "geheim-" + "p" * 20


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_all_off_is_ok(tmp_path):
    app = create_app(load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=False), start_workers=False, seed_demo=False)
    checks = await connections.run_all(app)
    assert not [c for c in checks if c.status == connections.FAIL]
    assert {c.area for c in checks} >= {"SMS", "E-Mail", "HIN", "MediData", "Video", "bexio"}


async def test_failures_and_secrets(tmp_path, monkeypatch, aiohttp_server):
    from nfizyo.core import sms

    calls = []

    async def fake_post(url, payload, headers=None):
        calls.append((url, payload))
        if url.endswith("/CheckCredits"):
            return 200, {"StatusCode": "1", "StatusInfo": "OK", "Credits": "123.5"}
        return 200, {"StatusCode": "1"}

    monkeypatch.setattr(sms, "http_post_json", fake_post)
    bexio_app = web.Application()
    async def accounts(request):
        return web.json_response({"error_code": 401, "message": "Unauthorized"}, status=401)

    bexio_app.router.add_get("/2.0/accounts", accounts)
    server = await aiohttp_server(bexio_app)
    closed = _free_port()
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=False, sms_engine="live", aspsms_userkey="KEY",
                             aspsms_password=SECRET_PW, smtp_host="127.0.0.1", smtp_port=closed, smtp_from="praxis@example.ch",
                             smtp_password=SECRET_PW, bexio_token="tok-" + SECRET_PW, bexio_api=str(server.make_url("")).rstrip("/"))
    app = create_app(settings, start_workers=False, seed_demo=False)
    checks = await connections.run_all(app)
    by = {c.area: c for c in checks}
    assert by["SMS ASPSMS"].status == connections.OK and "123.5" in by["SMS ASPSMS"].detail
    assert [u for u, _ in calls] == ["https://json.aspsms.com/CheckCredits"]               # ohne --sms keine SMS
    assert by["E-Mail SMTP"].status == connections.FAIL
    assert by["bexio"].status == connections.FAIL and "401" in by["bexio"].detail
    text = connections.render(checks)
    assert SECRET_PW not in text and "Fehler" in text
    # mit --sms: genau eine SMS
    checks = await connections.check_sms(settings, "079 000 00 01")
    assert any(c.area.startswith("SMS senden") and c.status == connections.OK for c in checks)
    assert calls[-1][0] == sms.ASPSMS_URL and calls[-1][1]["Recipients"] == ["+41790000001"]


def test_cli_registered():
    import pytest

    from nfizyo.__main__ import main

    with pytest.raises(SystemExit) as e:
        main(["check-connections", "--help"])
    assert e.value.code == 0


async def test_ai_server_in_connection_check(tmp_path):
    """Pilot: der KI-Server Schweiz gehört in den Verbindungstest – erreichbar, Token gültig, Bausteine bereit; Token nie sichtbar."""
    from aiohttp.test_utils import TestServer

    from nfizyo.ai.translate import TranslationResult
    from nfizyo.ai.types import Transcript
    from nfizyo.aiserver.app import Engines, create_ai_app
    from nfizyo.aiserver.store import Store

    class Eng:
        def available(self):
            return True

        def transcribe(self, path, language=None):
            return Transcript(text="", language="de", engine="fake", duration_sec=1.0)

        def generate(self, *a, **k):
            return "x"

        def translate(self, text, source, target):
            return TranslationResult("Bonjour", "fake")

    store = Store(tmp_path / "ai.sqlite3")
    _, token = store.add_client("Physio Test")
    server = TestServer(create_ai_app(store, Engines(Eng(), Eng(), Eng(), "w", "l", "t"), tmp_path / "aitmp"))
    await server.start_server()
    try:
        url = str(server.make_url("")).rstrip("/")

        def app_with(tok, server_url=url):
            s = load_settings(None, data_dir=tmp_path / tok[-6:], secret_key=b"t" * 32, demo=False, ai_mode="remote",
                              ai_server_url=server_url, ai_token=tok, allow_remote_ai=True)
            return create_app(s, start_workers=False, seed_demo=False)

        by = {c.area: c for c in await connections.run_all(app_with(token), ai_test=True)}
        assert by["KI-Server"].status == connections.OK and "Zugang gültig" in by["KI-Server"].detail
        assert all(by[f"KI-Server: {p}"].status == connections.OK for p in ("Spracherkennung", "Sprachmodell", "Übersetzung"))
        assert by["KI-Test: Übersetzung"].status == connections.OK and "Bonjour" in by["KI-Test: Übersetzung"].detail
        assert by["KI-Test: Spracherkennung"].status == connections.OK
        assert store.conn.execute("SELECT COUNT(*) FROM usage WHERE op = 'stt'").fetchone()[0] == 1

        wrong = await connections.run_all(app_with("nfa_falsch-" + SECRET_PW))
        ki = [c for c in wrong if c.area == "KI-Server"][0]
        assert ki.status == connections.FAIL and "Token prüfen" in ki.detail
        assert SECRET_PW not in connections.render(wrong)

        down = await connections.run_all(app_with(token, f"http://127.0.0.1:{_free_port()}"))
        ki = [c for c in down if c.area == "KI-Server"][0]
        assert ki.status == connections.FAIL and "nicht erreichbar" in ki.detail
    finally:
        await server.close()
