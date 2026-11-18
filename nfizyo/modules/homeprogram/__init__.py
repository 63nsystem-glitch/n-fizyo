"""Heimprogramm: Übungsbibliothek, Programm pro Patient, Freigabe, Anzeige in der Patienten-App (eigene Sprache)."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="homeprogram",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="hp.title",
    description_key="hp.description",
    version="2.2.1",
    requires=("patients",),
    tables=("exercises", "programs", "program_items", "exercise_logs", "exercise_skips"),
)
