"""Anatomie-Atlas und Strukturblatt (Therapie). Wissen und Körperfigur selbst liegen in knowledge/ und web/templates/ui/."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(name="anatomy", package_dir=Path(__file__).parent, setup=setup, title_key="anatomy.title", description_key="anatomy.description", version="2.0.0")
