"""Agenda: «Heute», Kalender (Tag/Woche) über Standorte, Therapeut/innen, Räume und Geräte; Serien; Warteliste (frei gewordene Termine per SMS anbieten);
Erinnerungen; Abwesenheiten, Schliessungen und Feiertage (pro Kanton); Einstellungen Standorte/Behandlungsarten/Arbeitszeiten/Feiertage."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="agenda",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="agenda.title",
    description_key="agenda.description",
    version="2.6.0",
    requires=("patients",),
    tables=("locations", "rooms", "devices", "treatment_types", "therapist_hours", "appointment_series", "appointments", "waitlist",
            "ical_tokens", "slot_offers", "slot_offer_recipients", "absences", "holiday_settings", "holiday_overrides"),
    optional=False,
)
