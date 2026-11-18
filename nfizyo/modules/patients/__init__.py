"""Patientenakte (Therapie) und Patienten-Stammblatt (Empfang). Stammdaten selbst liegen im Kern."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="patients",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="patients.title",
    description_key="patients.description",
    version="2.3.0",
    uses=("agenda",),        # Archivieren: künftige Termine anzeigen und absagen
    tables=("findings", "measurements", "measurement_shares"),
    optional=False,
)
