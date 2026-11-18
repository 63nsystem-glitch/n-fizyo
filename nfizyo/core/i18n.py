"""Mehrsprachigkeit.

- Praxis-Oberfläche (Personal): Deutsch und Französisch (STAFF_LANGS).
- Patienten-App und Patienten-Tablet: zusätzlich die häufigsten Sprachen der
  Patientinnen und Patienten in der Schweiz (PATIENT_LANGS).

Jedes Modul bringt locales/<sprache>.json mit. Schlüssel sind hierarchisch
("agenda.week"). Fehlt eine Übersetzung, wird Deutsch verwendet.
tests/test_i18n.py prüft: DE und FR vollständig; Patienten-Texte ("p.*",
"intake.*") in allen Patientensprachen vollständig.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

STAFF_LANGS = ("de", "fr")
PATIENT_LANGS = ("de", "fr", "it", "en", "pt", "es", "sq", "bks", "tr", "ar", "uk")
SUPPORTED = STAFF_LANGS  # Rückwärtskompatibel: Sprachen der Praxis-Oberfläche
ALL_LANGS = PATIENT_LANGS
RTL_LANGS = {"ar"}
PATIENT_PREFIXES = ("p.", "intake.", "common.")

# Name jeder Sprache in der Sprache selbst (für Sprachauswahl)
LANG_NAMES = {
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
    "en": "English",
    "pt": "Português",
    "es": "Español",
    "sq": "Shqip",
    "bks": "Bosanski / Hrvatski / Srpski",
    "tr": "Türkçe",
    "ar": "العربية",
    "uk": "Українська",
}


class Translations:
    def __init__(self) -> None:
        self.catalog: dict[str, dict[str, str]] = {lang: {} for lang in ALL_LANGS}

    def load_dirs(self, dirs: Iterable[Path]) -> None:
        for directory in dirs:
            for lang in ALL_LANGS:
                file = directory / f"{lang}.json"
                if file.exists():
                    data = json.loads(file.read_text(encoding="utf-8"))
                    self.catalog[lang].update(_flatten(data))

    def t(self, lang: str, key: str, **kwargs) -> str:
        cat = self.catalog.get(lang) or self.catalog["de"]
        text = cat.get(key) or self.catalog["de"].get(key)
        if text is None and "n" in kwargs:
            # Einzahl/Mehrzahl: key.one / key.few / key.many / key.other (fehlende Form → other)
            text = self._plural(lang, key, kwargs["n"])
        if text is None:
            text = key
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                return text
        return text

    def _plural(self, lang: str, key: str, n) -> str | None:
        try:
            count = int(float(n))
        except (TypeError, ValueError):
            count = 0
        for code in (lang, "de"):
            cat = self.catalog.get(code) or {}
            for full in (f"{key}.{plural_category(code, count)}", f"{key}.other"):
                if full in cat:
                    return cat[full]
        return None

    def tn(self, lang: str, key: str, n, **kwargs) -> str:
        """Text mit Zahl in der richtigen Einzahl/Mehrzahl («1 offene Rechnung», «2 offene Rechnungen»)."""
        return self.t(lang, key, n=n, **kwargs)

    def has(self, lang: str, key: str) -> bool:
        return key in self.catalog.get(lang, {})

    def missing(self) -> dict[str, list[str]]:
        """Fehlende Schlüssel. DE/FR: alle Schlüssel. Übrige Sprachen: nur Patienten-Texte."""
        all_keys = set().union(*(set(self.catalog[lang]) for lang in STAFF_LANGS))
        patient_keys = {k for k in all_keys if k.startswith(PATIENT_PREFIXES)}
        out = {}
        for lang in ALL_LANGS:
            needed = all_keys if lang in STAFF_LANGS else patient_keys
            out[lang] = sorted(needed - set(self.catalog[lang]))
        return out


def plural_category(lang: str, n: int) -> str:
    """Mehrzahl-Kategorie (vereinfacht nach CLDR) für ganze Zahlen: one | two | few | many | zero | other."""
    if lang in ("uk", "bks"):
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            return "few"
        return "many"
    if lang == "ar":
        if n == 0:
            return "zero"
        if n == 1:
            return "one"
        if n == 2:
            return "two"
        if 3 <= n % 100 <= 10:
            return "few"
        if 11 <= n % 100 <= 99:
            return "many"
        return "other"
    if lang == "fr":
        return "one" if n in (0, 1) else "other"
    return "one" if n == 1 else "other"


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in data.items():
        if key.startswith("_"):
            continue  # Kommentare wie "_review"
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, full + "."))
        else:
            out[full] = str(value)
    return out


def normalize(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip().lower().replace("_", "-")
    if code in ALL_LANGS:
        return code
    base = code.split("-")[0]
    if base in {"bs", "hr", "sr", "sh", "cnr"}:
        return "bks"
    return base if base in ALL_LANGS else None


def pick_lang(*candidates: str | None, default: str = "de", allowed: Iterable[str] = STAFF_LANGS) -> str:
    allowed = tuple(allowed)
    for c in candidates:
        code = normalize(c)
        if code and code in allowed:
            return code
    return default


def from_accept_language(header: str | None, allowed: Iterable[str] = STAFF_LANGS) -> str | None:
    if not header:
        return None
    allowed = tuple(allowed)
    for part in header.split(","):
        code = normalize(part.split(";")[0])
        if code and code in allowed:
            return code
    return None


def text_dir(lang: str) -> str:
    return "rtl" if lang in RTL_LANGS else "ltr"
