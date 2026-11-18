"""Öffentliche Schnittstelle der Nachrichten – nur das hier dürfen andere Module benutzen."""
from __future__ import annotations

from ...core.db import Database
from ...core.tenant import current_practice_id


def incoming_since(db: Database, patient_id: int, since: str | None, limit: int = 10):
    """Nachrichten der Patientin / des Patienten seit einem Zeitpunkt (neueste zuerst), mit Übersetzung, Art und Schmerz."""
    return db.query("SELECT id, kind, body, lang, translation, translation_lang, region, pain, read_at, handled_at, created_at"
                    " FROM messages WHERE practice_id = ? AND patient_id = ? AND direction = 'in' AND created_at >= ?"
                    " ORDER BY id DESC LIMIT ?", (current_practice_id(), patient_id, since or "0000", limit))


def open_items(db: Database, patient_id: int) -> dict:
    """Ungelesene Nachrichten und offene Problem-Meldungen: {"unread", "problems"}."""
    row = db.one("SELECT SUM(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread,"
                 " SUM(CASE WHEN kind = 'problem' AND handled_at IS NULL THEN 1 ELSE 0 END) AS problems"
                 " FROM messages WHERE practice_id = ? AND patient_id = ? AND direction = 'in'", (current_practice_id(), patient_id))
    return {"unread": (row["unread"] or 0) if row else 0, "problems": (row["problems"] or 0) if row else 0}


def unread_out(db: Database, message_ids: list[int]) -> set[int]:
    """Welche dieser Nachrichten der Praxis hat die Patientin / der Patient in der App noch nicht gelesen?"""
    ids = [int(i) for i in message_ids if i]
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    return {r["id"] for r in db.query(f"SELECT id FROM messages WHERE practice_id = ? AND direction = 'out' AND read_at IS NULL"
                                      f" AND id IN ({marks})", (current_practice_id(), *ids))}
