"""HTTP-Schnittstelle des KI-Servers (hinter Caddy mit HTTPS)."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from ..core.languages import BY_CODE
from .store import Store

log = logging.getLogger(__name__)

MAX_AUDIO = 40 * 1024 * 1024
MAX_PROMPT = 60_000
MAX_TRANSLATE = 6_000
PER_CLIENT = 3  # gleichzeitige Aufgaben pro Praxis


@dataclass
class Engines:
    stt: object
    llm: object
    translator: object
    stt_model: str = ""
    llm_model: str = ""
    translate_model: str = ""


STORE = web.AppKey("store", Store)
ENGINES = web.AppKey("engines", Engines)
GPU_LOCK = web.AppKey("gpu_lock", asyncio.Lock)
LIMITS = web.AppKey("limits", dict)
TMP = web.AppKey("tmp", Path)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/v1/"):
        return await handler(request)
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    client = request.app[STORE].client_for(token)
    if client is None:
        await asyncio.sleep(0.3)  # Raten bremsen
        return web.json_response({"error": "unauthorized"}, status=401)
    request["client"] = client
    if request.path == "/v1/status":
        # Status nie hinter den laufenden Aufgaben anstellen: sonst wirkt ein beschäftigter Server «nicht erreichbar»
        return await handler(request)
    sem = request.app[LIMITS].setdefault(client["id"], asyncio.Semaphore(PER_CLIENT))
    async with sem:
        return await handler(request)


def _record(request: web.Request, op: str, ok: bool, started: float, bytes_in: int = 0, audio_sec: float | None = None) -> None:
    request.app[STORE].record(request["client"]["id"], op, ok, int((time.monotonic() - started) * 1000), bytes_in, audio_sec)


def _unreadable(exc: Exception) -> bool:
    """Audio nicht dekodierbar (PyAV/FFmpeg meldet InvalidDataError, eine ValueError) – im Gegensatz zu GPU- oder Modellfehlern."""
    return isinstance(exc, (ValueError, EOFError)) or type(exc).__module__.split(".")[0] == "av"


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def status(request: web.Request) -> web.Response:
    e = request.app[ENGINES]
    stt, llm, tr = await asyncio.gather(*(asyncio.to_thread(x.available) for x in (e.stt, e.llm, e.translator)))
    return web.json_response({"stt": stt, "llm": llm, "translate": tr, "stt_model": e.stt_model, "llm_model": e.llm_model,
                              "translate_model": e.translate_model, "max_audio": MAX_AUDIO})


async def transcribe(request: web.Request) -> web.Response:
    started = time.monotonic()
    if not request.content_type.startswith("multipart/"):
        return web.json_response({"error": "multipart"}, status=400)
    reader = await request.multipart()
    language, path, size = None, None, 0
    try:
        while (part := await reader.next()) is not None:
            if part.name == "language":
                language = (await part.text()).strip()[:8] or None
            elif part.name == "audio":
                with tempfile.NamedTemporaryFile(dir=request.app[TMP], suffix=".audio", delete=False) as fh:
                    path = Path(fh.name)
                    while chunk := await part.read_chunk(256 * 1024):
                        size += len(chunk)
                        if size > MAX_AUDIO:
                            return web.json_response({"error": "too_large"}, status=413)
                        fh.write(chunk)
        if path is None or size == 0:
            return web.json_response({"error": "audio"}, status=400)
        async with request.app[GPU_LOCK]:
            t = await asyncio.to_thread(request.app[ENGINES].stt.transcribe, path, language)
        _record(request, "stt", True, started, size, t.duration_sec)
        return web.json_response({"text": t.text, "language": t.language, "engine": t.engine, "duration_sec": t.duration_sec})
    except Exception as exc:
        log.exception("Transkription fehlgeschlagen")  # ohne Inhalt
        _record(request, "stt", False, started, size)
        if _unreadable(exc):   # Aufnahme selbst kaputt: die Box soll sie nicht immer wieder schicken
            return web.json_response({"error": "audio_unreadable"}, status=422)
        return web.json_response({"error": "stt_failed"}, status=503)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)  # nichts bleibt liegen


async def generate(request: web.Request) -> web.Response:
    started = time.monotonic()
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "json"}, status=400)
    prompt, system = str(body.get("prompt") or ""), str(body.get("system") or "")
    if not prompt or len(prompt) + len(system) > MAX_PROMPT:
        return web.json_response({"error": "prompt"}, status=400)
    try:
        text = await asyncio.to_thread(request.app[ENGINES].llm.generate, prompt, system, bool(body.get("json_mode")),
                                       float(body.get("temperature") or 0.0))
    except Exception:
        log.exception("Sprachmodell fehlgeschlagen")
        _record(request, "llm", False, started, len(prompt))
        return web.json_response({"error": "llm_failed"}, status=503)
    _record(request, "llm", True, started, len(prompt))
    return web.json_response({"text": text, "model": request.app[ENGINES].llm_model})


async def translate(request: web.Request) -> web.Response:
    started = time.monotonic()
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "json"}, status=400)
    text, source, target = str(body.get("text") or ""), str(body.get("source") or ""), str(body.get("target") or "")
    if not text or len(text) > MAX_TRANSLATE or source not in BY_CODE or target not in BY_CODE:
        return web.json_response({"error": "input"}, status=400)
    try:
        res = await asyncio.to_thread(request.app[ENGINES].translator.translate, text, source, target)
    except Exception:
        log.exception("Übersetzung fehlgeschlagen")
        _record(request, "translate", False, started, len(text))
        return web.json_response({"error": "translate_failed"}, status=503)
    _record(request, "translate", bool(res.text), started, len(text))
    return web.json_response({"text": res.text, "engine": res.engine, "warning": res.warning})


def create_ai_app(store: Store, engines: Engines, tmp_dir: Path) -> web.Application:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for leftover in tmp_dir.glob("*.audio"):  # nach Absturz: Reste sofort löschen
        leftover.unlink(missing_ok=True)
    app = web.Application(middlewares=[auth_middleware], client_max_size=MAX_AUDIO + 1024 * 1024)
    app[STORE] = store
    app[ENGINES] = engines
    app[GPU_LOCK] = asyncio.Lock()
    app[LIMITS] = {}
    app[TMP] = tmp_dir
    app.router.add_get("/health", health)
    app.router.add_get("/v1/status", status)
    app.router.add_post("/v1/transcribe", transcribe)
    app.router.add_post("/v1/generate", generate)
    app.router.add_post("/v1/translate", translate)
    return app
