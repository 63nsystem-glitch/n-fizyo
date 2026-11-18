"""Posteingang: Arztberichte, OP-Berichte, Verordnungen – Upload oder Scan-Ordner, Texterkennung, KI-Vorschläge.

Empfang ordnet Dokumente Patient/innen zu (ohne Inhalt), die Therapie liest sie und übernimmt Vorschläge in die Akte."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="inbox",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="inbox.title",
    description_key="inbox.description",
    version="2.2.1",
    requires=("patients",),
    tables=("documents", "suggestions"),
)
