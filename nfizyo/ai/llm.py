"""Lokales Sprachmodell über Ollama (läuft auf der Praxis-Box).

Es wird nie ein Proxy verwendet und nur eine Adresse im lokalen Netz akzeptiert
(ausser NFIZYO_ALLOW_REMOTE_AI=1 für Tests). Patientendaten verlassen die Praxis nicht.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .safety import ensure_local_url

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


class OllamaLLM:
    def __init__(self, url: str, model: str, allow_remote: bool = False, timeout: float = 180):
        ensure_local_url(url, allow_remote)
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # Keine Proxys: Anfragen gehen direkt an die Box
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise LLMUnavailable(f"Ollama nicht erreichbar ({self.url}): {exc}") from exc

    def models(self) -> list[str]:
        try:
            with self._opener.open(self.url + "/api/tags", timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
            return []
        return [m.get("name", "") for m in data.get("models", [])]

    def available(self) -> bool:
        names = self.models()
        return any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)

    def generate(self, prompt: str, system: str = "", json_mode: bool = False, temperature: float = 0.0) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"
        data = self._post("/api/generate", payload)
        return str(data.get("response", ""))
