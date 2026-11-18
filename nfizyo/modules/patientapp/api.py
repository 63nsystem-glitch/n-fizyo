"""Öffentliche Schnittstelle der Patienten-App – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from ...core.db import Database
from ...core.tenant import current_practice_id


def has_app(db: Database, patient_id: int) -> bool:
    """Hat die Patientin / der Patient die App verbunden (Einwilligung erteilt und nicht widerrufen)?
    Nur dann kann sie/er sich (auch später per SMS-Code) anmelden – z. B. für Benachrichtigungen mit App-Link."""
    return bool(db.value("SELECT 1 FROM patient_consents WHERE practice_id = ? AND patient_id = ? AND kind = 'app'"
                         " AND revoked_at IS NULL", (current_practice_id(), patient_id)))
