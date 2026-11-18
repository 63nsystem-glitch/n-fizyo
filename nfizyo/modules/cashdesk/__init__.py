"""Kasse an der Theke: Produkte, Abos (z. B. MTT) und Selbstzahler-Leistungen verkaufen – bar, Twint, Karte oder
auf Rechnung. Quittung als PDF, Abo-Besuche abhaken, offene Rechnungen an der Theke bezahlen, Tagesabschluss.

Preise und MWST kommen aus der Preisliste der Abrechnung (Leitung › Einstellungen › Preisliste).
"""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="cashdesk",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="cashdesk.title",
    description_key="cashdesk.description",
    version="1.3.0",
    requires=("patients", "billing"),
    tables=("cash_closings", "cash_sales", "cash_sale_lines", "subscriptions", "subscription_visits", "cash_corrections"),
)
