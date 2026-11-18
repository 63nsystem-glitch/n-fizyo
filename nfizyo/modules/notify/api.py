"""Öffentliche Schnittstelle der Benachrichtigungen – nur das hier dürfen andere Module benutzen.

Für die Anzeige in der Praxis («Patient/in benachrichtigt per SMS, 14:32»): status_for(). Das Vormerken geschieht über
Ereignisse (message.sent, patientinfo.released, appointment.booked), nicht über diese Schnittstelle.
"""
from __future__ import annotations

from ...core.db import Database
from ...core.tenant import current_practice_id


def status_for(db: Database, kind: str, ref_ids: list[int]) -> dict[int, dict]:
    """Stand der Benachrichtigung je Anlass: {ref_id: {"status": pending|sent|skipped, "channel", "at", "reason"}}.
    at = Versandzeit (sent) bzw. geplante Zeit (pending)."""
    ids = [int(i) for i in ref_ids if i]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    out: dict[int, dict] = {}
    for r in db.query(f"SELECT ref_id, status, channel, reason, due_at, sent_at FROM notify_queue WHERE practice_id = ? AND kind = ?"
                      f" AND ref_id IN ({marks}) ORDER BY id", (current_practice_id(), kind, *ids)):
        out[r["ref_id"]] = {"status": r["status"], "channel": r["channel"], "reason": r["reason"],
                            "at": r["sent_at"] if r["status"] == "sent" else r["due_at"]}
    return out
