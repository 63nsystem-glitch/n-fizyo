"""Video-Sitzung: eigener Jitsi-Server der Praxis in der Schweiz. Raum pro Termin, Zutritt nur mit zeitlich begrenztem
Schlüssel (JWT), Link per SMS, Einwilligung vor dem ersten Mal. Keine Aufzeichnung."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="video",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="video.title",
    description_key="video.description",
    version="1.0.2",
    requires=("patients", "agenda"),
    tables=("video_rooms", "video_consents"),
)
