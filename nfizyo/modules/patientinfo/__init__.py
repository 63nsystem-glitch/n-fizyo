"""Information für Patient/innen: aus dem Diktat (KI-Vorschlag) oder selbst geschrieben, von der Therapie freigegeben,
lokal übersetzt, in der Patienten-App zum Lesen und Vorlesen (Stimme des eigenen Telefons)."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="patientinfo",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="patientinfo.title",
    description_key="patientinfo.description",
    version="1.1.1",
    requires=("patients", "dictation"),
    uses=("notify",),
    tables=("patient_infos",),
)
