"""Wer darf welche medizinische Akte sehen?

Regel (Entscheid Oktober 2026):
- Therapie sieht nur die eigenen Patient/innen: wer im Behandlungsteam ist (patient_team).
  Ins Team kommt man als Bezugsperson, durch einen gebuchten Termin oder manuell (Empfang/Leitung).
- Vertretung: Die Leitung gibt einer Person für eine Zeit Zugriff auf alle Patient/innen einer anderen
  (Ferien, Krankheit). Danach erlischt der Zugriff automatisch.
- Empfang und Leitung sehen nur administrative Daten (Name, Kontakt, Versicherung, Termine, Verordnungen),
  nie Notizen, Befunde oder Berichte – dafür gibt es in ihren Bildschirmen schlicht keine Seiten.
Jeder Zugriff auf eine medizinische Akte wird protokolliert.
"""
from __future__ import annotations

from datetime import date

from .audit import audit
from .db import Database, utcnow
from .tenant import current_practice_id, current_user_id
from .timeutil import today as zurich_today

_ACTIVE_DELEGATION = ("d.practice_id = ? AND d.to_user = ? AND d.revoked_at IS NULL AND d.starts_on <= ? AND d.ends_on >= ?")


def patient_ids_sql(user_id: int, today: date | None = None) -> tuple[str, list]:
    """SQL-Teil «p.id IN (...)» für alle Patient/innen, die diese Person sehen darf."""
    pid = current_practice_id()
    day = (today or zurich_today()).isoformat()
    sql = ("SELECT patient_id FROM patient_team WHERE practice_id = ? AND user_id = ?"
           " UNION SELECT t.patient_id FROM patient_team t JOIN delegations d ON d.from_user = t.user_id AND d.practice_id = t.practice_id"
           f" WHERE t.practice_id = ? AND {_ACTIVE_DELEGATION}")
    return sql, [pid, user_id, pid, pid, user_id, day, day]


def relation(db: Database, user_id: int, patient_id: int, today: date | None = None) -> str | None:
    """'team' (eigene/r Patient/in), 'delegated' (Vertretung) oder None (kein Zugriff)."""
    pid = current_practice_id()
    if db.value("SELECT 1 FROM patient_team WHERE practice_id = ? AND patient_id = ? AND user_id = ?", (pid, patient_id, user_id)):
        return "team"
    day = (today or zurich_today()).isoformat()
    if db.value("SELECT 1 FROM patient_team t JOIN delegations d ON d.from_user = t.user_id AND d.practice_id = t.practice_id"
                f" WHERE t.patient_id = ? AND {_ACTIVE_DELEGATION}", (patient_id, pid, user_id, day, day)):
        return "delegated"
    return None


def add_to_team(db: Database, patient_id: int, user_id: int, reason: str = "manual") -> None:
    db.execute("INSERT OR IGNORE INTO patient_team (practice_id, patient_id, user_id, since, added_by, reason) VALUES (?,?,?,?,?,?)",
               (current_practice_id(), patient_id, user_id, utcnow(), current_user_id(), reason))


def remove_from_team(db: Database, patient_id: int, user_id: int) -> None:
    db.execute("DELETE FROM patient_team WHERE practice_id = ? AND patient_id = ? AND user_id = ?",
               (current_practice_id(), patient_id, user_id))
    audit(db, "team_remove", "patient", patient_id, patient_id, {"user_id": user_id})


def team(db: Database, patient_id: int):
    return db.query("SELECT t.*, u.name, u.initials FROM patient_team t JOIN users u ON u.id = t.user_id"
                    " WHERE t.practice_id = ? AND t.patient_id = ? ORDER BY t.reason = 'main' DESC, u.name",
                    (current_practice_id(), patient_id))


# --------------------------------------------------------------------------- Vertretungen
def delegate(db: Database, from_user: int, to_user: int, starts_on: str, ends_on: str, note: str | None = None) -> int:
    did = db.execute("INSERT INTO delegations (practice_id, from_user, to_user, starts_on, ends_on, note, created_by, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (current_practice_id(), from_user, to_user, starts_on, ends_on, note, current_user_id(), utcnow()))
    audit(db, "create", "delegation", did, None, {"from": from_user, "to": to_user, "starts_on": starts_on, "ends_on": ends_on})
    return did


def revoke(db: Database, delegation_id: int) -> None:
    db.execute("UPDATE delegations SET revoked_at = ? WHERE id = ? AND practice_id = ? AND revoked_at IS NULL",
               (utcnow(), delegation_id, current_practice_id()))
    audit(db, "revoke", "delegation", delegation_id)


def delegations(db: Database, include_past: bool = False):
    sql = ("SELECT d.*, f.name AS from_name, t.name AS to_name FROM delegations d JOIN users f ON f.id = d.from_user"
           " JOIN users t ON t.id = d.to_user WHERE d.practice_id = ?")
    params: list = [current_practice_id()]
    if not include_past:
        sql += " AND d.revoked_at IS NULL AND d.ends_on >= ?"
        params.append(zurich_today().isoformat())
    return db.query(sql + " ORDER BY d.starts_on", params)


def delegations_for(db: Database, user_id: int):
    """Aktive Vertretungen, die diese Person gerade wahrnimmt (für den Hinweis im Therapie-Bildschirm)."""
    day = zurich_today().isoformat()
    return db.query("SELECT d.*, f.name AS from_name FROM delegations d JOIN users f ON f.id = d.from_user"
                    f" WHERE {_ACTIVE_DELEGATION} ORDER BY d.ends_on",
                    (current_practice_id(), user_id, day, day))
