"""Berichte auswerten (Arztberichte, OP-Berichte, Verordnungen).

Ergebnis sind VORSCHLÄGE mit wörtlichem Zitat. Die Therapeutin / der Therapeut
übernimmt oder verwirft jeden Vorschlag einzeln. Nichts wird automatisch in die
Akte geschrieben.

Regel-Engine (immer verfügbar) + optional lokales Sprachmodell. Vorschläge des
Sprachmodells werden nur übernommen, wenn ihr Zitat wörtlich im Dokument steht.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from ..knowledge import anatomy
from ..knowledge.clinical_terms import COMPLICATIONS, PATHOLOGY, PROCEDURE, RED_FLAGS, negated
from . import dates
from .types import SUGGESTION_KINDS, ExtractionResult, Suggestion
from ..core.timeutil import today as zurich_today

log = logging.getLogger(__name__)

INSURERS = (
    ("Suva", "UVG"), ("CSS", None), ("Helsana", None), ("Swica", None), ("Sanitas", None), ("Visana", None),
    ("Concordia", None), ("Groupe Mutuel", None), ("Assura", None), ("KPT", None), ("Atupri", None), ("EGK", None),
    ("Sympany", None), ("ÖKK", None), ("AXA", None), ("Allianz", None), ("Zurich", None), ("Baloise", None),
    ("Mobiliar", None), ("Generali", None), ("Vaudoise", None), ("Agrisano", None), ("Aquilana", None),
    ("Galenos", None), ("Philos", None), ("Avenir", None), ("Mutuel", None),
    ("IV-Stelle", "IVG"), ("Invalidenversicherung", "IVG"), ("Office AI", "IVG"), ("Militärversicherung", "MVG"),
    ("Assurance militaire", "MVG"),
)
_INSURER_RX = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n, _ in INSURERS) + r")(?![\w])")
_LAW_RX = {
    "UVG": re.compile(r"(?<![\w])(UVG|LAA|Unfall\w*|accident\w*|Schadennummer|n° de sinistre|numéro de sinistre)(?![\w])", re.IGNORECASE),
    "IVG": re.compile(r"(?<![\w])(IVG|LAI|IV-Stelle|Office AI)(?![\w])"),
    "MVG": re.compile(r"(?<![\w])(MVG|LAM|Militärversicherung|assurance militaire)(?![\w])", re.IGNORECASE),
    "KVG": re.compile(r"(?<![\w])(KVG|LAMal|Krankheit|maladie)(?![\w])", re.IGNORECASE),
}

HEADERS = {
    "diagnosis": re.compile(r"^\s*(diagnosen?|diagnostics?|haupt-?diagnose|nebendiagnosen?)\s*:?\s*(.*)$", re.IGNORECASE),
    "surgery": re.compile(r"^\s*(operation|eingriff|op-?bericht|intervention|opération)\s*:?\s*(.*)$", re.IGNORECASE),
    "plan": re.compile(
        r"^\s*(procedere|prozedere|empfehlung(?:en)?|nachbehandlung|weiteres vorgehen|therapie|physiotherapie|"
        r"recommandations?|suite de traitement|suite|traitement|proc[ée]d[ée]|attitude)\s*:?\s*(.*)$",
        re.IGNORECASE,
    ),
    "findings": re.compile(r"^\s*(befund|befunde|klinik|untersuchung|mri|mrt|röntgen|bildgebung|status|examen|imagerie|irm|radiographie)\s*:?\s*(.*)$", re.IGNORECASE),
    "other": re.compile(r"^\s*(anamnese|verlauf|beurteilung|évolution|anamnèse|appréciation|conclusion|mit freundlichen|meilleures salutations|avec mes meilleures)\b.*$", re.IGNORECASE),
}

RESTRICTION_RX = re.compile(
    r"(?<![\w])(teilbelastung|entlastung|abrollbelastung|orthese|schiene|keine\w*|kein|nicht über|maximal|max\.|limitiert|"
    r"begrenz\w*|karenz|verbot\w*|vermeiden|schonung|sportverbot|"
    r"charge partielle|décharge|attelle|orthèse|pas de|pas d'|pas d’|au-delà|limitée?|éviter|interdit\w*|pas plus de|au maximum)(?![\w])",
    re.IGNORECASE,
)
CLEARANCE_RX = re.compile(
    r"(?<![\w])(freigabe|freigegeben|vollbelastung|volle belastung|erlaubt|ab sofort|darf|wieder aufnehmen|"
    r"autorisée?|autorise|charge complète|reprise|peut reprendre|libre)(?![\w])",
    re.IGNORECASE,
)
LIMIT_VALUE_RX = re.compile(r"\d{1,3}\s*(?:°|grad|kg|degrés)", re.IGNORECASE)
# "Verordnung: 9 Behandlungen …" → ohne Überschrift (die Art steht schon davor: "Verordnung: 9 Behandlungen …")
PRESCRIPTION_LABEL_RX = re.compile(r"^\s*(?:ärztliche\s+)?(?:verordnung|verordnet|prescription|ordonnance)\s*:\s*", re.IGNORECASE)

SESSIONS_RX = re.compile(
    r"(?<![\d])(\d{1,2})\s*(?:x\s*)?(?:sitzungen|behandlungen|physiotherapie|séances|x physiothérapie|x physio)",
    re.IGNORECASE,
)
PATIENT_RX = re.compile(
    r"(?:Herr|Frau|Monsieur|Madame|M\.|Mme)\s+([A-ZÄÖÜÉÈ][\w\-’']+(?:\s+[A-ZÄÖÜÉÈ][\w\-’']+){1,2})"
    r"\s*,?\s*(?:geb\.?|geboren(?: am)?|né(?:e)? le|née?|DN|\*)\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4})",
)
PATIENT_LABEL_RX = re.compile(
    r"(?:Patient(?:in)?|Betrifft|Concerne|Patient\(e\)|Name)\s*:\s*(?:Herr|Frau|Monsieur|Madame|M\.|Mme)?\s*"
    r"([A-ZÄÖÜÉÈ][\w\-’']+(?:,?\s+[A-ZÄÖÜÉÈ][\w\-’']+){1,2})",
)
BIRTH_RX = re.compile(r"(?:geb\.?|geboren(?: am)?|Geburtsdatum:?|né(?:e)? le|date de naissance:?|DN:?)\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4})", re.IGNORECASE)
PLACE_DATE_RX = re.compile(
    r"^[A-ZÄÖÜÉÈ][\w\-. ]{1,30},\s*(?:den |le )?(" + dates.NUMERIC.pattern + "|" + dates.WORDS.pattern + ")",
    re.IGNORECASE | re.MULTILINE,
)
LABEL_DATE_RX = re.compile(r"(?:Datum|Date)\s*:\s*(" + dates.NUMERIC.pattern + "|" + dates.WORDS.pattern + ")", re.IGNORECASE)
FRENCH_HINTS = re.compile(r"(?<![\w])(le|la|les|du|des|avec|pour|genou|épaule|patient(?:e)?|séances?|semaines?|chirurgie)(?![\w])", re.IGNORECASE)
GERMAN_HINTS = re.compile(r"(?<![\w])(der|die|das|und|mit|für|nach|knie|schulter|wochen|sitzungen|patientin?)(?![\w])", re.IGNORECASE)
BULLET = re.compile(r"^\s*(?:[-•*·–]|\d{1,2}[.)])\s*")


def detect_language(text: str) -> str:
    fr = len(FRENCH_HINTS.findall(text))
    de = len(GERMAN_HINTS.findall(text))
    return "fr" if fr > de else "de"


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-ZÄÖÜÉÈ0-9])|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _clean(s: str) -> str:
    s = BULLET.sub("", s).strip()
    return re.sub(r"\s+", " ", s).strip(" .;:")


def _best_structure(sentence: str):
    matches = anatomy.find_structures(sentence)
    if not matches:
        return None, []
    # spezifische Strukturen (Band, Sehne, Muskel, Meniskus) vor Gelenk/Knochen
    order = {"ligament": 0, "meniscus": 0, "tendon": 0, "disc": 0, "muscle": 1, "nerve": 1, "bursa": 1,
             "fascia": 1, "group": 2, "joint": 3, "spine": 3, "bone": 4}
    best = sorted(matches, key=lambda m: (order.get(m.structure.type, 5), m.start))[0]
    return best.structure, matches


IMMEDIATE_RX = re.compile(r"(?<![\w])(ab sofort|sofort|dès maintenant|immédiatement|dès à présent)(?![\w])", re.IGNORECASE)
IMPROVING_RX = re.compile(r"(?<![\w])(rückläufig|regredient|abnehmend|besser|en régression|en diminution|diminué|amélior\w*)(?![\w])", re.IGNORECASE)
FROM_WEEK_RX = re.compile(
    r"(?:ab (?:der )?|dès (?:la )?|à partir de (?:la )?)(\d{1,2})\.?\s*(?:e|ème)?\s*(?:postoperativen\s*|postopératoire\s*)?(?:woche|semaine)",
    re.IGNORECASE,
)


def _locate(s: Suggestion, sentence: str, context_side: str | None, fallback=None) -> Suggestion:
    """Struktur, Seite und Region bestimmen. fallback = (Struktur, Seite) des Hauptbefunds."""
    structure, _ = _best_structure(sentence)
    s.levels = anatomy.spine_levels(sentence)
    if structure is None and fallback is not None and fallback[0] is not None:
        structure = fallback[0]
        context_side = anatomy.detect_side(sentence) or fallback[1]
    if structure is not None:
        s.structure_id = structure.id
        side = anatomy.detect_side(sentence) or context_side
        s.side = side if structure.sided else None
        regions = anatomy.muscle_regions(structure, s.side)
        s.region = regions[0] if regions else None
    return s


class _Collector:
    def __init__(self) -> None:
        self.items: list[Suggestion] = []
        self._keys: set[tuple] = set()

    def add(self, s: Suggestion) -> None:
        if s.kind not in SUGGESTION_KINDS or not s.text:
            return
        key = (s.kind, s.text.lower()[:80])
        if key in self._keys:
            return
        if s.kind == "surgery" and s.on_date and any(
            o.kind == "surgery" and o.on_date == s.on_date and o.structure_id == s.structure_id for o in self.items
        ):
            return
        self._keys.add(key)
        self.items.append(s)


def extract_rules(text: str, today: date | None = None) -> ExtractionResult:
    today = today or zurich_today()
    text = text.replace("\r", "")
    lang = detect_language(text)
    out = _Collector()
    warnings: list[str] = []

    # --- Kopf: Patient, Geburtsdatum, Dokumentdatum
    patient_name = birth = None
    m = PATIENT_RX.search(text)
    if m:
        patient_name, birth = m.group(1).strip(), dates.parse_first(m.group(2))
    else:
        m = PATIENT_LABEL_RX.search(text)
        if m:
            patient_name = m.group(1).replace(",", "").strip()
        m = BIRTH_RX.search(text)
        if m:
            birth = dates.parse_first(m.group(1))
    doc_date = None
    m = PLACE_DATE_RX.search(text) or LABEL_DATE_RX.search(text)
    if m:
        doc_date = dates.parse_first(m.group(1))
    if doc_date is None:
        for _, d in dates.all_dates(text):
            if d != birth and d <= today:
                doc_date = d
                break
    context_side = anatomy.detect_side(text[:600])

    # --- Abschnitte
    section = "other"
    surgery_date: date | None = None
    lines = text.split("\n")
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        rest = None
        for name, rx in HEADERS.items():
            hm = rx.match(line)
            if hm:
                section = name
                rest = hm.group(2).strip() if hm.lastindex and hm.lastindex >= 2 else ""
                break
        content = rest if rest is not None else line
        if not content:
            continue
        if section == "diagnosis":
            for part in re.split(r"\s*;\s*", content):
                item = _clean(part)
                if len(item) < 4:
                    continue
                kind = "surgery" if PROCEDURE.search(item) else "diagnosis"
                d = dates.parse_first(item) if kind == "surgery" else None
                if d:
                    surgery_date = surgery_date or d
                out.add(_locate(Suggestion(kind=kind, text=item, excerpt=part.strip(), on_date=d), item, context_side))
        elif section == "findings":
            for sentence in _sentences(content):
                item = _clean(sentence)
                if len(item) >= 6 and anatomy.find_structures(item):
                    neg = any(negated(item, m.start, m.end) for m in anatomy.find_structures(item))
                    if not neg:
                        out.add(_locate(Suggestion(kind="finding", text=item, excerpt=sentence), item, context_side))
        elif section == "surgery":
            item = _clean(content)
            if len(item) >= 4 and (PROCEDURE.search(item) or anatomy.find_structures(item)):
                d = dates.parse_first(item) or doc_date
                surgery_date = surgery_date or d
                out.add(_locate(Suggestion(kind="surgery", text=item, excerpt=content, on_date=d), item, context_side))

    base = surgery_date or doc_date or today

    # Hauptbefund (für Einschränkungen ohne eigene Körperstelle): Gelenk der ersten Diagnose
    primary = None
    for item in out.items:
        st = anatomy.get(item.structure_id)
        if st is not None:
            joint_struct = next(
                (j for j in anatomy.STRUCTURES if j.type == "joint" and j.joint and j.joint == st.joint), None
            )
            primary = (joint_struct or st, item.side)
            break

    # --- Satzweise: Einschränkungen, Freigaben, Verordnung, Befunde, Komplikationen, Warnzeichen
    for sentence in _sentences(text):
        clean = _clean(sentence)
        for rx in HEADERS.values():  # "Diagnose: …" → "…"
            hm = rx.match(clean)
            if hm and hm.lastindex and hm.lastindex >= 2 and hm.group(2):
                clean = _clean(hm.group(2))
                break
        if len(clean) < 6:
            continue
        lower = clean.lower()
        # Operation mit Datum im Fliesstext ("Status nach VKB-Plastik rechts am 02.10.2026")
        if PROCEDURE.search(clean) and anatomy.find_structures(clean) and dates.parse_first(clean):
            d = dates.parse_first(clean)
            surgery_date = surgery_date or d
            out.add(_locate(Suggestion(kind="surgery", text=clean, excerpt=sentence, on_date=d), clean, context_side))
        is_restriction = RESTRICTION_RX.search(clean) and (
            dates.WEEKS.search(clean) or dates.UNTIL.search(clean) or LIMIT_VALUE_RX.search(clean)
        )
        is_clearance = CLEARANCE_RX.search(clean) and not re.search(r"(?<![\w])(nicht|keine?|pas)(?![\w])", lower)
        if is_restriction:
            until = dates.duration_end(clean, base)
            out.add(_locate(Suggestion(kind="restriction", text=clean, excerpt=sentence, until=until), clean, context_side, primary))
        elif is_clearance:
            on = dates.parse_first(clean)
            if on is None and IMMEDIATE_RX.search(clean):
                on = doc_date or today
            wm = FROM_WEEK_RX.search(clean)
            if on is None and wm:
                on = base + timedelta(weeks=int(wm.group(1)) - 1)
            elif on is None and dates.WEEKS.search(clean):
                on = dates.duration_end(clean, base)
            out.add(_locate(Suggestion(kind="clearance", text=clean, excerpt=sentence, on_date=on), clean, context_side, primary))
        sm = SESSIONS_RX.search(clean)
        if sm and 1 <= int(sm.group(1)) <= 36:
            rx_text = PRESCRIPTION_LABEL_RX.sub("", clean) or clean
            out.add(_locate(Suggestion(kind="prescription", text=rx_text, excerpt=sentence, sessions=int(sm.group(1))),
                            clean, context_side, primary))
        for term in COMPLICATIONS:
            cm = term.pattern.search(clean)
            if cm and IMPROVING_RX.search(clean):
                # z. B. "Erguss rückläufig" – Verlauf, keine neue Komplikation
                out.add(_locate(Suggestion(kind="finding", text=clean, excerpt=sentence), clean, context_side, primary))
                continue
            if cm and not negated(clean, cm.start(), cm.end()):
                s = Suggestion(kind="complication", text=f"{term.fr if lang == 'fr' else term.de}: {clean}", excerpt=sentence)
                out.add(_locate(s, clean, context_side))
        for term in RED_FLAGS:
            rm = term.pattern.search(clean)
            if rm and not negated(clean, rm.start(), rm.end()):
                out.add(Suggestion(kind="red_flag", text=f"{term.fr if lang == 'fr' else term.de}: {clean}", excerpt=sentence))
        if PATHOLOGY.search(clean) and anatomy.find_structures(clean) and not is_restriction:
            pm = PATHOLOGY.search(clean)
            if not negated(clean, pm.start(), pm.end()):
                already = any(
                    s.kind in ("diagnosis", "surgery") and (s.excerpt.strip() in sentence or sentence in s.excerpt)
                    for s in out.items
                )
                if not already:
                    out.add(_locate(Suggestion(kind="finding", text=clean, excerpt=sentence), clean, context_side))

    # --- Versicherung
    im = _INSURER_RX.search(text)
    law = None
    for name, rx in _LAW_RX.items():
        if rx.search(text):
            law = name
            break
    if im:
        insurer_law = next((lw for n, lw in INSURERS if n == im.group(1)), None)
        out.add(Suggestion(kind="insurer", text=im.group(1), excerpt=im.group(1), insurer=im.group(1), law=insurer_law or law))

    if not out.items:
        warnings.append("extract.nothing_found")
    return ExtractionResult(
        suggestions=out.items,
        patient_name=patient_name,
        patient_birthdate=birth,
        document_date=doc_date,
        language=lang,
        engine="rules",
        warnings=warnings,
    )


# --------------------------------------------------------------------------- Sprachmodell (optional)
LLM_SYSTEM = {
    "de": (
        "Du hilfst einer Physiotherapiepraxis, einen ärztlichen Bericht zu lesen. Erfinde nichts. "
        "Gib NUR JSON zurück: {\"items\": [{\"kind\": ..., \"text\": ..., \"excerpt\": ...}]}. "
        "kind ist eines von: diagnosis, surgery, finding, restriction, clearance, complication, red_flag, prescription. "
        "text = kurze Zusammenfassung auf Deutsch. excerpt = WÖRTLICHES Zitat aus dem Bericht (unverändert kopiert). "
        "Keine eigenen Empfehlungen, keine Diagnosen, die nicht im Bericht stehen."
    ),
    "fr": (
        "Tu aides un cabinet de physiothérapie à lire un rapport médical. N'invente rien. "
        "Réponds UNIQUEMENT en JSON : {\"items\": [{\"kind\": ..., \"text\": ..., \"excerpt\": ...}]}. "
        "kind parmi : diagnosis, surgery, finding, restriction, clearance, complication, red_flag, prescription. "
        "text = court résumé en français. excerpt = citation MOT POUR MOT du rapport (copiée sans modification). "
        "Aucune recommandation propre, aucun diagnostic absent du rapport."
    ),
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def extract_with_llm(text: str, llm, ui_lang: str, base: ExtractionResult, today: date | None = None) -> ExtractionResult:
    """Ergänzt die Regel-Ergebnisse um Vorschläge des lokalen Sprachmodells.
    Nur Vorschläge mit wörtlichem Zitat werden übernommen."""
    prompt = f"Bericht:\n\"\"\"\n{text[:12000]}\n\"\"\""
    raw = llm.generate(prompt, system=LLM_SYSTEM.get(ui_lang, LLM_SYSTEM["de"]), json_mode=True)
    try:
        items = json.loads(raw).get("items", [])
    except (ValueError, AttributeError):
        base.warnings.append("extract.llm_invalid")
        return base
    doc = _norm(text)
    existing = {_norm(s.excerpt) for s in base.suggestions}
    collector = _Collector()
    for s in base.suggestions:
        collector.add(s)
    dropped = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind, summary, excerpt = item.get("kind"), str(item.get("text", "")).strip(), str(item.get("excerpt", "")).strip()
        if kind not in SUGGESTION_KINDS or kind == "insurer" or not summary or not excerpt:
            continue
        if _norm(excerpt) not in doc:
            dropped += 1
            continue
        if any(_norm(excerpt) in e or e in _norm(excerpt) for e in existing):
            continue
        s = Suggestion(kind=kind, text=summary[:300], excerpt=excerpt)
        if kind == "restriction":
            s.until = dates.duration_end(excerpt, base.document_date or today or zurich_today())
        if kind == "prescription":
            m = SESSIONS_RX.search(excerpt)
            s.sessions = int(m.group(1)) if m else None
        collector.add(_locate(s, excerpt, None))
    if dropped:
        base.warnings.append("extract.llm_dropped")
    base.suggestions = collector.items
    base.engine = "rules+llm"
    return base
