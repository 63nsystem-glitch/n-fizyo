"""Öffentliche Schnittstelle der Berichte – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from ...core.db import Database
from ...core.tenant import current_practice_id


def renewal_since(db: Database, patient_id: int, since: str | None):
    """Letztes Gesuch um Folgeverordnung seit einem Zeitpunkt (id, status, created_at, finalized_at, sent_at, sent_via) – oder None."""
    return db.one("SELECT id, status, created_at, finalized_at, sent_at, sent_via FROM reports WHERE practice_id = ? AND patient_id = ?"
                  " AND kind = 'renewal' AND created_at >= ? ORDER BY id DESC LIMIT 1",
                  (current_practice_id(), patient_id, since or "0000"))
