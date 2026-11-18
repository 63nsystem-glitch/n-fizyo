"""Öffentliche Schnittstelle der Patienteninformation – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from ...core.db import Database
from ...core.tenant import current_practice_id


def unread(db: Database, info_ids: list[int]) -> set[int]:
    """Welche dieser freigegebenen Informationen hat die Patientin / der Patient in der App noch nicht geöffnet?"""
    ids = [int(i) for i in info_ids if i]
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    return {r["id"] for r in db.query(f"SELECT id FROM patient_infos WHERE practice_id = ? AND status = 'approved' AND read_at IS NULL"
                                      f" AND id IN ({marks})", (current_practice_id(), *ids))}
