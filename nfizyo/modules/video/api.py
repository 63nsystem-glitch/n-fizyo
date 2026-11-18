"""Öffentliche Schnittstelle der Video-Sitzungen – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from ...core.db import Database
from ...core.tenant import current_practice_id


def took_place(db: Database, appointment_id: int) -> bool:
    """Ist zu diesem Termin jemand dem Video-Raum beigetreten (Therapeut/in oder Patient/in)?"""
    return bool(db.one("SELECT 1 FROM video_rooms WHERE practice_id = ? AND appointment_id = ?"
                       " AND (therapist_joined_at IS NOT NULL OR patient_joined_at IS NOT NULL)", (current_practice_id(), appointment_id)))
