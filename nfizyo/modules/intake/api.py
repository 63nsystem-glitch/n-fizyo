"""Öffentliche Schnittstelle der Anliegen – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from ...core.db import Database
from ...core.tenant import current_practice_id


def new_for_patient(db: Database, patient_id: int):
    """Neue (noch nicht gesehene) Anliegen aus der App: id, pain, regions, created_at, Text (original/übersetzt)."""
    return db.query("SELECT id, lang, regions, pain, text_original, text_translated, created_at FROM intakes"
                    " WHERE practice_id = ? AND patient_id = ? AND status = 'new' ORDER BY id DESC", (current_practice_id(), patient_id))
