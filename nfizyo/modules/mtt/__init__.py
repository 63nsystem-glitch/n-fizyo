"""MTT-Fläche: Geräte mit QR-Code, Trainingsplan, Sätze aus der Patienten-App, Live-Ansicht für die Aufsicht und
Vorschläge zur Steigerung – entscheiden tut immer die Therapeutin / der Therapeut."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="mtt",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="mtt.title",
    description_key="mtt.description",
    version="1.1.1",
    requires=("patients",),
    uses=("cashdesk", "agenda"),
    tables=("mtt_machines", "mtt_plans", "mtt_items", "mtt_visits", "mtt_sets", "mtt_suggestions"),
)
