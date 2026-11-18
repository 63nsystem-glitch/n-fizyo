"""Nachrichten mit Patient/innen (inkl. Problem melden) – lokale Übersetzung in beide Richtungen."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import views

    views.setup(ctx)


MODULE = Module(
    name="messages",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="messages.title",
    description_key="messages.description",
    version="2.1.1",
    requires=("patients",),
    uses=("notify",),
    tables=("messages",),
)
