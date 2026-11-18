"""Gesprochene Sprachen der Patientinnen und Patienten.

Unabhängig von der App-Sprache: Eine Patientin kann z. B. Tigrinya sprechen,
die App aber auf Englisch nutzen. Diese Liste steuert Übersetzung und Dolmetscher.

whisper = von der Spracherkennung (Whisper) unterstützt
ui      = App-Oberfläche in dieser Sprache vorhanden (sonst ui_fallback)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    native: str
    de: str
    fr: str
    whisper: bool = True
    ui: bool = False
    ui_fallback: str = "en"
    rtl: bool = False


LANGUAGES: tuple[Language, ...] = (
    Language("de", "Deutsch", "Deutsch", "allemand", ui=True),
    Language("gsw", "Schweizerdeutsch", "Schweizerdeutsch", "suisse allemand", whisper=True, ui=False, ui_fallback="de"),
    Language("fr", "Français", "Französisch", "français", ui=True),
    Language("it", "Italiano", "Italienisch", "italien", ui=True),
    Language("en", "English", "Englisch", "anglais", ui=True),
    Language("pt", "Português", "Portugiesisch", "portugais", ui=True),
    Language("es", "Español", "Spanisch", "espagnol", ui=True),
    Language("sq", "Shqip", "Albanisch", "albanais", ui=True),
    Language("bks", "Bosanski / Hrvatski / Srpski", "Bosnisch/Kroatisch/Serbisch", "bosniaque/croate/serbe", ui=True),
    Language("tr", "Türkçe", "Türkisch", "turc", ui=True),
    Language("ar", "العربية", "Arabisch", "arabe", ui=True, rtl=True),
    Language("uk", "Українська", "Ukrainisch", "ukrainien", ui=True),
    Language("ru", "Русский", "Russisch", "russe", ui_fallback="uk"),
    Language("pl", "Polski", "Polnisch", "polonais"),
    Language("ro", "Română", "Rumänisch", "roumain"),
    Language("hu", "Magyar", "Ungarisch", "hongrois"),
    Language("mk", "Македонски", "Mazedonisch", "macédonien", ui_fallback="bks"),
    Language("el", "Ελληνικά", "Griechisch", "grec"),
    Language("fa", "فارسی / دری", "Persisch/Dari", "persan/dari", rtl=True, ui_fallback="en"),
    Language("ps", "پښتو", "Paschtu", "pachto", rtl=True),
    Language("ku", "Kurdî", "Kurdisch", "kurde", whisper=False, ui_fallback="tr"),
    Language("ti", "ትግርኛ", "Tigrinya", "tigrigna", whisper=False),
    Language("am", "አማርኛ", "Amharisch", "amharique"),
    Language("so", "Soomaali", "Somali", "somali"),
    Language("ta", "தமிழ்", "Tamil", "tamoul"),
    Language("hi", "हिन्दी", "Hindi", "hindi"),
    Language("ur", "اردو", "Urdu", "ourdou", rtl=True),
    Language("zh", "中文", "Chinesisch", "chinois"),
    Language("vi", "Tiếng Việt", "Vietnamesisch", "vietnamien"),
    Language("th", "ไทย", "Thai", "thaï"),
)

BY_CODE = {lang.code: lang for lang in LANGUAGES}
# Whisper-Codes: BKS wird je nach Sprecher als bs/hr/sr erkannt
WHISPER_CODE = {"bks": "bs", "gsw": "de"}


def get(code: str | None) -> Language | None:
    return BY_CODE.get(code or "")


def name_for(code: str | None, ui_lang: str) -> str:
    lang = get(code)
    if lang is None:
        return code or ""
    return lang.fr if ui_lang == "fr" else lang.de


def ui_lang_for(code: str | None) -> str:
    lang = get(code)
    if lang is None:
        return "de"
    return lang.code if lang.ui else lang.ui_fallback


def whisper_code(code: str | None) -> str | None:
    lang = get(code)
    if lang is None or not lang.whisper:
        return None
    return WHISPER_CODE.get(lang.code, lang.code)


def from_whisper(code: str | None) -> str | None:
    if code in {"bs", "hr", "sr"}:
        return "bks"
    return code if code in BY_CODE else None
