"""KI-Server im Pilotbetrieb: hängender, beschäftigter oder ablehnender Server legt die Praxis-Software nie lahm
und kein Diktat bleibt endlos «wird nachgeholt»."""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
from aiohttp import FormData

from nfizyo.ai import remote as remote_mod
from nfizyo.ai.remote import RemoteAIClient, RemoteAIUnreachable, RemoteSTT
from nfizyo.ai.statuscache import CachedStatus
from nfizyo.ai.stt import STTRejected
from nfizyo.ai.translate import TranslationResult
from nfizyo.ai.types import Transcript
from nfizyo.aiserver.app import Engines, create_ai_app
from nfizyo.aiserver.store import Store

from conftest import csrf_of, login


class SlowSTT:
    def __init__(self):
        self.gate = threading.Event()

    def available(self):
        return True

    def transcribe(self, path, language=None):
        if path.read_bytes().startswith(b"BAD"):
            raise ValueError("Invalid data found when processing input")
        self.gate.wait(10)
        return Transcript(text="Flexion 100 Grad", language="de", engine="fake", duration_sec=2.0)


class Ok:
    def available(self):
        return True

    def generate(self, *a, **k):
        return "x"

    def translate(self, text, source, target):
        return TranslationResult(text, "fake")


@pytest.fixture
def ai_server(tmp_path):
    store = Store(tmp_path / "ai.sqlite3")
    _, token = store.add_client("Physio Test")
    stt = SlowSTT()
    return create_ai_app(store, Engines(stt, Ok(), Ok(), "w", "l", "t"), tmp_path / "tmp"), token, stt


async def test_status_answers_while_the_practice_has_tasks_running(aiohttp_client, ai_server):
    app, token, stt = ai_server
    client = await aiohttp_client(app)
    auth = {"Authorization": f"Bearer {token}"}

    async def transcribe():
        fd = FormData()
        fd.add_field("audio", b"RIFF", filename="a.webm")
        return (await client.post("/v1/transcribe", data=fd, headers=auth)).status

    jobs = [asyncio.create_task(transcribe()) for _ in range(3)]   # alle Plätze dieser Praxis belegt
    await asyncio.sleep(0.3)
    r = await asyncio.wait_for(client.get("/v1/status", headers=auth), 2)   # früher: wartete hinter den Aufgaben → «nicht erreichbar»
    st = await r.json()
    assert st["stt"] is True and st["max_audio"] > 0
    stt.gate.set()
    assert await asyncio.gather(*jobs) == [200, 200, 200]


async def test_unreadable_or_too_long_recording_is_not_sent_again_and_again(ai_server, tmp_path):
    from aiohttp.test_utils import TestServer

    app, token, stt = ai_server
    stt.gate.set()
    server = TestServer(app)
    await server.start_server()
    try:
        client = RemoteAIClient(str(server.make_url("")).rstrip("/"), token)
        bad = tmp_path / "kaputt.webm"
        bad.write_bytes(b"BADDATA")
        with pytest.raises(STTRejected) as exc:
            await asyncio.to_thread(RemoteSTT(client).transcribe, bad, "de")
        assert exc.value.reason == "unknown"
        assert (await asyncio.to_thread(client.status, 0))["stt"] is True   # Server gilt weiter als bereit
        client._status.set({"stt": True, "llm": True, "translate": True, "max_audio": 10})
        big = tmp_path / "lang.webm"
        big.write_bytes(b"x" * 11)
        with pytest.raises(STTRejected) as exc:
            await asyncio.to_thread(RemoteSTT(client).transcribe, big, "de")
        assert exc.value.reason == "too_large"
    finally:
        await server.close()


async def test_dictation_rejected_by_ai_server_asks_for_text_instead_of_waiting(client, app, tmp_path):
    from nfizyo.core.keys import AI, DB, JOBS
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.dictation import repo, views

    db, ai = app[DB], app[AI]
    ai.simulate_demo = False
    uid = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"x" * 100)
    with practice_scope(1, uid):
        rid = repo.create(db, client_uuid="r1", patient_id=1, appointment_id=None, language="de", recorded_at=None, path=str(audio))

    def rejected(*a, **k):
        raise STTRejected("too_large")
    ai.can_transcribe = lambda: True
    ai.transcribe = rejected
    app[JOBS].enqueue(1, "transcribe", rid)
    await app[JOBS].run_all(app)
    rec = db.one("SELECT status, error FROM recordings WHERE id = ?", (rid,))
    assert (rec["status"], rec["error"]) == ("failed", "too_large")
    assert views.retry_waiting(app) == 0   # wird nicht automatisch nochmals geschickt
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/diktat/{rid}")).text()
    assert "zu lang für die Spracherkennung" in page and "<textarea" in page


def _hung_server():
    """Nimmt Verbindungen an, antwortet aber nie."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(50)
    return s


async def test_hanging_ai_server_never_freezes_the_practice_software(tmp_path):
    from nfizyo.ai.service import AIServices
    from nfizyo.core.config import load_settings

    hung = _hung_server()
    try:
        s = load_settings(None, data_dir=tmp_path, secret_key=b"x" * 32, demo=False, ai_mode="remote",
                          ai_server_url=f"http://127.0.0.1:{hung.getsockname()[1]}", ai_token="nfa_x", allow_remote_ai=True)
        ai = AIServices(s)
        t = time.monotonic()
        # so rufen es die Seiten im Webserver auf (Event-Loop): sofort zurück, Abfrage läuft im Hintergrund
        assert ai.can_transcribe() is False and ai.can_translate() is False
        assert ai.status()["checking"] is True
        assert ai.missing() == []          # noch kein «KI fehlt»-Hinweis, solange nur geprüft wird
        assert time.monotonic() - t < 0.5
    finally:
        hung.close()


async def test_status_cache_waits_in_threads_but_not_on_the_event_loop():
    gate = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        gate.wait(5)
        return {"ok": True}

    cache = CachedStatus(fetch, {"ok": False})
    t = time.monotonic()
    assert cache.get(30) == {"ok": False, "checking": True}
    assert cache.get(30)["checking"] and time.monotonic() - t < 0.3   # nur eine Abfrage gleichzeitig
    gate.set()
    assert await asyncio.to_thread(cache.get, 30) == {"ok": True}   # im Thread: wartet auf das Ergebnis
    assert cache.get(30) == {"ok": True} and len(calls) == 1


def test_unreachable_server_gives_up_after_a_short_connect_timeout(monkeypatch):
    monkeypatch.setattr(remote_mod, "CONNECT_TIMEOUT", 0.5)
    full = socket.socket()
    full.bind(("127.0.0.1", 0))
    full.listen(0)
    fillers = []
    try:
        for _ in range(4):   # Warteschlange voll: weitere Verbindungen bleiben im Aufbau hängen (wie ein Server hinter einer Firewall)
            x = socket.socket()
            x.setblocking(False)
            try:
                x.connect(("127.0.0.1", full.getsockname()[1]))
            except BlockingIOError:
                pass
            fillers.append(x)
        client = RemoteAIClient(f"http://127.0.0.1:{full.getsockname()[1]}", "nfa_x")
        client._status.set({"stt": True, "llm": True, "translate": True})
        t = time.monotonic()
        with pytest.raises(RemoteAIUnreachable):
            client.post_json("/v1/generate", {"prompt": "x"}, timeout=300)
        assert time.monotonic() - t < 5
        assert client.status()["stt"] is False   # sofort als «nicht erreichbar» angezeigt
    finally:
        for x in fillers:
            x.close()
        full.close()


async def test_report_translation_that_fails_is_not_announced_as_done(client, app):
    from nfizyo.core.keys import AI, DB
    from nfizyo.core.tenant import practice_scope
    from nfizyo.modules.reports import repo

    db, ai = app[DB], app[AI]
    uid = db.value("SELECT id FROM users WHERE email = 'therapie@demo.n-fizyo.ch'")
    with practice_scope(1, uid):
        rid = repo.create(db, 1, kind="progress", lang="fr", referrer_id=None, prescription_id=None, subject="Bericht",
                          salutation="", sections={"assessment": "Gute Fortschritte, Flexion 100 Grad.", "recommendation": "Weiter 6x."})
    before = db.one("SELECT assessment FROM reports WHERE id = ?", (rid,))["assessment"]
    ai.can_translate = lambda: True
    ai.translate = lambda *a, **k: TranslationResult(None, "none")
    await login(client, "therapie@demo.n-fizyo.ch")
    page = await (await client.get(f"/therapie/berichte/{rid}")).text()
    r = await client.post(f"/therapie/berichte/{rid}/uebersetzen", data={"_csrf": csrf_of(page)})
    text = await r.text()
    assert "Übersetzung im Moment nicht möglich" in text and "Maschinell übersetzt" not in text
    assert db.one("SELECT assessment FROM reports WHERE id = ?", (rid,))["assessment"] == before


async def test_ai_settings_page_in_remote_mode_names_the_server_and_says_unreachable(aiohttp_client, tmp_path):
    from nfizyo.app import create_app
    from nfizyo.core.config import load_settings

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()   # niemand hört zu: Verbindung wird sofort abgelehnt
    s = load_settings(None, data_dir=tmp_path, secret_key=b"t" * 32, demo=True, ai_mode="remote",
                      ai_server_url=f"http://127.0.0.1:{port}", ai_token="nfa_x", allow_remote_ai=True)
    from nfizyo.core.keys import AI, DB

    app = create_app(s, start_workers=False)
    app[AI].simulate_demo = False   # echte Praxis: keine Beispieltexte
    await asyncio.to_thread(app[AI].status)   # erste Abfrage abwarten (Verbindung abgelehnt)
    client = await aiohttp_client(app)
    await login(client, "leitung@demo.n-fizyo.ch")
    page = await (await client.get("/leitung/einstellungen/ki")).text()
    assert "KI über den KI-Server Schweiz" in page and "Lokale KI auf dem Praxis-Server" not in page
    assert "nicht erreichbar" in page and "Diktate bleiben gespeichert" in page
    rows = page.split("Spracherkennung (Diktat)")[1].split("Texterkennung (Scans)")[0]
    assert "nicht eingerichtet" not in rows   # eingerichtet, nur gerade nicht erreichbar

    # Datenweg ehrlich: nicht «lokale KI», sondern KI-Server Schweiz; Diktieren geht weiter (wird später verarbeitet)
    from nfizyo.core.keys import I18N
    tr = client.app[I18N].t
    await login(client, "therapie@demo.n-fizyo.ch")
    today = await (await client.get("/therapie/heute")).text()
    assert tr("de", "ai.remote") in today and tr("de", "ai.local") not in today
    quick = await (await client.get("/therapie/diktat")).text()
    assert "im Moment nicht erreichbar" in quick and "noch nicht eingerichtet" not in quick
    aid = client.app[DB].value("SELECT id FROM appointments WHERE practice_id = 1 AND patient_id IS NOT NULL ORDER BY id LIMIT 1")
    rec = await (await client.get(f"/therapie/diktat/termin/{aid}")).text()
    assert "verschlüsselt an den KI-Server Schweiz" in rec and "lokale KI" not in rec
    banner = await (await client.get("/therapie/patienten")).text()
    assert "Lokale KI" not in banner
    for lang in ("de", "fr", "it", "en", "es", "pt", "tr", "sq", "bks", "uk", "ar"):
        assert tr(lang, "p.profile.privacy_text_remote") != "p.profile.privacy_text_remote"
        assert tr(lang, "p.invite.consent_translation_remote") != "p.invite.consent_translation_remote"


def test_device_queue_does_not_resend_rejected_recordings_every_30_seconds():
    """Geprüft im echten Browser (Playwright, Testmikrofon): 415/413/400 → «nicht gesendet» mit Löschen, kein neuer Versuch;
    Aufnahme mit 48 kbit/s; Uhrzeit 18:39 statt «6:38:» bei englisch eingestelltem Gerät."""
    from pathlib import Path

    js = (Path(__file__).parents[1] / "nfizyo/web/static/js/dictation.js").read_text(encoding="utf-8")
    assert "e.permanent = r.status" in js and "if (item.failed || !mine(item)) return" in js and "data-drop" in js
    assert "audioBitsPerSecond: 48000" in js and "toLocaleTimeString" not in js
