"""Zentrale KI-Dienste der Praxis-Box (lokal, KI-Server Schweiz oder – nur in Vorführungen – Cloud-Dienst)."""
from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

from . import extract as extract_mod
from . import notes
from .llm import LLMUnavailable, OllamaLLM
from .ocr import extract_text, tesseract_available
from .stt import STTUnavailable, make_stt
from .translate import NoTranslator, OllamaTranslator, translate_cached
from .types import ExtractionResult, NoteContext, NoteDraft, Transcript

log = logging.getLogger(__name__)


class AIServices:
    def __init__(self, settings):
        self.settings = settings
        self._missing_cache: tuple[float, list[str]] | None = None
        self.remote = None
        self.cloud = None
        self.mode = settings.ai_mode if settings.ai_mode in ("remote", "cloud_demo") else "local"
        # Ohne KI zeigt die Demo klar gekennzeichnete Beispiele; mit cloud_demo sind die Ergebnisse echt
        self.simulate_demo = bool(settings.demo) and self.mode != "cloud_demo"
        self.cloud_host = None
        if self.mode == "cloud_demo":
            # NUR Vorführung mit fiktiven Daten: OpenAI-kompatibler Cloud-Dienst (ohne NFIZYO_DEMO=1 startet nichts)
            from .cloud import CloudAIClient, CloudDemoRefused, CloudLLM, CloudSTT, CloudTranslator, ensure_demo_only

            ensure_demo_only(settings)
            missing = [k for k, v in (("NFIZYO_CLOUD_AI_STT_MODEL", settings.cloud_ai_stt_model),
                                      ("NFIZYO_CLOUD_AI_LLM_MODEL", settings.cloud_ai_llm_model)) if not v]
            if missing:
                raise CloudDemoRefused(f"NFIZYO_AI_MODE=cloud_demo: {', '.join(missing)} fehlt (siehe docs/AI.md).")
            self.cloud = CloudAIClient(settings.cloud_ai_url, settings.cloud_ai_key, demo=settings.demo,
                                       timeout=settings.cloud_ai_timeout, reasoning=settings.cloud_ai_reasoning)
            self.cloud_host = self.cloud.host
            self.stt = CloudSTT(self.cloud, settings.cloud_ai_stt_model)
            self.llm = CloudLLM(self.cloud, settings.cloud_ai_llm_model)
            self.translator = CloudTranslator(CloudLLM(self.cloud, self._cloud_translate_model()))
            return
        if self.mode == "remote":
            # Kleine Box ohne Grafikkarte: KI-Server von NSystem in der Schweiz (Daten bleiben in der Praxis)
            from .remote import RemoteAIClient, RemoteLLM, RemoteSTT, RemoteTranslator

            self.remote = RemoteAIClient(settings.ai_server_url, settings.ai_token, allow_insecure=settings.allow_remote_ai)
            self.stt = RemoteSTT(self.remote)
            self.llm = RemoteLLM(self.remote, settings.llm_model)
            self.translator = RemoteTranslator(self.remote)
            return
        self.stt = make_stt(settings.stt_engine, settings.stt_model, settings.stt_device)
        self.llm = None
        if settings.llm_engine == "ollama":
            self.llm = OllamaLLM(settings.ollama_url, settings.llm_model, settings.allow_remote_ai)
        self.translator = NoTranslator()
        if settings.translate_engine == "ollama":
            self.translator = OllamaTranslator(
                OllamaLLM(settings.ollama_url, settings.translate_model, settings.allow_remote_ai, timeout=60)
            )

    def _cloud_translate_model(self) -> str:
        return self.settings.cloud_ai_translate_model or self.settings.cloud_ai_llm_model

    # --- Status (Einstellungen > KI, CLI check-ai)
    def status(self) -> dict:
        if self.mode == "cloud_demo":
            st = self.cloud.status()
            ok = bool(st.get("ok"))
            models = {"stt": self.settings.cloud_ai_stt_model, "llm": self.settings.cloud_ai_llm_model,
                      "translate": self._cloud_translate_model()}
            out = {"mode": "cloud_demo", "server": self.cloud.host, "provider": self.cloud.provider, "error": st.get("error"),
                   "checking": bool(st.get("checking")), "ocr": {"engine": "tesseract", "available": tesseract_available()}}
            for k, model in models.items():
                out[k] = {"engine": "cloud", "available": ok and bool(model), "model": model or None}
            return out
        if self.mode == "remote":
            st = self.remote.status()
            return {
                "mode": "remote", "server": self.remote.host, "error": st.get("error"), "checking": bool(st.get("checking")),
                "stt": {"engine": "remote", "available": bool(st.get("stt")), "model": st.get("stt_model")},
                "llm": {"engine": "remote", "available": bool(st.get("llm")), "model": st.get("llm_model")},
                "translate": {"engine": "remote", "available": bool(st.get("translate")), "model": st.get("translate_model")},
                "ocr": {"engine": "tesseract", "available": tesseract_available()},
            }
        return {
            "mode": "local", "server": None,
            "stt": {"engine": self.stt.name, "available": self.stt.available(), "model": self.settings.stt_model},
            "llm": {
                "engine": self.settings.llm_engine,
                "model": self.settings.llm_model if self.llm else None,
                "available": bool(self.llm and self.llm.available()),
            },
            "translate": {
                "engine": self.translator.name,
                "model": self.settings.translate_model if self.translator.name != "none" else None,
                "available": self.translator.available(),
            },
            "ocr": {"engine": "tesseract", "available": tesseract_available()},
        }

    def missing(self) -> list[str]:
        """Welche KI-Bausteine fehlen gerade? (für den Hinweis im Kopf der Seiten; 60 s zwischengespeichert)"""
        now = time.monotonic()
        if self._missing_cache is None or now - self._missing_cache[0] > 60:
            st = self.status()
            if st.get("checking"):   # KI-Server wird gerade zum ersten Mal abgefragt: noch nichts melden
                return []
            self._missing_cache = (now, [k for k in ("stt", "llm", "translate") if not st[k]["available"]])
        return self._missing_cache[1]

    def can_transcribe(self) -> bool:
        return self.stt.available()

    def stt_state(self) -> str:
        """Für die Diktatseiten: ready | checking (KI-Server wird gerade gefragt) | down (eingerichtet, gerade nicht
        erreichbar – Aufnahmen werden später verarbeitet) | off (nicht eingerichtet)."""
        if self.stt.available():
            return "ready"
        client = self.remote or self.cloud
        if client is None:
            return "off"
        st = client.status()
        if st.get("checking"):
            return "checking"
        return "down" if st.get("error") else "off"

    def self_test(self) -> dict:
        """check-ai im Demo-Betrieb mit Cloud-Dienst: kleine echte Testanfragen (keine Patientendaten), Schlüssel nur maskiert."""
        if self.cloud is None:
            return {}
        return {"provider": self.cloud.provider, "server": self.cloud.host, "key": self.cloud.masked_key,
                "only_fictitious_data": True,
                **self.cloud.self_test(self.settings.cloud_ai_stt_model, self.settings.cloud_ai_llm_model,
                                       self._cloud_translate_model())}

    def can_translate(self) -> bool:
        if self.mode in ("remote", "cloud_demo"):
            return self.translator.available()
        return self.translator.name != "none"

    # --- Aufgaben
    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        return self.stt.transcribe(path, language)

    def draft_note(self, transcript: str, lang: str, ctx: NoteContext | None = None) -> NoteDraft:
        if self.llm is not None:
            try:
                return notes.draft_with_llm(transcript, lang, self.llm, ctx)
            except LLMUnavailable:
                log.warning("Sprachmodell nicht erreichbar – Regel-Engine wird verwendet")
                draft = notes.draft_rules(transcript, ctx)
                draft.warnings.append("note.llm_unavailable")
                return draft
        return notes.draft_rules(transcript, ctx)

    def patient_info(self, transcript: str, lang: str):
        """Kurze Information für die Patientin / den Patienten (zu Hause tun, wann melden) – nur aus dem Diktat."""
        from . import patientinfo

        return patientinfo.draft(transcript, lang, self.llm)

    def read_document(self, path: Path, mime: str | None = None) -> tuple[str, str]:
        return extract_text(path, mime)

    def extract(self, text: str, ui_lang: str = "de", today: date | None = None) -> ExtractionResult:
        result = extract_mod.extract_rules(text, today=today)
        if self.llm is not None:
            try:
                result = extract_mod.extract_with_llm(text, self.llm, ui_lang, result, today=today)
            except LLMUnavailable:
                result.warnings.append("extract.llm_unavailable")
        return result

    def translate(self, db, practice_id: int, text: str, source: str, target: str):
        return translate_cached(db, practice_id, self.translator, text, source, target)


__all__ = ["AIServices", "STTUnavailable", "LLMUnavailable"]
