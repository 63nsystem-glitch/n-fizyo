"""Datenübernahme aus der bisherigen Software: Excel/CSV → Spalten zuordnen → Probelauf → Übernahme.

Patient/innen, Versicherer, Zuweisende, Termine, Verordnungen und offene Posten. Nur die Leitung. Duplikate werden erkannt und übersprungen,
Fehler pro Zeile angezeigt (Fehlerliste als CSV). Die hochgeladene Datei wird nach der Übernahme gelöscht.
"""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="dataimport",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="dataimport.title",
    description_key="dataimport.description",
    version="1.1.0",
    requires=("patients",),
    uses=("agenda", "billing"),
    tables=("import_jobs",),
)
