"""Öffentliche Schnittstelle des Diktats – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from ...core.db import Database
from . import repo


def transcript(db: Database, recording_id: int) -> tuple[str, str] | None:
    """(Text, Sprache) eines Diktats – z. B. für die Patienteninformation. None, wenn es (noch) keinen Text gibt."""
    rec = repo.get(db, recording_id)
    if rec is None or not rec["transcript"]:
        return None
    return rec["transcript"], rec["language"]
