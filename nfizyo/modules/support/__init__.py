"""Hilfe: Support-Anfrage an NSystem (alle Rollen) – ohne Patientendaten, Antwort von NSystem in der App."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="support",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="support.title",
    description_key="support.description",
    version="1.0.0",
    tables=(),                 # Daten im Kern (box_support_requests, core/nsystem.py): gehören dem ganzen Praxis-Server
    optional=False,
)
