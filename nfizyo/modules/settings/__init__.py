"""Leitung: Team und Rollen, Vertretungen, Einstellungen der Praxis (Praxis, Zuweisende, KI, Module)."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="settings",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="settings.title",
    description_key="settings.description",
    version="2.5.0",
    uses=("agenda", "billing"),
    tables=("practice_setup",),
    optional=False,
)
