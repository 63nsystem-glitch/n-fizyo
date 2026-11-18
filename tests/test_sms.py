"""SMS: Testmodus versendet nichts, echter Versand über ASPSMS oder eCall (Praxis wählt), Wiederholung, Einstellungen."""
from __future__ import annotations

import asyncio

import pytest
from conftest import csrf_of, login

from nfizyo.app import create_app
from nfizyo.core import sms
from nfizyo.core.config import load_settings
from nfizyo.core.keys import DB, SMS


def test_normalize_phone():
    assert sms.normalize_phone("079 123 45 67") == "+41791234567"
    assert sms.normalize_phone("0041 79 123 45 67") == "+41791234567"
    assert sms.normalize_phone("+49 151 2345 6789") == "+4915123456789"
    assert sms.normalize_phone("12") is None
    assert sms.normalize_phone("") is None
    assert sms.clean_sender("Physio Balance Biel") == "PhysioBalan"


def test_log_mode_never_sends(app, monkeypatch):
    called = []
    monkeypatch.setattr(sms, "http_post_json", lambda *a, **k: called.append(a))
    db = app[DB]
    db.execute("UPDATE practices SET sms_provider = 'aspsms' WHERE id = 1")
    mid = app[SMS].send(1, "sms", "079 123 45 67", "Hallo")
    row = db.one("SELECT * FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "logged" and row["provider"] == "log" and not called


@pytest.fixture
def live_app(tmp_path):
    settings = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, sms_engine="live",
                             aspsms_userkey="KEY", aspsms_password="PW", ecall_username="u", ecall_password="p")
    return create_app(settings, start_workers=False)


async def test_aspsms_sends_and_keeps_credentials_out_of_db(live_app, monkeypatch):
    sent = []

    async def fake(url, payload, headers=None):
        sent.append((url, payload, headers))
        return 200, {"StatusCode": "1", "StatusInfo": "OK"}

    monkeypatch.setattr(sms, "http_post_json", fake)
    db = live_app[DB]
    sms.set_provider(db, 1, "aspsms", "Physio Demo")
    mid = live_app[SMS].send(1, "sms", "079 123 45 67", "Termin morgen 10:00")
    await asyncio.sleep(0.05)
    row = db.one("SELECT * FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "sent" and row["attempts"] == 1
    url, payload, _ = sent[0]
    assert url == sms.ASPSMS_URL and payload["Recipients"] == ["+41791234567"] and payload["Originator"] == "PhysioDemo"
    dump = "".join(str(tuple(r)) for r in db.query("SELECT * FROM outbox")) + "".join(str(tuple(r)) for r in db.query("SELECT * FROM practices"))
    assert "KEY" not in dump and "PW" not in dump


async def test_ecall_failure_retries_then_fails(live_app, monkeypatch):
    async def fake(url, payload, headers=None):
        assert headers["Authorization"].startswith("Basic ")
        assert payload["channel"] == "Sms" and payload["content"]["type"] == "Text"
        return 401, {"errorCode": "Unauthorized", "errorMessage": "bad"}

    monkeypatch.setattr(sms, "http_post_json", fake)
    db = live_app[DB]
    sms.set_provider(db, 1, "ecall", None)
    service = live_app[SMS]
    mid = service.send(1, "sms", "+41791234567", "x")
    await asyncio.sleep(0.05)
    row = db.one("SELECT * FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "queued" and row["attempts"] == 1 and row["next_try_at"] and "eCall 401" in row["error"]
    assert mid not in service.due_retries()  # wartet 1 Minute
    for _ in range(sms.MAX_ATTEMPTS):
        db.execute("UPDATE outbox SET next_try_at = NULL WHERE id = ?", (mid,))
        await service.deliver(mid)
    assert db.one("SELECT status FROM outbox WHERE id = ?", (mid,))["status"] == "failed"


async def test_ecall_success(live_app, monkeypatch):
    async def fake(url, payload, headers=None):
        return 200, {"messageId": "abc-123", "timestamp": "2026-10-28T10:00:00Z"}

    monkeypatch.setattr(sms, "http_post_json", fake)
    db = live_app[DB]
    sms.set_provider(db, 1, "ecall", None)
    mid = live_app[SMS].send(1, "sms", "+41791234567", "x")
    await asyncio.sleep(0.05)
    row = db.one("SELECT * FROM outbox WHERE id = ?", (mid,))
    assert row["status"] == "sent" and row["provider_ref"] == "abc-123"


async def test_settings_only_offers_configured_providers(client, app):
    csrf = await login(client, "leitung@demo.n-fizyo.ch")
    page = await client.get("/leitung/einstellungen/sms")
    html = await page.text()
    assert page.status == 200 and "Testmodus" in html
    r = await client.post("/leitung/einstellungen/sms", data={"_csrf": csrf_of(html), "provider": "ecall"}, allow_redirects=False)
    assert app[DB].value("SELECT sms_provider FROM practices WHERE id = 1") == "log"
    r = await client.post("/leitung/einstellungen/sms", data={"_csrf": csrf, "do": "test", "phone": "079 000 00 00"},
                          allow_redirects=False)
    assert r.status == 302
    assert app[DB].value("SELECT status FROM outbox ORDER BY id DESC LIMIT 1") == "logged"


async def test_outbox_list_is_per_practice(client, app, practice_b):
    app[SMS].send(practice_b["id"], "sms", "+41790000099", "B geheim")
    await login(client, "leitung@demo.n-fizyo.ch")
    html = await (await client.get("/leitung/einstellungen/sms")).text()
    assert "+4179…99" not in html
