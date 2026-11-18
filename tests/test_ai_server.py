"""KI-Server (Schweiz) für Praxen ohne Grafikkarte: Zugang nur mit Token, nichts wird gespeichert, Box spricht nur HTTPS."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestServer

from nfizyo.ai.remote import RemoteAIClient, RemoteLLM, RemoteSTT, RemoteTranslator, check_server_url
from nfizyo.ai.translate import TranslationResult
from nfizyo.ai.types import Transcript
from nfizyo.aiserver.app import Engines, create_ai_app
from nfizyo.aiserver.store import Store


class FakeSTT:
    seen: list = []

    def available(self):
        return True

    def transcribe(self, path, language=None):
        assert path.exists()
        self.seen.append(path)
        return Transcript(text="Flexion 105 Grad", language=language or "de", engine="fake", duration_sec=3.0)


class FakeLLM:
    def available(self):
        return True

    def generate(self, prompt, system="", json_mode=False, temperature=0.0):
        return "{}" if json_mode else "Entwurf"


class FakeTranslator:
    def available(self):
        return True

    def translate(self, text, source, target):
        return TranslationResult("Merhaba" if target == "tr" else text, "fake")


@pytest.fixture
def ai(tmp_path):
    store = Store(tmp_path / "ai.sqlite3")
    _, token = store.add_client("Physio Test")
    app = create_ai_app(store, Engines(FakeSTT(), FakeLLM(), FakeTranslator(), "whisper-x", "llm-x", "tr-x"), tmp_path / "tmp")
    return app, store, token, tmp_path / "tmp"


async def test_requires_token(aiohttp_client, ai):
    app, store, token, _ = ai
    client = await aiohttp_client(app)
    assert (await client.get("/v1/status")).status == 401
    assert (await client.get("/v1/status", headers={"Authorization": "Bearer falsch"})).status == 401
    r = await client.get("/v1/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200 and (await r.json())["stt"] is True
    store.set_active(1, False)
    assert (await client.get("/v1/status", headers={"Authorization": f"Bearer {token}"})).status == 401


async def test_audio_is_deleted_and_no_content_logged(aiohttp_client, ai):
    app, store, token, tmp = ai
    client = await aiohttp_client(app)
    fd = FormData()
    fd.add_field("language", "de")
    fd.add_field("audio", b"RIFFdemo", filename="a.webm")
    r = await client.post("/v1/transcribe", data=fd, headers={"Authorization": f"Bearer {token}"})
    assert (await r.json())["text"] == "Flexion 105 Grad"
    assert not list(tmp.iterdir())
    assert all(not p.exists() for p in FakeSTT.seen)
    cols = [c[1] for c in store.conn.execute("PRAGMA table_info(usage)")]
    assert "text" not in cols and "prompt" not in cols
    assert store.conn.execute("SELECT op, ok FROM usage").fetchone()["op"] == "stt"


def test_box_accepts_only_https():
    with pytest.raises(ValueError):
        check_server_url("http://ai.example.ch")
    check_server_url("https://ai.example.ch")
    with pytest.raises(ValueError):
        RemoteAIClient("https://ai.example.ch", "")
    assert "nfa_" not in repr(RemoteAIClient("https://ai.example.ch", "nfa_geheim"))


async def test_box_uses_remote_server(ai, tmp_path):
    app, _, token, _ = ai
    server = TestServer(app)
    await server.start_server()
    try:
        url = str(server.make_url("")).rstrip("/")
        client = RemoteAIClient(url, token)
        audio = tmp_path / "diktat.webm"
        audio.write_bytes(b"RIFFdemo")
        t = await asyncio.to_thread(RemoteSTT(client).transcribe, audio, "de")
        assert t.text == "Flexion 105 Grad" and t.engine.startswith("remote:")
        assert await asyncio.to_thread(RemoteLLM(client).generate, "Notiz", "", False) == "Entwurf"
        res = await asyncio.to_thread(RemoteTranslator(client).translate, "Guten Tag", "de", "tr")
        assert res.text == "Merhaba"
        assert await asyncio.to_thread(RemoteSTT(client).available)
        bad = RemoteAIClient(url, "nfa_falsch")
        assert not await asyncio.to_thread(RemoteSTT(bad).available)
    finally:
        await server.close()


def test_services_switch_to_remote(tmp_path):
    from nfizyo.ai.service import AIServices
    from nfizyo.core.config import load_settings

    s = load_settings(None, data_dir=tmp_path, secret_key=b"x" * 32, demo=False, ai_mode="remote",
                      ai_server_url="https://ai.example.ch", ai_token="nfa_x")
    ai = AIServices(s)
    assert ai.mode == "remote" and isinstance(ai.stt, RemoteSTT) and isinstance(ai.translator, RemoteTranslator)
