"""Spracherkennung (lokal).

none            – keine Spracherkennung; Therapeut tippt oder nutzt die Diktierfunktion des Geräts
faster_whisper  – Whisper auf der Box (CPU oder NVIDIA-GPU). Für Schweizerdeutsch eignen sich
                  feinabgestimmte Modelle (in CTranslate2 umgewandelt, Pfad in NFIZYO_STT_MODEL).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..core.languages import from_whisper
from .types import Transcript

log = logging.getLogger(__name__)


class STTUnavailable(RuntimeError):
    pass


class STTRejected(RuntimeError):
    """Diese Aufnahme kann nicht erkannt werden (zu lang, nicht lesbar) – nochmals versuchen hilft nicht.
    reason: Schlüssel für die Meldung (dictation.err_<reason>)."""

    def __init__(self, reason: str = "unknown"):
        super().__init__(reason)
        self.reason = reason


class NoSTT:
    name = "none"

    def available(self) -> bool:
        return False

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        raise STTUnavailable("Keine Spracherkennung installiert (NFIZYO_STT=none).")


class FasterWhisperSTT:
    name = "faster_whisper"

    def __init__(self, model: str, device: str = "cuda"):
        self.model_name = model
        self.device = device
        self._model = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise STTUnavailable("faster-whisper ist nicht installiert (pip install -r requirements-ai.txt).") from exc
                compute = "float16" if self.device == "cuda" else "int8"
                log.info("Lade Whisper-Modell %s auf %s …", self.model_name, self.device)
                self._model = WhisperModel(self.model_name, device=self.device, compute_type=compute)
        return self._model

    def transcribe(self, path: Path, language: str | None = None) -> Transcript:
        model = self._load()
        segments, info = model.transcribe(str(path), language=language, vad_filter=True, beam_size=5)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        lang = from_whisper(getattr(info, "language", None)) or (language or "de")
        return Transcript(text=text, language=lang, engine=f"faster_whisper:{self.model_name}",
                          duration_sec=getattr(info, "duration", None))


def make_stt(engine: str, model: str, device: str):
    if engine == "faster_whisper":
        return FasterWhisperSTT(model, device)
    return NoSTT()
