"""Audit-Protokoll: wer hat wann welche Patientendaten eingegeben, geändert oder angesehen."""
from __future__ import annotations

import json
from typing import Any

from .db import Database, utcnow
from .tenant import current_patient_account, current_user_id, maybe_practice_id


def audit(
    db: Database,
    action: str,
    entity: str,
    entity_id: int | None = None,
    patient_id: int | None = None,
    detail: dict[str, Any] | str | None = None,
) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    db.execute(
        "INSERT INTO audit_log (practice_id, user_id, patient_account, patient_id, action, entity, entity_id, detail, at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            maybe_practice_id(),
            current_user_id(),
            current_patient_account(),
            patient_id,
            action,
            entity,
            entity_id,
            detail,
            utcnow(),
        ),
    )


def entries_for_patient(db: Database, practice_id: int, patient_id: int, limit: int = 200):
    return db.query(
        "SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
        " WHERE a.practice_id = ? AND a.patient_id = ? ORDER BY a.id DESC LIMIT ?",
        (practice_id, patient_id, limit),
    )
