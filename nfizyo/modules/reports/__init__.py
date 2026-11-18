"""Berichte an Zuweisende: Entwurf aus bestätigten Daten der Akte, bearbeiten, abschliessen (unveränderbar), drucken/PDF."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="reports",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="reports.title",
    description_key="reports.description",
    version="2.2.0",
    requires=("patients", "documentation"),
    uses=("agenda",),
    tables=("reports",),
)
