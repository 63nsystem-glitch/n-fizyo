"""Patienteninformationen speichern, freigeben, übersetzen."""
from __future__ import annotations

import json

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id

_SELECT = "SELECT i.*, u.name AS author_name FROM patient_infos i JOIN users u ON u.id = i.author_id"


def get(db: Database, info_id: int):
    return db.one(_SELECT + " WHERE i.id = ? AND i.practice_id = ?", (info_id, current_practice_id()))


def by_recording(db: Database, recording_id: int):
    return db.one(_SELECT + " WHERE i.practice_id = ? AND i.recording_id = ?", (current_practice_id(), recording_id))


def for_patient(db: Database, patient_id: int, approved_only: bool = False, limit: int = 50):
    sql = _SELECT + " WHERE i.practice_id = ? AND i.patient_id = ?"
    sql += " AND i.status = 'approved'" if approved_only else " AND i.status != 'discarded'"
    return db.query(sql + " ORDER BY i.id DESC LIMIT ?", (current_practice_id(), patient_id, limit))


def start(db: Database, patient_id: int, recording_id: int, author_id: int, source_lang: str) -> int | None:
    """Vorschlag zu einem Diktat anlegen (oder neu erzeugen lassen). Freigegebene Infos bleiben unangetastet."""
    pid = current_practice_id()
    row = by_recording(db, recording_id)
    now = utcnow()
    if row is not None:
        if row["status"] == "approved":
            return None
        db.execute("UPDATE patient_infos SET status = 'generating', patient_id = ?, source_lang = ?, updated_at = ?"
                   " WHERE id = ? AND practice_id = ?", (patient_id, source_lang, now, row["id"], pid))
        return row["id"]
    return db.execute("INSERT INTO patient_infos (practice_id, patient_id, recording_id, author_id, status, source_lang, created_at,"
                      " updated_at) VALUES (?,?,?,?,?,?,?,?)", (pid, patient_id, recording_id, author_id, "generating", source_lang, now, now))


def save_draft(db: Database, info_id: int, home: str, contact: str, engine: str, warnings: list[str]) -> None:
    db.execute("UPDATE patient_infos SET status = 'draft', home = ?, contact = ?, ai_engine = ?, ai_warnings = ?, updated_at = ?"
               " WHERE id = ? AND practice_id = ? AND status = 'generating'",
               (home or None, contact or None, engine, json.dumps(warnings), utcnow(), info_id, current_practice_id()))


def create_approved(db: Database, patient_id: int, home: str, contact: str, source_lang: str, target_lang: str) -> int:
    """Selbst geschriebene Information (ohne Diktat) – gilt mit dem Speichern als freigegeben."""
    now = utcnow()
    iid = db.execute(
        "INSERT INTO patient_infos (practice_id, patient_id, author_id, status, source_lang, home, contact, target_lang, approved_by,"
        " approved_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, current_user_id(), "approved", source_lang, home or None, contact or None, target_lang,
         current_user_id(), now, now, now))
    audit(db, "approve", "patient_info", iid, patient_id)
    return iid


def approve(db: Database, info_id: int, home: str, contact: str, target_lang: str) -> None:
    now = utcnow()
    row = get(db, info_id)
    if row is None:
        return
    db.execute("UPDATE patient_infos SET status = 'approved', home = ?, contact = ?, target_lang = ?, approved_by = ?, approved_at = ?,"
               " home_translated = NULL, contact_translated = NULL, translation_engine = NULL, updated_at = ?"
               " WHERE id = ? AND practice_id = ?",
               (home or None, contact or None, target_lang, current_user_id(), now, now, info_id, current_practice_id()))
    audit(db, "approve", "patient_info", info_id, row["patient_id"])


def discard(db: Database, info_id: int) -> None:
    db.execute("UPDATE patient_infos SET status = 'discarded', updated_at = ? WHERE id = ? AND practice_id = ? AND status != 'approved'",
               (utcnow(), info_id, current_practice_id()))


def save_translation(db: Database, info_id: int, home: str | None, contact: str | None, engine: str, warning: str | None) -> None:
    db.execute("UPDATE patient_infos SET home_translated = ?, contact_translated = ?, translation_engine = ?, translation_warning = ?,"
               " updated_at = ? WHERE id = ? AND practice_id = ?",
               (home, contact, engine, warning, utcnow(), info_id, current_practice_id()))


def mark_read(db: Database, patient_id: int) -> None:
    db.execute("UPDATE patient_infos SET read_at = ? WHERE practice_id = ? AND patient_id = ? AND status = 'approved' AND read_at IS NULL",
               (utcnow(), current_practice_id(), patient_id))


def unread_count(db: Database, patient_id: int) -> int:
    return db.value("SELECT COUNT(*) FROM patient_infos WHERE practice_id = ? AND patient_id = ? AND status = 'approved'"
                    " AND read_at IS NULL", (current_practice_id(), patient_id), 0)
