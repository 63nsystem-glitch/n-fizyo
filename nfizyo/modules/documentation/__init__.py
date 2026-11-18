"""Verlaufsdokumentation: Notizen (Befund, Behandlung, Beurteilung, Plan) mit Versionen und Sperre nach Freigabe."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="documentation",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="notes.title",
    description_key="notes.description",
    version="2.1.1",
    requires=("patients",),
    uses=("agenda",),
    tables=("notes", "note_versions", "note_templates"),
    optional=False,
)
