"""Export: Buchhaltung (Buchungsjournal als CSV, Excel mit Rechnungen, Zahlungen, Kasse, offenen Posten) und vollständiger
Datenexport der Praxis (ZIP, nur Leitung, mit Passwort, 24 Stunden verfügbar)."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="exports",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="exports.title",
    description_key="exports.description",
    version="1.3.0",
    requires=("billing",),
    uses=("cashdesk", "firms"),
    tables=("export_settings", "bexio_connections", "bexio_bookings"),
)
