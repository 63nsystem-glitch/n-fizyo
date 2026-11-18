"""Diktate speichern und Status verwalten."""
from __future__ import annotations

import json
from pathlib import Path

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.tenant import current_practice_id, current_user_id

AUDIO_TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav",
               "audio/x-wav": ".wav", "audio/aac": ".aac", "video/webm": ".webm"}


def get(db: Database, rid: int):
    return db.one(
        "SELECT r.*, p.first_name, p.last_name FROM recordings r LEFT JOIN patients p ON p.id = r.patient_id"
        " WHERE r.id = ? AND r.practice_id = ?", (rid, current_practice_id()))


def by_uuid(db: Database, client_uuid: str):
    return db.one("SELECT * FROM recordings WHERE practice_id = ? AND client_uuid = ?", (current_practice_id(), client_uuid))


def create(db: Database, *, client_uuid: str, patient_id: int | None, appointment_id: int | None, language: str,
           recorded_at: str | None, path: str | None = None, mime: str | None = None, size: int | None = None,
           transcript: str | None = None) -> int:
    now = utcnow()
    rid = db.execute(
        "INSERT INTO recordings (practice_id, patient_id, appointment_id, user_id, client_uuid, path, mime, size, language,"
        " status, transcript, transcript_engine, recorded_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (current_practice_id(), patient_id, appointment_id, current_user_id(), client_uuid, path, mime, size, language,
         "transcribed" if transcript else "uploaded", transcript, "typed" if transcript else None, recorded_at or now, now, now),
    )
    audit(db, "create", "recording", rid, patient_id)
    return rid


def set_status(db: Database, rid: int, status: str, **fields) -> None:
    allowed = {"transcript", "transcript_engine", "draft_json", "note_id", "error", "language"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "draft_json" in fields and not isinstance(fields["draft_json"], str):
        fields["draft_json"] = json.dumps(fields["draft_json"], ensure_ascii=False)
    sets = "".join(f", {k} = ?" for k in fields)
    db.execute(f"UPDATE recordings SET status = ?, updated_at = ?{sets} WHERE id = ? AND practice_id = ?",
               (status, utcnow(), *fields.values(), rid, current_practice_id()))


def delete_audio(db: Database, rec) -> None:
    if rec["path"]:
        try:
            Path(rec["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    db.execute("UPDATE recordings SET path = NULL, audio_deleted_at = ? WHERE id = ? AND practice_id = ?",
               (utcnow(), rec["id"], current_practice_id()))


def pending_for(db: Database, user_id: int):
    return db.query(
        "SELECT r.*, p.first_name, p.last_name FROM recordings r LEFT JOIN patients p ON p.id = r.patient_id"
        " WHERE r.practice_id = ? AND r.user_id = ? AND r.status NOT IN ('approved') ORDER BY r.id DESC LIMIT 50",
        (current_practice_id(), user_id))


def for_appointment(db: Database, appointment_id: int):
    return db.query("SELECT * FROM recordings WHERE practice_id = ? AND appointment_id = ? ORDER BY id DESC",
                    (current_practice_id(), appointment_id))


def set_patient(db: Database, rid: int, patient_id: int) -> None:
    db.execute("UPDATE recordings SET patient_id = ?, updated_at = ? WHERE id = ? AND practice_id = ?",
               (patient_id, utcnow(), rid, current_practice_id()))
    audit(db, "update", "recording", rid, patient_id)


def unlink_note(db: Database, note_id: int) -> None:
    db.execute("UPDATE recordings SET note_id = NULL WHERE note_id = ? AND practice_id = ?", (note_id, current_practice_id()))
