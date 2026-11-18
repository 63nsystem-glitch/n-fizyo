"""Datentypen der KI-Schicht."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

SECTIONS = ("befund", "behandlung", "beurteilung", "plan")


@dataclass
class Measurement:
    kind: str  # flexion | extension | abduction | adduction | external_rotation | internal_rotation | elevation | pain_vas
    value: float
    unit: str  # "°" oder "/10"
    joint: str | None = None
    side: str | None = None

    def label(self, lang: str = "de") -> str:
        names = MEASUREMENT_NAMES.get(lang, MEASUREMENT_NAMES["de"])
        name = names.get(self.kind, self.kind)
        value = int(self.value) if float(self.value).is_integer() else self.value
        return f"{name} {value}{self.unit}" if self.unit == "°" else f"{name} {value}{self.unit}"


MEASUREMENT_NAMES = {
    "de": {
        "flexion": "Flexion",
        "extension": "Extension",
        "abduction": "Abduktion",
        "adduction": "Adduktion",
        "external_rotation": "Aussenrotation",
        "internal_rotation": "Innenrotation",
        "elevation": "Elevation",
        "pain_vas": "VAS",
    },
    "fr": {
        "flexion": "Flexion",
        "extension": "Extension",
        "abduction": "Abduction",
        "adduction": "Adduction",
        "external_rotation": "Rotation externe",
        "internal_rotation": "Rotation interne",
        "elevation": "Élévation",
        "pain_vas": "EVA",
    },
}


@dataclass
class Transcript:
    text: str
    language: str  # de | fr
    engine: str
    duration_sec: float | None = None


@dataclass
class NoteDraft:
    befund: str = ""
    behandlung: str = ""
    beurteilung: str = ""
    plan: str = ""
    measurements: list[Measurement] = field(default_factory=list)
    engine: str = "rules"
    warnings: list[str] = field(default_factory=list)
    # Diktat nennt ein anderes Gelenk / eine andere Seite als die Akte (Hinweis auf der Prüfseite; Messwerte folgen dem Diktat)
    context_differs: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NoteDraft":
        ms = [Measurement(**m) for m in data.get("measurements", [])]
        return cls(
            befund=data.get("befund", ""),
            behandlung=data.get("behandlung", ""),
            beurteilung=data.get("beurteilung", ""),
            plan=data.get("plan", ""),
            measurements=ms,
            engine=data.get("engine", "rules"),
            warnings=list(data.get("warnings", [])),
            context_differs=data.get("context_differs"),
        )


SUGGESTION_KINDS = (
    "diagnosis",
    "surgery",
    "finding",
    "restriction",
    "clearance",
    "complication",
    "red_flag",
    "prescription",
    "insurer",
)


@dataclass
class Suggestion:
    kind: str  # siehe SUGGESTION_KINDS
    text: str
    excerpt: str  # wörtliches Zitat aus dem Dokument (wird im Original markiert)
    until: date | None = None
    on_date: date | None = None
    sessions: int | None = None
    structure_id: str | None = None
    region: str | None = None
    side: str | None = None  # l | r | b
    insurer: str | None = None
    law: str | None = None  # KVG | UVG | IVG | MVG
    levels: list[str] = field(default_factory=list)  # Wirbelhöhen, z. B. ["l4", "l5"]


@dataclass
class ExtractionResult:
    suggestions: list[Suggestion]
    patient_name: str | None = None
    patient_birthdate: date | None = None
    document_date: date | None = None
    language: str = "de"
    engine: str = "rules"
    warnings: list[str] = field(default_factory=list)


@dataclass
class NoteContext:
    patient_first_name: str = ""
    patient_last_name: str = ""
    joint: str | None = None
    side: str | None = None
