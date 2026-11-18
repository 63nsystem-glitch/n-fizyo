"""Öffentliche Schnittstelle der MTT-Fläche – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from datetime import date

from ...core.db import Database
from ...core.tenant import current_practice_id
from . import repo


def trainings(db: Database, patient_id: int, since: date, until: date) -> dict | None:
    """MTT-Trainings im Zeitraum (inklusive): {"n", "sets", "max_pain", "max_effort", "open_suggestions"} – None ohne Plan."""
    if repo.current_plan(db, patient_id) is None:
        return None
    row = db.one("SELECT COUNT(DISTINCT v.id) AS n, COUNT(s.id) AS sets, MAX(s.pain) AS max_pain, MAX(s.effort) AS max_effort"
                 " FROM mtt_visits v LEFT JOIN mtt_sets s ON s.visit_id = v.id WHERE v.practice_id = ? AND v.patient_id = ?"
                 " AND v.day BETWEEN ? AND ?", (current_practice_id(), patient_id, since.isoformat(), until.isoformat()))
    return {"n": row["n"], "sets": row["sets"], "max_pain": row["max_pain"], "max_effort": row["max_effort"],
            "open_suggestions": len(repo.open_suggestions(db, patient_id=patient_id))}
