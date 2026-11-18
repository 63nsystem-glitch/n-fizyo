"""Online-Termine: Patient/innen fragen auf einer öffentlichen Seite der Praxis einen freien Termin an (in ihrer Sprache,
mit SMS-Code). Der Empfang bestätigt oder lehnt ab; die Nachricht geht per SMS (und mit Einverständnis per E-Mail).
"""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="booking",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="booking.title",
    description_key="booking.description",
    version="1.1.1",
    requires=("patients", "agenda"),
    tables=("booking_settings", "booking_types", "booking_therapists", "booking_requests"),
)
