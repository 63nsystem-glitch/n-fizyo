"""Diktat → strukturierter Notizentwurf (Befund / Behandlung / Beurteilung / Plan).

Regel-Engine: sortiert Sätze nach Stichworten und liest Messwerte (Grad, VAS/EVA).
Sprachmodell (optional): formuliert die Abschnitte. Enthält die Ausgabe Zahlen,
die nicht im Diktat stehen, wird sie verworfen und die Regel-Engine verwendet.
Messwerte kommen immer aus der Regel-Engine (nachvollziehbar).
"""
from __future__ import annotations

import json
import logging
import re

from .safety import invented_numbers
from .types import SECTIONS, Measurement, NoteContext, NoteDraft

log = logging.getLogger(__name__)

KEYWORDS = {
    "befund": (
        r"befund", r"flexion", r"extension", r"abduktion", r"rotation", r"schmerz", r"vas", r"nrs", r"schwellung",
        r"erguss", r"kraft", r"beweglichkeit", r"gangbild", r"palpation", r"druckdolent", r"berichtet", r"gibt an",
        r"klagt", r"rom", r"umfang", r"messung", r"test", r"positiv", r"negativ", r"hinkt", r"stöcke",
        r"bilan", r"douleur", r"eva", r"œdème", r"oedème", r"gonflement", r"force", r"mobilité", r"marche",
        r"rapporte", r"signale", r"se plaint", r"amplitude", r"mesure",
    ),
    "behandlung": (
        r"behandlung", r"behandelt", r"mobilisation", r"mobilisiert", r"lymphdrainage", r"mld", r"training",
        r"übung", r"kräftigung", r"manuelle therapie", r"massage", r"dehnung", r"instruiert", r"instruktion",
        r"gangschule", r"elektrotherapie", r"taping", r"durchgeführt", r"geübt", r"angeleitet", r"weichteil",
        r"traitement", r"mobilisé", r"drainage", r"renforcement", r"exercice", r"thérapie manuelle", r"étirement",
        r"instruit", r"réalisé", r"école de marche", r"travaillé",
    ),
    "beurteilung": (
        r"beurteilung", r"verlauf", r"besser", r"verbessert", r"fortschritt", r"unverändert", r"verschlechtert",
        r"zufrieden", r"ziel erreicht", r"im vergleich", r"gegenüber",
        r"évaluation", r"évolution", r"amélior", r"progrès", r"inchangé", r"aggrav", r"objectif atteint", r"par rapport",
    ),
    "plan": (
        r"plan", r"nächste", r"weiter", r"fortsetzen", r"steigern", r"termin", r"kontrolle", r"heimprogramm",
        r"arzt", r"bericht", r"ab nächster", r"geplant",
        r"prochaine", r"poursuivre", r"continuer", r"augmenter", r"rendez-vous", r"contrôle", r"programme à domicile",
        r"médecin", r"rapport", r"prévu",
    ),
}
_KW = {sec: re.compile(r"(?<![\w])(?:" + "|".join(words) + r")", re.IGNORECASE) for sec, words in KEYWORDS.items()}
_HEADER = re.compile(
    r"^\s*(befund|behandlung|beurteilung|plan|bilan|traitement|évaluation|evaluation|appréciation)\s*[:\-–]\s*",
    re.IGNORECASE,
)
_HEADER_MAP = {
    "befund": "befund", "bilan": "befund",
    "behandlung": "behandlung", "traitement": "behandlung",
    "beurteilung": "beurteilung", "évaluation": "beurteilung", "evaluation": "beurteilung", "appréciation": "beurteilung",
    "plan": "plan",
}

# Messwerte
_FILL = r"(?:\s*(?:von|de|à|bis|auf|:|aktiv|passiv|heute|aktuell|active|passive|actuelle(?:ment)?))*\s*"
_NUM = r"((?:minus|moins|-)\s*)?(\d{1,3})\s*(?:°|grad|degrés?|degré)"
MEASURE_RX = [
    ("flexion", re.compile(r"(?:flexion|flex\.?|beugung)" + _FILL + _NUM, re.IGNORECASE)),
    ("extension", re.compile(r"(?:extension|ext\.?|streckung)" + _FILL + _NUM, re.IGNORECASE)),
    ("abduction", re.compile(r"(?:abduktion|abduction|abd\.?)" + _FILL + _NUM, re.IGNORECASE)),
    ("external_rotation", re.compile(r"(?:aussenrotation|außenrotation|rotation externe)" + _FILL + _NUM, re.IGNORECASE)),
    ("internal_rotation", re.compile(r"(?:innenrotation|rotation interne)" + _FILL + _NUM, re.IGNORECASE)),
    ("elevation", re.compile(r"(?:elevation|élévation)" + _FILL + _NUM, re.IGNORECASE)),
]
NEUTRAL_ZERO = re.compile(r"(?:ext(?:ension)?\s*/\s*flex(?:ion)?|e\s*/\s*f)\s*(\d{1,3})\s*[-–/]\s*(\d{1,3})\s*[-–/]\s*(\d{1,3})", re.IGNORECASE)
PAIN_RX = re.compile(
    r"(?:vas|nrs|eva|schmerz(?:en)?|douleurs?)\s*(?:von|de|bei|à|auf|:)?\s*(\d{1,2})(?:[.,]\d)?\s*(?:/\s*10|von\s*10|sur\s*10)?",
    re.IGNORECASE,
)
JOINTS = {
    "knee": re.compile(r"(?<![\w])(knie\w*|genou)", re.IGNORECASE),
    "shoulder": re.compile(r"(?<![\w])(schulter\w*|épaule)", re.IGNORECASE),
    "hip": re.compile(r"(?<![\w])(hüft\w*|hanche)", re.IGNORECASE),
    "elbow": re.compile(r"(?<![\w])(ellbogen\w*|ellenbogen\w*|coude)", re.IGNORECASE),
    "ankle": re.compile(r"(?<![\w])(osg|sprunggelenk\w*|cheville)", re.IGNORECASE),
    "wrist": re.compile(r"(?<![\w])(handgelenk\w*|poignet)", re.IGNORECASE),
    "cervical": re.compile(r"(?<![\w])(hws|nacken|cervical\w*)", re.IGNORECASE),
    "lumbar": re.compile(r"(?<![\w])(lws|lenden\w*|lombaire)", re.IGNORECASE),
}
# Seiten (wie knowledge.anatomy.detect_side, hier mit Position im Text)
_SIDE_RX = (
    ("b", re.compile(r"(?<![\w])(beidseits|beidseitig|bds\.?|bilateral\w*|des deux côtés)(?![\w])", re.IGNORECASE)),
    ("r", re.compile(r"(?<![\w])(rechts|rechte[nrs]?|re\.|droite?s?)(?![\w])(?! (?:de l'abdomen|fémoral|antérieur))", re.IGNORECASE)),
    ("l", re.compile(r"(?<![\w])(links|linke[nrs]?|li\.|gauches?)(?![\w])", re.IGNORECASE)),
)
# «Ausstrahlung ins linke Bein bis zum Knie»: das Knie ist hier nicht das gemessene Gelenk
_RADIATION = re.compile(r"(ausstrahl\w*|irradi\w*|bis\s+(?:zum|zur|ins|in)|jusqu'\s*(?:au|à))\b[^.;!?,]*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?;](?=\s|$)")


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.replace("\n", ". ")).strip()
    parts = re.split(r"(?<=[.!?;])\s+", text)
    return [p.strip() for p in parts if p.strip(" .")]


class _Locator:
    """Gelenk und Seite für einen Messwert an einer Stelle im Diktat.

    Was im Diktat steht, gilt – die Akte (NoteContext) ist nur Ersatz, wenn nichts diktiert wurde. Nennt das Diktat eine
    Seite, die sich keinem Messwert sicher zuordnen lässt, bleibt die Seite leer (lieber keine Seite als die falsche)."""

    def __init__(self, text: str, ctx: NoteContext):
        self.ctx = ctx
        self.spans: list[tuple[int, int]] = []
        start = 0
        for m in _SENTENCE_END.finditer(text):
            self.spans.append((start, m.end()))
            start = m.end()
        self.spans.append((start, len(text) + 1))
        self.joints: list[tuple[int, str]] = []
        for name, rx in JOINTS.items():
            for m in rx.finditer(text):
                lo = self.span(m.start())[0]
                if not _RADIATION.search(text[lo:m.start()]):
                    self.joints.append((m.start(), name))
        self.joints.sort()
        taken: list[tuple[int, int]] = []
        self.sides: list[tuple[int, str, str | None]] = []   # (Position, Seite, zugehöriges Gelenk)
        for side, rx in _SIDE_RX:
            for m in rx.finditer(text):
                if any(a <= m.start() < b for a, b in taken) or _RADIATION.search(text[self.span(m.start())[0]:m.start()]):
                    continue
                taken.append((m.start(), m.end()))
                self.sides.append((m.start(), side, None))
        self.sides = sorted((pos, side, self._owner(pos)) for pos, side, _ in self.sides)

    def span(self, pos: int) -> tuple[int, int]:
        for a, b in self.spans:
            if a <= pos < b:
                return a, b
        return self.spans[-1]

    def _owner(self, pos: int) -> str | None:
        """Gelenk, zu dem eine Seitenangabe gehört: nächstes Gelenk im selben Satz, sonst das zuletzt genannte."""
        a, b = self.span(pos)
        near = [(abs(p - pos), j) for p, j in self.joints if a <= p < b]
        if near:
            return min(near)[1]
        before = [j for p, j in self.joints if p < a]
        return before[-1] if before else None

    def joint_at(self, pos: int) -> str | None:
        a, b = self.span(pos)
        here_before = [j for p, j in self.joints if a <= p < pos]
        here_after = [j for p, j in self.joints if pos < p < b]
        earlier = [j for p, j in self.joints if p < a]
        return (here_before[-1:] or here_after[:1] or earlier[-1:] or [None])[0]

    def side_at(self, pos: int, joint: str | None) -> str | None:
        mine = [(p, s) for p, s, owner in self.sides if owner in (None, joint)]
        if not mine:                       # keine Seite für dieses Gelenk diktiert → Seite aus der Akte, nur für dasselbe Gelenk
            ctx = self.ctx
            return ctx.side if ctx.side in ("l", "r") and (joint is None or joint == ctx.joint) else None
        a, b = self.span(pos)
        here_before = [s for p, s in mine if a <= p < pos]
        here_after = [s for p, s in mine if pos < p < b]
        earlier = [s for p, s in mine if p < a]
        later = {s for p, s in mine if p >= b}
        side = (here_before[-1:] or here_after[:1] or earlier[-1:] or [None])[0]
        if side is None and len(later) == 1:
            side = next(iter(later))
        return side if side in ("l", "r") else None

    def locate(self, pos: int) -> tuple[str | None, str | None]:
        joint = self.joint_at(pos)
        if joint is None:
            joint = self.ctx.joint
        return joint, self.side_at(pos, joint)


def extract_measurements(text: str, ctx: NoteContext | None = None) -> list[Measurement]:
    ctx = ctx or NoteContext()
    loc = _Locator(text, ctx)
    out: list[Measurement] = []
    seen: set[tuple] = set()

    def add(kind: str, value: float, unit: str, pos: int) -> None:
        joint, side = loc.locate(pos)
        key = (kind, value, joint, side)
        if key not in seen:
            seen.add(key)
            out.append(Measurement(kind=kind, value=value, unit=unit, joint=joint, side=side))

    for m in NEUTRAL_ZERO.finditer(text):
        a, b, c = (int(x) for x in m.groups())
        if b == 0:
            add("extension", float(a), "°", m.start())
            add("flexion", float(c), "°", m.start())
        elif a == 0 and b > 0:
            add("extension", float(-b), "°", m.start())  # Streckdefizit
            add("flexion", float(c), "°", m.start())
    for kind, rx in MEASURE_RX:
        for m in rx.finditer(text):
            value = float(m.group(2)) * (-1 if m.group(1) else 1)
            if 0 <= abs(value) <= 200:
                add(kind, value, "°", m.start())
    for m in PAIN_RX.finditer(text):
        value = float(m.group(1))
        if 0 <= value <= 10:
            add("pain_vas", value, "/10", m.start())
    return out


def context_differs(measurements: list[Measurement], ctx: NoteContext | None) -> dict | None:
    """Diktat und Akte nennen ein anderes Gelenk / eine andere Seite? → Hinweis für die Prüfseite (die Messwerte folgen dem
    Diktat). {"dictated": [{"joint", "side"}, …], "stored": {"joint", "side"}} oder None."""
    if ctx is None or not (ctx.joint or ctx.side in ("l", "r")):
        return None
    dictated: list[dict] = []
    for m in measurements:
        other_joint = bool(ctx.joint and m.joint and m.joint != ctx.joint)
        other_side = bool(ctx.side in ("l", "r") and m.side in ("l", "r") and m.side != ctx.side)
        loc = {"joint": m.joint, "side": m.side}
        if (other_joint or other_side) and loc not in dictated:
            dictated.append(loc)
    if not dictated:
        return None
    return {"dictated": dictated, "stored": {"joint": ctx.joint, "side": ctx.side if ctx.side in ("l", "r") else None}}


# Sätze an die Patientin / den Patienten («Zu Hause …», «Melden Sie sich, wenn …») gehören in den Plan, nie in den Befund –
# auch wenn sie Stichworte des Befunds enthalten («Kühlen bei Schmerzen»).
_ADVICE = re.compile(
    r"^\s*(?:für\s+)?(?:zu ?hause|daheim|hausaufgabe\w*|à la maison|chez (?:vous|soi)|à domicile)\b"
    r"|\b(?:melden sie sich|sich (?:bitte |sofort |umgehend )?(?:bei uns |bei mir )?melden|soll(?:te|en)? sich (?:\w+ ){0,4}melden"
    r"|rufen sie (?:\w+ ){0,3}an|appelez|contactez|nous (?:appeler|contacter|recontacter)|doit (?:nous )?(?:appeler|contacter|recontacter))",
    re.IGNORECASE,
)


def draft_rules(transcript: str, ctx: NoteContext | None = None) -> NoteDraft:
    sections: dict[str, list[str]] = {s: [] for s in SECTIONS}
    ordered: list[tuple[str, str]] = []          # (Abschnitt, Satz) in der Reihenfolge des Diktats
    current = "befund"
    for sentence in _sentences(transcript):
        header = _HEADER.match(sentence)
        if header:
            current = _HEADER_MAP.get(header.group(1).lower(), current)
            sentence = sentence[header.end():].strip()
            if not sentence:
                continue
            if _ADVICE.search(sentence):
                current = "plan"
        elif _ADVICE.search(sentence):
            current = "plan"                     # «Zu Hause …», «Melden Sie sich, wenn …»: Plan / Info für die Patientin
        else:
            scores = {sec: len(rx.findall(sentence)) for sec, rx in _KW.items()}
            best = max(scores.values())
            if best > 0:
                # bei Gleichstand bleibt der aktuelle Abschnitt
                if scores.get(current, 0) < best:
                    current = max(SECTIONS, key=lambda s: (scores[s], -SECTIONS.index(s)))
        sections[current].append(sentence)
        ordered.append((current, sentence))
    draft = NoteDraft(**{sec: " ".join(lines).strip() for sec, lines in sections.items()})
    # Messwerte nur aus Befund/Behandlung/Beurteilung – Zielwerte im Plan sind keine Messungen
    measured = " ".join(sentence for sec, sentence in ordered if sec != "plan")
    draft.measurements = extract_measurements(measured, ctx) if measured.strip() else []
    draft.context_differs = context_differs(draft.measurements, ctx)
    draft.engine = "rules"
    return draft


LLM_SYSTEM = {
    "de": (
        "Du bist eine Dokumentationshilfe für Physiotherapie in der Schweiz. Ordne das Diktat in vier Abschnitte: "
        "befund, behandlung, beurteilung, plan. Schreibe sachlich in vollständigen Sätzen auf Deutsch (Schweizer "
        "Rechtschreibung, ss statt ß). Erfinde NICHTS: keine Werte, keine Diagnosen, keine Massnahmen, die nicht "
        "diktiert wurden. Leere Abschnitte bleiben leer. Antworte NUR als JSON mit den Schlüsseln "
        "befund, behandlung, beurteilung, plan."
    ),
    "fr": (
        "Tu aides à documenter des séances de physiothérapie en Suisse. Classe la dictée en quatre parties : "
        "befund (bilan), behandlung (traitement), beurteilung (évaluation), plan. Écris en français, phrases "
        "complètes et factuelles. N'invente RIEN : aucune valeur, aucun diagnostic, aucune mesure non dictés. "
        "Les parties vides restent vides. Réponds UNIQUEMENT en JSON avec les clés befund, behandlung, beurteilung, plan."
    ),
}


def draft_with_llm(transcript: str, lang: str, llm, ctx: NoteContext | None = None) -> NoteDraft:
    raw = llm.generate(f"Diktat:\n\"\"\"\n{transcript}\n\"\"\"", system=LLM_SYSTEM.get(lang, LLM_SYSTEM["de"]), json_mode=True)
    try:
        data = json.loads(raw)
        draft = NoteDraft(**{sec: str(data.get(sec, "") or "").strip() for sec in SECTIONS})
    except (ValueError, TypeError, AttributeError):
        fallback = draft_rules(transcript, ctx)
        fallback.warnings.append("note.llm_invalid")
        return fallback
    combined = " ".join(getattr(draft, s) for s in SECTIONS)
    if invented_numbers(transcript, combined):
        fallback = draft_rules(transcript, ctx)
        fallback.warnings.append("note.llm_numbers_rejected")
        return fallback
    rules = draft_rules(transcript, ctx)
    draft.measurements = rules.measurements
    draft.context_differs = rules.context_differs
    draft.engine = f"llm:{getattr(llm, 'model', 'local')}"
    return draft
