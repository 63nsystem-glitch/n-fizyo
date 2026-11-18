"""Firmenkunden: Rechnungen an Firmen – z. B. Rückenschule im Betrieb, Ergonomie-Beratung am Arbeitsplatz, Firmen-Abos.
Firmen erfassen, Positionen aus der Preisliste oder frei, MWST nur mit MWST-Nummer der Praxis, eigene Nummernfolge
(F2026-0001), PDF mit QR-Zahlteil, Versand per E-Mail, Zahlungen (auch automatisch aus der Bankdatei der Abrechnung),
Mahnungen in drei Stufen, Storno. Keine Patientendaten."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="firms",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="firms.title",
    description_key="firms.description",
    version="1.1.0",
    requires=("billing",),
    optional=True,
    tables=("firms", "firm_invoices", "firm_invoice_lines", "firm_payments", "firm_reminders"),
)
