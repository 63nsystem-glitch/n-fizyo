"""Verordnungen im Blick: freie Sitzungen, Fristen (5 Wochen, 36 Sitzungen), unvollständige Verordnungen,
Anfragen an die Ärztin / den Arzt – für Therapie und Empfang. Die Software erinnert, Menschen entscheiden."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="rxwatch",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="rx.title",
    version="1.1.0",
    requires=("patients", "agenda"),
    uses=("reports", "billing"),
    tables=("rx_requests",),
    description_key="rx.description",
)
