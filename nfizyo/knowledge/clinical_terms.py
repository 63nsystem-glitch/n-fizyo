"""Klinische Begriffe für die Berichtsauswertung (DE/FR).

Komplikationen und Warnzeichen werden der Therapeutin / dem Therapeuten nur
angezeigt (mit Zitat aus dem Bericht). Die KI stellt keine Diagnose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

I = re.IGNORECASE


def _rx(*parts: str) -> re.Pattern:
    return re.compile(r"(?<![\w])(?:" + "|".join(parts) + r")", I)


PATHOLOGY = _rx(
    # Deutsch
    r"\w*ruptur", r"\w*riss\b", r"läsion", r"\w*fraktur", r"tendinopathie", r"tendinitis", r"tendinose", r"\w*itis\b",
    r"\w*arthros", r"luxation", r"subluxation", r"impingement", r"hernie", r"protrusion", r"prolaps", r"vorfall",
    r"stenose", r"kontusion", r"distorsion", r"insuffizienz", r"instabilität", r"ödem", r"erguss", r"zerrung",
    r"\w*syndrom", r"degenerat", r"verkalkung", r"tendinosis calcarea", r"chondropathie", r"knorpelschaden", r"bone bruise",
    # Französisch
    r"rupture", r"déchirure", r"lésion", r"fracture", r"tendinopathie", r"tendinite", r"\w*arthrose", r"entorse",
    r"conflit", r"hernie", r"protrusion", r"sténose", r"contusion", r"instabilité", r"œdème", r"oedème",
    r"épanchement", r"élongation", r"syndrome", r"dégénérat", r"calcification", r"chondropathie",
)

PROCEDURE = _rx(
    r"\w*plastik", r"rekonstruktion", r"\w*naht", r"refixation", r"arthroskopi\w*", r"osteosynthese", r"\w*-?tep\b",
    r"\w*prothese", r"spondylodese", r"dekompression", r"akromioplastik", r"meniskektomie", r"teilresektion",
    r"operiert", r"operation", r"\bop\b", r"eingriff", r"implantation",
    r"plastie", r"ligamentoplastie", r"reconstruction", r"suture", r"arthroscopie", r"ostéosynthèse", r"prothèse",
    r"\bptg\b", r"\bpth\b", r"spondylodèse", r"décompression", r"acromioplastie", r"méniscectomie", r"opéré",
    r"intervention", r"chirurgie", r"r[ée]insertion", r"suture",
)

NEGATION = re.compile(
    r"(?<![\w])(kein|keine|keinen|keiner|keinerlei|ohne|nicht|kein hinweis auf|keine zeichen|ausgeschlossen|"
    r"pas de|pas d'|sans|aucun|aucune|absence de|exclu|exclue|négati\w*)(?![\w])",
    I,
)


@dataclass(frozen=True)
class Term:
    id: str
    de: str
    fr: str
    pattern: re.Pattern


COMPLICATIONS: tuple[Term, ...] = (
    Term("wound", "Wundheilungsstörung", "Trouble de cicatrisation",
         _rx(r"wundheilungsstörung", r"wunddehiszenz", r"trouble de (?:la )?cicatrisation", r"désunion")),
    Term("infection", "Infekt", "Infection",
         _rx(r"\w*infekt\w*", r"protheseninfekt", r"infection", r"infecté", r"sepsis", r"septique")),
    Term("thrombosis", "Thrombose", "Thrombose",
         _rx(r"\w*thrombose", r"\btvt\b", r"\btvp\b", r"thrombose veineuse")),
    Term("embolism", "Lungenembolie", "Embolie pulmonaire", _rx(r"lungenembolie", r"embolie pulmonaire")),
    Term("arthrofibrosis", "Arthrofibrose / Steifigkeit", "Arthrofibrose / raideur",
         _rx(r"arthrofibrose", r"zyklopsläsion", r"cyclope", r"raideur")),
    Term("crps", "CRPS (Morbus Sudeck)", "SDRC (algodystrophie)",
         _rx(r"\bcrps\b", r"morbus sudeck", r"sudeck", r"\bsdrc\b", r"algodystrophie", r"algoneurodystrophie")),
    Term("nerve", "Nervenläsion", "Lésion nerveuse",
         _rx(r"nervenläsion", r"nervenschaden", r"parese", r"hypästhesie", r"parästhesie", r"lésion nerveuse",
             r"parésie", r"hypoesthésie", r"paresthésie")),
    Term("nonunion", "Pseudarthrose / verzögerte Heilung", "Pseudarthrose / retard de consolidation",
         _rx(r"pseudarthrose", r"verzögerte (?:knochen)?heilung", r"retard de consolidation")),
    Term("loosening", "Implantatlockerung", "Descellement",
         _rx(r"lockerung", r"descellement", r"implantatversagen")),
    Term("redislocation", "Re-Luxation", "Récidive de luxation", _rx(r"re-?luxation", r"récidive de luxation")),
    Term("rerupture", "Re-Ruptur", "Re-rupture", _rx(r"re-?ruptur", r"re-?rupture", r"transplantatversagen")),
    Term("hematoma", "Hämatom", "Hématome", _rx(r"hämatom", r"hématome")),
    Term("effusion", "Erguss / Schwellung", "Épanchement / tuméfaction",
         _rx(r"\w*erguss", r"épanchement", r"tuméfaction")),
    Term("compartment", "Kompartmentsyndrom", "Syndrome des loges", _rx(r"kompartment", r"syndrome des loges")),
)

RED_FLAGS: tuple[Term, ...] = (
    Term("cauda", "Cauda-equina-Zeichen", "Signes de queue de cheval",
         _rx(r"cauda[- ]equina", r"queue de cheval", r"reithosen", r"anesthésie en selle")),
    Term("bladder", "Blasen-/Mastdarmstörung", "Troubles sphinctériens",
         _rx(r"blasen[- ]?(?:und |/)?mastdarm", r"blasenstörung", r"inkontinenz neu", r"troubles sphinctériens",
             r"rétention urinaire")),
    Term("progressive_paresis", "Zunehmende Lähmung", "Parésie progressive",
         _rx(r"progrediente\w* parese", r"zunehmende\w* (?:lähmung|schwäche)", r"parésie progressive", r"déficit progressif")),
    Term("fever", "Fieber", "Fièvre", _rx(r"fieber", r"fièvre", r"fébrile")),
    Term("weight_loss", "Ungewollter Gewichtsverlust", "Perte de poids involontaire",
         _rx(r"gewichtsverlust", r"perte de poids", r"amaigrissement")),
    Term("night_pain", "Nächtlicher Ruheschmerz", "Douleur nocturne",
         _rx(r"nachtschmerz", r"nächtliche\w* (?:ruhe)?schmerz", r"douleurs? nocturnes?")),
    Term("tumor", "Tumorverdacht", "Suspicion de tumeur",
         _rx(r"tumor", r"malign", r"metasta", r"tumeur", r"néoplas")),
)


NEGATION_AFTER = re.compile(
    r"^\W{0,3}(?:\w+\s+){0,2}(verneint|negativ|nicht vorhanden|ausgeschlossen|nicht nachweisbar|"
    r"exclue?s?|absente?s?|négati\w*|non retrouvée?s?)(?![\w])",
    I,
)


def negated(sentence: str, start: int, end: int | None = None) -> bool:
    """True, wenn die Fundstelle verneint ist: davor ("kein Infekt", "pas de fièvre")
    oder direkt danach ("Nachtschmerz verneint")."""
    window = sentence[max(0, start - 40):start]
    window = re.split(r"[,;:]", window)[-1]
    if NEGATION.search(window):
        return True
    if end is not None:
        after = re.split(r"[,;:.]", sentence[end:end + 40])[0]
        if NEGATION_AFTER.search(after):
            return True
    return False
