"""KI über den N-Fizyo-KI-Server von NSystem (Rechenzentrum in der Schweiz).

Für Praxen mit kleiner Box ohne Grafikkarte: Patientendaten bleiben auf der Box in der Praxis.
Nur der Inhalt der einzelnen Aufgabe (Audio eines Diktats, Text eines Berichts, ein Satz zum Übersetzen)
geht verschlüsselt (HTTPS) zum KI-Server. Der Server speichert nichts und protokolliert keine Inhalte.

Jede Praxis hat ein eigenes Zugangs-Token (nur in der .env der Box, nie im Chat oder im Code).
"""
from __future__ import annotations

import http.client
import json
import logging
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .llm import LLMUnavailable
from .safety import is_local_url
from .statuscache import CachedStatus
from .stt import STTRejected, STTUnavailable
from .types import Transcript

log = logging.getLogger(__name__)
USER_AGENT = "N-Fizyo-Box/1"
CONNECT_TIMEOUT = 10  # Verbindungsaufbau; ein nicht antwortender Server blockiert so nie minutenlang alle Hintergrundaufgaben
STATUS_DOWN = {"stt": False, "llm": False, "translate": False}


class RemoteAIError(RuntimeError):
    pass


class RemoteAIRejected(RemoteAIError):
    """Der KI-Server hat die Aufgabe selbst abgelehnt (Aufnahme zu gross, nicht lesbar …) – nochmals senden hilft nicht."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class RemoteAIUnreachable(RemoteAIError):
    """Keine Verbindung (Server aus, Netz weg, keine Antwort)."""


def _short_connect(conn_class):
    class Conn(conn_class):
        def connect(self):
            read_timeout = self.timeout
            if isinstance(read_timeout, (int, float)):
                self.timeout = min(CONNECT_TIMEOUT, read_timeout)
            try:
                super().connect()
            finally:
                self.timeout = read_timeout
            if isinstance(read_timeout, (int, float)):
                self.sock.settimeout(read_timeout)
    return Conn


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_short_connect(http.client.HTTPConnection), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_short_connect(http.client.HTTPSConnection), req, context=self._context)


def check_server_url(url: str, allow_insecure: bool = False) -> None:
    """Nur HTTPS (ausser Tests im lokalen Netz)."""
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("NFIZYO_AI_SERVER_URL fehlt oder ist ungültig.")
    if parsed.scheme != "https" and not (allow_insecure or is_local_url(url)):
        raise ValueError(f"KI-Server {url!r}: nur HTTPS erlaubt (Patientendaten werden verschlüsselt übertragen).")


class RemoteAIClient:
    def __init__(self, url: str, token: str, *, allow_insecure: bool = False, timeout: float = 120):
        check_server_url(url, allow_insecure)
        if not token:
            raise ValueError("NFIZYO_AI_TOKEN fehlt (gehört nur in die .env der Box).")
        self.url = url.rstrip("/")
        self.host = urlparse(url).hostname or url
        self._token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler())
        self._status = CachedStatus(self._fetch_status, STATUS_DOWN)

    def __repr__(self) -> str:  # Token nie ausgeben
        return f"RemoteAIClient({self.url!r})"

    def _request(self, path: str, *, data: bytes | None = None, content_type: str | None = None, timeout: float | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RemoteAIError("KI-Server lehnt den Zugang ab (Token prüfen).") from exc
            if exc.code in (400, 413, 415, 422):
                raise RemoteAIRejected(f"KI-Server lehnt die Aufgabe ab (Fehler {exc.code}).", exc.code) from exc
            raise RemoteAIError(f"KI-Server antwortet mit Fehler {exc.code}.") from exc
        except (OSError, http.client.HTTPException, ValueError) as exc:   # URLError, Zeitüberschreitung, abgebrochene Verbindung
            err = RemoteAIUnreachable(f"KI-Server nicht erreichbar ({self.host}).")
            if path != "/v1/status":
                self._status.set({**STATUS_DOWN, "error": str(err)})   # sofort anzeigen, nicht erst nach der nächsten Abfrage
            raise err from exc

    def _fetch_status(self) -> dict:
        try:
            st = self._request("/v1/status", timeout=5)
        except RemoteAIError as exc:
            log.warning("%s", exc)
            return {**STATUS_DOWN, "error": str(exc)}
        return st if isinstance(st, dict) else {**STATUS_DOWN, "error": "KI-Server: unerwartete Antwort."}

    def status(self, max_age: float = 30) -> dict:
        """Was der KI-Server gerade kann. Im Webserver nie wartend (siehe statuscache), in Hintergrundaufgaben aktuell."""
        return self._status.get(max_age)


    def post_json(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        return self._request(path, data=json.dumps(payload).encode("utf-8"), content_type="application/json", timeout=timeout)

    def post_audio(self, path: str, audio: Path, fields: dict[str, str]) -> dict:
        boundary = "nfizyo" + secrets.token_hex(12)
        parts: list[bytes] = []
        for key, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="audio{audio.suffix or ".webm"}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
        parts.append(audio.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        return self._request(path, data=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}", timeout=600)


class RemoteSTT:
    name = "remote"

    def __init__(self, client: RemoteAIClient):
        self.client = client

    def available(self) -> bool:
        return bool(self.client.status().get("stt"))

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        limit = self.client.status().get("max_audio")
        if isinstance(limit, int) and limit > 0 and path.stat().st_size > limit:
            raise STTRejected("too_large")   # gar nicht erst senden (der Server würde mitten im Hochladen abbrechen)
        try:
            data = self.client.post_audio("/v1/transcribe", path, {"language": language or ""})
        except RemoteAIRejected as exc:
            raise STTRejected("too_large" if exc.code == 413 else "unknown") from exc
        except RemoteAIError as exc:
            raise STTUnavailable(str(exc)) from exc
        return Transcript(text=str(data.get("text", "")), language=str(data.get("language") or language or "de"),
                          engine=f"remote:{data.get('engine', 'whisper')}", duration_sec=data.get("duration_sec"))


class RemoteLLM:
    """Gleiche Schnittstelle wie OllamaLLM (generate, available, model)."""

    def __init__(self, client: RemoteAIClient, model_hint: str = ""):
        self.client = client
        self.model = model_hint or "remote"

    def available(self) -> bool:
        st = self.client.status()
        if st.get("llm_model"):
            self.model = str(st["llm_model"])
        return bool(st.get("llm"))

    def generate(self, prompt: str, system: str = "", json_mode: bool = False, temperature: float = 0.0) -> str:
        try:
            data = self.client.post_json("/v1/generate", {"prompt": prompt, "system": system, "json_mode": json_mode,
                                                          "temperature": temperature}, timeout=300)
        except RemoteAIError as exc:
            raise LLMUnavailable(str(exc)) from exc
        return str(data.get("text", ""))


class RemoteTranslator:
    name = "remote"

    def __init__(self, client: RemoteAIClient):
        self.client = client

    def available(self) -> bool:
        return bool(self.client.status().get("translate"))

    def translate(self, text: str, source: str, target: str):
        from .translate import TranslationResult

        if not text.strip() or source == target:
            return TranslationResult(text, "identity")
        try:
            data = self.client.post_json("/v1/translate", {"text": text, "source": source, "target": target}, timeout=120)
        except RemoteAIError as exc:
            log.warning("Übersetzung über KI-Server fehlgeschlagen: %s", exc)
            return TranslationResult(None, "none")
        return TranslationResult(data.get("text"), f"remote:{data.get('engine', '')}", data.get("warning"))
