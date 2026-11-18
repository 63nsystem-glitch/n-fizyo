"""Daten der Video-Sitzungen."""
from __future__ import annotations

import secrets

from ...core.audit import audit
from ...core.db import Database, utcnow
from ...core.security import new_token, token_hash
from ...core.tenant import current_practice_id, current_user_id


def room_for(db: Database, appointment_id: int):
    return db.one("SELECT * FROM video_rooms WHERE practice_id = ? AND appointment_id = ?", (current_practice_id(), appointment_id))


def create_room(db: Database, appointment_id: int, patient_id: int) -> int:
    room = "nf-" + secrets.token_hex(12)          # nicht erratbar, enthält keinen Namen
    rid = db.execute("INSERT INTO video_rooms (practice_id, appointment_id, patient_id, room, created_by, created_at) VALUES (?,?,?,?,?,?)",
                     (current_practice_id(), appointment_id, patient_id, room, current_user_id(), utcnow()))
    audit(db, "create", "video_room", rid, patient_id, {"appointment": appointment_id})
    return rid


def new_link(db: Database, room_id: int, patient_id: int) -> str:
    """Neuer Link für die Patientin / den Patienten (ein alter wird ungültig). Gibt das Token zurück – nur hier im Klartext."""
    tok = new_token(24)
    db.execute("UPDATE video_rooms SET link_hash = ?, sms_sent_at = ? WHERE id = ? AND practice_id = ?",
               (token_hash(tok), utcnow(), room_id, current_practice_id()))
    audit(db, "update", "video_room", room_id, patient_id, {"link": "new"})
    return tok


def by_link(db: Database, tok: str):
    """Absichtlich ohne Praxis-Filter: der Link bestimmt die Praxis. Nur der Hash steht in der Datenbank."""
    return db.one("SELECT * FROM video_rooms WHERE link_hash = ?", (token_hash(tok),))


def mark_joined(db: Database, room_id: int, who: str) -> None:
    col = "therapist_joined_at" if who == "therapist" else "patient_joined_at"
    db.execute(f"UPDATE video_rooms SET {col} = COALESCE({col}, ?) WHERE id = ? AND practice_id = ?", (utcnow(), room_id, current_practice_id()))


def has_consent(db: Database, patient_id: int) -> bool:
    return bool(db.one("SELECT 1 FROM video_consents WHERE practice_id = ? AND patient_id = ?", (current_practice_id(), patient_id)))


def give_consent(db: Database, patient_id: int, via: str) -> None:
    db.execute("INSERT INTO video_consents (practice_id, patient_id, via, consented_at) VALUES (?,?,?,?)"
               " ON CONFLICT (practice_id, patient_id) DO NOTHING", (current_practice_id(), patient_id, via, utcnow()))
    audit(db, "consent", "video", None, patient_id, {"via": via})


def rooms_for_patient(db: Database, patient_id: int):
    return db.query("SELECT * FROM video_rooms WHERE practice_id = ? AND patient_id = ? ORDER BY id DESC LIMIT 20",
                    (current_practice_id(), patient_id))
