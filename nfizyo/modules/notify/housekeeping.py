"""Aufräumen der eigenen Warteschlange notify_queue (ruft core/housekeeping.py regelmässig auf).

Gelöscht werden nur erledigte Einträge (versendet oder übersprungen) nach 90 Tagen – nie offene (pending). Die Übersicht
unter Einstellungen › Benachrichtigungen zeigt die letzten 30 Tage, die Bündelung schaut 15 Minuten zurück.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

RETENTION_DAYS = 90


def purge(db, now: datetime) -> int:
    cutoff = (now - timedelta(days=RETENTION_DAYS)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    with db.tx():
        db.execute("DELETE FROM notify_queue WHERE status IN ('sent', 'skipped') AND created_at < ?", (cutoff,))
        return db.value("SELECT changes()", (), 0)
