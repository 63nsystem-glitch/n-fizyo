"""NUR FÜR VORFÜHRUNGEN: KI über einen OpenAI-kompatiblen Cloud-Dienst (z. B. OpenAI oder Mistral).

Damit zeigt eine Demo echte KI (Diktat → Text, Notizentwurf, Berichte lesen, Patienteninformation, Übersetzung)
auf einem Laptop ohne Grafikkarte. Einstellung: NFIZYO_AI_MODE=cloud_demo.

Hart abgesichert (ensure_demo_only, ensure_only_demo_practices):
- läuft nur mit NFIZYO_DEMO=1 – Demo-Daten sind immer fiktiv; ohne Demo-Modus startet N-Fizyo nicht;
- läuft nur, wenn die Datenbank ausschliesslich Demo-Praxen enthält – geprüft beim Start UND vor jeder einzelnen Anfrage
  (CloudAIClient.guard): entsteht im Betrieb eine echte Praxis, geht ab sofort nichts mehr an den externen Dienst.
Für echte Patientinnen und Patienten: KI auf dem Praxis-Server (local) oder KI-Server Schweiz (remote).

Der API-Schlüssel steht nur in der .env: nie in Logs, Fehlermeldungen, Oberfläche oder in der Ausgabe von check-ai.
Dieser Baustein ersetzt nur das Modell. Alle Prüfungen bleiben dieselben (notes.py, patientinfo.py, extract.py,
translate.py): erfundene Zahlen → Entwurf verworfen, Zitate müssen im Original stehen, Freigabe durch die Therapie.
"""
from __future__ import annotations

import io
import json
import logging
import secrets
import socket
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from urllib.parse import urlparse

from ..core.languages import from_whisper
from .llm import LLMUnavailable
from .safety import is_local_url
from .statuscache import CachedStatus
from .stt import STTUnavailable
from .translate import OllamaTranslator, TranslationResult
from .types import Transcript

log = logging.getLogger(__name__)
USER_AGENT = "N-Fizyo-Demo/1"
PROVIDERS = {"api.openai.com": "OpenAI", "api.mistral.ai": "Mistral AI"}
AUDIO_MIME = {".webm": "audio/webm", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".mp3": "audio/mpeg",
              ".wav": "audio/wav", ".aac": "audio/aac", ".flac": "audio/flac"}
# Parameter, die nicht jedes Modell kennt: lehnt der Dienst sie ab (400), wird ohne sie wiederholt
OPTIONAL_PARAMS = ("temperature", "reasoning_effort", "response_format")

REFUSED_NO_DEMO = (
    "NFIZYO_AI_MODE=cloud_demo ist nur im Demo-Modus erlaubt (NFIZYO_DEMO=1, nur fiktive Daten). "
    "Echte Patientendaten gehen nie an externe KI-Dienste. Für echte Praxen: KI auf dem Praxis-Server "
    "(NFIZYO_AI_MODE=local) oder KI-Server Schweiz (NFIZYO_AI_MODE=remote)."
)
REFUSED_REAL_PRACTICE = (
    "NFIZYO_AI_MODE=cloud_demo: In der Datenbank gibt es eine Praxis, die keine Demo-Praxis ist. "
    "Externe KI-Dienste nur mit der Demo-Praxis (fiktive Daten). Eigenen Datenordner für die Vorführung verwenden "
    "(NFIZYO_DATA_DIR) oder NFIZYO_AI_MODE=local/remote setzen."
)


class CloudDemoRefused(RuntimeError):
    """N-Fizyo startet nicht: externer KI-Dienst ausserhalb einer Vorführung mit fiktiven Daten."""


class CloudAIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


def ensure_demo_only(settings) -> None:
    if settings.ai_mode == "cloud_demo" and not settings.demo:
        raise CloudDemoRefused(REFUSED_NO_DEMO)


def ensure_only_demo_practices(db) -> None:
    if db.value("SELECT COUNT(*) FROM practices WHERE is_demo = 0", (), 0):
        raise CloudDemoRefused(REFUSED_REAL_PRACTICE)


def mask(key: str) -> str:
    return f"gesetzt (…{key[-4:]})" if len(key) >= 16 else ("gesetzt" if key else "fehlt")


def silent_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    """Stille als WAV – Testaufnahme für check-ai (keine Patientendaten)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _content(data: dict) -> str:
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise CloudAIError("Externer KI-Dienst: Antwort ohne Text.") from exc
    if isinstance(content, list):  # z. B. Mistral mit Denk-Abschnitten: nur Text-Teile
        content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type", "text") == "text")
    return str(content or "").strip()


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class CloudAIClient:
    def __init__(self, url: str, key: str, *, demo: bool, timeout: float = 60, reasoning: str = ""):
        if not demo:
            raise CloudDemoRefused(REFUSED_NO_DEMO)
        parsed = urlparse(url or "")
        if not parsed.hostname:
            raise CloudDemoRefused("NFIZYO_CLOUD_AI_URL fehlt oder ist ungültig (z. B. https://api.openai.com/v1).")
        if parsed.scheme != "https" and not is_local_url(url):
            raise CloudDemoRefused(f"NFIZYO_CLOUD_AI_URL: nur HTTPS erlaubt ({parsed.hostname}).")
        if not key:
            raise CloudDemoRefused("NFIZYO_CLOUD_AI_KEY fehlt (gehört nur in die .env des Vorführ-Laptops).")
        self.url = url.rstrip("/")
        self.host = parsed.hostname
        self.provider = PROVIDERS.get(self.host, self.host)
        self._key = key
        self.timeout = float(timeout)
        self.reasoning = reasoning
        # Lokale Testserver ohne Proxy; der Cloud-Dienst über den Proxy des Laptops, falls einer eingestellt ist
        handlers = [urllib.request.ProxyHandler({})] if is_local_url(url) else []
        self._opener = urllib.request.build_opener(*handlers)
        self._status = CachedStatus(self._fetch_status, {"ok": False})
        self._unsupported: set[tuple[str, str]] = set()
        # Prüfung vor JEDER Anfrage (app.py setzt sie: nur Demo-Praxen in der Datenbank). Wirft CloudDemoRefused.
        self.guard = None

    def __repr__(self) -> str:  # Schlüssel nie ausgeben
        return f"CloudAIClient({self.url!r})"

    @property
    def masked_key(self) -> str:
        return mask(self._key)

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _detail(self, exc: urllib.error.HTTPError) -> str:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - nur für die Fehlermeldung
            return ""
        try:
            data = json.loads(body)
            err = data.get("error") if isinstance(data, dict) else None
            msg = (err.get("message") if isinstance(err, dict) else err) or data.get("message") or data.get("detail") or body
        except (ValueError, AttributeError):
            msg = body
        return self._scrub(" ".join(str(msg).split()))[:300]

    def _request(self, path: str, *, data: bytes | None = None, content_type: str | None = None,
                 timeout: float | None = None) -> dict:
        timeout = timeout or self.timeout
        if self.guard is not None:
            try:
                self.guard()
            except CloudDemoRefused as exc:
                log.error("Externer KI-Dienst gesperrt: %s", exc)
                raise CloudAIError(f"Externer KI-Dienst gesperrt: {exc}") from None
        headers = {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = self._detail(exc)
            if exc.code in (401, 403):
                raise CloudAIError("Externer KI-Dienst lehnt den Schlüssel ab (NFIZYO_CLOUD_AI_KEY prüfen).", exc.code, detail) from None
            if exc.code == 429:
                raise CloudAIError("Externer KI-Dienst: zu viele Anfragen oder Guthaben aufgebraucht (429).", exc.code, detail) from None
            raise CloudAIError(f"Externer KI-Dienst antwortet mit Fehler {exc.code}: {detail or '–'}", exc.code, detail) from None
        except (TimeoutError, socket.timeout):
            raise CloudAIError(f"Externer KI-Dienst ({self.host}) antwortet nicht innert {timeout:g} s.") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise CloudAIError(f"Externer KI-Dienst ({self.host}) antwortet nicht innert {timeout:g} s.") from None
            raise CloudAIError(f"Externer KI-Dienst nicht erreichbar ({self.host}).") from None
        except ValueError:
            raise CloudAIError(f"Externer KI-Dienst ({self.host}): ungültige Antwort.") from None
        except (ConnectionError, OSError):
            raise CloudAIError(f"Externer KI-Dienst nicht erreichbar ({self.host}).") from None

    # --- Erreichbarkeit (Einstellungen › KI, Diktat): GET /models kostet nichts und enthält keine Daten
    def _fetch_status(self) -> dict:
        try:
            data = self._request("/models", timeout=min(5.0, self.timeout))
            ids: set[str] = set()
            for m in data.get("data", []) if isinstance(data, dict) else []:
                if isinstance(m, dict):
                    ids.add(str(m.get("id", "")))
                    ids.update(str(a) for a in m.get("aliases") or [])
            return {"ok": True, "models": sorted(i for i in ids if i)}
        except CloudAIError as exc:
            log.warning("%s", exc)
            return {"ok": False, "error": str(exc)}

    def status(self, max_age: float = 60) -> dict:
        """Im Webserver nie wartend (siehe statuscache), in Hintergrundaufgaben aktuell."""
        return self._status.get(max_age)

    def forget_status(self) -> None:
        self._status.forget()

    # --- Aufgaben
    def chat(self, model: str, system: str, prompt: str, *, json_mode: bool = False, temperature: float | None = 0.0,
             timeout: float | None = None) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        payload: dict = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.reasoning:
            payload["reasoning_effort"] = self.reasoning
        for p in OPTIONAL_PARAMS:
            if (model, p) in self._unsupported:
                payload.pop(p, None)
        for _ in range(len(OPTIONAL_PARAMS) + 1):
            try:
                data = self._request("/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                     content_type="application/json", timeout=timeout)
                break
            except CloudAIError as exc:
                bad = next((p for p in OPTIONAL_PARAMS if p in payload and exc.status in (400, 422) and p in exc.detail), None)
                if bad is None:
                    raise
                log.info("Externer KI-Dienst: Modell %s kennt %s nicht – ohne diesen Parameter wiederholt", model, bad)
                self._unsupported.add((model, bad))
                payload.pop(bad)
        else:  # pragma: no cover - alle Parameter abgelehnt
            raise CloudAIError("Externer KI-Dienst lehnt die Anfrage ab.")
        text = _content(data)
        return _strip_fences(text) if json_mode else text

    def transcribe(self, model: str, audio: bytes, filename: str, language: str | None = None,
                   timeout: float | None = None) -> dict:
        fields: list[tuple[str, str]] = [("model", model)]
        if language:
            # gpt-transcribe (OpenAI) erwartet languages[], die übrigen Modelle (whisper-1, gpt-4o-*, Voxtral) language
            fields.append(("languages[]" if model.startswith("gpt-transcribe") else "language", language))
        boundary = "nfizyo" + secrets.token_hex(12)
        parts: list[bytes] = []
        for key, value in fields:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        suffix = Path(filename).suffix.lower() or ".webm"
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio{suffix}"\r\n'
                     f"Content-Type: {AUDIO_MIME.get(suffix, 'application/octet-stream')}\r\n\r\n".encode("utf-8"))
        parts.append(audio)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return self._request("/audio/transcriptions", data=b"".join(parts),
                             content_type=f"multipart/form-data; boundary={boundary}", timeout=timeout)

    # --- check-ai: kleine echte Testanfragen ohne Patientendaten
    def self_test(self, stt_model: str, llm_model: str, translate_model: str) -> dict:
        def run(fn) -> dict:
            t0 = time.monotonic()
            try:
                fn()
            except CloudAIError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "ms": int((time.monotonic() - t0) * 1000)}

        out = {"llm": run(lambda: self.chat(llm_model, "", "Antworte nur mit: OK"))}
        if translate_model and translate_model != llm_model:
            out["translate"] = run(lambda: self.chat(translate_model, "", "Translate into French: Guten Tag"))
        else:
            out["translate"] = {**out["llm"], "same_model_as": "llm"}
        out["stt"] = run(lambda: self.transcribe(stt_model, silent_wav(), "test.wav", "de"))
        self.forget_status()
        return out


class CloudSTT:
    name = "cloud"

    def __init__(self, client: CloudAIClient, model: str):
        self.client = client
        self.model = model

    def available(self) -> bool:
        return bool(self.model) and bool(self.client.status().get("ok"))

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        try:
            data = self.client.transcribe(self.model, Path(path).read_bytes(), Path(path).name, language)
        except CloudAIError as exc:
            raise STTUnavailable(str(exc)) from exc
        detected = data.get("language") if isinstance(data.get("language"), str) else None
        langs = data.get("languages")
        if not detected and isinstance(langs, list) and langs and isinstance(langs[0], dict):
            detected = langs[0].get("code")
        lang = language if language in ("de", "fr") else (from_whisper(detected) if from_whisper(detected) in ("de", "fr") else "de")
        return Transcript(text=str(data.get("text") or "").strip(), language=lang, engine=f"cloud:{self.model}")


class CloudLLM:
    """Gleiche Schnittstelle wie OllamaLLM (generate, available, model)."""

    def __init__(self, client: CloudAIClient, model: str):
        self.client = client
        self.model = model

    def available(self) -> bool:
        return bool(self.model) and bool(self.client.status().get("ok"))

    def generate(self, prompt: str, system: str = "", json_mode: bool = False, temperature: float = 0.0) -> str:
        try:
            return self.client.chat(self.model, system, prompt, json_mode=json_mode, temperature=temperature)
        except CloudAIError as exc:
            raise LLMUnavailable(str(exc)) from exc


class CloudTranslator(OllamaTranslator):
    """Gleicher Ablauf wie die lokale Übersetzung (gleicher Prompt, gleiche Zahlenprüfung), nur anderes Modell."""

    name = "cloud"

    def translate(self, text: str, source: str, target: str) -> TranslationResult:
        try:
            return super().translate(text, source, target)
        except LLMUnavailable as exc:
            log.warning("Übersetzung über externen KI-Dienst fehlgeschlagen: %s", exc)
            return TranslationResult(None, "none")
