"""Bilder der Übungen: eigene Strichzeichnungen (static/img/exercises/<code>.svg, erzeugt mit tools/gen_exercise_drawings.py)
und neutrale Körperbereich-Kacheln (region-<bereich>.svg) für Übungen ohne Zeichnung – nie eine fremde Pose.
Ein eigenes Video der Patientin / des Patienten geht in der App immer vor."""
from __future__ import annotations

from . import library_data
from .repo import AREAS
# Zu jeder Übung der Start-Bibliothek gibt es genau eine Zeichnung (gleicher Code)
DRAWINGS: tuple[str, ...] = tuple(row[0] for row in library_data.EXERCISES)
_NAMES = {row[0]: (row[6], row[7]) for row in library_data.EXERCISES}


def valid(code: str | None) -> bool:
    return bool(code) and code in DRAWINGS


def label(code: str, lang: str) -> str:
    de, fr = _NAMES.get(code, (code, code))
    return fr if lang == "fr" else de


def region_of(regions: str | None) -> str:
    for r in (regions or "").split(","):
        if r.strip() in AREAS:
            return r.strip()
    return "general"


def picture(row) -> dict:
    """{"kind": "drawing"|"region", "src": Pfad unter static/, "region": Bereich} für eine Übung oder einen Programmpunkt."""
    keys = row.keys() if hasattr(row, "keys") else ()
    code = row["drawing"] if "drawing" in keys else None
    region = region_of(row["regions"] if "regions" in keys else None)
    if valid(code):
        return {"kind": "drawing", "src": f"img/exercises/{code}.svg", "code": code, "region": region}
    return {"kind": "region", "src": f"img/exercises/region-{region}.svg", "code": None, "region": region}


def choices(lang: str) -> list[dict]:
    """Zeichnungen zur Auswahl beim Anlegen/Bearbeiten einer Übung, nach Name sortiert."""
    out = [{"code": c, "label": label(c, lang), "src": f"img/exercises/{c}.svg"} for c in DRAWINGS]
    return sorted(out, key=lambda x: x["label"].lower())
