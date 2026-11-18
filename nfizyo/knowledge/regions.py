"""Körperregionen der Muskelkarte und Schmerzskala – gemeinsame Bausteine (Patienten-App, Akte, Meldungen).

Regionen sind Codes (z. B. "lower_back_l"); die Texte stehen in den Sprachdateien unter common.region.*.
"""
from __future__ import annotations

REGION_BASES = ("head", "neck", "trapezius", "chest", "abs", "obliques", "front_deltoids", "back_deltoids", "biceps",
                "triceps", "forearm", "upper_back", "lower_back", "gluteal", "abductors", "quadriceps", "hamstring",
                "knees", "calves", "soleus")

# Muskelkarten-Region → Anatomie-Struktur (für die Übernahme als Befund)
REGION_STRUCTURE = {
    "head": "skull", "neck": "cervical_spine", "trapezius": "trapezius", "chest": "ribs", "abs": "rectus_abdominis",
    "obliques": "obliquus_externus", "front_deltoids": "shoulder_joint", "back_deltoids": "shoulder_joint",
    "biceps": "biceps_brachii", "triceps": "triceps_brachii", "forearm": "wrist", "upper_back": "thoracic_spine",
    "lower_back": "lumbar_spine", "gluteal": "hip_joint", "abductors": "adductors", "quadriceps": "quadriceps",
    "hamstring": "hamstrings", "knees": "knee_joint", "calves": "ankle", "soleus": "achilles",
}

# Schmerzskala mit Gesichtern (0–10), für Menschen, die Zahlenskalen schlecht lesen können
FACES = ((0, "sentiment_very_satisfied", "f0"), (2, "sentiment_satisfied", "f2"), (4, "sentiment_neutral", "f4"),
         (6, "sentiment_dissatisfied", "f6"), (8, "sentiment_very_dissatisfied", "f8"),
         (10, "sentiment_extremely_dissatisfied", "f10"))


def valid_region(r: str) -> bool:
    if r == "head":
        return True
    return r[-2:] in ("_l", "_r") and r[:-2] in REGION_BASES


def split(value: str | None) -> list[str]:
    return [x for x in (value or "").split(",") if x]
