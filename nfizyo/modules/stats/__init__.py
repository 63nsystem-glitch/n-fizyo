"""Übersicht und Kennzahlen für die Leitung (direkt aus den Praxisdaten berechnet, ohne medizinische Inhalte)."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="stats",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="stats.title",
    description_key="stats.description",
    version="2.0.0",
    requires=("agenda",),
    optional=False,
)
