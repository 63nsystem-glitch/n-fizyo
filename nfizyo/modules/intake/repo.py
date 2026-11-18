"""Anliegen speichern, übersetzen lassen, in die Akte übernehmen."""
from __future__ import annotations

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id
from ...knowledge.regions import REGION_BASES, REGION_STRUCTURE, split, valid_region  # noqa: F401

SINCE = ("days", "weeks", "months", "years")
WORSE = ("move", "rest", "night", "sitting", "standing", "walking", "stairs", "lifting", "morning")
GOALS = ("sleep", "walk", "stairs", "work", "sport", "lift", "overhead", "household", "drive")
def create(db: Database, patient_id: int, *, channel: str, lang: str, regions: list[str], pain: int | None,
           since: str | None, worse: list[str], goals: list[str], text: str | None, appointment_id: int | None = None) -> int:
    iid = db.execute(
        "INSERT INTO intakes (practice_id, patient_id, appointment_id, channel, lang, regions, pain, since, worse, goals,"
        " text_original, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, appointment_id, channel, lang,
         ",".join(r for r in regions if valid_region(r)) or None,
         pain if pain is not None and 0 <= pain <= 10 else None, since if since in SINCE else None,
         ",".join(w for w in worse if w in WORSE) or None, ",".join(g for g in goals if g in GOALS) or None,
         (text or "").strip()[:4000] or None, utcnow()),
    )
    audit(db, "create", "intake", iid, patient_id, {"channel": channel, "lang": lang})
    return iid


def count_since(db: Database, patient_id: int, *, hours: int) -> int:
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    return db.value("SELECT COUNT(*) FROM intakes WHERE practice_id = ? AND patient_id = ? AND created_at >= ?",
                    (current_practice_id(), patient_id, since), 0)


def get(db: Database, iid: int):
    return db.one("SELECT i.*, p.first_name, p.last_name FROM intakes i JOIN patients p ON p.id = i.patient_id"
                  " WHERE i.id = ? AND i.practice_id = ?", (iid, current_practice_id()))


def for_patient(db: Database, patient_id: int, limit: int = 20):
    return db.query("SELECT * FROM intakes WHERE practice_id = ? AND patient_id = ? ORDER BY id DESC LIMIT ?",
                    (current_practice_id(), patient_id, limit))


def listing(db: Database, only_new: bool = True, only: tuple[str, list] | None = None):
    """Anliegen. only=(sql, params): nur diese Patient/innen (eigene + Vertretung)."""
    sql = ("SELECT i.*, p.first_name, p.last_name FROM intakes i JOIN patients p ON p.id = i.patient_id WHERE i.practice_id = ?"
           + (" AND i.status = 'new'" if only_new else ""))
    params: list = [current_practice_id()]
    if only:
        sql += f" AND i.patient_id IN ({only[0]})"
        params += only[1]
    return db.query(sql + " ORDER BY i.id DESC LIMIT 200", params)


def set_translation(db: Database, iid: int, text: str | None, target: str, engine: str, warning: str | None) -> None:
    db.execute("UPDATE intakes SET text_translated = ?, translated_to = ?, translation_engine = ?, translation_warning = ?"
               " WHERE id = ? AND practice_id = ?", (text, target, engine, warning, iid, current_practice_id()))


def mark(db: Database, iid: int, status: str) -> None:
    row = db.one("SELECT patient_id FROM intakes WHERE id = ? AND practice_id = ?", (iid, current_practice_id()))
    if row is None:
        return
    db.execute("UPDATE intakes SET status = ?, seen_by = ?, seen_at = ? WHERE id = ? AND practice_id = ?",
               (status, current_user_id(), utcnow(), iid, current_practice_id()))
    audit(db, "update", "intake", iid, row["patient_id"], {"status": status})

