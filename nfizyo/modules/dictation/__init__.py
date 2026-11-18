"""Diktat: Sitzung einsprechen (Tablet, Telefon, offline), lokale Spracherkennung, Notizentwurf, Freigabe."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="dictation",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="dictation.title",
    description_key="dictation.description",
    version="2.0.1",
    requires=("patients", "documentation"),
    uses=("agenda",),
    tables=("recordings",),
)
