"""Benachrichtigungen an Patient/innen: «Sie haben eine neue Nachricht von <Praxis>» per E-Mail (nur mit Einverständnis) oder SMS.

Ohne Gesundheitsdaten, in der Sprache der Patientin / des Patienten, mit Link in die App. Höchstens eine Benachrichtigung
pro Person in 15 Minuten (gebündelt), keine SMS zwischen 20 und 8 Uhr (wartet bis zum Morgen). Die Praxis wählt den Weg
unter Leitung › Einstellungen › Benachrichtigungen."""
from __future__ import annotations

from pathlib import Path

from ...core.modules import Module


def setup(ctx) -> None:
    from . import housekeeping, views

    views.setup(ctx)
    ctx.housekeeping(housekeeping.purge)      # erledigte Einträge der eigenen Warteschlange (core/housekeeping.py)


MODULE = Module(
    name="notify",
    package_dir=Path(__file__).parent,
    setup=setup,
    title_key="notify.title",
    description_key="notify.description",
    version="1.0.1",
    requires=("patients", "patientapp"),
    uses=("agenda", "messages", "patientinfo"),
    tables=("notify_settings", "notify_queue"),
)
