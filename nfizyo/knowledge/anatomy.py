"""Anatomie-Suche: findet Strukturen in Berichten und Diktaten (DE/FR) und
übersetzt sie in Markierungen auf Skelett und Muskelkarte."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from .anatomy_data import BY_ID, STRUCTURES, TYPE_NAMES, Structure

SIDED_BONES = {"clavicle", "scapula", "humerus", "radius", "ulna", "hand", "pelvis", "femur", "patella", "tibia", "fibula", "foot", "ribs"}
SIDED_JOINTS = {"shoulder", "ac", "elbow", "wrist", "hip", "knee", "ankle", "sij", "tmj"}
VERTEBRAE = [f"c{i}" for i in range(1, 8)] + [f"t{i}" for i in range(1, 13)] + [f"l{i}" for i in range(1, 6)]


@dataclass(frozen=True)
class Match:
    structure: Structure
    start: int
    end: int
    text: str


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@lru_cache(maxsize=1)
def _patterns() -> list[tuple[Structure, re.Pattern]]:
    out: list[tuple[Structure, re.Pattern]] = []
    for s in STRUCTURES:
        for term in s.terms_de:
            # Deutsch: Komposita erlaubt ("Kreuzbandruptur", "Supraspinatussehne")
            out.append((s, re.compile(rf"(?<![\w])(?:{term})\w*", re.IGNORECASE)))
        for term in s.terms_fr:
            out.append((s, re.compile(rf"(?<![\w])(?:{term})(?:s|x)?(?![\w])", re.IGNORECASE)))
        for ab in s.abbr:
            out.append((s, re.compile(rf"(?<![\w]){re.escape(ab)}(?![\w])")))
        # Latein- und Hauptnamen
        for name in (s.la, s.de, s.fr):
            base = re.sub(r"\s*\(.*?\)", "", name).strip()
            if len(base) >= 6:
                out.append((s, re.compile(rf"(?<![\w]){re.escape(base)}(?![\w])", re.IGNORECASE)))
    return out


def find_structures(text: str) -> list[Match]:
    """Alle erkannten Strukturen, Überschneidungen aufgelöst (längster Treffer gewinnt)."""
    text = _fold(text)
    found: list[Match] = []
    for structure, pattern in _patterns():
        for m in pattern.finditer(text):
            found.append(Match(structure, m.start(), m.end(), m.group(0)))
    found.sort(key=lambda m: (-(m.end - m.start), m.start))
    kept: list[Match] = []
    for m in found:
        if any(not (m.end <= k.start or m.start >= k.end) for k in kept):
            continue
        kept.append(m)
    kept.sort(key=lambda m: m.start)
    return kept


_SIDE_R = re.compile(
    r"(?<![\w])(rechts|rechte[nrs]?|re\.|droite?s?)(?![\w])(?! (?:de l'abdomen|fémoral|antérieur))", re.IGNORECASE
)
_SIDE_L = re.compile(r"(?<![\w])(links|linke[nrs]?|li\.|gauches?)(?![\w])", re.IGNORECASE)
_SIDE_B = re.compile(r"(?<![\w])(beidseits|beidseitig|bds\.?|bilateral\w*|des deux côtés)(?![\w])", re.IGNORECASE)


def detect_side(text: str) -> str | None:
    """'r', 'l', 'b' (beidseits) oder None."""
    if _SIDE_B.search(text):
        return "b"
    r, l = _SIDE_R.search(text), _SIDE_L.search(text)
    if r and l:
        return "b"
    if r:
        return "r"
    if l:
        return "l"
    return None


_LEVEL = re.compile(
    r"(?<![\w])(C|Th|T|L|S)\s?(\d{1,2})\s*(?:/|-|–)\s*(?:(C|Th|T|L|S)\s?)?(\d{1,2})(?![\w])"
)
_SINGLE = re.compile(r"(?<![\w])(HWK|BWK|LWK|C|Th|T|L)\s?(\d{1,2})(?![\w/\-–])")


def _vertebra(prefix: str, number: int) -> str | None:
    p = {"C": "c", "HWK": "c", "Th": "t", "T": "t", "BWK": "t", "L": "l", "LWK": "l", "S": "s"}.get(prefix)
    if p == "s":
        return "sacrum"
    vid = f"{p}{number}"
    return vid if vid in VERTEBRAE else None


def spine_levels(text: str) -> list[str]:
    """Wirbel-IDs aus Höhenangaben, z. B. "L4/5" → ["l4", "l5"], "L5/S1" → ["l5", "sacrum"]."""
    levels: list[str] = []
    for m in _LEVEL.finditer(text):
        a = _vertebra(m.group(1), int(m.group(2)))
        b = _vertebra(m.group(3) or m.group(1), int(m.group(4)))
        for v in (a, b):
            if v and v not in levels:
                levels.append(v)
    if not levels:
        for m in _SINGLE.finditer(text):
            v = _vertebra(m.group(1), int(m.group(2)))
            if v and v not in levels:
                levels.append(v)
    return levels


def _sides(side: str | None) -> list[str]:
    return ["l", "r"] if side in (None, "b") else [side]


def muscle_regions(structure: Structure | None, side: str | None) -> list[str]:
    if structure is None:
        return []
    out = []
    for region in structure.muscles:
        if region == "head":
            out.append("head")
            continue
        for s in _sides(side if structure.sided else "b"):
            out.append(f"{region}_{s}")
    return out


def skeleton_ids(structure: Structure | None, side: str | None, levels: list[str] | None = None) -> list[str]:
    """IDs der Skelett-Elemente (Knochen mit Seite, Wirbel, Gelenkpunkte)."""
    if structure is None:
        return list(levels or [])
    out: list[str] = []
    bones = list(structure.bones)
    if structure.type in ("disc", "spine") and levels:
        bones = list(levels)
    for bone in bones:
        if bone in SIDED_BONES and structure.sided:
            out += [f"{bone}_{s}" for s in _sides(side)]
        elif bone in SIDED_BONES:
            out += [f"{bone}_l", f"{bone}_r"]
        else:
            out.append(bone)
    if structure.joint:
        if structure.joint in SIDED_JOINTS and structure.sided:
            out += [f"joint_{structure.joint}_{s}" for s in _sides(side)]
        else:
            out.append(f"joint_{structure.joint}")
    return out


def get(structure_id: str | None) -> Structure | None:
    return BY_ID.get(structure_id or "")


def name(structure: Structure, lang: str) -> str:
    return structure.fr if lang == "fr" else structure.de


def type_name(structure: Structure, lang: str) -> str:
    de, fr = TYPE_NAMES.get(structure.type, (structure.type, structure.type))
    return fr if lang == "fr" else de


def text_of(pair: tuple[str, str] | None, lang: str) -> str:
    if not pair:
        return ""
    return pair[1] if lang == "fr" else pair[0]


def search(query: str, limit: int = 20) -> list[Structure]:
    q = query.strip().lower()
    if not q:
        return []
    scored = []
    for s in STRUCTURES:
        hay = [s.de.lower(), s.fr.lower(), s.la.lower()] + [a.lower() for a in s.abbr]
        if any(h == q for h in hay):
            scored.append((0, s.de, s))
        elif any(h.startswith(q) for h in hay):
            scored.append((1, s.de, s))
        elif any(q in h for h in hay):
            scored.append((2, s.de, s))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [s for _, _, s in scored[:limit]]


SIDE_NAMES = {
    "de": {"l": "links", "r": "rechts", "b": "beidseits"},
    "fr": {"l": "côté gauche", "r": "côté droit", "b": "des deux côtés"},
}


def side_name(side: str | None, lang: str) -> str:
    if not side:
        return ""
    return SIDE_NAMES.get(lang, SIDE_NAMES["de"]).get(side, "")


def joint_structure(joint: str | None) -> Structure | None:
    """Gelenk-Eintrag zu einem Gelenkpunkt (z. B. "knee" → Kniegelenk)."""
    if not joint:
        return None
    for s in STRUCTURES:
        if s.type == "joint" and s.joint == joint:
            return s
    return None


def place_name(structure: Structure | None, side: str | None, lang: str) -> str:
    """Kurze Körperstelle für Listen, z. B. "Kniegelenk rechts" / "Genou droite"."""
    if structure is None:
        return ""
    base = joint_structure(structure.joint) or structure
    label = name(base, lang)
    s = side_name(side, lang) if base.sided else ""
    if not s:
        return label
    return f"{label}, {s}" if lang == "fr" else f"{label} {s}"


_BONE_BASE = {
    "skull": "skull", "mandible": "mandible", "clavicle": "clavicle", "scapula": "scapula", "humerus": "humerus",
    "radius": "radius", "ulna": "ulna", "hand": "metacarpals", "sternum": "sternum", "ribs": "ribs", "pelvis": "pelvis",
    "femur": "femur", "patella": "patella", "tibia": "tibia", "fibula": "fibula", "foot": "metatarsals",
    "sacrum": "sacrum", "coccyx": "coccyx",
}
_HAND_FOOT = {"hand": ("Hand", "Main"), "foot": ("Fuss", "Pied")}
_JOINT_BASE = {"shoulder": "shoulder_joint", "ac": "ac_joint", "elbow": "elbow", "wrist": "wrist", "hip": "hip_joint",
               "knee": "knee_joint", "ankle": "ankle", "sij": "sij", "tmj": "tmj"}


def bone_structure(bone_id: str) -> tuple[Structure | None, str | None]:
    """Skelett-Element → (Struktur, Seite). Wirbel → Wirbelsäulenabschnitt."""
    side = None
    base = bone_id
    if base.startswith("joint_"):
        parts = base.split("_")
        joint = parts[1]
        side = parts[2] if len(parts) > 2 else None
        return BY_ID.get(_JOINT_BASE.get(joint, "")), side
    if base[-2:] in ("_l", "_r"):
        base, side = base[:-2], base[-1]
    if base in VERTEBRAE:
        sid = {"c": "cervical_spine", "t": "thoracic_spine", "l": "lumbar_spine"}[base[0]]
        return BY_ID.get(sid), None
    return BY_ID.get(_BONE_BASE.get(base, "")), side


def bone_label(bone_id: str, lang: str) -> str:
    base = bone_id[:-2] if bone_id[-2:] in ("_l", "_r") else bone_id
    side = bone_id[-1] if bone_id[-2:] in ("_l", "_r") else None
    if base in VERTEBRAE:
        return base.upper().replace("T", "Th") if lang == "de" else base.upper()
    if base in _HAND_FOOT:
        label = _HAND_FOOT[base][1 if lang == "fr" else 0]
    else:
        structure, side2 = bone_structure(bone_id)
        side = side or side2
        label = name(structure, lang) if structure else bone_id
    s = side_name(side, lang)
    if not s:
        return label
    return f"{label}, {s}" if lang == "fr" else f"{label} {s}"
