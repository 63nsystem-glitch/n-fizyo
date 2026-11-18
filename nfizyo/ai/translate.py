"""Maschinelle Übersetzung – lokal (Ollama, z. B. TranslateGemma); nur in Vorführungen auch über cloud.py.

Regeln:
- Original bleibt immer sichtbar; Übersetzung ist als "maschinell übersetzt" markiert.
- Zahlen werden geprüft: stimmen sie nicht überein, wird eine Warnung angezeigt.
- Ohne Modell gibt es keine Übersetzung (None) – nie eine geratene.
"""
from __future__ import annotations

import hashlib
import logging

from ..core.db import Database, utcnow
from ..core.languages import BY_CODE
from .safety import numbers_in

log = logging.getLogger(__name__)

ENGLISH_NAMES = {
    "de": "German", "gsw": "Swiss German", "fr": "French", "it": "Italian", "en": "English", "pt": "Portuguese",
    "es": "Spanish", "sq": "Albanian", "bks": "Bosnian/Croatian/Serbian", "tr": "Turkish", "ar": "Arabic",
    "uk": "Ukrainian", "ru": "Russian", "pl": "Polish", "ro": "Romanian", "hu": "Hungarian", "mk": "Macedonian",
    "el": "Greek", "fa": "Persian (Dari/Farsi)", "ps": "Pashto", "ku": "Kurdish", "ti": "Tigrinya", "am": "Amharic",
    "so": "Somali", "ta": "Tamil", "hi": "Hindi", "ur": "Urdu", "zh": "Chinese", "vi": "Vietnamese", "th": "Thai",
}


def lang_name(code: str) -> str:
    return ENGLISH_NAMES.get(code) or (BY_CODE[code].native if code in BY_CODE else code)


PROMPT = (
    "You are a professional {src} ({src_code}) to {tgt} ({tgt_code}) translator in a physiotherapy practice. "
    "Accurately convey the meaning of the original {src} text, keep all numbers, body parts and left/right exactly, "
    "and adhere to {tgt} grammar and vocabulary. Produce only the {tgt} translation, without any additional "
    "explanations or commentary. Please translate the following {src} text into {tgt}:\n\n\n{text}"
)


class TranslationResult:
    def __init__(self, text: str | None, engine: str, warning: str | None = None, cached: bool = False):
        self.text = text
        self.engine = engine
        self.warning = warning
        self.cached = cached


class NoTranslator:
    name = "none"

    def available(self) -> bool:
        return False

    def translate(self, text: str, source: str, target: str) -> TranslationResult:
        return TranslationResult(None, self.name)


class OllamaTranslator:
    name = "ollama"

    def __init__(self, llm):
        self.llm = llm

    def available(self) -> bool:
        return self.llm.available()

    def translate(self, text: str, source: str, target: str) -> TranslationResult:
        if not text.strip() or source == target:
            return TranslationResult(text, "identity")
        prompt = PROMPT.format(src=lang_name(source), src_code=source, tgt=lang_name(target), tgt_code=target, text=text)
        out = self.llm.generate(prompt).strip()
        warning = None
        if numbers_in(text) != numbers_in(out):
            warning = "translate.numbers_differ"
        return TranslationResult(out, f"{self.name}:{self.llm.model}", warning)


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def translate_cached(db: Database, practice_id: int, translator, text: str, source: str, target: str) -> TranslationResult:
    """Übersetzen mit Zwischenspeicher pro Praxis."""
    if not text or not text.strip() or source == target:
        return TranslationResult(text, "identity")
    h = _hash(text)
    row = db.one(
        "SELECT text, engine FROM translations WHERE practice_id = ? AND source_hash = ? AND source_lang = ? AND target_lang = ?",
        (practice_id, h, source, target),
    )
    if row is not None:
        return TranslationResult(row["text"], row["engine"], cached=True)
    result = translator.translate(text, source, target)
    if result.text and result.engine not in ("identity", "none"):
        db.execute(
            "INSERT OR IGNORE INTO translations (practice_id, source_hash, source_lang, target_lang, text, engine, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (practice_id, h, source, target, result.text, result.engine, utcnow()),
        )
    return result


def cached(db: Database, practice_id: int, text: str | None, source: str, target: str):
    """Nur nachschlagen (ohne Modell): gespeicherte Übersetzung {text, engine} oder None – für Seiten, die nicht warten dürfen."""
    if not text or not text.strip() or source == target:
        return None
    return db.one(
        "SELECT text, engine FROM translations WHERE practice_id = ? AND source_hash = ? AND source_lang = ? AND target_lang = ?",
        (practice_id, _hash(text), source, target),
    )


def store_translation(db: Database, practice_id: int, text: str, source: str, target: str, translated: str, engine: str) -> None:
    """Für Demo-Daten und manuell geprüfte Übersetzungen."""
    db.execute(
        "INSERT OR REPLACE INTO translations (practice_id, source_hash, source_lang, target_lang, text, engine, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (practice_id, _hash(text), source, target, translated, engine, utcnow()),
    )
