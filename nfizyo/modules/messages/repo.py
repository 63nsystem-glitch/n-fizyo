"""Nachrichten-Verläufe."""
from __future__ import annotations

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id


def thread(db: Database, patient_id: int, limit: int = 200):
    return db.query("SELECT m.*, u.name AS author_name FROM messages m LEFT JOIN users u ON u.id = m.author_user_id"
                    " WHERE m.practice_id = ? AND m.patient_id = ? ORDER BY m.id DESC LIMIT ?",
                    (current_practice_id(), patient_id, limit))[::-1]


def threads(db: Database, only: tuple[str, list] | None = None):
    """Gespräche (pro Patient/in). only=(sql, params): nur diese Patient/innen (z. B. eigene)."""
    where, params = "", []
    if only:
        where, params = f" AND m.patient_id IN ({only[0]})", list(only[1])
    return db.query(
        "SELECT t.*, l.direction AS last_direction, l.kind AS last_kind, l.body AS last_body, l.translation AS last_translation,"
        " l.lang AS last_lang, l.created_at AS last_at FROM ("
        "SELECT p.id AS patient_id, p.first_name, p.last_name, p.spoken_lang, MAX(m.id) AS last_id,"
        " SUM(CASE WHEN m.direction = 'in' AND m.read_at IS NULL THEN 1 ELSE 0 END) AS unread,"
        " SUM(CASE WHEN m.kind = 'problem' AND m.handled_at IS NULL THEN 1 ELSE 0 END) AS open_problems"
        f" FROM messages m JOIN patients p ON p.id = m.patient_id WHERE m.practice_id = ?{where} GROUP BY p.id"
        ") t JOIN messages l ON l.id = t.last_id AND l.practice_id = ? ORDER BY t.unread DESC, t.last_id DESC",
        (current_practice_id(), *params, current_practice_id()))


def unread_threads(db: Database, only: tuple[str, list]) -> int:
    return db.value(f"SELECT COUNT(DISTINCT patient_id) FROM messages WHERE practice_id = ? AND direction = 'in' AND read_at IS NULL"
                    f" AND patient_id IN ({only[0]})", (current_practice_id(), *only[1]), 0)


def last(db: Database, message_id: int):
    return db.one("SELECT * FROM messages WHERE id = ? AND practice_id = ?", (message_id, current_practice_id()))


def add(db: Database, patient_id: int, *, direction: str, body: str, lang: str, kind: str = "message",
        region: str | None = None, pain: int | None = None, translation: str | None = None,
        translation_lang: str | None = None, translation_engine: str | None = None, translation_warning: str | None = None) -> int:
    mid = db.execute(
        "INSERT INTO messages (practice_id, patient_id, direction, kind, author_user_id, body, lang, translation,"
        " translation_lang, translation_engine, translation_warning, region, pain, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, direction, kind, current_user_id() if direction == "out" else None,
         body.strip()[:4000], lang, translation, translation_lang, translation_engine, translation_warning, region,
         pain, utcnow()),
    )
    audit(db, "create", "message", mid, patient_id, {"direction": direction, "kind": kind})
    return mid


def set_translation(db: Database, mid: int, text: str, lang: str, engine: str, warning: str | None) -> None:
    db.execute("UPDATE messages SET translation = ?, translation_lang = ?, translation_engine = ?, translation_warning = ?"
               " WHERE id = ? AND practice_id = ?", (text, lang, engine, warning, mid, current_practice_id()))


def mark_read(db: Database, patient_id: int, direction: str) -> None:
    db.execute("UPDATE messages SET read_at = ? WHERE practice_id = ? AND patient_id = ? AND direction = ? AND read_at IS NULL",
               (utcnow(), current_practice_id(), patient_id, direction))


def handle(db: Database, mid: int) -> None:
    db.execute("UPDATE messages SET handled_at = ?, handled_by = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), current_user_id(), mid, current_practice_id()))


MAX_BODY = 4000            # Zeichen je Nachricht (eine lange Nachricht ist ~1000; mehr ist meist ein eingefügter Text)
MAX_PER_HOUR = 20          # Nachrichten einer Person pro Stunde – sonst würden Übersetzungen die Diktate aufhalten


def too_many(db: Database, patient_id: int) -> bool:
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    return db.value("SELECT COUNT(*) FROM messages WHERE practice_id = ? AND patient_id = ? AND direction = 'in' AND created_at >= ?",
                    (current_practice_id(), patient_id, since), 0) >= MAX_PER_HOUR
