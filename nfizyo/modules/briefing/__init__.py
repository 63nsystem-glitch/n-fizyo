"""Kurzüberblick vor dem Termin und Übergabe beim Wechsel der Therapeutin / des Therapeuten.

Alles stammt aus der Akte (freigegebene Notizen, Befunde, Messwerte, Heimprogramm, Nachrichten, Anliegen, Verordnung) –
ohne KI, mit Quelle zu jeder Angabe. Eigene Tabellen braucht das Modul nicht."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="briefing",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="brief.title",
    version="1.1.0",
    requires=("patients", "agenda"),
    uses=("documentation", "homeprogram", "messages", "intake", "rxwatch", "mtt"),
    description_key="brief.description",
)
