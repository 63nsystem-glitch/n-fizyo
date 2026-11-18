"""Abrechnung: Tarifkatalog mit Quellen, Rechnungsentwurf aus erledigten Terminen (Empfang), Kontrolle und Abschluss
(Leitung), Storno, Rechnung als PDF mit QR-Zahlteil, XML-Rechnung 5.0 (MediData), Zahlungseingang (camt.054), Mahnungen,
Selbstzahler/VVG mit Preisliste der Praxis (Leistungen, Produkte, Abos) und MWST, Antworten der Versicherer
(Rückweisungen korrigieren und neu senden), Kostengutsprachen."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="billing",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="billing.title",
    description_key="billing.description",
    version="2.8.0",
    requires=("patients", "agenda"),
    uses=("video", "firms"),
    tables=("tax_point_values", "invoices", "invoice_lines", "billing_settings", "invoice_payments", "payment_imports",
            "invoice_reminders", "invoice_transmissions", "price_items", "invoice_responses", "cost_approvals", "payment_moves",
            "medidata_participants", "medidata_downloads", "medidata_notifications", "medidata_sync"),
)
