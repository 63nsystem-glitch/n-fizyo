"""Anliegen: Patient/in beschreibt das Problem zu Hause in der eigenen Sprache (Patienten-App); KI übersetzt lokal;
die Therapie sieht es in der Akte."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="intake",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="intake_staff.list_title",
    description_key="intake_staff.description",
    version="2.0.1",
    requires=("patients",),
    tables=("intakes",),
)
