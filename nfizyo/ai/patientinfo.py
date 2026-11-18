"""Information für Patient/innen aus dem Diktat der Therapeutin / des Therapeuten.

Nur zwei Teile – bewusst kurz und in einfacher Sprache:
  home     «Was Sie zu Hause tun können» (Übungen, Kühlen, Schonen, was vermeiden)
  contact  «Wann Sie sich melden sollten» (wenn es schlimmer wird, Fieber, Rötung, Taubheit …)

Regeln: Die KI erfindet nichts (keine neuen Zahlen, Übungen oder Ratschläge), sie formuliert nur Gesagtes um.
Die Therapeutin / der Therapeut prüft und gibt frei – erst dann wird übersetzt und in der App angezeigt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .llm import LLMUnavailable
from .safety import invented_numbers

HOME_RX = {
    "de": re.compile(r"\b(zu ?hause|daheim|heimprogramm|übung|üben|täglich|pro tag|mal am tag|kühlen|kühl|eis|wärme|wärmen|"
                     r"hochlagern|schonen|belast|vermeiden|nicht heben|keine?n? (?:sport|joggen|heben)|spazier|dehnen|bewegen)", re.I),
    "fr": re.compile(r"\b(à la maison|chez vous|programme|exercice|chaque jour|par jour|fois par jour|glace|froid|chaleur|"
                     r"surélever|ménager|charge|éviter|ne pas porter|marche|étirer|bouger)", re.I),
}
CONTACT_RX = {
    "de": re.compile(r"\b(melden|anrufen|rufen sie|kontakt|wenn .*(schlimmer|zunimmt|stärker)|fieber|rötung|gerötet|schwellung|"
                     r"geschwollen|taub|kribbel|lähmung|notfall|arzt|ärztin)", re.I),
    "fr": re.compile(r"\b(appeler|appelez|contacter|signaler|si .*(aggrave|augmente|pire)|fièvre|rougeur|gonfl|engourdi|"
                     r"fourmill|paralysie|urgence|médecin)", re.I),
}

SYSTEM = {
    "de": (
        "Du schreibst für Patientinnen und Patienten einer Physiotherapie in der Schweiz. Aus dem Diktat der Therapeutin "
        "entnimmst du NUR, was die Person zu Hause tun soll und wann sie sich melden soll. Schreibe in einfacher Sprache, "
        "kurze Sätze, Höflichkeitsform «Sie», Schweizer Rechtschreibung (ss statt ß). Erfinde NICHTS: keine Übungen, Zahlen, "
        "Medikamente oder Ratschläge, die nicht diktiert wurden. Keine Fachwörter, keine Diagnosen. Wenn etwas nicht "
        "diktiert wurde, lass den Teil leer. Antworte NUR als JSON mit den Schlüsseln home und contact."
    ),
    "fr": (
        "Tu écris pour des patient·e·s d'un cabinet de physiothérapie en Suisse. Dans la dictée du/de la thérapeute, tu "
        "reprends SEULEMENT ce que la personne doit faire à la maison et quand elle doit nous contacter. Langage simple, "
        "phrases courtes, vouvoiement. N'invente RIEN : aucun exercice, chiffre, médicament ou conseil non dicté. Pas de "
        "termes médicaux, pas de diagnostic. Si rien n'a été dicté pour une partie, laisse-la vide. Réponds UNIQUEMENT en "
        "JSON avec les clés home et contact."
    ),
}


@dataclass
class PatientInfoDraft:
    home: str = ""
    contact: str = ""
    engine: str = "rules"
    warnings: list[str] = field(default_factory=list)


# Abschnitts-Überschriften des Diktats («Plan:», «Zu Hause:» …) gehören nicht in den Text für die Patientin / den Patienten
_LABEL = re.compile(
    r"^\s*(befund|behandlung|beurteilung|plan|bilan|traitement|évaluation|evaluation|appréciation|zu ?hause|daheim|"
    r"heimprogramm|hausaufgaben?|à la maison|chez vous|programme à domicile)\s*[:\-–]\s*",
    re.I,
)
_CLINICAL = {"befund", "behandlung", "beurteilung", "bilan", "traitement", "évaluation", "evaluation", "appréciation"}
# In Befund/Behandlung/Beurteilung nur Sätze, die sich klar an die Patientin / den Patienten richten
_STRONG = re.compile(r"\b(zu ?hause|daheim|melden|anrufen|rufen sie|à la maison|chez vous|appelez|appeler|contactez|contacter)", re.I)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _strip_label(sentence: str) -> tuple[str, str | None]:
    """(Satz ohne Überschrift, Überschrift klein geschrieben oder None)."""
    m = _LABEL.match(sentence)
    if not m:
        return sentence, None
    rest = sentence[m.end():].strip()
    return (rest[:1].upper() + rest[1:]) if rest else "", m.group(1).lower()


def draft_rules(transcript: str, lang: str) -> PatientInfoDraft:
    """Ohne Sprachmodell: passende Sätze aus dem Diktat übernehmen (unverändert, nur ohne Überschriften wie «Plan:»)."""
    home_rx = HOME_RX.get(lang, HOME_RX["de"])
    contact_rx = CONTACT_RX.get(lang, CONTACT_RX["de"])
    home, contact = [], []
    sentences = _sentences(transcript)
    # Mit Überschriften diktiert: was vor der ersten Überschrift steht, ist Befund (wie im Notizentwurf)
    clinical = any(_LABEL.match(s) for s in sentences)
    for raw in sentences:
        s, label = _strip_label(raw)
        if label is not None:
            clinical = label in _CLINICAL
        if not s or (clinical and not _STRONG.search(s)):
            continue
        if contact_rx.search(s):
            contact.append(s)
        elif home_rx.search(s):
            home.append(s)
    draft = PatientInfoDraft(home=" ".join(home), contact=" ".join(contact), engine="rules")
    if not draft.home and not draft.contact:
        draft.warnings.append("patientinfo.nothing_found")
    return draft


def draft_with_llm(transcript: str, lang: str, llm) -> PatientInfoDraft:
    raw = llm.generate(f"Diktat:\n\"\"\"\n{transcript}\n\"\"\"", system=SYSTEM.get(lang, SYSTEM["de"]), json_mode=True)
    try:
        data = json.loads(raw)
        draft = PatientInfoDraft(home=str(data.get("home", "") or "").strip(), contact=str(data.get("contact", "") or "").strip())
    except (ValueError, TypeError, AttributeError):
        fallback = draft_rules(transcript, lang)
        fallback.warnings.append("patientinfo.llm_invalid")
        return fallback
    if invented_numbers(transcript, f"{draft.home} {draft.contact}"):
        fallback = draft_rules(transcript, lang)
        fallback.warnings.append("patientinfo.llm_numbers_rejected")
        return fallback
    draft.engine = f"llm:{getattr(llm, 'model', 'local')}"
    return draft


def draft(transcript: str, lang: str, llm=None) -> PatientInfoDraft:
    if llm is not None:
        try:
            return draft_with_llm(transcript, lang, llm)
        except LLMUnavailable:
            result = draft_rules(transcript, lang)
            result.warnings.append("patientinfo.llm_unavailable")
            return result
    return draft_rules(transcript, lang)
